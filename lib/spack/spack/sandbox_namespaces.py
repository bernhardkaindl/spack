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
import enum
import errno
import json
import os
import platform
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List, NamedTuple, NoReturn, Optional, Tuple

from spack.sandbox import Sandbox, SandboxError

if TYPE_CHECKING:
    from spack.sandbox import LandlockSandbox

# Linux namespace and mount flags.
CLONE_NEWUSER = 0x10000000
CLONE_NEWNS = 0x00020000
MS_BIND = 0x00001000
MS_PRIVATE = 0x00040000
MS_REC = 0x00004000
LINUX_CAPABILITY_VERSION_3 = 0x20080522


class NamespaceCapability(NamedTuple):
    """Result of probing unprivileged user and mount namespaces."""

    available: bool
    operation: Optional[str]
    reason: Optional[str]


class NamespaceSandboxBackend(enum.Enum):
    """Backend selected after the namespace capability probe."""

    NAMESPACE = "namespace"
    LANDLOCK = "landlock"
    UNCONSTRAINED = "unconstrained"


class NamespaceSandboxDecision(NamedTuple):
    """Namespace preference and its constrained fallback decision."""

    backend: NamespaceSandboxBackend
    capability: NamespaceCapability

    @property
    def is_fallback(self) -> bool:
        return self.backend is not NamespaceSandboxBackend.NAMESPACE

    @property
    def is_constrained(self) -> bool:
        return self.backend is not NamespaceSandboxBackend.UNCONSTRAINED


class NamespaceSetupError(OSError):
    """Failure of a specific namespace setup operation."""

    def __init__(self, error_number: int, operation: str, reason: str) -> None:
        super().__init__(error_number, f"{operation}: {reason}")
        self.operation = operation
        self.reason = reason


class NamespaceMount(NamedTuple):
    """One validated source-to-target mount in a namespace plan."""

    source: str
    target: str


class NamespaceMountPlan(NamedTuple):
    """Immutable mount plan validated before namespace mutation."""

    stage_path: str
    mounts: Tuple[NamespaceMount, ...]


class _CapabilityHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapabilityData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def _check_syscall(result: int, name: str) -> int:
    """Raise OSError if a libc syscall returned a negative value."""
    if result < 0:
        err = ctypes.get_errno()
        raise NamespaceSetupError(err, name, os.strerror(err))
    return result


# Process-local set of PIDs that have already entered the user/mount namespace.
# Makes _enter_user_mount_namespace idempotent within a single process.
_namespace_entered_pids: set = set()

# The installer probes before launching build workers. POSIX workers inherit
# this result, avoiding a second fork after their logging thread has started.
_namespace_probe_result: Optional[NamespaceCapability] = None


def _raise_namespace_setup_error(operation: str, error: OSError) -> NoReturn:
    reason = error.strerror or str(error)
    raise NamespaceSetupError(error.errno or errno.EIO, operation, reason) from error


def _mount_plan_error(operation: str, reason: str, error_number: int = errno.EINVAL):
    raise NamespaceSetupError(error_number, operation, reason)


def build_namespace_mount_plan(paths: Iterable[str], stage_path: str) -> NamespaceMountPlan:
    """Validate and deterministically plan empty-directory bind mounts.

    Missing targets retain the narrow masking helper's no-op behavior. Existing
    targets must be directories. Canonical targets are sorted before assigning
    stage-owned sources, and duplicate or ancestor/descendant targets are
    rejected before entering a namespace or creating a source directory.
    """
    resolved_stage = os.path.realpath(os.path.abspath(stage_path))
    if os.path.exists(resolved_stage) and not os.path.isdir(resolved_stage):
        _mount_plan_error(
            "validate mount plan stage",
            f"stage path is not a directory: {stage_path}",
            errno.ENOTDIR,
        )

    targets = []
    for path in paths:
        if not os.path.exists(path):
            continue
        if not os.path.isdir(path):
            _mount_plan_error(
                "validate mount plan target",
                f"mount target is not a directory: {path}",
                errno.ENOTDIR,
            )
        targets.append(os.path.realpath(os.path.abspath(path)))

    sorted_targets = sorted(targets)
    for index, target in enumerate(sorted_targets):
        if index and target == sorted_targets[index - 1]:
            _mount_plan_error("validate mount plan targets", f"duplicate mount target: {target}")
        if (
            index
            and os.path.commonpath((sorted_targets[index - 1], target))
            == sorted_targets[index - 1]
        ):
            _mount_plan_error(
                "validate mount plan targets",
                f"conflicting mount targets: {sorted_targets[index - 1]} and {target}",
            )

    mount_root = os.path.join(resolved_stage, "spack-empty-host-dirs")
    mounts = tuple(
        NamespaceMount(os.path.join(mount_root, str(index)), target)
        for index, target in enumerate(sorted_targets)
    )
    return NamespaceMountPlan(resolved_stage, mounts)


def _apply_namespace_mount_plan(plan: NamespaceMountPlan, libc: ctypes.CDLL) -> bool:
    """Create and apply a previously validated namespace mount plan."""
    if not plan.mounts:
        return True

    os.makedirs(os.path.dirname(plan.mounts[0].source), exist_ok=True)
    for mount in plan.mounts:
        try:
            os.mkdir(mount.source)
        except FileExistsError:
            if not os.path.isdir(mount.source):
                _mount_plan_error(
                    "apply mount plan source",
                    f"mount source is not a directory: {mount.source}",
                    errno.ENOTDIR,
                )
        if not os.path.isdir(mount.target):
            _mount_plan_error(
                "apply mount plan target",
                f"mount target disappeared: {mount.target}",
                errno.ENOENT,
            )
        _check_syscall(
            libc.mount(
                os.fsencode(mount.source),
                os.fsencode(mount.target),
                None,
                ctypes.c_ulong(MS_BIND),
                None,
            ),
            "mount(MS_BIND)",
        )
    return True


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
            _raise_namespace_setup_error("write /proc/self/setgroups", error)
    try:
        with open("/proc/self/uid_map", "w", encoding="ascii") as stream:
            stream.write("{0} {0} 1".format(user_id))
    except OSError as error:
        _raise_namespace_setup_error("write /proc/self/uid_map", error)
    try:
        with open("/proc/self/gid_map", "w", encoding="ascii") as stream:
            stream.write("{0} {0} 1".format(group_id))
    except OSError as error:
        _raise_namespace_setup_error("write /proc/self/gid_map", error)
    _check_syscall(
        libc.mount(None, b"/", None, ctypes.c_ulong(MS_REC | MS_PRIVATE), None),
        "mount(MS_PRIVATE)",
    )
    _namespace_entered_pids.add(my_pid)


def _drop_namespace_capabilities(libc) -> None:
    """Drop all capabilities gained by entering the private user namespace.

    Namespace setup temporarily needs mount authority. Once trusted code has
    prepared the mount tree, no later recipe or build tool may create mounts in
    this namespace.
    """
    header = _CapabilityHeader(version=LINUX_CAPABILITY_VERSION_3, pid=0)
    capabilities = (_CapabilityData * 2)()
    _check_syscall(
        libc.capset(ctypes.byref(header), capabilities), "capset(drop namespace capabilities)"
    )


def _encode_capability(capability: NamespaceCapability) -> bytes:
    return json.dumps(capability._asdict(), sort_keys=True).encode("utf-8")


def _decode_capability(payload: bytes) -> NamespaceCapability:
    if not payload:
        return NamespaceCapability(False, "namespace setup", "probe child returned no result")
    try:
        result = json.loads(payload.decode("utf-8"))
        return NamespaceCapability(
            bool(result["available"]), result.get("operation"), result.get("reason")
        )
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        return NamespaceCapability(False, "decode probe result", str(error))


def _probe_namespace_capability(libc) -> NamespaceCapability:
    """Probe namespace setup in a disposable child process.

    Forks, runs ``_enter_user_mount_namespace`` in the child, and reports
    a structured result through a pipe. The parent is never affected.
    """

    fork_fn = getattr(os, "fork", None)
    if fork_fn is None:
        return NamespaceCapability(False, "fork", "os.fork is unavailable")
    probe_root = None
    try:
        probe_root = tempfile.mkdtemp(prefix="spack-namespace-probe-")
        bind_source = os.path.join(probe_root, "source")
        bind_target = os.path.join(probe_root, "target")
        os.mkdir(bind_source)
        os.mkdir(bind_target)
    except OSError as error:
        if probe_root is not None:
            shutil.rmtree(probe_root, ignore_errors=True)
        _raise_namespace_setup_error("prepare directory bind probe", error)
    try:
        read_fd, write_fd = os.pipe()
    except OSError as error:
        shutil.rmtree(probe_root, ignore_errors=True)
        _raise_namespace_setup_error("create namespace probe pipe", error)
    try:
        pid = fork_fn()
    except OSError as error:
        os.close(read_fd)
        os.close(write_fd)
        shutil.rmtree(probe_root, ignore_errors=True)
        _raise_namespace_setup_error("fork namespace probe", error)
    except BaseException:
        os.close(read_fd)
        os.close(write_fd)
        shutil.rmtree(probe_root, ignore_errors=True)
        raise
    if pid == 0:
        # Never unwind into the caller in the forked child, even when setup
        # raises an unexpected exception. EOF also reports probe failure.
        try:
            os.close(read_fd)
            try:
                _enter_user_mount_namespace(libc, _probe_child=True)
                _check_syscall(
                    libc.mount(
                        os.fsencode(bind_source),
                        os.fsencode(bind_target),
                        None,
                        ctypes.c_ulong(MS_BIND),
                        None,
                    ),
                    "mount(MS_BIND probe)",
                )
                _drop_namespace_capabilities(libc)
            except NamespaceSetupError as error:
                result = NamespaceCapability(False, error.operation, error.reason)
            except OSError as error:
                result = NamespaceCapability(False, "namespace setup", str(error))
            except BaseException as error:
                result = NamespaceCapability(
                    False, "namespace setup", f"{type(error).__name__}: {error}"
                )
            else:
                result = NamespaceCapability(True, None, None)
            os.write(write_fd, _encode_capability(result))
        finally:
            os._exit(0)

    os.close(write_fd)
    try:
        chunks = []
        try:
            while True:
                chunk = os.read(read_fd, 4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except OSError as error:
            _raise_namespace_setup_error("read namespace probe result", error)
        return _decode_capability(b"".join(chunks))
    finally:
        os.close(read_fd)
        try:
            os.waitpid(pid, 0)
        finally:
            shutil.rmtree(probe_root, ignore_errors=True)


def namespace_sandbox_capability(libc: Optional[ctypes.CDLL] = None) -> NamespaceCapability:
    """Return structured availability for unprivileged user and mount namespaces.

    Completed default-libc probes are cached. Parent-side operational failures
    remain retryable until a worker explicitly freezes its pre-thread result.
    """
    global _namespace_probe_result

    if platform.system() != "Linux":
        return NamespaceCapability(False, "platform", "Linux is required")
    use_cache = libc is None
    if use_cache and _namespace_probe_result is not None:
        return _namespace_probe_result
    try:
        if libc is None:
            try:
                libc = ctypes.CDLL(None, use_errno=True)
            except OSError as error:
                _raise_namespace_setup_error("load libc", error)
        result = _probe_namespace_capability(libc)
    except NamespaceSetupError as error:
        return NamespaceCapability(False, error.operation, error.reason)
    except OSError as error:
        return NamespaceCapability(False, "run namespace probe", str(error))
    if use_cache:
        _namespace_probe_result = result
    return result


def namespace_sandbox_available(libc: Optional[ctypes.CDLL] = None) -> bool:
    """Return whether unprivileged user and mount namespaces can be used.

    Returns ``False`` off Linux or when the probe fails. Returns ``True`` when
    such a namespace can be created.
    """
    return namespace_sandbox_capability(libc).available


def namespace_sandbox_decision() -> NamespaceSandboxDecision:
    """Prefer namespaces and otherwise select the constrained Landlock fallback."""
    capability = namespace_sandbox_capability()
    backend = (
        NamespaceSandboxBackend.NAMESPACE
        if capability.available
        else NamespaceSandboxBackend.LANDLOCK
    )
    return NamespaceSandboxDecision(backend, capability)


def freeze_namespace_sandbox_capability() -> NamespaceCapability:
    """Freeze the worker-local capability result before any worker thread starts.

    This is side-effect-free: it must not enter a namespace or create a mount.
    Namespace entry remains adjacent to trusted mount preparation and Landlock
    application, after recipe-controlled setup has completed.
    """
    global _namespace_probe_result

    capability = namespace_sandbox_capability()
    if _namespace_probe_result is None:
        _namespace_probe_result = capability
    return capability


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
    capability = namespace_sandbox_capability(libc)
    if cache_unavailable and libc is None:
        freeze_namespace_sandbox_capability()
    if not capability.available:
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
    plan = build_namespace_mount_plan(path_list, stage_path)
    if not namespace_ready and not prepare_empty_directory_masking(path_list, libc):
        return False
    if libc is None:
        libc = ctypes.CDLL(None, use_errno=True)
    return _apply_namespace_mount_plan(plan, libc)


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
        self._mount_authority_dropped = False
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
        plan = build_namespace_mount_plan(hidden_list, stage_path)
        if not prepare_empty_directory_masking(hidden_list, self.libc):
            return False
        libc = self.libc or ctypes.CDLL(None, use_errno=True)
        self._namespace_ready = _apply_namespace_mount_plan(plan, libc)
        return self._namespace_ready

    def drop_mount_authority(self) -> bool:
        """Irreversibly drop namespace capabilities after trusted mount setup.

        The caller must prepare all namespace mounts first. Returns ``False``
        if the namespace is inactive and otherwise drops capability state once.
        """
        if not self._namespace_ready:
            return False
        if self._mount_authority_dropped:
            return True
        libc = self.libc
        if libc is None:
            libc = ctypes.CDLL(None, use_errno=True)
        _drop_namespace_capabilities(libc)
        self._mount_authority_dropped = True
        return True

    def bind_mount(self, source: str, target: Optional[str] = None) -> bool:
        """Bind-mount *source* at *target* inside the namespace.

        *target* defaults to *source*. Returns ``True`` if the mount was
        performed, ``False`` if the namespace is not active.
        """
        if not self._namespace_ready:
            return False
        if self._mount_authority_dropped:
            raise SandboxError("Namespace mount authority was already dropped")
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
