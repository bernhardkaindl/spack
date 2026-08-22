# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

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
        self.prctl_called = False
        super().__init__()

    def _prctl_no_new_privs(self):
        self.prctl_called = True

    def _get_syscall_number(self, name: str) -> int:
        return hash(name) % 10000

    def _rule_add(self, context, syscall: int) -> None:
        self.rules_added.append(syscall)

    def _load(self, context) -> None:
        self.loaded = True


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


def test_seccomp_sandbox_deny_groups_execution(tmp_path):
    """Integration test verifying seccomp blocks forbidden syscalls in worker process."""
    try:
        spack.sandbox.get_recipe_import_seccomp()
    except spack.sandbox.SandboxError as e:
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
