# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Canonical metadata for verifying a sandboxed build's install tree."""

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Tuple

import spack.error

MAX_INSTALL_TREE_ENTRIES = 1_000_000


class InstallTreeError(spack.error.SpackError):
    """Raised when an install tree cannot be represented safely."""


def validate_install_tree_metadata(metadata: Any) -> Dict[str, Any]:
    """Validate and return a bounded canonical install-tree identity."""
    if not isinstance(metadata, dict) or set(metadata) != {"sha256", "entries", "bytes"}:
        raise InstallTreeError("invalid install-tree metadata")
    if (
        not isinstance(metadata["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"]) is None
        or not isinstance(metadata["entries"], int)
        or isinstance(metadata["entries"], bool)
        or metadata["entries"] < 1
        or metadata["entries"] > MAX_INSTALL_TREE_ENTRIES
        or not isinstance(metadata["bytes"], int)
        or isinstance(metadata["bytes"], bool)
        or metadata["bytes"] < 0
    ):
        raise InstallTreeError("invalid install-tree metadata")
    return metadata


def verified_hardlink_inodes(root: Path) -> Dict[Tuple[int, int], int]:
    """Return internal regular-file hardlinks and reject links outside the tree."""
    root = Path(os.path.abspath(root))
    try:
        root_info = root.lstat()
    except OSError as error:
        raise InstallTreeError(f"cannot inspect install-tree root: {error}") from error
    if not stat.S_ISDIR(root_info.st_mode):
        raise InstallTreeError("install-tree root must be a directory")
    links = {}
    entries = 1

    def visit(directory: Path, relative: PurePosixPath) -> None:
        nonlocal entries
        with os.scandir(directory) as children:
            for child_entry in sorted(children, key=lambda item: item.name):
                entries += 1
                if entries > MAX_INSTALL_TREE_ENTRIES:
                    raise InstallTreeError("install tree exceeds the entry limit")
                info = child_entry.stat(follow_symlinks=False)
                child = relative / child_entry.name
                if stat.S_ISDIR(info.st_mode):
                    visit(Path(child_entry.path), child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                    key = (info.st_dev, info.st_ino)
                    count, expected, first = links.get(key, (0, info.st_nlink, child))
                    if info.st_nlink != expected:
                        raise InstallTreeError(
                            "hardlink count changed while inspecting install tree: {0}".format(
                                child
                            )
                        )
                    links[key] = (count + 1, expected, first)

    try:
        visit(root, PurePosixPath())
    except OSError as error:
        raise InstallTreeError(f"cannot inspect install tree: {error}") from error

    verified = {}
    for key, (count, expected, first) in links.items():
        if count != expected:
            raise InstallTreeError(
                "regular file has hardlinks outside install tree: {0}".format(first)
            )
        verified[key] = expected
    return verified


def install_tree_metadata(root: Path) -> Dict[str, Any]:
    """Bind entry paths, types, modes, link targets, and file contents."""
    root = Path(os.path.abspath(root))
    try:
        root_info = root.lstat()
    except OSError as error:
        raise InstallTreeError(f"cannot inspect install-tree root: {error}") from error
    if not stat.S_ISDIR(root_info.st_mode):
        raise InstallTreeError("install-tree root must be a directory")

    identity = hashlib.sha256()
    entries = 0
    total_bytes = 0
    hardlinks = {}
    observed_hardlinks = {}
    internal_hardlinks = verified_hardlink_inodes(root)

    def add(record: Dict[str, Any]) -> None:
        """Add one canonical entry record to the tree identity."""
        nonlocal entries
        entries += 1
        if entries > MAX_INSTALL_TREE_ENTRIES:
            raise InstallTreeError("install tree exceeds the entry limit")
        identity.update(json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        identity.update(b"\n")

    def visit(directory: Path, relative: PurePosixPath) -> None:
        """Visit one directory without following symbolic links."""
        nonlocal total_bytes
        with os.scandir(directory) as children:
            for child_entry in sorted(children, key=lambda item: item.name):
                path = Path(child_entry.path)
                child = relative / child_entry.name
                info = child_entry.stat(follow_symlinks=False)
                mode = stat.S_IMODE(info.st_mode)
                if stat.S_ISDIR(info.st_mode):
                    add({"path": str(child), "type": "directory", "mode": mode})
                    visit(path, child)
                    continue
                if stat.S_ISLNK(info.st_mode):
                    add({"path": str(child), "type": "symlink", "target": os.readlink(path)})
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise InstallTreeError(f"unsupported install-tree entry: {child}")
                key = (info.st_dev, info.st_ino)
                if info.st_nlink > 1:
                    expected_links = internal_hardlinks.get(key)
                    if expected_links != info.st_nlink:
                        raise InstallTreeError(
                            f"hardlink count changed while inspecting install tree: {child}"
                        )
                    observed_hardlinks[key] = observed_hardlinks.get(key, 0) + 1
                    first = hardlinks.get(key)
                    if first is not None:
                        total_bytes += info.st_size
                        add({"path": str(child), "type": "hardlink", "target": first})
                        continue
                    hardlinks[key] = str(child)
                content = hashlib.sha256()
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                with os.fdopen(os.open(path, flags), "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    expected = (
                        info.st_dev,
                        info.st_ino,
                        info.st_mode,
                        info.st_nlink,
                        info.st_size,
                        info.st_mtime_ns,
                    )
                    observed = (
                        opened.st_dev,
                        opened.st_ino,
                        opened.st_mode,
                        opened.st_nlink,
                        opened.st_size,
                        opened.st_mtime_ns,
                    )
                    if observed != expected:
                        raise InstallTreeError(
                            f"install-tree entry changed while hashing: {child}"
                        )
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        content.update(chunk)
                    final = os.fstat(stream.fileno())
                    if (
                        final.st_dev,
                        final.st_ino,
                        final.st_mode,
                        final.st_nlink,
                        final.st_size,
                        final.st_mtime_ns,
                    ) != expected:
                        raise InstallTreeError(
                            f"install-tree entry changed while hashing: {child}"
                        )
                total_bytes += info.st_size
                add(
                    {
                        "path": str(child),
                        "type": "file",
                        "mode": mode,
                        "size": info.st_size,
                        "sha256": content.hexdigest(),
                    }
                )

    add({"path": ".", "type": "directory", "mode": stat.S_IMODE(root_info.st_mode)})
    try:
        visit(root, PurePosixPath())
        if observed_hardlinks != internal_hardlinks:
            raise InstallTreeError("hardlink topology changed while inspecting install tree")
    except OSError as error:
        raise InstallTreeError(f"cannot inspect install tree: {error}") from error
    return {"sha256": identity.hexdigest(), "entries": entries, "bytes": total_bytes}
