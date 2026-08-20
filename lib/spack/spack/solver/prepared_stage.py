# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Trusted preparation of source plans without importing recipe code."""

import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, FrozenSet, Optional, Tuple

import spack.error
import spack.util.web
from spack.solver.source_plan import validate_source_plan

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


@dataclass
class _PreparationBudget:
    entries: int = 0
    downloaded_bytes: int = 0
    expanded_bytes: int = 0

    def add_download(self, size: int) -> None:
        self.downloaded_bytes += size
        if self.downloaded_bytes > MAX_DOWNLOAD_BYTES:
            raise PreparedStageError("source downloads exceed the size limit")

    def add_archive_entry(self, size: int) -> None:
        if size < 0:
            raise PreparedStageError("archive entry has an invalid size")
        self.entries += 1
        self.expanded_bytes += size
        if self.entries > MAX_ARCHIVE_ENTRIES:
            raise PreparedStageError("archives contain too many entries")
        if self.expanded_bytes > MAX_EXPANDED_BYTES:
            raise PreparedStageError("archives expand beyond the size limit")


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


def _fetch(
    source: Dict[str, Any],
    destination: Path,
    policy: SourceFetchPolicy,
    budget: _PreparationBudget,
) -> Path:
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
                url, headers={"User-Agent": spack.util.web.SPACK_USER_AGENT, "Accept": "*/*"}
            )
            digest = hashlib.sha256()
            with opener.open(request) as response, open(destination, "xb") as output:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    budget.add_download(len(chunk))
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


def _extract_tar(archive: Path, destination: Path, budget: _PreparationBudget) -> None:
    with tarfile.open(archive, mode="r:*") as source:
        for member in source:
            relative = _relative_archive_path(member.name)
            budget.add_archive_entry(member.size)
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


def _extract_zip(archive: Path, destination: Path, budget: _PreparationBudget) -> None:
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            relative = _relative_archive_path(member.filename)
            budget.add_archive_entry(member.file_size)
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


def _extract_archive(
    archive: Path, destination: Path, extension: Optional[str], budget: _PreparationBudget
) -> None:
    if extension == "zip" or (extension is None and zipfile.is_zipfile(archive)):
        _extract_zip(archive, destination, budget)
        return
    if extension in (None, "tar", "tar.gz", "tgz", "tar.bz2", "tbz2", "tar.xz", "txz"):
        try:
            _extract_tar(archive, destination, budget)
            return
        except tarfile.TarError as error:
            raise PreparedStageError(f"unsupported or invalid source archive: {error}") from error
    raise PreparedStageError(f"unsupported source archive extension: {extension}")


def _publish_extracted_archive(container: Path, destination: Path) -> None:
    entries = list(container.iterdir())
    non_hidden = [entry for entry in entries if not entry.name.startswith(".")]
    if len(non_hidden) == 1 and non_hidden[0].is_dir():
        if len(entries) != 1:
            raise PreparedStageError("unsupported archive layout beside top-level directory")
        entries = list(non_hidden[0].iterdir())
    for entry in entries:
        target = destination / entry.name
        if target.exists():
            raise PreparedStageError(f"archive extraction conflict: {entry.name}")
        entry.rename(target)


def _prepare_source(
    source: Dict[str, Any],
    destination: Path,
    download: Path,
    fetch_policy: SourceFetchPolicy,
    budget: _PreparationBudget,
) -> None:
    download.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = _fetch(source, download, fetch_policy, budget)
    if source["expand"]:
        container = destination.parent / f".{destination.name}.extracting"
        container.mkdir(mode=0o700)
        try:
            _extract_archive(archive, container, source["extension"], budget)
            _publish_extracted_archive(container, destination)
        finally:
            shutil.rmtree(container, ignore_errors=True)
    else:
        filename = Path(urllib.parse.urlsplit(source["urls"][0]).path).name or "source"
        shutil.copy2(archive, destination / filename)


def _prepare_resources(
    resources: Any,
    root: Path,
    workspace: Path,
    fetch_policy: SourceFetchPolicy,
    budget: _PreparationBudget,
) -> None:
    for index, resource in enumerate(resources):
        resource_root = workspace / "resources" / str(index)
        resource_root.mkdir(mode=0o700, parents=True)
        _prepare_source(
            resource["source"],
            resource_root,
            workspace / "downloads" / str(index),
            fetch_policy,
            budget,
        )
        target = root / resource["destination"] / resource["placement"]
        if target.exists():
            raise PreparedStageError(
                f"resource placement already exists: {target.relative_to(root)}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        resource_root.rename(target)


def prepare_stage(
    plan: Dict[str, Any],
    destination: Path,
    *,
    expected_provenance: Dict[str, Any],
    fetch_policy: SourceFetchPolicy,
) -> PreparedStage:
    """Fetch, verify, and safely expand a validated fixed-URL source plan."""
    validate_source_plan(plan, expected_provenance=expected_provenance)
    for source in [plan["source"]] + [resource["source"] for resource in plan["resources"]]:
        for url in source["urls"]:
            fetch_policy.validate(url)
    destination = destination.resolve()
    if destination.exists():
        raise PreparedStageError("prepared stage destination must not already exist")
    destination.parent.mkdir(parents=True, exist_ok=True)

    workspace = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.preparing-", dir=destination.parent)
    )
    temporary = workspace / "source"
    temporary.mkdir(mode=0o700)
    budget = _PreparationBudget()
    try:
        _prepare_source(
            plan["source"], temporary, workspace / "downloads" / "source", fetch_policy, budget
        )
        _prepare_resources(plan["resources"], temporary, workspace, fetch_policy, budget)
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
