# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Trusted preparation of source plans without importing recipe code."""

import base64
import bz2
import gzip
import hashlib
import json
import lzma
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, FrozenSet, Optional, Tuple

import spack.error
import spack.util.compression
import spack.util.url
import spack.util.web
from spack.solver.source_plan import (
    MAX_PATCH_BYTES,
    MAX_PATCH_BYTES_TOTAL,
    SUPPORTED_PATCH_ARCHIVE_EXTENSIONS,
    SourcePlanError,
    _validate_unified_diff,
    validate_source_plan,
)

MAX_ARCHIVE_ENTRIES = 100000
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 16 * 1024 * 1024 * 1024
MAX_PATCH_DOWNLOAD_BYTES = 4 * 1024 * 1024
PATCH_PROTOCOL_VERSION = 1
PATCH_TIMEOUT_SECONDS = 120
MAX_PATCH_WORKER_RESPONSE_BYTES = 64 * 1024
MAX_PATCH_WORKER_STDERR_BYTES = 1024 * 1024


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
    max_entries: Optional[int] = None
    max_expanded_bytes: Optional[int] = None

    def add_download(self, size: int) -> None:
        self.downloaded_bytes += size
        if self.downloaded_bytes > MAX_DOWNLOAD_BYTES:
            raise PreparedStageError("source downloads exceed the size limit")

    def add_archive_entry(self, size: int) -> None:
        if size < 0:
            raise PreparedStageError("archive entry has an invalid size")
        self.entries += 1
        entry_limit = self.max_entries if self.max_entries is not None else MAX_ARCHIVE_ENTRIES
        if self.entries > entry_limit:
            raise PreparedStageError("archives contain too many entries")
        self.add_expanded(size)

    def add_expanded(self, size: int) -> None:
        self.expanded_bytes += size
        expanded_limit = (
            self.max_expanded_bytes if self.max_expanded_bytes is not None else MAX_EXPANDED_BYTES
        )
        if self.expanded_bytes > expanded_limit:
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
    *,
    max_bytes: Optional[int] = None,
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
            downloaded = 0
            with opener.open(request) as response, open(destination, "xb") as output:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    budget.add_download(len(chunk))
                    downloaded += len(chunk)
                    if max_bytes is not None and downloaded > max_bytes:
                        raise PreparedStageError("download exceeds the item size limit")
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


def _extract_single_file_compression(
    archive: Path, destination: Path, extension: str, budget: _PreparationBudget
) -> None:
    opener = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[extension]
    budget.add_archive_entry(0)
    try:
        with opener(archive, "rb") as source, open(destination, "xb") as output:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                budget.add_expanded(len(chunk))
                output.write(chunk)
    except (EOFError, OSError, lzma.LZMAError) as error:
        raise PreparedStageError(f"invalid compressed URL patch: {error}") from error


def _extract_unix_compress(archive: Path, destination: Path, budget: _PreparationBudget) -> None:
    gzip_executable = shutil.which("gzip")
    if gzip_executable is None:
        raise PreparedStageError("gzip executable is required for .Z URL patches")
    gzip_executable = str(Path(gzip_executable).resolve(strict=True))
    budget.add_archive_entry(0)
    process = subprocess.Popen(
        (gzip_executable, "-cd", "--", str(archive)),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        with process.stdout, open(destination, "xb") as output:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                budget.add_expanded(len(chunk))
                output.write(chunk)
        if process.wait() != 0:
            raise PreparedStageError("invalid .Z-compressed URL patch")
    except BaseException:
        destination.unlink(missing_ok=True)
        if process.poll() is None:
            _kill_process_group(process)
        raise


def _detected_patch_archive_extension(archive: Path) -> str:
    try:
        extension = spack.util.compression.extension_from_magic_numbers(
            str(archive), decompress=True
        ) or spack.util.compression.extension_from_magic_numbers(str(archive), decompress=False)
    except (EOFError, OSError, lzma.LZMAError) as error:
        raise PreparedStageError(f"invalid compressed URL patch: {error}") from error
    if extension not in SUPPORTED_PATCH_ARCHIVE_EXTENSIONS:
        raise PreparedStageError("unsupported extensionless compressed URL patch format")
    return extension


def _extract_url_patch(
    archive: Path, destination: Path, extension: Optional[str], budget: _PreparationBudget
) -> Path:
    extension = extension or _detected_patch_archive_extension(archive)
    if extension in ("gz", "bz2", "xz"):
        payload = destination / "patch"
        _extract_single_file_compression(archive, payload, extension, budget)
        return payload
    if extension == "Z":
        payload = destination / "patch"
        _extract_unix_compress(archive, payload, budget)
        return payload
    archive_extension = {
        "whl": "zip",
        "tbz": "tar.bz2",
        "TAR": "tar",
        "TAR.gz": "tar.gz",
        "TAR.bz2": "tar.bz2",
        "TAR.xz": "tar.xz",
    }.get(extension, extension)
    _extract_archive(archive, destination, archive_extension, budget)
    entries = list(destination.iterdir())
    if len(entries) != 1 or not entries[0].is_file() or entries[0].is_symlink():
        raise PreparedStageError("compressed URL patch must contain exactly one regular file")
    return entries[0]


def _publish_extracted_archive(container: Path, destination: Path) -> Optional[str]:
    entries = list(container.iterdir())
    non_hidden = [entry for entry in entries if not entry.name.startswith(".")]
    top_level_directory = None
    if len(non_hidden) == 1 and non_hidden[0].is_dir():
        if len(entries) != 1:
            raise PreparedStageError("unsupported archive layout beside top-level directory")
        top_level_directory = non_hidden[0].name
        entries = list(non_hidden[0].iterdir())
    for entry in entries:
        target = destination / entry.name
        if target.exists():
            raise PreparedStageError(f"archive extraction conflict: {entry.name}")
        entry.rename(target)
    return top_level_directory


def _prepare_source(
    source: Dict[str, Any],
    destination: Path,
    download: Path,
    fetch_policy: SourceFetchPolicy,
    budget: _PreparationBudget,
) -> Optional[str]:
    download.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = _fetch(source, download, fetch_policy, budget)
    if source["expand"]:
        container = destination.parent / f".{destination.name}.extracting"
        container.mkdir(mode=0o700)
        try:
            _extract_archive(archive, container, source["extension"], budget)
            return _publish_extracted_archive(container, destination)
        finally:
            shutil.rmtree(container, ignore_errors=True)
    else:
        filename = spack.util.url.default_download_filename(source["urls"][0])
        shutil.copy2(archive, destination / filename)
    return None


def _place_resource_mapping(resource_root: Path, root: Path, resource: Any) -> None:
    copies = []
    for mapping in resource["placement"]:
        source = resource_root / mapping["source"]
        target = root / resource["destination"] / mapping["destination"]
        if not source.exists():
            raise PreparedStageError(
                f"resource placement source does not exist: {mapping['source']}"
            )
        if source.is_symlink() or not (source.is_file() or source.is_dir()):
            raise PreparedStageError(f"unsupported resource placement source: {mapping['source']}")
        if target.exists() or target.is_symlink():
            raise PreparedStageError(
                f"resource placement already exists: {target.relative_to(root)}"
            )
        for parent in target.parents:
            if parent == root:
                break
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise PreparedStageError(
                    f"resource placement parent is not a directory: {parent.relative_to(root)}"
                )
        copies.append((source, target))

    for source, target in copies:
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, copy_function=shutil.copy2)
        else:
            shutil.copy2(source, target)


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
        implicit_placement = _prepare_source(
            resource["source"],
            resource_root,
            workspace / "downloads" / str(index),
            fetch_policy,
            budget,
        )
        placement = resource["placement"]
        if isinstance(placement, list):
            _place_resource_mapping(resource_root, root, resource)
            continue
        if placement is None:
            if implicit_placement is None:
                raise PreparedStageError(
                    "implicit resource placement requires one top-level directory"
                )
            placement = str(_relative_archive_path(implicit_placement))
        target = root / resource["destination"] / placement
        if target.exists():
            raise PreparedStageError(
                f"resource placement already exists: {target.relative_to(root)}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        resource_root.rename(target)


def _patch_worker_command() -> Tuple[str, ...]:
    worker = Path(__file__).with_name("_patch_worker.py")
    return (sys.executable, "-I", "-S", "-B", str(worker))


def _patch_worker_environment(state: Path) -> Dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH", "LD_PRELOAD", "SPACK_ENV"}
    }
    environment.update({"HOME": str(state), "TMPDIR": str(state), "LC_ALL": "C"})
    return environment


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.kill()
    process.wait()


def _apply_patches(
    patches: Any,
    source: Path,
    workspace: Path,
    fetch_policy: SourceFetchPolicy,
    budget: _PreparationBudget,
) -> None:
    if not patches:
        return
    patch_executable = shutil.which("patch")
    if patch_executable is None:
        raise PreparedStageError("patch executable is required for source plan patches")
    patch_executable = str(Path(patch_executable).resolve(strict=True))
    state = workspace / "patch-state"
    state.mkdir(mode=0o700)
    descriptions = []
    total_patch_bytes = 0
    for index, patch in enumerate(patches):
        path = state / str(index)
        if patch["kind"] == "inline":
            content = base64.b64decode(patch["content_base64"], validate=True)
            targets = patch["targets"]
        else:
            patch_source = {
                "kind": "url",
                "urls": [patch["url"]],
                "sha256": patch["archive_sha256"] or patch["sha256"],
                "expand": patch["archive_sha256"] is not None,
                "extension": patch["extension"],
            }
            download = workspace / "downloads" / f"patch-{index}"
            download.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            archive = _fetch(
                patch_source,
                download,
                fetch_policy,
                budget,
                max_bytes=(
                    MAX_PATCH_DOWNLOAD_BYTES
                    if patch["archive_sha256"] is not None
                    else MAX_PATCH_BYTES
                ),
            )
            payload = archive
            if patch["archive_sha256"] is not None:
                extracted = workspace / "patches" / str(index)
                extracted.mkdir(mode=0o700, parents=True)
                patch_budget = _PreparationBudget(
                    max_entries=1, max_expanded_bytes=MAX_PATCH_BYTES
                )
                payload = _extract_url_patch(archive, extracted, patch["extension"], patch_budget)
            with open(payload, "rb") as stream:
                content = stream.read(MAX_PATCH_BYTES + 1)
            if not content or len(content) > MAX_PATCH_BYTES:
                raise PreparedStageError("URL patch payload exceeds the size limit")
            if hashlib.sha256(content).hexdigest() != patch["sha256"]:
                raise PreparedStageError("URL patch payload checksum does not match")
            try:
                targets = _validate_unified_diff(content, patch["level"])
            except SourcePlanError as error:
                raise PreparedStageError(f"invalid URL patch payload: {error}") from error
        total_patch_bytes += len(content)
        if total_patch_bytes > MAX_PATCH_BYTES_TOTAL:
            raise PreparedStageError("patch payloads exceed the aggregate size limit")
        with open(path, "xb") as stream:
            stream.write(content)
        descriptions.append(
            {
                "path": str(path),
                "sha256": patch["sha256"],
                "level": patch["level"],
                "working_dir": patch["working_dir"],
                "reverse": patch["reverse"],
                "targets": targets,
            }
        )
    request = {
        "protocol_version": PATCH_PROTOCOL_VERSION,
        "source_path": str(source),
        "state_directory": str(state),
        "patch_executable": patch_executable,
        "patches": descriptions,
    }
    request_bytes = json.dumps(request, allow_nan=False, separators=(",", ":")).encode("utf-8")
    process = subprocess.Popen(
        _patch_worker_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
        env=_patch_worker_environment(state),
    )
    timeout_error = None
    try:
        stdout, stderr = process.communicate(request_bytes, timeout=PATCH_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        timeout_error = error
        _kill_process_group(process)
        stdout, stderr = process.communicate()
    if timeout_error is not None:
        raise PreparedStageError(
            f"patch worker timed out after {PATCH_TIMEOUT_SECONDS} seconds"
        ) from timeout_error
    if len(stdout) > MAX_PATCH_WORKER_RESPONSE_BYTES:
        raise PreparedStageError("patch worker response is too large")
    if len(stderr) > MAX_PATCH_WORKER_STDERR_BYTES:
        raise PreparedStageError("patch worker diagnostic output is too large")

    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        response = json.loads(stdout.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise PreparedStageError("patch worker returned an invalid response") from error
    if process.returncode != 0 or not isinstance(response, dict):
        raise PreparedStageError(f"patch worker exited with status {process.returncode}")
    if (
        response.get("protocol_version") != PATCH_PROTOCOL_VERSION
        or response.get("ok") is not True
    ):
        error = response.get("error", {})
        raise PreparedStageError(
            "patch worker failed during {} ({}): {}".format(
                error.get("phase", "unknown"),
                error.get("type", "Error"),
                error.get("message", "patch application failed"),
            )
        )
    if set(response) != {"protocol_version", "ok", "sandbox", "applied"}:
        raise PreparedStageError("patch worker response has unexpected fields")
    sandbox = response["sandbox"]
    if (
        not isinstance(sandbox, dict)
        or set(sandbox) != {"backend", "abi_version", "filesystem_restricted", "tcp_restricted"}
        or sandbox["backend"] != "landlock"
        or type(sandbox["abi_version"]) is not int
        or sandbox["abi_version"] < 4
        or sandbox["filesystem_restricted"] is not True
        or sandbox["tcp_restricted"] is not True
    ):
        raise PreparedStageError("patch worker did not apply the required restrictions")
    if response["applied"] != [patch["sha256"] for patch in patches]:
        raise PreparedStageError("patch worker returned inconsistent patch identities")


def prepare_stage(
    plan: Dict[str, Any],
    destination: Path,
    *,
    expected_provenance: Dict[str, Any],
    fetch_policy: SourceFetchPolicy,
) -> PreparedStage:
    """Fetch, verify, and safely expand a validated fixed-URL source plan."""
    validate_source_plan(plan, expected_provenance=expected_provenance)
    planned_sources = [plan["source"]] + [resource["source"] for resource in plan["resources"]]
    planned_sources.extend(
        {"urls": [patch["url"]]} for patch in plan["patches"] if patch["kind"] == "url"
    )
    for source in planned_sources:
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
        _apply_patches(plan["patches"], temporary, workspace, fetch_policy, budget)
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
