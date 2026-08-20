# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Create and verify immutable inputs for sandboxed package-repository use.

A repository snapshot is a private copy of the files that define a Spack package repository at a
particular point in time. It is not a Git or archival snapshot: this module walks a local repository,
copies its regular files into a separate tree, and computes a deterministic SHA-256 identity from
each relative path, executable mode, size, and file content. Version-control metadata, Python bytecode
caches, and bytecode files are omitted; symbolic links and special files are rejected because their
meaning can depend on state outside the copied tree.

Snapshots are needed because package recipes are executable Python and their contents contribute to
concretization and package hashes. Passing a live repository to a confined worker would allow another
process to change recipe input while concretization is in progress, making the result ambiguous even
if the worker itself cannot write to the repository. Importing those recipes again in the trusted
parent to verify the result would also execute the less-trusted code outside the sandbox.

The sandboxed-concretization protocol therefore creates snapshots before launching the worker and
places them outside the worker's writable state. The worker verifies each snapshot identity before it
enables the repository or imports recipe code, and the parent verifies the same identity after the
worker exits. This binds the concrete Spec and its worker-produced package hashes to an ordered,
content-addressed set of repository inputs without executing recipe code in the parent.
"""

import hashlib
import os
from pathlib import Path
import shutil
import stat
from typing import Iterator, Tuple


MAX_REPOSITORY_FILES = 100000
MAX_REPOSITORY_BYTES = 1024 * 1024 * 1024
_IGNORED_DIRECTORIES = {".git", "__pycache__"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


class RepositorySnapshotError(Exception):
    """Raised when a repository cannot be represented by a safe deterministic snapshot."""


def snapshot_root(base: Path, index: int, namespace: str, package_api: Tuple[int, int]) -> Path:
    """Return a repository root that preserves API-v2 import namespace semantics."""
    root = base / str(index)
    if package_api >= (2, 0):
        root = root / "spack_repo"
        for component in namespace.split("."):
            root = root / component
    return root


def _repository_files(root: Path) -> Iterator[Tuple[str, Path, int]]:
    pending = [("", root)]
    while pending:
        relative_directory, directory = pending.pop()
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name, reverse=True)
        for entry in ordered:
            relative = f"{relative_directory}/{entry.name}" if relative_directory else entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise RepositorySnapshotError(f"repository symlink is unsupported: {relative}")
            if stat.S_ISDIR(info.st_mode):
                if entry.name not in _IGNORED_DIRECTORIES:
                    pending.append((relative, Path(entry.path)))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise RepositorySnapshotError(f"repository special file is unsupported: {relative}")
            if Path(entry.name).suffix not in _IGNORED_SUFFIXES:
                yield relative, Path(entry.path), info.st_mode & 0o111


def repository_digest(root: Path) -> str:
    """Hash repository paths, executable bits, sizes, and file contents deterministically."""
    digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    for relative, path, executable_mode in sorted(_repository_files(root)):
        file_count += 1
        if file_count > MAX_REPOSITORY_FILES:
            raise RepositorySnapshotError("repository contains too many files")
        size = path.stat().st_size
        byte_count += size
        if byte_count > MAX_REPOSITORY_BYTES:
            raise RepositorySnapshotError("repository snapshot is too large")
        relative_bytes = relative.encode("utf-8")
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        digest.update(executable_mode.to_bytes(2, "big"))
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def create_repository_snapshot(source: Path, destination: Path) -> str:
    """Copy a repository without links or special files, then hash the completed snapshot."""
    destination.mkdir(parents=True, mode=0o700)
    for relative, path, executable_mode in _repository_files(source):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(0o600 | executable_mode)
    return repository_digest(destination)