# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""
This module implements an unprivileged sandbox for build environments.

It enforces path-based filesystem whitelisting and optional network isolation,
dynamically adapting to the host kernel's supported Landlock ABI version.

By design, to support standard build system behaviors like `try_compile` tests,
read access implicitly includes execution rights. IOCTLs and IPC mechanisms are
left unrestricted to ensure compatibility with compilers, terminal output, and
build jobservers.
"""

import contextlib
import ctypes
import enum
import errno
import os
import platform
import resource
import stat
import sys
import warnings
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Iterable, Union

# os.O_PATH is only defined on linux. Appease mypy with our own O_PATH.
if sys.platform == "linux":
    O_PATH = os.O_PATH
else:
    O_PATH = 0

import spack.config
import spack.error

# Linux landlock syscalls
SYSCALL_LANDLOCK_CREATE_RULESET = 444
SYSCALL_LANDLOCK_ADD_RULE = 445
SYSCALL_LANDLOCK_RESTRICT_SELF = 446

PR_SET_NO_NEW_PRIVS = 38
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1
LANDLOCK_ACCESS_NET_BIND_TCP = 1 << 0
LANDLOCK_ACCESS_NET_CONNECT_TCP = 1 << 1
LANDLOCK_RESTRICT_SELF_TSYNC = 1 << 3
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_USER_NOTIF = 0x7FC00000
SECCOMP_ADDFD_FLAG_SEND = 1 << 1
SYS_PIDFD_OPEN = 434
SYS_PIDFD_GETFD = 438
SECCOMP_IOCTL_NOTIF_ADDFD = 0x40182103

_SOCKET_SYSCALLS = (
    "accept",
    "accept4",
    "bind",
    "connect",
    "getpeername",
    "getsockname",
    "getsockopt",
    "listen",
    "recvmmsg",
    "recvfrom",
    "recvmsg",
    "sendmmsg",
    "sendmsg",
    "sendto",
    "setsockopt",
    "shutdown",
    "socket",
    "socketcall",
    "socketpair",
)

_NETWORK_WORKER_DENY_SYSCALLS = (
    "accept",
    "accept4",
    "bind",
    "listen",
    "sendmmsg",
    "sendmsg",
    "socketcall",
    "socketpair",
)

_PROCESS_EXEC_SYSCALLS = ("clone", "clone3", "fork", "vfork", "execve", "execveat")

_IPC_SYSCALLS = (
    "ipc",
    "semctl",
    "semget",
    "semop",
    "semtimedop",
    "shmat",
    "shmctl",
    "shmdt",
    "shmget",
    "msgctl",
    "msgget",
    "msgrcv",
    "msgsnd",
    "mq_open",
    "mq_unlink",
    "mq_timedsend",
    "mq_timedreceive",
    "mq_notify",
    "mq_getsetattr",
)


class FSAccess(enum.IntFlag):
    EXECUTE = 1 << 0
    WRITE_FILE = 1 << 1
    READ_FILE = 1 << 2
    READ_DIR = 1 << 3
    REMOVE_DIR = 1 << 4
    REMOVE_FILE = 1 << 5
    MAKE_CHAR = 1 << 6
    MAKE_DIR = 1 << 7
    MAKE_REG = 1 << 8
    MAKE_SOCK = 1 << 9
    MAKE_FIFO = 1 << 10
    MAKE_BLOCK = 1 << 11
    MAKE_SYM = 1 << 12
    REFER = 1 << 13  # ABI v2
    TRUNCATE = 1 << 14  # ABI v3


def _check_syscall(result: int, name: str) -> int:
    """Raise OSError if a libc syscall returned a negative value.

    Mirrors what Python's stdlib does for syscall-backed os.* functions.
    """
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{name}: {os.strerror(err)}")
    return result


def pidfd_open(pid: int, libc=None) -> int:
    """Open a Linux pidfd for ``pid`` using the Python 3.6-compatible syscall API."""
    if platform.system() != "Linux":
        raise OSError(errno.ENOSYS, "pidfd_open is only available on Linux")
    libc = libc if libc is not None else ctypes.CDLL(None, use_errno=True)
    return _check_syscall(
        libc.syscall(ctypes.c_long(SYS_PIDFD_OPEN), ctypes.c_int(pid), ctypes.c_uint(0)),
        "pidfd_open",
    )


def pidfd_getfd(pidfd: int, target_fd: int, libc=None) -> int:
    """Duplicate ``target_fd`` from the process represented by ``pidfd``."""
    if platform.system() != "Linux":
        raise OSError(errno.ENOSYS, "pidfd_getfd is only available on Linux")
    libc = libc if libc is not None else ctypes.CDLL(None, use_errno=True)
    return _check_syscall(
        libc.syscall(
            ctypes.c_long(SYS_PIDFD_GETFD),
            ctypes.c_int(pidfd),
            ctypes.c_int(target_fd),
            ctypes.c_uint(0),
        ),
        "pidfd_getfd",
    )


class RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class SeccompData(ctypes.Structure):
    """Register data supplied with a seccomp notification."""

    _fields_ = [
        ("nr", ctypes.c_int),
        ("arch", ctypes.c_uint32),
        ("instruction_pointer", ctypes.c_uint64),
        ("args", ctypes.c_uint64 * 6),
    ]


class SeccompNotification(ctypes.Structure):
    """Notification describing a syscall blocked in a target thread."""

    _fields_ = [
        ("id", ctypes.c_uint64),
        ("pid", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("data", SeccompData),
    ]


class SeccompNotificationResponse(ctypes.Structure):
    """Spoofed syscall result returned to a blocked target thread."""

    _fields_ = [
        ("id", ctypes.c_uint64),
        ("val", ctypes.c_int64),
        ("error", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
    ]


class SeccompNotificationAddfd(ctypes.Structure):
    """Descriptor injected into a target blocked on a notification."""

    _fields_ = [
        ("id", ctypes.c_uint64),
        ("flags", ctypes.c_uint32),
        ("srcfd", ctypes.c_uint32),
        ("newfd", ctypes.c_uint32),
        ("newfd_flags", ctypes.c_uint32),
    ]


class Sandbox(ABC):
    """Abstract base class for sandbox implementations."""

    def allow_read(self, path: Union[str, Path]):
        p = Path(path).absolute()
        resolved = p.resolve()
        if resolved.exists():
            self._allow_read(p, resolved)

    def allow_write(self, path: Union[str, Path]):
        p = Path(path).absolute()
        resolved = p.resolve()
        if resolved.exists():
            self._allow_write(p, resolved)

    @abstractmethod
    def _allow_read(self, original: Path, resolved: Path): ...

    @abstractmethod
    def _allow_write(self, original: Path, resolved: Path): ...

    @abstractmethod
    def apply(
        self,
        block_network: bool = False,
        block_process: bool = False,
        block_ipc: bool = False,
        allow_tcp_network_fallback: bool = True,
    ): ...


def _get_write_flags(abi_version: int) -> int:
    flags = (
        FSAccess.MAKE_BLOCK
        | FSAccess.MAKE_CHAR
        | FSAccess.MAKE_DIR
        | FSAccess.MAKE_FIFO
        | FSAccess.MAKE_REG
        | FSAccess.MAKE_SOCK
        | FSAccess.MAKE_SYM
        | FSAccess.REMOVE_DIR
        | FSAccess.REMOVE_FILE
        | FSAccess.WRITE_FILE
    )
    if abi_version >= 2:
        flags |= FSAccess.REFER
    if abi_version >= 3:
        flags |= FSAccess.TRUNCATE
    return flags


class LandlockSandbox(Sandbox):
    def __init__(self, libc=None):
        self.libc = libc if libc is not None else ctypes.CDLL(None, use_errno=True)
        self.abi_version = self._get_abi_version()
        self.path_rules: Dict[Path, int] = {}
        self.write_flags = _get_write_flags(self.abi_version)
        self.read_flags = FSAccess.EXECUTE | FSAccess.READ_FILE | FSAccess.READ_DIR
        self.dir_flags = (
            FSAccess.MAKE_BLOCK
            | FSAccess.MAKE_CHAR
            | FSAccess.MAKE_DIR
            | FSAccess.MAKE_FIFO
            | FSAccess.MAKE_REG
            | FSAccess.MAKE_SOCK
            | FSAccess.MAKE_SYM
            | FSAccess.READ_DIR
            | FSAccess.REFER
            | FSAccess.REMOVE_DIR
            | FSAccess.REMOVE_FILE
        )

    def _get_abi_version(self) -> int:
        res = self.libc.syscall(
            ctypes.c_long(SYSCALL_LANDLOCK_CREATE_RULESET),
            None,
            ctypes.c_size_t(0),
            ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION),
        )
        return _check_syscall(res, "landlock_create_ruleset(version)")

    def _allow_read(self, original: Path, resolved: Path):
        current_flags = self.path_rules.get(resolved, 0)
        self.path_rules[resolved] = current_flags | self.read_flags

    def _allow_write(self, original: Path, resolved: Path):
        current_flags = self.path_rules.get(resolved, 0)
        self.path_rules[resolved] = current_flags | self.write_flags | self.read_flags

    def _syscall_create_ruleset(self, handled_access_fs: int, handled_access_net: int) -> int:
        attr = RulesetAttr(
            handled_access_fs=handled_access_fs, handled_access_net=handled_access_net
        )
        return _check_syscall(
            self.libc.syscall(
                ctypes.c_long(SYSCALL_LANDLOCK_CREATE_RULESET),
                ctypes.byref(attr),
                ctypes.c_size_t(ctypes.sizeof(attr)),
                ctypes.c_uint32(0),
            ),
            "landlock_create_ruleset",
        )

    def _syscall_add_rule(self, ruleset_fd: int, allowed_access: int, path_fd: int) -> None:
        rule = PathBeneathAttr(allowed_access=allowed_access, parent_fd=path_fd)
        _check_syscall(
            self.libc.syscall(
                ctypes.c_long(SYSCALL_LANDLOCK_ADD_RULE),
                ctypes.c_int(ruleset_fd),
                ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH),
                ctypes.byref(rule),
                ctypes.c_uint32(0),
            ),
            "landlock_add_rule",
        )

    def _syscall_restrict_self(self, ruleset_fd: int, tsync_flag: int) -> None:
        _check_syscall(
            self.libc.syscall(
                ctypes.c_long(SYSCALL_LANDLOCK_RESTRICT_SELF),
                ctypes.c_int(ruleset_fd),
                ctypes.c_uint32(tsync_flag),
            ),
            "landlock_restrict_self",
        )

    def _prctl_no_new_privs(self) -> None:
        _check_syscall(
            self.libc.prctl(
                ctypes.c_int(PR_SET_NO_NEW_PRIVS),
                ctypes.c_ulong(1),
                ctypes.c_ulong(0),
                ctypes.c_ulong(0),
                ctypes.c_ulong(0),
            ),
            "prctl(PR_SET_NO_NEW_PRIVS)",
        )

    def _apply_landlock(self, net_flags: int) -> None:
        try:
            self._apply(net_flags)
        except OSError as e:
            raise SandboxError(f"Failed to apply build sandbox: {e}") from e

    def network_isolation_available(self, allow_tcp_network_fallback: bool = True) -> bool:
        """Return whether complete or explicitly allowed TCP-only isolation is available."""
        try:
            SeccompSandbox()
        except OSError:
            return allow_tcp_network_fallback and self.abi_version >= 4
        return True

    def apply(
        self,
        block_network: bool = False,
        block_process: bool = False,
        block_ipc: bool = False,
        allow_tcp_network_fallback: bool = True,
    ):
        """Apply filesystem confinement and optional internal seccomp restrictions."""
        if not (block_network or block_process or block_ipc):
            self._apply_landlock(0)
            return

        try:
            seccomp = SeccompSandbox()
        except OSError as error:
            if block_network and allow_tcp_network_fallback and self.abi_version >= 4:
                self._apply_landlock(
                    LANDLOCK_ACCESS_NET_CONNECT_TCP | LANDLOCK_ACCESS_NET_BIND_TCP
                )
                return
            raise SandboxError(f"Seccomp sandboxing is unavailable: {error}") from error

        # Landlock constrains the filesystem before seccomp irreversibly restricts the worker.
        self._apply_landlock(0)
        try:
            seccomp.apply(
                block_sockets=block_network, block_process=block_process, block_ipc=block_ipc
            )
        except OSError as error:
            if block_network and allow_tcp_network_fallback and self.abi_version >= 4:
                self._apply_landlock(
                    LANDLOCK_ACCESS_NET_CONNECT_TCP | LANDLOCK_ACCESS_NET_BIND_TCP
                )
                return
            raise SandboxError(f"Failed to apply seccomp sandbox: {error}") from error

    def _apply(self, net_flags: int) -> None:
        ruleset_fd = self._syscall_create_ruleset(self.write_flags | self.read_flags, net_flags)

        try:
            for path, flags in self.path_rules.items():
                try:
                    # use O_PATH to get an fd w/o needing permissions, and O_NOFOLLOW to avoid
                    # TOCTOU issues after we've called resolve() on the path.
                    fd = os.open(str(path), O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
                except OSError as e:
                    warnings.warn(f"Cannot allow sandbox access to {path} due to: {e}")
                    continue
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISDIR(st.st_mode):
                        # Strip directory-specific flags
                        flags &= ~self.dir_flags
                    self._syscall_add_rule(ruleset_fd, flags, fd)
                finally:
                    os.close(fd)

            # Lock down the current process with this ruleset
            self._prctl_no_new_privs()
            tsync_flag = LANDLOCK_RESTRICT_SELF_TSYNC if self.abi_version >= 8 else 0
            self._syscall_restrict_self(ruleset_fd, tsync_flag)
        finally:
            os.close(ruleset_fd)


class SeccompSandbox:
    """Deny dangerous syscall groups while preserving the Python runtime."""

    def __init__(self, libc=None):
        self.libc = libc if libc is not None else ctypes.CDLL(None, use_errno=True)
        self.libseccomp = ctypes.CDLL("libseccomp.so.2", use_errno=True)
        self.libseccomp.seccomp_init.argtypes = [ctypes.c_uint32]
        self.libseccomp.seccomp_init.restype = ctypes.c_void_p
        self.libseccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        self.libseccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
        self.libseccomp.seccomp_rule_add.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        self.libseccomp.seccomp_rule_add.restype = ctypes.c_int
        self.libseccomp.seccomp_load.argtypes = [ctypes.c_void_p]
        self.libseccomp.seccomp_load.restype = ctypes.c_int
        self.libseccomp.seccomp_notify_fd.argtypes = [ctypes.c_void_p]
        self.libseccomp.seccomp_notify_fd.restype = ctypes.c_int
        notification_pointer = ctypes.POINTER(SeccompNotification)
        response_pointer = ctypes.POINTER(SeccompNotificationResponse)
        self.libseccomp.seccomp_notify_alloc.argtypes = [
            ctypes.POINTER(notification_pointer),
            ctypes.POINTER(response_pointer),
        ]
        self.libseccomp.seccomp_notify_alloc.restype = ctypes.c_int
        self.libseccomp.seccomp_notify_free.argtypes = [notification_pointer, response_pointer]
        self.libseccomp.seccomp_notify_free.restype = None
        self.libseccomp.seccomp_notify_receive.argtypes = [ctypes.c_int, notification_pointer]
        self.libseccomp.seccomp_notify_receive.restype = ctypes.c_int
        self.libseccomp.seccomp_notify_id_valid.argtypes = [ctypes.c_int, ctypes.c_uint64]
        self.libseccomp.seccomp_notify_id_valid.restype = ctypes.c_int
        self.libseccomp.seccomp_notify_respond.argtypes = [ctypes.c_int, response_pointer]
        self.libseccomp.seccomp_notify_respond.restype = ctypes.c_int
        self.libseccomp.seccomp_release.argtypes = [ctypes.c_void_p]
        self.libseccomp.seccomp_release.restype = None

    def _prctl_no_new_privs(self) -> None:
        _check_syscall(
            self.libc.prctl(
                ctypes.c_int(PR_SET_NO_NEW_PRIVS),
                ctypes.c_ulong(1),
                ctypes.c_ulong(0),
                ctypes.c_ulong(0),
                ctypes.c_ulong(0),
            ),
            "prctl(PR_SET_NO_NEW_PRIVS)",
        )

    def _get_syscall_number(self, name: str) -> int:
        return self.libseccomp.seccomp_syscall_resolve_name(name.encode("ascii"))

    def _rule_add(
        self, context, syscall: int, action: int = SECCOMP_RET_ERRNO | errno.EPERM
    ) -> None:
        result = self.libseccomp.seccomp_rule_add(
            context, ctypes.c_uint32(action), ctypes.c_int(syscall), 0
        )
        if result < 0:
            raise OSError(-result, f"seccomp_rule_add({syscall}): {os.strerror(-result)}")

    def _load(self, context) -> None:
        result = self.libseccomp.seccomp_load(context)
        if result < 0:
            error_number = ctypes.get_errno() if result == -errno.ECANCELED else -result
            raise OSError(error_number, f"seccomp_load: {os.strerror(error_number)}")

    def _notify_fd(self, context) -> int:
        result = self.libseccomp.seccomp_notify_fd(context)
        if result < 0:
            raise OSError(-result, f"seccomp_notify_fd: {os.strerror(-result)}")
        return result

    def _notification_listener(self, syscall_names: Iterable[str]) -> int:
        self._prctl_no_new_privs()
        context = self.libseccomp.seccomp_init(ctypes.c_uint32(SECCOMP_RET_ALLOW))
        if not context:
            raise OSError(errno.ENOMEM, "seccomp_init failed")
        try:
            for name in syscall_names:
                syscall = self._get_syscall_number(name)
                if syscall < 0:
                    raise OSError(errno.ENOSYS, f"seccomp cannot resolve {name}")
                self._rule_add(context, syscall, SECCOMP_RET_USER_NOTIF)
            self._load(context)
            return self._notify_fd(context)
        finally:
            self.libseccomp.seccomp_release(context)

    def connect_listener(self) -> int:
        """Install a filter notifying on ``connect`` and return its listener descriptor."""
        return self._notification_listener(("connect",))

    def network_listener(self) -> int:
        """Install a filter notifying on TCP socket creation and connection."""
        return self._notification_listener(("socket", "connect"))

    def deny_network_bypass(self) -> None:
        """Deny socket operations that could bypass the supervised TCP path."""
        self._prctl_no_new_privs()
        context = self.libseccomp.seccomp_init(ctypes.c_uint32(SECCOMP_RET_ALLOW))
        if not context:
            raise OSError(errno.ENOMEM, "seccomp_init failed")
        try:
            for name in _NETWORK_WORKER_DENY_SYSCALLS:
                syscall = self._get_syscall_number(name)
                if syscall >= 0:
                    self._rule_add(context, syscall)
            self._load(context)
        finally:
            self.libseccomp.seccomp_release(context)

    def receive_notification(self, listener_fd: int) -> SeccompNotification:
        """Receive and copy the next notification from ``listener_fd``."""
        request = ctypes.POINTER(SeccompNotification)()
        response = ctypes.POINTER(SeccompNotificationResponse)()
        result = self.libseccomp.seccomp_notify_alloc(
            ctypes.byref(request), ctypes.byref(response)
        )
        if result < 0:
            raise OSError(-result, f"seccomp_notify_alloc: {os.strerror(-result)}")
        try:
            result = self.libseccomp.seccomp_notify_receive(listener_fd, request)
            if result < 0:
                raise OSError(-result, f"seccomp_notify_receive: {os.strerror(-result)}")
            return SeccompNotification.from_buffer_copy(request.contents)
        finally:
            self.libseccomp.seccomp_notify_free(request, response)

    def notification_is_valid(self, listener_fd: int, notification_id: int) -> bool:
        """Return whether a target is still blocked on a notification."""
        result = self.libseccomp.seccomp_notify_id_valid(listener_fd, notification_id)
        if result == 0:
            return True
        if result == -errno.ENOENT:
            return False
        raise OSError(-result, f"seccomp_notify_id_valid: {os.strerror(-result)}")

    def respond_to_notification(
        self, listener_fd: int, notification_id: int, value: int = 0, error: int = 0
    ) -> None:
        """Return a spoofed result without continuing the target syscall."""
        response = SeccompNotificationResponse(
            id=notification_id, val=value, error=-abs(error), flags=0
        )
        result = self.libseccomp.seccomp_notify_respond(listener_fd, ctypes.byref(response))
        if result < 0:
            raise OSError(-result, f"seccomp_notify_respond: {os.strerror(-result)}")

    def addfd_to_notification(
        self,
        listener_fd: int,
        notification_id: int,
        source_fd: int,
        flags: int = 0,
        newfd: int = 0,
        newfd_flags: int = 0,
    ) -> int:
        """Inject ``source_fd`` into a blocked target notification."""
        addfd = SeccompNotificationAddfd(
            id=notification_id, flags=flags, srcfd=source_fd, newfd=newfd, newfd_flags=newfd_flags
        )
        return _check_syscall(
            self.libc.ioctl(
                ctypes.c_int(listener_fd),
                ctypes.c_ulong(SECCOMP_IOCTL_NOTIF_ADDFD),
                ctypes.byref(addfd),
            ),
            "SECCOMP_IOCTL_NOTIF_ADDFD",
        )

    def apply(
        self, block_sockets: bool = True, block_process: bool = False, block_ipc: bool = False
    ) -> None:
        """Install an irreversible seccomp filter denying specified syscall groups."""
        self._prctl_no_new_privs()

        syscall_groups = []
        if block_sockets:
            syscall_groups.append(_SOCKET_SYSCALLS)
        if block_process:
            syscall_groups.append(_PROCESS_EXEC_SYSCALLS)
        if block_ipc:
            syscall_groups.append(_IPC_SYSCALLS)

        context = self.libseccomp.seccomp_init(ctypes.c_uint32(SECCOMP_RET_ALLOW))
        if not context:
            raise OSError(errno.ENOMEM, "seccomp_init failed")
        try:
            for group in syscall_groups:
                for name in group:
                    syscall = self._get_syscall_number(name)
                    if syscall >= 0:
                        self._rule_add(context, syscall)
            self._load(context)
        finally:
            self.libseccomp.seccomp_release(context)


def get_sandbox() -> LandlockSandbox:
    if platform.system() != "Linux":
        raise SandboxError("Build sandboxing is only supported on Linux")
    try:
        return LandlockSandbox()
    except OSError as e:
        raise SandboxError(f"Landlock sandboxing is unavailable: {e}") from e


def get_recipe_import_sandbox() -> LandlockSandbox:
    """Return a Landlock sandbox suitable for recipe-import filesystem confinement."""
    return get_sandbox()


def sandbox_fallback_allowed() -> bool:
    """Return whether unavailable worker confinement may use a trusted direct path."""
    return spack.config.CONFIG.get("config:sandbox:allow_fallback", False)


def recipe_import_sandbox_available() -> bool:
    """Probe recipe-import confinement, honoring the configured fallback policy."""
    if platform.system() != "Linux":
        if sandbox_fallback_allowed():
            return False
        raise SandboxError("Recipe-import confinement is only supported on Linux")

    # If Landlock is unavailable, allow a normal in-process fallback if configured.
    try:
        sandbox = get_recipe_import_sandbox()
    except SandboxError:
        if sandbox_fallback_allowed():
            return False
        raise

    if sandbox.network_isolation_available(allow_tcp_network_fallback=False):
        return True
    if sandbox_fallback_allowed():
        return False
    raise SandboxError("Recipe-import network isolation is unavailable")


def network_supervision_available() -> bool:
    """Return whether a child listener can be duplicated through a pidfd."""
    if platform.system() != "Linux" or not hasattr(os, "fork"):
        return False
    listener_read_fd, listener_write_fd = os.pipe()
    acknowledge_read_fd, acknowledge_write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(listener_read_fd)
        os.close(acknowledge_write_fd)
        try:
            listener_fd = SeccompSandbox().network_listener()
            os.write(listener_write_fd, str(listener_fd).encode("ascii"))
            os.read(acknowledge_read_fd, 1)
            os.close(listener_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.write(listener_write_fd, b"!")
        os._exit(0)

    os.close(listener_write_fd)
    os.close(acknowledge_read_fd)
    try:
        listener_bytes = os.read(listener_read_fd, 32)
        if not listener_bytes or listener_bytes == b"!":
            return False
        pidfd = pidfd_open(pid)
        try:
            listener_fd = pidfd_getfd(pidfd, int(listener_bytes.decode("ascii")))
            os.close(listener_fd)
        finally:
            os.close(pidfd)
        os.write(acknowledge_write_fd, b"1")
        return True
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    finally:
        os.close(listener_read_fd)
        with contextlib.suppress(OSError):
            os.write(acknowledge_write_fd, b"0")
        os.close(acknowledge_write_fd)
        os.waitpid(pid, 0)


def set_recipe_import_rlimits(memory_limit_bytes: int = 1024 * 1024 * 1024) -> None:
    """Set process resource limits (memory ceiling) for recipe evaluation workers."""
    _set_worker_rlimits(memory_limit_bytes, limit_file_size=True)


def set_network_worker_rlimits(memory_limit_bytes: int = 1024 * 1024 * 1024) -> None:
    """Set resource limits for a network worker that writes only to its stage root."""
    _set_worker_rlimits(memory_limit_bytes, limit_file_size=False)


def _set_worker_rlimits(memory_limit_bytes: int, limit_file_size: bool) -> None:
    """Apply shared worker rlimits, optionally forbidding all file output."""

    # See man setrlimit(2) for details on the resource limits being set here.
    # Disable core dumps to avoid causing I/O and disk space consumption issues.
    if hasattr(resource, "RLIMIT_CORE"):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if limit_file_size and hasattr(resource, "RLIMIT_FSIZE"):
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))

    # Set the soft and hard memory ceilings to fail if too much memory is used.
    if hasattr(resource, "RLIMIT_AS"):
        soft, _ = resource.getrlimit(resource.RLIMIT_AS)
        new_limit = (
            memory_limit_bytes if soft == resource.RLIM_INFINITY else min(memory_limit_bytes, soft)
        )
        resource.setrlimit(resource.RLIMIT_AS, (new_limit, new_limit))
    if hasattr(resource, "RLIMIT_DATA"):
        soft, _ = resource.getrlimit(resource.RLIMIT_DATA)
        new_limit = (
            memory_limit_bytes if soft == resource.RLIM_INFINITY else min(memory_limit_bytes, soft)
        )
        resource.setrlimit(resource.RLIMIT_DATA, (new_limit, new_limit))
    if hasattr(resource, "RLIMIT_STACK"):
        soft, _ = resource.getrlimit(resource.RLIMIT_STACK)
        new_limit = (
            memory_limit_bytes if soft == resource.RLIM_INFINITY else min(memory_limit_bytes, soft)
        )
        resource.setrlimit(resource.RLIMIT_STACK, (new_limit, new_limit))


def restrict_recipe_import(repository_roots: Iterable[Union[str, Path]]) -> None:
    """Apply recipe filesystem, network, process, and IPC confinement."""
    set_recipe_import_rlimits(1024 * 1024 * 1024)
    sandbox = get_recipe_import_sandbox()
    for root in repository_roots:
        sandbox.allow_read(root)
    sandbox.apply(
        block_network=True, block_process=True, block_ipc=True, allow_tcp_network_fallback=False
    )


def restrict_network_worker(
    read_roots: Iterable[Union[str, Path]], write_roots: Iterable[Union[str, Path]] = ()
) -> None:
    """Confine a worker whose socket and connect calls are already supervised."""
    set_network_worker_rlimits(1024 * 1024 * 1024)
    sandbox = get_sandbox()
    for root in read_roots:
        sandbox.allow_read(root)
    for root in write_roots:
        sandbox.allow_write(root)
    sandbox.apply(block_network=False, block_process=True, block_ipc=True)


class SandboxError(spack.error.SpackError):
    """Raised when the build sandbox cannot be set up or applied."""
