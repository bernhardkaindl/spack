# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import array
import contextlib
import errno
import os
import socket
import sys

import pytest

if sys.platform != "linux":
    pytest.skip("Seccomp sandboxing is Linux only", allow_module_level=True)

import spack.sandbox
from spack.util.sandbox import run_json_worker


class SpySeccompSandbox(spack.sandbox.SeccompSandbox):
    """Spy subclass recording calls to libseccomp C functions."""

    def __init__(self):
        self.rules_added = []
        self.loaded = False
        self.listener_fd = 42
        self.prctl_called = False
        super().__init__()

    def _prctl_no_new_privs(self):
        self.prctl_called = True

    def _get_syscall_number(self, name: str) -> int:
        return hash(name) % 10000

    def _rule_add(self, context, syscall: int, action=spack.sandbox.SECCOMP_RET_ERRNO | 1) -> None:
        self.rules_added.append((syscall, action))

    def _load(self, context) -> None:
        self.loaded = True

    def _notify_fd(self, context) -> int:
        return self.listener_fd


def test_seccomp_sandbox_calls_prctl_no_new_privs():
    spy = SpySeccompSandbox()
    spy.apply(block_sockets=True, block_process=False, block_ipc=False)
    assert spy.prctl_called
    assert spy.loaded
    assert len(spy.rules_added) == len(spack.sandbox._SOCKET_SYSCALLS)


def test_seccomp_sandbox_groups():
    spy = SpySeccompSandbox()
    spy.apply(block_sockets=True, block_process=True, block_ipc=True)
    total_expected = (
        len(spack.sandbox._SOCKET_SYSCALLS)
        + len(spack.sandbox._PROCESS_EXEC_SYSCALLS)
        + len(spack.sandbox._IPC_SYSCALLS)
    )
    assert len(spy.rules_added) == total_expected


def test_seccomp_connect_listener():
    spy = SpySeccompSandbox()

    assert spy.connect_listener() == spy.listener_fd
    connect = spy._get_syscall_number("connect")
    assert spy.rules_added == [(connect, spack.sandbox.SECCOMP_RET_USER_NOTIF)]
    assert spy.prctl_called
    assert spy.loaded


def test_seccomp_network_listener():
    spy = SpySeccompSandbox()

    assert spy.network_listener() == spy.listener_fd
    assert spy.rules_added == [
        (spy._get_syscall_number("socket"), spack.sandbox.SECCOMP_RET_USER_NOTIF),
        (spy._get_syscall_number("connect"), spack.sandbox.SECCOMP_RET_USER_NOTIF),
    ]


def test_seccomp_denies_network_bypass_after_listener_transfer():
    spy = SpySeccompSandbox()

    spy.deny_network_bypass()

    assert spy.rules_added == [
        (spy._get_syscall_number(name), spack.sandbox.SECCOMP_RET_ERRNO | errno.EPERM)
        for name in spack.sandbox._NETWORK_WORKER_DENY_SYSCALLS
    ]


def test_seccomp_connect_notification_round_trip():
    control_parent, control_child = socket.socketpair()
    pid = os.fork()
    if pid == 0:
        control_parent.close()
        try:
            seccomp = spack.sandbox.SeccompSandbox()
            listener_fd = seccomp.connect_listener()
            control_child.sendmsg(
                [b"L"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [listener_fd]))]
            )
            os.close(listener_fd)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                try:
                    connection.connect(("127.0.0.1", 9))
                except OSError as error:
                    os._exit(0 if error.errno == errno.ECONNREFUSED else 2)
            os._exit(3)
        except BaseException as error:
            with contextlib.suppress(OSError):
                error_number = error.errno if isinstance(error, OSError) else 0
                control_child.send(
                    "ERROR:{0}:{1}".format(error_number, repr(error)).encode("utf-8")[:1024]
                )
            os._exit(4)

    control_child.close()
    try:
        message, ancillary, _flags, _address = control_parent.recvmsg(
            1024, socket.CMSG_SPACE(array.array("i").itemsize)
        )
        if not ancillary:
            diagnostic = message.decode("utf-8", errors="replace")
            error_number = (
                int(diagnostic.split(":", 2)[1]) if diagnostic.startswith("ERROR:") else 0
            )
            if error_number in (errno.EACCES, errno.EBUSY, errno.EOPNOTSUPP):
                pytest.skip(diagnostic)
            pytest.fail(diagnostic)
        listener_fds = array.array("i")
        listener_fds.frombytes(ancillary[0][2][: listener_fds.itemsize])
        listener_fd = listener_fds[0]
        try:
            seccomp = spack.sandbox.SeccompSandbox()
            notification = seccomp.receive_notification(listener_fd)
            assert notification.pid == pid
            assert notification.data.nr == seccomp._get_syscall_number("connect")
            assert seccomp.notification_is_valid(listener_fd, notification.id)
            seccomp.respond_to_notification(listener_fd, notification.id, error=errno.ECONNREFUSED)
        finally:
            os.close(listener_fd)
    finally:
        control_parent.close()
        _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0


def test_seccomp_sandbox_deny_groups_execution(tmp_path):
    """Integration test verifying seccomp blocks forbidden syscalls in worker process."""
    try:
        spack.sandbox.SeccompSandbox()
    except OSError as e:
        pytest.skip(str(e))

    def worker(request):
        seccomp = spack.sandbox.SeccompSandbox()
        seccomp.apply(block_sockets=True, block_process=True, block_ipc=True)

        # 1. Test socket blocked
        import socket

        try:
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            socket_err = None
        except OSError as err:
            socket_err = err.errno

        # 2. Test fork/exec blocked
        import subprocess

        try:
            subprocess.run(["/bin/true"], check=True)
            exec_err = None
        except OSError as err:
            exec_err = err.errno

        return {"socket_err": socket_err, "exec_err": exec_err}

    result = run_json_worker({}, worker)
    assert result["socket_err"] in (1, 13)  # EPERM or EACCES
    assert result["exec_err"] in (1, 13)
