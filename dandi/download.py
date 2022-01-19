from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import os
import os.path as op
from pathlib import Path
import random
from shutil import rmtree
import sys
import time
from typing import Dict, Iterator, Optional, Tuple

from dandischema.models import DigestType
import humanize
from interleave import FINISH_CURRENT, interleave
import requests

from . import get_logger
from .consts import RETRY_STATUSES, dandiset_metadata_file
from .dandiapi import RemoteZarrAsset
from .dandiarchive import DandisetURL, MultiAssetURL, SingleAssetURL, parse_dandi_url
from .dandiset import Dandiset
from .exceptions import NotFoundError
from .files import DandisetMetadataFile, find_dandi_files
from .support.digests import get_digest
from .support.pyout import naturalsize
from .utils import (
    abbrev_prompt,
    ensure_datetime,
    flattened,
    is_same_time,
    on_windows,
    path_is_subpath,
    pluralize,
    yaml_load,
)

lgr = get_logger()


def download(
    urls,
    output_dir,
    *,
    format="pyout",
    existing="error",
    jobs=1,
    jobs_per_zarr=None,
    get_metadata=True,
    get_assets=True,
    sync=False,
):
    # TODO: unduplicate with upload. For now stole from that one
    # We will again use pyout to provide a neat table summarizing our progress
    # with upload etc
    from .support import pyout as pyouts

    urls = flattened([urls])
    if len(urls) > 1:
        raise NotImplementedError("multiple URLs not supported")
    if not urls:
        # if no paths provided etc, we will download dandiset path
        # we are at, BUT since we are not git -- we do not even know
        # on which instance it exists!  Thus ATM we would do nothing but crash
        raise NotImplementedError("No URLs were provided.  Cannot download anything")

    parsed_url = parse_dandi_url(urls[0])

    # TODO: if we are ALREADY in a dandiset - we can validate that it is the
    # same dandiset and use that dandiset path as the one to download under
    if isinstance(parsed_url, DandisetURL):
        output_path = op.join(output_dir, parsed_url.dandiset_id)
    else:
        output_path = output_dir

    # dandi.cli.formatters are used in cmd_ls to provide switchable
    pyout_style = pyouts.get_style(hide_if_missing=False)

    rec_fields = ("path", "size", "done", "done%", "checksum", "status", "message")
    out = pyouts.LogSafeTabular(style=pyout_style, columns=rec_fields, max_workers=jobs)

    out_helper = PYOUTHelper()
    pyout_style["done"] = pyout_style["size"].copy()
    pyout_style["size"]["aggregate"] = out_helper.agg_size
    pyout_style["done"]["aggregate"] = out_helper.agg_done

    # I thought I was making a beautiful flower but ended up with cacti
    # which never blooms... All because assets are looped through inside download_generator
    # TODO: redo
    kw = dict(assets_it=out_helper.it)
    if jobs > 1 and format == "pyout":
        # It could handle delegated to generator downloads
        kw["yield_generator_for_fields"] = rec_fields[1:]  # all but path

    gen_ = download_generator(
        parsed_url,
        output_path,
        existing=existing,
        get_metadata=get_metadata,
        get_assets=get_assets,
        jobs_per_zarr=jobs_per_zarr,
        **kw,
    )

    # TODOs:
    #  - redo frontends similarly to how command_ls did it
    #  - have a single loop with analysis of `rec` to either any file
    #    has failed to download.  If any was: exception should probably be
    #    raised.  API discussion for Python side of API:
    #
    if format == "debug":
        for rec in gen_:
            print(rec)
            sys.stdout.flush()
    elif format == "pyout":
        with out:
            for rec in gen_:
                out(rec)
    else:
        raise ValueError(format)

    if sync and not isinstance(parsed_url, SingleAssetURL):
        with parsed_url.get_client() as client:
            asset_paths = {asset.path for asset in parsed_url.get_assets(client)}
        if isinstance(parsed_url, DandisetURL):
            prefix = os.curdir
            download_dir = output_path
        elif isinstance(parsed_url, MultiAssetURL):
            folder_path = op.normpath(parsed_url.path)
            prefix = folder_path
            download_dir = op.join(output_path, op.basename(folder_path))
        else:
            raise NotImplementedError(
                f"Unexpected URL type {type(parsed_url).__name__}"
            )
        to_delete = []
        for df in find_dandi_files(download_dir, allow_all=True):
            if isinstance(df, DandisetMetadataFile):
                continue
            a_path = op.normpath(op.join(prefix, df.path))
            if on_windows:
                a_path = a_path.replace("\\", "/")
            if a_path not in asset_paths:
                to_delete.append(df.filepath)
        if to_delete:
            while True:
                opt = abbrev_prompt(
                    f"Delete {pluralize(len(to_delete), 'local asset')}?",
                    "yes",
                    "no",
                    "list",
                )
                if opt == "list":
                    for p in to_delete:
                        print(p)
                elif opt == "yes":
                    for p in to_delete:
                        os.unlink(p)
                    break
                else:
                    break


def download_generator(
    parsed_url,
    output_path,
    *,
    assets_it=None,
    yield_generator_for_fields=None,
    existing="error",
    get_metadata=True,
    get_assets=True,
    jobs_per_zarr=None,
):
    """A generator for downloads of files, folders, or entire dandiset from DANDI
    (as identified by URL)

    This function is a generator which would yield records on ongoing activities.
    Activities include traversal of the remote resource (DANDI archive), download of
    individual assets while yielding records (TODO: schema) while validating their
    checksums "on the fly", etc.

    Parameters
    ----------
    assets_it: IteratorWithAggregation
      which will be set .gen to assets.  Purpose is to make it possible to get
      summary statistics while already downloading.  TODO: reimplement properly!

    """

    with parsed_url.navigate(strict=True) as (client, dandiset, assets):
        if assets_it:
            assets_it.gen = assets
            assets = assets_it

        if isinstance(parsed_url, DandisetURL) and get_metadata:
            for resp in _populate_dandiset_yaml(output_path, dandiset, existing):
                yield dict(path=dandiset_metadata_file, **resp)

        # TODO: do analysis of assets for early detection of needed renames etc
        # to avoid any need for late treatment of existing and also for
        # more efficient download if files are just renamed etc

        if not get_assets:
            return

        for asset in assets:
            path = asset.path.lstrip("/")  # make into relative path
            path = op.normpath(path)
            if not isinstance(parsed_url, DandisetURL):
                if isinstance(parsed_url, MultiAssetURL):
                    folder_path = op.normpath(parsed_url.path)
                    path = op.join(
                        op.basename(folder_path), op.relpath(path, folder_path)
                    )
                elif isinstance(parsed_url, SingleAssetURL):
                    path = op.basename(path)
                else:
                    raise NotImplementedError(
                        f"Unexpected URL type {type(parsed_url).__name__}"
                    )
            download_path = op.join(output_path, path)

            try:
                metadata = asset.get_raw_metadata()
            except NotFoundError as e:
                yield {"path": path, "status": "error", "message": str(e)}
                continue
            d = metadata.get("digest", {})

            if asset.is_blob():
                if "dandi:dandi-etag" in d:
                    digests = {"dandi-etag": d["dandi:dandi-etag"]}
                else:
                    raise RuntimeError(
                        f"dandi-etag not available for asset. Known digests: {d}"
                    )
                try:
                    digests["sha256"] = d["dandi:sha2-256"]
                except KeyError:
                    pass
                try:
                    mtime = ensure_datetime(metadata["blobDateModified"])
                except KeyError:
                    mtime = None
                if mtime is None:
                    lgr.warning(
                        "Asset %s is missing blobDateModified metadata field",
                        asset.path,
                    )
                    mtime = asset.modified
                _download_generator = _download_file(
                    asset.get_download_file_iter(),
                    download_path,
                    toplevel_path=output_path,
                    # size and modified generally should be there but better to
                    # redownload than to crash
                    size=asset.size,
                    mtime=mtime,
                    existing=existing,
                    digests=digests,
                )

            else:
                assert asset.is_zarr(), f"Asset {asset.path} is neither blob nor Zarr"
                if not isinstance(asset, RemoteZarrAsset):
                    raise NotImplementedError(
                        "Downloading a Zarr asset identified by a URL without"
                        " Dandiset details is not yet implemented"
                    )
                _download_generator = _download_zarr(
                    asset,
                    download_path,
                    toplevel_path=output_path,
                    existing=existing,
                    jobs=jobs_per_zarr,
                )

            if yield_generator_for_fields:
                yield {"path": path, yield_generator_for_fields: _download_generator}
            else:
                for resp in _download_generator:
                    yield dict(resp, path=path)


class ItemsSummary:
    """A helper "structure" to accumulate information about assets to be downloaded

    To be used as a callback to IteratorWithAggregation
    """

    def __init__(self):
        self.files = 0
        # TODO: get rid of needing it
        self.t0 = None  # when first record is seen
        self.size = 0
        self.has_unknown_sizes = False

    def as_dict(self):
        return {a: getattr(self, a) for a in ("files", "size", "has_unknown_sizes")}

    def __call__(self, rec, prior=None):
        assert prior in (None, self)
        if not self.files:
            self.t0 = time.time()
        self.files += 1
        self.size += rec.size
        return self


class PYOUTHelper:
    """Helper for PYOUT styling

    Provides aggregation callbacks for PyOUT and also an iterator to be wrapped around
    iterating over assets, so it would get "totals" as soon as they are available.
    """

    def __init__(self):
        # Establish "fancy" download while still possibly traversing the dandiset
        # functionality.
        from .support.iterators import IteratorWithAggregation

        self.items_summary = ItemsSummary()
        self.it = IteratorWithAggregation(
            # unfortunately Yarik missed the point that we need to wrap
            # "assets" generator within downloader_generator
            # so we do not have assets here!  Ad-hoc solution for now is to
            # pass this beast so it could get .gen set within downloader_generator
            None,  # download_generator(urls, output_dir, existing=existing),
            self.items_summary,
        )

    def agg_files(self, *ignored):
        ret = str(self.items_summary.files)
        if not self.it.finished:
            ret += "+"
        return ret

    def agg_size(self, sizes):
        """Formatter for "size" column where it would show

        how much is "active" (or done)
        +how much yet to be "shown".
        """
        active = sum(sizes)
        if (active, self.items_summary.size) == (0, 0):
            return ""
        v = [naturalsize(active)]
        if not self.it.finished or (
            active != self.items_summary.size or self.items_summary.has_unknown_sizes
        ):
            extra = self.items_summary.size - active
            if extra < 0:
                lgr.debug("Extra size %d < 0 -- must not happen", extra)
            else:
                extra_str = "+%s" % naturalsize(extra)
                if not self.it.finished:
                    extra_str = ">" + extra_str
                if self.items_summary.has_unknown_sizes:
                    extra_str += "+?"
                v.append(extra_str)
        return v

    def agg_done(self, done_sizes):
        """Formatter for "DONE" column"""
        done = sum(done_sizes)
        if self.it.finished and done == 0 and self.items_summary.size == 0:
            # even with 0s everywhere consider it 100%
            r = 1.0
        elif self.items_summary.size:
            r = done / self.items_summary.size
        else:
            r = 0
        pref = ""
        if not self.it.finished:
            pref += "<"
        if self.items_summary.has_unknown_sizes:
            pref += "?"
        v = [naturalsize(done), "%s%.2f%%" % (pref, 100 * r)]
        if (
            done
            and self.items_summary.t0 is not None
            and r
            and self.items_summary.size != 0
        ):
            dt = time.time() - self.items_summary.t0
            more_time = dt / r if r != 1 else 0
            more_time_str = humanize.naturaldelta(more_time)
            if not self.it.finished:
                more_time_str += "<"
            if self.items_summary.has_unknown_sizes:
                more_time_str += "+?"
            if more_time:
                v.append("ETA: %s" % more_time_str)
        return v


def _skip_file(msg):
    return {"status": "skipped", "message": str(msg)}


def _populate_dandiset_yaml(dandiset_path, dandiset, existing):
    metadata = dandiset.get_raw_metadata()
    if not metadata:
        lgr.warning(
            "Got completely empty metadata record for dandiset, not producing dandiset.yaml"
        )
        return
    dandiset_yaml = op.join(dandiset_path, dandiset_metadata_file)
    yield {"message": "updating"}
    lgr.debug("Updating %s from obtained dandiset metadata", dandiset_metadata_file)
    mtime = dandiset.modified
    if op.lexists(dandiset_yaml):
        with open(dandiset_yaml) as fp:
            if yaml_load(fp, typ="safe") == metadata:
                yield _skip_file("no change")
                return
        if existing == "error":
            yield {"status": "error", "message": "already exists"}
            return
        elif existing == "refresh" and op.lexists(
            op.join(dandiset_path, ".git", "annex")
        ):
            raise RuntimeError("Not refreshing path in git annex repository")
        elif existing == "skip" or (
            existing == "refresh"
            and os.lstat(dandiset_yaml).st_mtime >= mtime.timestamp()
        ):
            yield _skip_file("already exists")
            return
    ds = Dandiset(dandiset_path, allow_empty=True)
    ds.path_obj.mkdir(exist_ok=True)  # exist_ok in case of parallel race
    old_metadata = ds.metadata
    ds.update_metadata(metadata)
    os.utime(dandiset_yaml, (time.time(), mtime.timestamp()))
    yield {
        "status": "done",
        "message": "updated" if metadata != old_metadata else "same",
    }


def _download_file(
    downloader,
    path,
    toplevel_path,
    size=None,
    mtime=None,
    existing="error",
    digests=None,
):
    """
    Common logic for downloading a single file.

    Yields progress records that take the following forms::

        {"status": "skipped", "message": "<MESSAGE>"}
        {"size": <int>}
        {"status": "downloading"}
        {"done": <bytes downloaded>[, "done%": <percentage done, from 0 to 100>]}
        {"status": "error", "message": "<MESSAGE>"}
        {"checksum": "differs", "status": "error", "message": "<MESSAGE>"}
        {"checksum": "ok"}
        {"checksum": "-"}  #  No digests were provided
        {"status": "setting mtime"}
        {"status": "done"}

    Parameters
    ----------
    downloader: callable returning a generator
      A backend-specific fixture for downloading some file into path. It should
      be a generator yielding downloaded blocks.
    size: int, optional
      Target size if known
    digests: dict, optional
      possible checksums or other digests provided for the file. Only one
      will be used to verify download
    """
    if op.lexists(path):
        block = f"File {path!r} already exists"
        annex_path = op.join(toplevel_path, ".git", "annex")
        if existing == "error":
            raise FileExistsError(block)
        elif existing == "skip":
            yield _skip_file("already exists")
            return
        elif existing == "overwrite":
            pass
        elif existing == "overwrite-different":
            realpath = op.realpath(path)
            key_parts = op.basename(realpath).split("-")
            if size is not None and os.stat(realpath).st_size != size:
                lgr.debug(
                    "Size of %s does not match size on server; redownloading", path
                )
            elif (
                op.lexists(annex_path)
                and op.islink(path)
                and path_is_subpath(realpath, op.abspath(annex_path))
                and key_parts[0] == "SHA256E"
                and digests
                and "sha256" in digests
            ):
                if key_parts[-1].partition(".")[0] == digests["sha256"]:
                    yield _skip_file("already exists")
                    return
                else:
                    lgr.debug(
                        "%s is in git-annex, and hash does not match hash on server; redownloading",
                        path,
                    )
            elif (
                "dandi-etag" in digests
                and get_digest(path, "dandi-etag") == digests["dandi-etag"]
            ):
                yield _skip_file("already exists")
                return
            elif (
                "dandi-etag" not in digests
                and "md5" in digests
                and get_digest(path, "md5") == digests["md5"]
            ):
                yield _skip_file("already exists")
                return
            else:
                lgr.debug(
                    "Etag of %s does not match etag on server; redownloading", path
                )
        elif existing == "refresh":
            if op.lexists(annex_path):
                raise RuntimeError("Not refreshing path in git annex repository")
            if mtime is None:
                lgr.warning(
                    f"{path!r} - no mtime or ctime in the record, redownloading"
                )
            else:
                stat = os.stat(op.realpath(path))
                same = []
                if is_same_time(stat.st_mtime, mtime):
                    same.append("mtime")
                if size is not None and stat.st_size == size:
                    same.append("size")
                # TODO: use digests if available? or if e.g. size is identical
                # but mtime is different
                if same == ["mtime", "size"]:
                    # TODO: add recording and handling of .nwb object_id
                    yield _skip_file("same time and size")
                    return
                lgr.debug(f"{path!r} - same attributes: {same}.  Redownloading")

    if size is not None:
        yield {"size": size}

    destdir = op.dirname(path)
    os.makedirs(destdir, exist_ok=True)

    yield {"status": "downloading"}

    algo, digester, digest, downloaded_digest = None, None, None, None
    if digests:
        # choose first available for now.
        # TODO: reuse that sorting based on speed
        for algo, digest in digests.items():
            if algo == "dandi-etag":
                from dandischema.digests.dandietag import ETagHashlike

                digester = lambda: ETagHashlike(size)  # noqa: E731
            else:
                digester = getattr(hashlib, algo, None)
            if digester:
                break
        if not digester:
            lgr.warning("Found no digests in hashlib for any of %s", str(digests))

    # TODO: how do we discover the total size????
    # TODO: do not do it in-place, but rather into some "hidden" file
    resuming = False
    for attempt in range(3):
        try:
            if digester:
                downloaded_digest = digester()  # start empty
            warned = False
            # I wonder if we could make writing async with downloader
            with DownloadDirectory(path, digests) as dldir:
                downloaded = dldir.offset
                resuming = downloaded > 0
                if size is not None and downloaded == size:
                    # Exit early when downloaded == size, as making a Range
                    # request in such a case results in a 416 error from S3.
                    # Problems will result if `size` is None but we've already
                    # downloaded everything.
                    break
                for block in downloader(start_at=dldir.offset):
                    if digester:
                        downloaded_digest.update(block)
                    downloaded += len(block)
                    # TODO: yield progress etc
                    msg = {"done": downloaded}
                    if size:
                        if downloaded > size and not warned:
                            warned = True
                            # Yield ERROR?
                            lgr.warning(
                                "Downloaded %d bytes although size was told to be just %d",
                                downloaded,
                                size,
                            )
                        msg["done%"] = 100 * downloaded / size if size else "100"
                        # TODO: ETA etc
                    yield msg
                    dldir.append(block)
            break
        except requests.exceptions.HTTPError as exc:
            # TODO: actually we should probably retry only on selected codes, and also
            # respect Retry-After
            if attempt >= 2 or exc.response.status_code not in (
                400,  # Bad Request, but happened with gider:
                # https://github.com/dandi/dandi-cli/issues/87
                *RETRY_STATUSES,
            ):
                lgr.debug("Download failed: %s", exc)
                yield {"status": "error", "message": str(exc)}
                return
            # if is_access_denied(exc) or attempt >= 2:
            #     raise
            # sleep a little and retry
            lgr.debug(
                "Failed to download on attempt#%d: %s, will sleep a bit and retry",
                attempt,
                exc,
            )
            time.sleep(random.random() * 5)

    if downloaded_digest and not resuming:
        downloaded_digest = downloaded_digest.hexdigest()  # we care only about hex
        if digest != downloaded_digest:
            msg = f"{algo}: downloaded {downloaded_digest} != {digest}"
            yield {"checksum": "differs", "status": "error", "message": msg}
            lgr.debug("%s is different: %s.", path, msg)
            return
        else:
            yield {"checksum": "ok"}
            lgr.debug("Verified that %s has correct %s %s", path, algo, digest)
    else:
        # shouldn't happen with more recent metadata etc
        yield {
            "checksum": "-",
            # "message": "no digests were provided"
        }

    # TODO: dissolve attrs and pass specific mtime?
    if mtime:
        yield {"status": "setting mtime"}
        os.utime(path, (time.time(), ensure_datetime(mtime).timestamp()))

    yield {"status": "done"}


class DownloadDirectory:
    def __init__(self, filepath, digests):
        #: The path to which to save the file after downloading
        self.filepath = Path(filepath)
        #: Expected hashes of the downloaded data, as a mapping from algorithm
        #: names to digests
        self.digests = digests
        #: The working directory in which downloaded data will be temporarily
        #: stored
        self.dirpath = self.filepath.with_name(self.filepath.name + ".dandidownload")
        #: The file in `dirpath` to which data will be written as it is
        #: received
        self.writefile = self.dirpath / "file"
        #: A `fasteners.InterProcessLock` on `dirpath`
        self.lock = None
        #: An open filehandle to `writefile`
        self.fp = None
        #: How much of the data has been downloaded so far
        self.offset = None

    def __enter__(self):
        from fasteners import InterProcessLock

        self.dirpath.mkdir(parents=True, exist_ok=True)
        self.lock = InterProcessLock(str(self.dirpath / "lock"))
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("Could not acquire download lock for {self.filepath}")
        chkpath = self.dirpath / "checksum"
        try:
            with chkpath.open() as fp:
                digests = json.load(fp)
        except (FileNotFoundError, ValueError):
            digests = {}
        matching_algs = self.digests.keys() & digests.keys()
        if matching_algs and all(
            self.digests[alg] == digests[alg] for alg in matching_algs
        ):
            # Pick up where we left off, writing to the end of the file
            lgr.debug(
                "Download directory exists and has matching checksum; resuming download"
            )
            self.fp = self.writefile.open("ab")
        else:
            # Delete the file (if it even exists) and start anew
            if not chkpath.exists():
                lgr.debug("Starting new download in new download directory")
            else:
                lgr.debug(
                    "Download directory found, but digests do not match; starting new download"
                )
            try:
                self.writefile.unlink()
            except FileNotFoundError:
                pass
            self.fp = self.writefile.open("wb")
        with chkpath.open("w") as fp:
            json.dump(self.digests, fp)
        self.offset = self.fp.tell()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.fp.close()
        try:
            if exc_type is None:
                try:
                    self.writefile.replace(self.filepath)
                except IsADirectoryError:
                    rmtree(self.filepath)
                    self.writefile.replace(self.filepath)
        finally:
            self.lock.release()
            if exc_type is None:
                rmtree(self.dirpath, ignore_errors=True)
            self.lock = None
            self.fp = None
            self.offset = None
        return False

    def append(self, blob):
        self.fp.write(blob)


def _download_zarr(
    asset: RemoteZarrAsset,
    download_path: str,
    toplevel_path: str,
    existing: str,
    jobs: Optional[int] = None,
) -> Iterator[dict]:
    download_gens = {}
    for entry in asset.iterfiles():
        etag = entry.get_etag()
        assert etag.algorithm is DigestType.md5
        stat = entry.stat()
        download_gens[entry.path] = _download_file(
            entry.get_download_file_iter(),
            op.join(download_path, op.normpath(str(entry))),
            toplevel_path=toplevel_path,
            size=stat.size,
            mtime=stat.modified,
            existing=existing,
            digests={"md5": etag.value},
        )
    pc = ProgressCombiner(zarr_size=asset.size, file_qty=len(download_gens))
    with interleave(
        [pairing(p, gen) for p, gen in download_gens.items()],
        onerror=FINISH_CURRENT,
        max_workers=jobs or 4,
    ) as it:
        for path, status in it:
            for out in pc.feed(path, status):
                if out.get("status") == "done":
                    break
                else:
                    yield out
        else:
            return
    # TODO: Delete local files not in remote Zarr
    yield {"status": "done"}


def pairing(p: str, gen: Iterator[dict]) -> Iterator[Tuple[str, dict]]:
    for d in gen:
        yield (p, d)


DLState = Enum("DLState", "STARTING DOWNLOADING SKIPPED ERROR CHECKSUM_ERROR DONE")


@dataclass
class DownloadProgress:
    state: DLState = DLState.STARTING
    downloaded: int = 0
    size: Optional[int] = None


@dataclass
class ProgressCombiner:
    zarr_size: int
    file_qty: int
    files: Dict[str, DownloadProgress] = field(default_factory=dict)
    #: Total size of all files that were not skipped and did not error out
    #: during download
    maxsize: int = 0
    prev_status: str = ""
    yielded_size: bool = False

    @property
    def message(self) -> str:
        done = 0
        errored = 0
        skipped = 0
        for s in self.files.values():
            if s.state is DLState.DONE:
                done += 1
            elif s.state in (DLState.ERROR, DLState.CHECKSUM_ERROR):
                errored += 1
            elif s.state is DLState.SKIPPED:
                skipped += 1
        parts = []
        if done:
            parts.append(f"{done} done")
        if errored:
            parts.append(f"{errored} errored")
        if skipped:
            parts.append(f"{skipped} skipped")
        return ", ".join(parts)

    def get_done(self) -> dict:
        total_downloaded = sum(
            s.downloaded
            for s in self.files.values()
            if s.state in (DLState.DOWNLOADING, DLState.CHECKSUM_ERROR, DLState.DONE)
        )
        return {
            "done": total_downloaded,
            "done%": total_downloaded / self.maxsize * 100,
        }

    def set_status(self, statusdict: dict) -> None:
        state_qtys = Counter(s.state for s in self.files.values())
        total = len(self.files)
        if (
            total == self.file_qty
            and state_qtys[DLState.STARTING] == state_qtys[DLState.DOWNLOADING] == 0
        ):
            # All files have finished
            if state_qtys[DLState.ERROR] or state_qtys[DLState.CHECKSUM_ERROR]:
                new_status = "error"
            elif state_qtys[DLState.DONE]:
                new_status = "done"
            else:
                new_status = "skipped"
        elif total - state_qtys[DLState.STARTING] - state_qtys[DLState.SKIPPED] > 0:
            new_status = "downloading"
        else:
            new_status = ""
        if new_status != self.prev_status:
            statusdict["status"] = new_status
            self.prev_status = new_status

    def feed(self, path: str, status: dict) -> Iterator[dict]:
        keys = list(status.keys())
        self.files.setdefault(path, DownloadProgress())
        if status.get("status") == "skipped":
            self.files[path].state = DLState.SKIPPED
            out = {"message": self.message}
            self.set_status(out)
            yield out
        elif keys == ["size"]:
            if not self.yielded_size:
                yield {"size": self.zarr_size}
                self.yielded_size = True
            self.files[path].size = status["size"]
            self.maxsize += status["size"]
            if any(s.state is DLState.DOWNLOADING for s in self.files.values()):
                yield self.get_done()
        elif status == {"status": "downloading"}:
            self.files[path].state = DLState.DOWNLOADING
            out = {}
            self.set_status(out)
            if out:
                yield out
        elif "done" in status:
            self.files[path].downloaded = status["done"]
            yield self.get_done()
        elif status.get("status") == "error":
            if "checksum" in status:
                self.files[path].state = DLState.CHECKSUM_ERROR
                out = {"message": self.message}
                self.set_status(out)
                yield out
            else:
                self.files[path].state = DLState.ERROR
                out = {"message": self.message}
                self.set_status(out)
                yield out
                sz = self.files[path].size
                if sz is not None:
                    self.maxsize -= sz
                    yield self.get_done()
        elif keys == ["checksum"]:
            pass
        elif status == {"status": "setting mtime"}:
            pass
        elif status == {"status": "done"}:
            self.files[path].state = DLState.DONE
            out = {"message": self.message}
            self.set_status(out)
            yield out
        else:
            lgr.warning(
                "Unexpected download status dict for %r received: %r", path, status
            )
