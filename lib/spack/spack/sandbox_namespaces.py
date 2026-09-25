# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""
Linux user and mount namespace sandbox backend.

Provides a capability probe for unprivileged namespaces, helpers to hide host
directories by mounting empty stage-owned directories over them, and a sandbox
backend class that combines the namespace with Landlock. The namespace backend
is the preferred Linux confinement backend when available; Landlock-only is the
fallback.
"""

import ctypes
import errno
import os
import platform
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List, Optional

from spack.sandbox import Sandbox, SandboxError

if TYPE_CHECKING:
    from spack.sandbox import LandlockSandbox

# Linux namespace and mount flags.
CLONE_NEWUSER = 0x10000000
CLONE_NEWNS = 0x00020000
MS_BIND = 0x00001000
MS_PRIVATE = 0x00040000
MS_REC = 0x00004000


def _check_syscall(result: int, name: str) -> int:
    """Raise OSError if a libc syscall returned a negative value."""
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{name}: {os.strerror(err)}")
    return result


# Process-local set of PIDs that have already entered the user/mount namespace.
# Makes _enter_user_mount_namespace idempotent within a single process.
_namespace_entered_pids: set = set()

# The installer probes before launching build workers. POSIX workers inherit
# this result, avoiding a second fork after their logging thread has started.
_namespace_probe_result: Optional[bool] = None


def _enter_user_mount_namespace(libc, _probe_child: bool = False) -> None:
    """Enter a private user and mount namespace and contain mount propagation.

    Creates ``CLONE_NEWUSER | CLONE_NEWNS``, writes the invoking user's UID
    and GID mapping, disables setgroups, and makes the root mount private
    (``MS_REC | MS_PRIVATE``) so mounts do not propagate back to the host.

    Idempotent within a single process: subsequent calls from the same PID
    are no-ops. A forked child gets its own copy of the guard.
    """

    my_pid = os.getpid()
    if not _probe_child and my_pid in _namespace_entered_pids:
        return

    user_id = os.getuid()
    group_id = os.getgid()
    _check_syscall(
        libc.unshare(ctypes.c_int(CLONE_NEWUSER | CLONE_NEWNS)),
        "unshare(CLONE_NEWUSER | CLONE_NEWNS)",
    )
    try:
        with open("/proc/self/setgroups", "w", encoding="ascii") as stream:
            stream.write("deny")
    except OSError as error:
        if error.errno != errno.ENOENT:
            raise
    with open("/proc/self/uid_map", "w", encoding="ascii") as stream:
        stream.write("{0} {0} 1".format(user_id))
    with open("/proc/self/gid_map", "w", encoding="ascii") as stream:
        stream.write("{0} {0} 1".format(group_id))
    _check_syscall(
        libc.mount(None, b"/", None, ctypes.c_ulong(MS_REC | MS_PRIVATE), None),
        "mount(MS_PRIVATE)",
    )
    _namespace_entered_pids.add(my_pid)


def _namespace_available(libc) -> bool:
    """Probe namespace setup in a disposable child process.

    Forks, runs ``_enter_user_mount_namespace`` in the child, and reports
    the result through a pipe. The parent is never affected.
    """

    fork_fn = getattr(os, "fork", None)
    if fork_fn is None:
        return False
    read_fd, write_fd = os.pipe()
    try:
        pid = fork_fn()
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        raise
    if pid == 0:
        # Never unwind into the caller in the forked child, even when setup
        # raises an unexpected exception. EOF also reports probe failure.
        try:
            os.close(read_fd)
            try:
                _enter_user_mount_namespace(libc, _probe_child=True)
            except OSError:
                result = b"0"
            else:
                result = b"1"
            os.write(write_fd, result)
        finally:
            os._exit(0)

    os.close(write_fd)
    try:
        result = os.read(read_fd, 1)
        return result == b"1"
    finally:
        os.close(read_fd)
        os.waitpid(pid, 0)


def namespace_sandbox_available(libc: Optional[ctypes.CDLL] = None) -> bool:
    """Return whether unprivileged user and mount namespaces can be used.

    Returns ``False`` off Linux or when the probe fails. Returns ``True`` when
    such a namespace can be created.
    """
    global _namespace_probe_result

    if platform.system() != "Linux":
        return False
    use_cache = libc is None
    if use_cache and _namespace_probe_result is not None:
        return _namespace_probe_result
    try:
        if libc is None:
            libc = ctypes.CDLL(None, use_errno=True)
        result = _namespace_available(libc)
    except OSError:
        return False
    if use_cache:
        _namespace_probe_result = result
    return result


def prepare_empty_directory_masking(
    paths: Iterable[str], libc: Optional[ctypes.CDLL] = None, cache_unavailable: bool = False
) -> bool:
    """Enter a private user and mount namespace for directory masking.

    Returns ``True`` once the namespace is active, ``False`` when
    unprivileged namespaces are not permitted by the kernel.

    Even when *paths* is empty, success means the calling process has entered
    the namespace. Setup errors after the probe propagate to the caller. When
    *cache_unavailable* is true, freeze an unavailable result so later backend
    selection in the same worker cannot start another probe after threads exist.
    """
    global _namespace_probe_result

    if platform.system() != "Linux":
        return False
    if os.getpid() in _namespace_entered_pids:
        return True
    available = namespace_sandbox_available(libc)
    if cache_unavailable and libc is None and _namespace_probe_result is None:
        _namespace_probe_result = available
    if not available:
        return False
    if libc is None:
        libc = ctypes.CDLL(None, use_errno=True)
    _enter_user_mount_namespace(libc)
    return True


def hide_directories_as_empty(
    paths: Iterable[str],
    stage_path: str,
    namespace_ready: bool = False,
    libc: Optional[ctypes.CDLL] = None,
) -> bool:
    """Mask each existing host directory with an empty stage-owned directory.

    Each existing directory in *paths* is covered by a bind-mount of an empty
    directory created under ``stage_path/spack-empty-host-dirs/<index>``.
    Sandboxed tools see an empty directory (``ENOENT`` for missing entries)
    instead of the host tree.

    Returns ``True`` on success (including an empty path list), ``False`` when
    the namespace backend is unavailable. Mount failures raise ``OSError``;
    callers must not continue with a partially prepared mount tree.
    """

    path_list = list(paths)
    if not path_list:
        return True
    if not namespace_ready and not prepare_empty_directory_masking(path_list, libc):
        return False
    if libc is None:
        libc = ctypes.CDLL(None, use_errno=True)
    empty_root = os.path.join(stage_path, "spack-empty-host-dirs")
    os.makedirs(empty_root, exist_ok=True)
    for index, path in enumerate(path_list):
        if not os.path.isdir(path):
            continue
        empty_dir = os.path.join(empty_root, str(index))
        os.mkdir(empty_dir)
        _check_syscall(
            libc.mount(
                os.fsencode(empty_dir), os.fsencode(path), None, ctypes.c_ulong(MS_BIND), None
            ),
            "mount(MS_BIND)",
        )
    return True


class NamespaceSandbox(Sandbox):
    """Sandbox backend that combines Linux user/mount namespaces with Landlock.

    On Linux, when unprivileged user and mount namespaces are available, this
    backend is preferred over the Landlock-only backend because it can hide host
    filesystem content via empty mounts and bind mounts, rather than only
    denying access through Landlock rules.

    The class delegates ``allow_read`` / ``allow_write`` to an internal
    ``LandlockSandbox`` so that Landlock's deny rules operate on the
    namespace-restricted mount tree.
    """

    def __init__(
        self, libc: Optional[ctypes.CDLL] = None, landlock: Optional["LandlockSandbox"] = None
    ) -> None:
        self.libc = libc
        self._namespace_ready = False
        self._hidden_dirs: List[str] = []
        self._stage_path: Optional[str] = None
        self._granted_dirs: List[Path] = []
        # Create the internal Landlock sandbox lazily unless selection already
        # preflighted and supplied it.
        self._landlock = landlock

    @property
    def namespace_available(self) -> bool:
        """Return whether the kernel permits unprivileged user+mount namespaces."""
        return namespace_sandbox_available(self.libc)

    @property
    def namespace_ready(self) -> bool:
        """Return whether the private namespace and mount tree are active."""
        return self._namespace_ready

    def _ensure_landlock(self) -> "LandlockSandbox":
        """Create the internal LandlockSandbox on first use."""
        from spack.sandbox import LandlockSandbox

        if self._landlock is None:
            try:
                self._landlock = LandlockSandbox(self.libc)
            except OSError as error:
                raise SandboxError(f"Landlock is unavailable: {error}") from error
        return self._landlock  # type: ignore[return-value]

    def prepare_mount_tree(self, hidden_dirs: Iterable[str], stage_path: str) -> bool:
        """Enter the private namespace and mask *hidden_dirs* as empty.

        Called before Landlock rules are built so that Landlock operates on the
        namespace-restricted mount tree. Returns ``True`` if the namespace was
        entered or was already active, ``False`` if not available.
        """
        hidden_list = list(hidden_dirs)
        if self._namespace_ready:
            return True
        self._hidden_dirs = hidden_list
        self._stage_path = stage_path
        if not prepare_empty_directory_masking(hidden_list, self.libc):
            return False
        self._namespace_ready = hide_directories_as_empty(
            hidden_list, stage_path, namespace_ready=True, libc=self.libc
        )
        return self._namespace_ready

    def bind_mount(self, source: str, target: Optional[str] = None) -> bool:
        """Bind-mount *source* at *target* inside the namespace.

        *target* defaults to *source*. Returns ``True`` if the mount was
        performed, ``False`` if the namespace is not active.
        """
        if not self._namespace_ready:
            return False
        if target is None:
            target = source
        resolved_source = os.path.realpath(source)
        if not os.path.exists(resolved_source):
            return False
        libc = self.libc
        if libc is None:
            libc = ctypes.CDLL(None, use_errno=True)
        _check_syscall(
            libc.mount(
                os.fsencode(resolved_source),
                os.fsencode(target),
                None,
                ctypes.c_ulong(MS_BIND | (MS_REC if os.path.isdir(resolved_source) else 0)),
                None,
            ),
            "mount(MS_BIND)",
        )
        self._granted_dirs.append(Path(resolved_source))
        return True

    def _allow_read(self, original: Path, resolved: Path) -> None:
        self._ensure_landlock()._allow_read(original, resolved)  # type: ignore[attr-defined]

    def _allow_write(self, original: Path, resolved: Path) -> None:
        self._ensure_landlock()._allow_write(original, resolved)  # type: ignore[attr-defined]

    def apply(self, block_network: bool = False) -> None:
        """Apply Landlock on top of the namespace-restricted mount tree.

        If no rules were granted, an empty deny-by-default Landlock ruleset is
        still applied so the namespace is not left unconstrained.
        """
        self._ensure_landlock().apply(block_network=block_network)

    def cleanup(self) -> None:
        """No-op for the namespace case.

        The namespace, its mount tree, and the Landlock ruleset all die with the
        process.
        """
