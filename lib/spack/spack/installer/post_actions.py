# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Allowlisted privileged parent actions for prepared sandboxed installs."""

import os
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import spack.error
import spack.hooks.sbang
import spack.package_prefs
from spack.installer.install_tree import (
    MAX_INSTALL_TREE_ENTRIES,
    InstallTreeError,
    install_tree_metadata,
    verified_hardlink_inodes,
)
from spack.spec import Spec

SBANG = "sbang"
SET_PERMISSIONS = "set_permissions"
SUPPORTED_POST_ACTIONS = (SBANG, SET_PERMISSIONS)
MAX_POST_ACTIONS = 16
COPY_CHUNK_SIZE = 1024 * 1024


class PostActionError(spack.error.SpackError):
    """Raised when a trusted parent post-action cannot be applied safely."""


def _reject_external_hardlink(
    path: Path, prefix: Path, info: os.stat_result, internal_hardlinks: Dict[Tuple[int, int], int]
) -> None:
    if info.st_nlink != 1 and internal_hardlinks.get((info.st_dev, info.st_ino)) != info.st_nlink:
        raise PostActionError(
            "refusing external hardlink from install-tree entry: {0}".format(
                path.relative_to(prefix)
            )
        )


def _pwrite_all(descriptor: int, data: bytes, offset: int) -> None:
    while data:
        written = os.pwrite(descriptor, data, offset)
        if written <= 0:
            raise PostActionError("cannot rewrite install-tree entry")
        data = data[written:]
        offset += written


def _rewrite_shebang(
    descriptor: int, size: int, old_shebang: bytes, replacement: bytes, shebang: bytes
) -> None:
    prefix = shebang + replacement
    delta = len(prefix) - len(old_shebang)
    os.ftruncate(descriptor, size + delta)
    end = size
    while end > len(old_shebang):
        start = max(len(old_shebang), end - COPY_CHUNK_SIZE)
        chunk = os.pread(descriptor, end - start, start)
        if len(chunk) != end - start:
            raise PostActionError("install-tree entry changed during post-action")
        _pwrite_all(descriptor, chunk, start + delta)
        end = start
    _pwrite_all(descriptor, prefix, 0)


def validate_sbang_path(sbang_path: Path) -> bytes:
    """Return the encoded sbang line after validating its explicit store path."""
    shebang = ("#!/bin/sh {0}\n".format(sbang_path)).encode("utf-8")
    if len(shebang) - 1 > spack.hooks.sbang.system_shebang_limit:
        raise PostActionError("store root is too long for sbang shebang rewriting")
    return shebang


def _sbang(
    prefix: Path, sbang_path: Path, internal_hardlinks: Dict[Tuple[int, int], int]
) -> Dict[str, Any]:
    """Rewrite overlong executable shebangs to an explicit store sbang path."""
    if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "pread", "pwrite")):
        raise PostActionError("sbang rewriting requires no-follow positional file I/O")
    shebang = validate_sbang_path(sbang_path)
    patched = 0
    entries = 0
    for root, directories, files in os.walk(prefix, followlinks=False):
        directories.sort()
        files.sort()
        for name in files:
            entries += 1
            if entries > MAX_INSTALL_TREE_ENTRIES:
                raise PostActionError("install tree exceeds the post-action entry limit")
            path = Path(root) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                continue
            if not info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                continue
            _reject_external_hardlink(path, prefix, info, internal_hardlinks)
            flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino, opened.st_mode) != (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                ):
                    relative = path.relative_to(prefix)
                    raise PostActionError(
                        "install-tree entry changed during post-action: {0}".format(relative)
                    )
                old_shebang = os.pread(descriptor, 2, 0)
                if old_shebang != b"#!":
                    continue
                candidate = os.pread(descriptor, spack.hooks.sbang.spack_shebang_limit - 2, 2)
                newline = candidate.find(b"\n")
                old_shebang += candidate if newline < 0 else candidate[: newline + 1]
                if len(old_shebang) <= spack.hooks.sbang.system_shebang_limit:
                    continue
                if (
                    len(old_shebang) == spack.hooks.sbang.spack_shebang_limit
                    and old_shebang[-1:] != b"\n"
                ):
                    continue
                interpreter = spack.hooks.sbang.get_interpreter(old_shebang)
                if not interpreter:
                    continue
                if interpreter[-4:] == b"/lua" or interpreter[-7:] == b"/luajit":
                    replacement = b"--!" + old_shebang[2:]
                elif interpreter[-5:] == b"/node":
                    replacement = b"//!" + old_shebang[2:]
                elif interpreter[-4:] == b"/php":
                    replacement = b"<?php " + old_shebang + b" ?>"
                else:
                    replacement = old_shebang
                _rewrite_shebang(descriptor, opened.st_size, old_shebang, replacement, shebang)
                patched += 1
            finally:
                os.close(descriptor)
    return {"type": SBANG, "entries": entries, "patched": patched, "sbang_path": str(sbang_path)}


def _validate_permission_mode(mode: int) -> None:
    if mode & stat.S_ISUID and mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PostActionError("refusing writable SUID install-tree entry")
    if mode & stat.S_ISGID and mode & stat.S_IWOTH:
        raise PostActionError("refusing world-writable SGID install-tree entry")


def _set_permissions(
    spec: Spec, prefix: Path, internal_hardlinks: Dict[Tuple[int, int], int]
) -> Dict[str, Any]:
    """Apply configured modes and group without following symbolic links."""
    if not hasattr(os, "fchmod") or not hasattr(os, "O_NOFOLLOW"):
        raise PostActionError(
            "permission normalization requires no-follow descriptor chmod support"
        )
    file_mode = spack.package_prefs.get_package_permissions(spec)
    directory_mode = spack.package_prefs.get_package_dir_permissions(spec)
    group = spack.package_prefs.get_package_group(spec)
    if group:
        try:
            import grp
        except ImportError as error:
            raise PostActionError(
                "configured group permissions require POSIX group support"
            ) from error
        if not hasattr(os, "fchown"):
            raise PostActionError("configured group permissions require descriptor chown support")
        group_id = grp.getgrnam(group).gr_gid
    else:
        group_id = None
    entries = 0
    changed = 0

    paths = [prefix]
    for root, directories, files in os.walk(prefix, followlinks=False):
        directories.sort()
        files.sort()
        paths.extend(Path(root) / name for name in directories + files)

    for path in paths:
        entries += 1
        if entries > MAX_INSTALL_TREE_ENTRIES:
            raise PostActionError("install tree exceeds the post-action entry limit")
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISDIR(info.st_mode):
            mode = directory_mode
        elif stat.S_ISREG(info.st_mode):
            _reject_external_hardlink(path, prefix, info, internal_hardlinks)
            mode = file_mode
            if not info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                mode &= ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        else:
            relative = "." if path == prefix else path.relative_to(prefix)
            raise PostActionError(f"unsupported install-tree entry: {relative}")
        mode |= info.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
        _validate_permission_mode(mode)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
        if stat.S_ISDIR(info.st_mode):
            flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                relative = "." if path == prefix else path.relative_to(prefix)
                raise PostActionError(f"install-tree entry changed during post-action: {relative}")
            os.fchmod(descriptor, mode)
            if group_id is not None:
                os.fchown(descriptor, -1, group_id)
        finally:
            os.close(descriptor)
        changed += 1

    return {
        "type": SET_PERMISSIONS,
        "entries": changed,
        "file_mode": file_mode,
        "directory_mode": directory_mode,
        "group": group or None,
    }


def validate_post_actions(actions: List[str]) -> None:
    """Reject unknown, duplicate, malformed, or oversized action lists."""
    if (
        not isinstance(actions, list)
        or len(actions) > MAX_POST_ACTIONS
        or any(
            not isinstance(action, str) or action not in SUPPORTED_POST_ACTIONS
            for action in actions
        )
    ):
        raise PostActionError("invalid parent post-action list")
    if len(set(actions)) != len(actions):
        raise PostActionError("invalid parent post-action list")
    order = {action: index for index, action in enumerate(SUPPORTED_POST_ACTIONS)}
    if actions != sorted(actions, key=order.__getitem__):
        raise PostActionError("parent post-actions are not in canonical order")


def run_post_actions(
    spec: Spec,
    prefix: Path,
    actions: List[str],
    expected_install_tree: Dict[str, Any],
    sbang_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Verify worker output, execute allowlisted actions, and identify the result."""
    validate_post_actions(actions)
    try:
        if install_tree_metadata(prefix) != expected_install_tree:
            raise PostActionError("install tree changed before parent post-actions")
        internal_hardlinks = verified_hardlink_inodes(prefix)
        results = []
        for action in actions:
            if action == SBANG:
                if sbang_path is None:
                    raise PostActionError("sbang post-action requires an explicit store path")
                results.append(_sbang(prefix, sbang_path, internal_hardlinks))
            elif action == SET_PERMISSIONS:
                results.append(_set_permissions(spec, prefix, internal_hardlinks))
        final_install_tree = install_tree_metadata(prefix)
    except (InstallTreeError, OSError, KeyError) as error:
        raise PostActionError(f"cannot apply parent post-actions: {error}") from error
    return {"actions": results, "install_tree": final_install_tree}
