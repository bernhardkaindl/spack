# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Fresh-exec worker for applying validated patches to an unpublished source tree."""

import hashlib
import json
import os
import resource
import subprocess
import sys
from pathlib import Path

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_PATCHES = 32


class PatchWorkerError(Exception):
    pass


def _spack_library_path():
    return str(Path(__file__).resolve().parents[2])


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PatchWorkerError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_request():
    data = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(data) > MAX_REQUEST_BYTES:
        raise PatchWorkerError("request is too large")
    try:
        request = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(PatchWorkerError(value)),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise PatchWorkerError(f"invalid request JSON: {error}") from error
    if not isinstance(request, dict) or set(request) != {
        "protocol_version",
        "source_path",
        "state_directory",
        "patch_executable",
        "patches",
    }:
        raise PatchWorkerError("request has unexpected fields")
    if request["protocol_version"] != PROTOCOL_VERSION:
        raise PatchWorkerError("unsupported protocol version")
    for key in ("source_path", "state_directory", "patch_executable"):
        value = request[key]
        if not isinstance(value, str) or not os.path.isabs(value):
            raise PatchWorkerError(f"{key} must be an absolute path")
    source_path = Path(request["source_path"]).resolve(strict=True)
    state_directory = Path(request["state_directory"]).resolve(strict=True)
    patch_executable = Path(request["patch_executable"]).resolve(strict=True)
    if not source_path.is_dir() or not state_directory.is_dir():
        raise PatchWorkerError("worker directories must exist")
    if not patch_executable.is_file() or not os.access(patch_executable, os.X_OK):
        raise PatchWorkerError("patch executable is not executable")
    patches = request["patches"]
    if not isinstance(patches, list) or not patches or len(patches) > MAX_PATCHES:
        raise PatchWorkerError("patches must be a bounded non-empty list")
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) != {
            "path",
            "sha256",
            "level",
            "working_dir",
            "reverse",
            "targets",
        }:
            raise PatchWorkerError("invalid patch description")
        if not isinstance(patch["path"], str) or not os.path.isabs(patch["path"]):
            raise PatchWorkerError("patch path must be absolute")
        path = Path(patch["path"]).resolve(strict=True)
        try:
            path.relative_to(state_directory)
        except ValueError as error:
            raise PatchWorkerError("patch path is outside private state") from error
        if not path.is_file():
            raise PatchWorkerError("patch file does not exist")
        patch["path"] = str(path)
        sha256 = patch["sha256"]
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise PatchWorkerError("invalid patch checksum")
        level = patch["level"]
        if type(level) is not int or not 0 <= level <= 16:
            raise PatchWorkerError("invalid patch level")
        if not isinstance(patch["working_dir"], str) or not patch["working_dir"]:
            raise PatchWorkerError("invalid patch working directory")
        if not isinstance(patch["reverse"], bool):
            raise PatchWorkerError("invalid patch reverse policy")
        targets = patch["targets"]
        if (
            not isinstance(targets, list)
            or not targets
            or len(targets) > 256
            or any(not isinstance(target, str) or not target for target in targets)
        ):
            raise PatchWorkerError("invalid patch targets")
    return request, source_path, state_directory, patch_executable


def _apply_sandbox(source_path, state_directory):
    sys.path.insert(0, _spack_library_path())
    from spack.sandbox import LandlockSandbox, get_sandbox

    sandbox = get_sandbox()
    if not isinstance(sandbox, LandlockSandbox):
        raise RuntimeError("Landlock sandbox is unavailable")
    sandbox.allow_read("/")
    sandbox.allow_write(str(source_path))
    sandbox.allow_write(str(state_directory))
    sandbox.apply(restrict_filesystem=True, restrict_network=True)
    return sandbox.abi_version


def _digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _working_directory(source_path, relative):
    working_directory = (source_path / relative).resolve(strict=True)
    try:
        working_directory.relative_to(source_path)
    except ValueError as error:
        raise PatchWorkerError("patch working directory escapes source tree") from error
    if not working_directory.is_dir():
        raise PatchWorkerError("patch working directory is not a directory")
    return working_directory


def _apply_patches(request, source_path, patch_executable):
    applied = []
    for patch in request["patches"]:
        path = Path(patch["path"])
        if _digest(path) != patch["sha256"]:
            raise PatchWorkerError("patch checksum changed before application")
        working_directory = _working_directory(source_path, patch["working_dir"])
        for target in patch["targets"]:
            unresolved = working_directory / target
            if unresolved.is_symlink():
                raise PatchWorkerError("patch target must not be a symbolic link")
            resolved = unresolved.resolve(strict=True)
            try:
                resolved.relative_to(source_path)
            except ValueError as error:
                raise PatchWorkerError("patch target escapes source tree") from error
            if not resolved.is_file():
                raise PatchWorkerError("patch target is not a regular file")
        command = [
            str(patch_executable),
            "-s",
            "-f",
            "-r",
            "-",
            "-p",
            str(patch["level"]),
            "-i",
            str(path),
            "-d",
            str(working_directory),
        ]
        if patch["reverse"]:
            command.append("-R")
        result = subprocess.run(
            command,
            stdout=sys.stderr,
            stderr=sys.stderr,
            env={
                "HOME": request["state_directory"],
                "TMPDIR": request["state_directory"],
                "LC_ALL": "C",
            },
        )
        if result.returncode != 0:
            raise PatchWorkerError(f"patch command failed with status {result.returncode}")
        applied.append(patch["sha256"])
    return applied


def _response(ok, **kwargs):
    sys.stdout.write(json.dumps({"protocol_version": PROTOCOL_VERSION, "ok": ok, **kwargs}))
    sys.stdout.flush()


def main():
    phase = "validate"
    try:
        request, source_path, state_directory, patch_executable = _read_request()
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024, 512 * 1024 * 1024))
        phase = "sandbox"
        abi_version = _apply_sandbox(source_path, state_directory)
        phase = "apply"
        applied = _apply_patches(request, source_path, patch_executable)
        _response(
            True,
            sandbox={
                "backend": "landlock",
                "abi_version": abi_version,
                "filesystem_restricted": True,
                "tcp_restricted": True,
            },
            applied=applied,
        )
    except BaseException as error:
        _response(
            False, error={"phase": phase, "type": type(error).__name__, "message": str(error)}
        )


if __name__ == "__main__":
    main()
