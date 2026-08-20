# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Allowlisted privileged parent actions for prepared sandboxed installs."""

import os
import stat
from pathlib import Path
from typing import Any, Dict, List

import spack.error
import spack.package_prefs
from spack.installer.install_tree import (
    MAX_INSTALL_TREE_ENTRIES,
    InstallTreeError,
    install_tree_metadata,
)
from spack.spec import Spec

SET_PERMISSIONS = "set_permissions"
SUPPORTED_POST_ACTIONS = frozenset((SET_PERMISSIONS,))
MAX_POST_ACTIONS = 16


class PostActionError(spack.error.SpackError):
    """Raised when a trusted parent post-action cannot be applied safely."""


def _validate_permission_mode(mode: int) -> None:
    if mode & stat.S_ISUID and mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PostActionError("refusing writable SUID install-tree entry")
    if mode & stat.S_ISGID and mode & stat.S_IWOTH:
        raise PostActionError("refusing world-writable SGID install-tree entry")


def _set_permissions(spec: Spec, prefix: Path) -> Dict[str, Any]:
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


def run_post_actions(
    spec: Spec, prefix: Path, actions: List[str], expected_install_tree: Dict[str, Any]
) -> Dict[str, Any]:
    """Verify worker output, execute allowlisted actions, and identify the result."""
    validate_post_actions(actions)
    try:
        if install_tree_metadata(prefix) != expected_install_tree:
            raise PostActionError("install tree changed before parent post-actions")
        results = []
        for action in actions:
            if action == SET_PERMISSIONS:
                results.append(_set_permissions(spec, prefix))
        final_install_tree = install_tree_metadata(prefix)
    except (InstallTreeError, OSError, KeyError) as error:
        raise PostActionError(f"cannot apply parent post-actions: {error}") from error
    return {"actions": results, "install_tree": final_install_tree}
