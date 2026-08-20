# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Trusted preparation of source plans without importing recipe code."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile
from typing import Any, Dict, FrozenSet, Optional, Tuple
import urllib.parse
import urllib.request
import zipfile

import spack.error
from spack.solver.source_plan import validate_source_plan
import spack.util.web


MAX_ARCHIVE_ENTRIES = 100000
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 16 * 1024 * 1024 * 1024


class PreparedStageError(spack.error.SpackError):
    """Raised when trusted source preparation cannot be completed safely."""


@dataclass(frozen=True)
class PreparedStage:
    """Published source tree and the identities needed to verify it."""

    path: Path
    source_plan_sha256: str
    content_sha256: str


@dataclass(frozen=True)
class SourceFetchPolicy:
    """Explicit authorities that trusted source preparation may access."""

    https_origins: FrozenSet[Tuple[str, int]] = frozenset()
    http_origins: FrozenSet[Tuple[str, int]] = frozenset()
    file_roots: Tuple[Path, ...] = ()

    def validate(self, url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise PreparedStageError("source URL contains forbidden credentials or fragment")
        if parsed.scheme == "file":
            if parsed.netloc not in ("", "localhost"):
                raise PreparedStageError("source file URL must be local")
            path = Path(urllib.request.url2pathname(parsed.path)).resolve()
            roots = tuple(root.resolve() for root in self.file_roots)
            if not roots or not any(path == root or root in path.parents for root in roots):
                raise PreparedStageError("source URL is outside allowed file roots")
            return
        try:
            port = parsed.port
        except ValueError as error:
            raise PreparedStageError(f"invalid source URL port: {error}") from error
        host = (parsed.hostname or "").lower()
        origin = (host, port or (443 if parsed.scheme == "https" else 80))
        if parsed.scheme == "https" and origin in self.https_origins:
            return
        if parsed.scheme == "http" and origin in self.http_origins:
            return
        raise PreparedStageError("source URL authority is not allowed")


class _PolicyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, policy: SourceFetchPolicy) -> None:
        self.policy = policy

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.policy.validate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def source_plan_digest(plan: Dict[str, Any]) -> str:
    data = json.dumps(plan, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def prepared_stage_digest(root: Path) -> str:
    """Compute a deterministic identity without following links or accepting special files."""
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode):
        raise PreparedStageError("prepared-stage root must be a directory")
    identity = hashlib.sha256()

    def visit(directory: Path, relative: PurePosixPath) -> None:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = Path(entry.path)
                child = relative / entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    record = {"path": str(child), "type": "directory"}
                    identity.update(
                        json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
                    )
                    identity.update(b"\n")
                    visit(path, child)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise PreparedStageError(f"unsupported prepared-stage entry: {child}")
                content = hashlib.sha256()
                with open(path, "rb") as stream:
                    for chunk in iter(lambda: stream.read(64 * 1024), b""):
                        content.update(chunk)
                record = {
                    "path": str(child),
                    "type": "file",
                    "executable": bool(info.st_mode & 0o111),
                    "size": info.st_size,
                    "sha256": content.hexdigest(),
                }
                identity.update(
                    json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
                )
                identity.update(b"\n")

    visit(root, PurePosixPath())
    return identity.hexdigest()


def _fetch(plan: Dict[str, Any], destination: Path, policy: SourceFetchPolicy) -> Path:
    source = plan["source"]
    for url in source["urls"]:
        policy.validate(url)
    opener = urllib.request.build_opener(
        _PolicyRedirectHandler(policy),
        spack.util.web.SpackHTTPSHandler(context=spack.util.web.ssl_create_default_context()),
        spack.util.web.SpackHTTPDefaultErrorHandler(),
    )
    errors = []
    for url in source["urls"]:
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": spack.util.web.SPACK_USER_AGENT, "Accept": "*/*"},
            )
            digest = hashlib.sha256()
            size = 0
            with opener.open(request) as response, open(destination, "xb") as output:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise PreparedStageError("source download exceeds the size limit")
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != source["sha256"]:
                raise PreparedStageError("source checksum does not match the plan")
            return destination
        except Exception as error:
            destination.unlink(missing_ok=True)
            errors.append(error)
    raise PreparedStageError(f"all source URLs failed: {errors[-1]}")


def _relative_archive_path(name: str) -> Path:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\x00" in name:
        raise PreparedStageError(f"unsafe archive path: {name!r}")
    parts = [part for part in path.parts if part not in ("", ".")]
    if not parts:
        raise PreparedStageError(f"unsafe archive path: {name!r}")
    return Path(*parts)


def _check_archive_limits(entries: int, expanded_bytes: int) -> None:
    if entries > MAX_ARCHIVE_ENTRIES:
        raise PreparedStageError("archive contains too many entries")
    if expanded_bytes > MAX_EXPANDED_BYTES:
        raise PreparedStageError("archive expands beyond the size limit")


def _extract_tar(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, mode="r:*") as source:
        expanded_bytes = 0
        for index, member in enumerate(source, start=1):
            relative = _relative_archive_path(member.name)
            expanded_bytes += member.size
            _check_archive_limits(index, expanded_bytes)
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise PreparedStageError(f"unsupported archive entry: {member.name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = source.extractfile(member)
            if extracted is None:
                raise PreparedStageError(f"cannot read archive entry: {member.name!r}")
            with extracted, open(target, "xb") as output:
                shutil.copyfileobj(extracted, output)
            os.chmod(target, stat.S_IMODE(member.mode) & 0o777)


def _extract_zip(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as source:
        expanded_bytes = 0
        for index, member in enumerate(source.infolist(), start=1):
            relative = _relative_archive_path(member.filename)
            expanded_bytes += member.file_size
            _check_archive_limits(index, expanded_bytes)
            target = destination / relative
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise PreparedStageError(f"unsupported archive entry: {member.filename!r}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if mode and not stat.S_ISREG(mode):
                raise PreparedStageError(f"unsupported archive entry: {member.filename!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as extracted, open(target, "xb") as output:
                shutil.copyfileobj(extracted, output)
            if mode:
                os.chmod(target, stat.S_IMODE(mode) & 0o777)


def _extract_archive(archive: Path, destination: Path, extension: Optional[str]) -> None:
    if extension == "zip" or (extension is None and zipfile.is_zipfile(archive)):
        _extract_zip(archive, destination)
        return
    if extension in (None, "tar", "tar.gz", "tgz", "tar.bz2", "tbz2", "tar.xz", "txz"):
        try:
            _extract_tar(archive, destination)
            return
        except tarfile.TarError as error:
            raise PreparedStageError(f"unsupported or invalid source archive: {error}") from error
    raise PreparedStageError(f"unsupported source archive extension: {extension}")


def prepare_stage(
    plan: Dict[str, Any],
    destination: Path,
    *,
    expected_provenance: Dict[str, Any],
    fetch_policy: SourceFetchPolicy,
) -> PreparedStage:
    """Fetch, verify, and safely expand a validated fixed-URL source plan."""
    validate_source_plan(plan, expected_provenance=expected_provenance)
    destination = destination.resolve()
    if destination.exists():
        raise PreparedStageError("prepared stage destination must not already exist")
    destination.parent.mkdir(parents=True, exist_ok=True)

    source = plan["source"]
    workspace = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.preparing-", dir=destination.parent)
    )
    temporary = workspace / "source"
    temporary.mkdir(mode=0o700)
    download = workspace / "download" / "archive"
    download.parent.mkdir(mode=0o700)
    try:
        archive = _fetch(plan, download, fetch_policy)
        if source["expand"]:
            _extract_archive(archive, temporary, source["extension"])
        else:
            filename = Path(urllib.parse.urlsplit(source["urls"][0]).path).name or "source"
            shutil.copy2(archive, temporary / filename)
        temporary.rename(destination)
    except BaseException:
        raise
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return PreparedStage(
        path=destination,
        source_plan_sha256=source_plan_digest(plan),
        content_sha256=prepared_stage_digest(destination),
    )
