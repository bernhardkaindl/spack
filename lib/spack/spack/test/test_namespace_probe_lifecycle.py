# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Process-lifecycle regressions for the disposable namespace probe."""

import errno
import os
import subprocess
import sys

import pytest

import spack.sandbox_namespaces as ns


@pytest.fixture(autouse=True)
def reset_namespace_probe_result(monkeypatch):
    monkeypatch.setattr(ns, "_namespace_probe_result", None)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_probe_child_cannot_return_to_caller_on_unexpected_error():
    # Run out of process: a broken probe must not duplicate the pytest runner.
    code = """
import spack.sandbox_namespaces as ns

def fail(*args, **kwargs):
    raise RuntimeError("unexpected setup failure")

ns._enter_user_mount_namespace = fail
try:
    result = ns._namespace_available(None)
except RuntimeError:
    print("escaped child", flush=True)
else:
    print("result=" + str(result), flush=True)
"""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "result=False\n"


def test_fork_failure_closes_pipe(monkeypatch):
    descriptors = os.pipe()
    monkeypatch.setattr(ns.os, "pipe", lambda: descriptors)

    def fail():
        raise OSError(errno.EAGAIN, "fork denied")

    monkeypatch.setattr(ns.os, "fork", fail, raising=False)
    try:
        with pytest.raises(OSError, match="fork denied"):
            ns._namespace_available(None)
        for fd in descriptors:
            with pytest.raises(OSError) as error:
                os.fstat(fd)
            assert error.value.errno == errno.EBADF
    finally:
        for fd in descriptors:
            try:
                os.close(fd)
            except OSError:
                pass


def test_read_failure_still_reaps_child(monkeypatch):
    reaped = []
    monkeypatch.setattr(ns.os, "fork", lambda: 12345, raising=False)
    monkeypatch.setattr(ns.os, "waitpid", lambda pid, options: reaped.append(pid), raising=False)

    def fail(*args):
        raise OSError(errno.EIO, "read failed")

    monkeypatch.setattr(ns.os, "read", fail)
    with pytest.raises(OSError, match="read failed"):
        ns._namespace_available(None)
    assert reaped == [12345]


def test_libc_failure_is_unavailable(monkeypatch):
    monkeypatch.setattr(ns.platform, "system", lambda: "Linux")
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(None)
        raise OSError(errno.ENOENT, "libc unavailable")

    monkeypatch.setattr(ns.ctypes, "CDLL", fail)
    assert not ns.namespace_sandbox_available()
    assert not ns.namespace_sandbox_available()
    assert len(attempts) == 2


def test_probe_without_fork(monkeypatch):
    monkeypatch.setattr(ns.os, "fork", None, raising=False)
    assert not ns._namespace_available(None)


def test_probe_off_linux(monkeypatch):
    monkeypatch.setattr(ns.platform, "system", lambda: "Windows")
    assert not ns.namespace_sandbox_available()


@pytest.mark.parametrize("available", [False, True])
def test_default_libc_probe_result_is_cached(monkeypatch, available):
    probes = []
    monkeypatch.setattr(ns.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ns.ctypes, "CDLL", lambda *args, **kwargs: object())
    monkeypatch.setattr(ns, "_namespace_available", lambda libc: probes.append(libc) or available)

    assert ns.namespace_sandbox_available() is available
    assert ns.namespace_sandbox_available() is available
    assert len(probes) == 1


def test_worker_freezes_unavailable_result_after_retry(monkeypatch):
    attempts = []
    monkeypatch.setattr(ns.platform, "system", lambda: "Linux")

    def fail(*args, **kwargs):
        attempts.append(None)
        raise OSError(errno.EAGAIN, "temporary probe failure")

    monkeypatch.setattr(ns.ctypes, "CDLL", fail)
    assert not ns.namespace_sandbox_available()
    assert ns._namespace_probe_result is None
    assert not ns.prepare_empty_directory_masking([], cache_unavailable=True)
    assert ns._namespace_probe_result is False
    assert not ns.namespace_sandbox_available()
    assert len(attempts) == 2
