# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Unit tests for the namespace sandbox capability probe and mount-tree helpers.

Tests use a fake libc and monkeypatched probes so they never enter a namespace
in the pytest process itself.  The live test at the bottom runs the real
``unshare``/``mount`` path in a child process.
"""

import os
import sys

import pytest

import spack.sandbox_namespaces as ns


class FakeLibc:
    """Minimal fake libc that records ``unshare`` and ``mount`` calls."""

    def __init__(self):
        self.unshare_calls = []
        self.mount_calls = []

    def unshare(self, flags):
        self.unshare_calls.append(int(flags))
        return -1

    def mount(self, source, target, filesystemtype, mountflags, data):
        self.mount_calls.append((source, target, int(mountflags)))
        return -1


# ---------------------------------------------------------------------------
# Capability probe (Phase 1)
# ---------------------------------------------------------------------------


def test_namespace_sandbox_available_is_false_off_linux(monkeypatch):
    monkeypatch.setattr(ns.platform, "system", lambda: "Windows")
    assert ns.namespace_sandbox_available() is False


def test_namespace_sandbox_available_is_false_when_probe_fails(monkeypatch):
    monkeypatch.setattr(ns.platform, "system", lambda: "Linux")

    fake = FakeLibc()

    def fake_cdll(*args, **kwargs):
        return fake

    monkeypatch.setattr(os, "fork", None)  # type: ignore[assignment]
    monkeypatch.setattr(ns.ctypes, "CDLL", fake_cdll)

    # The probe forks and the child calls unshare; the fake returns -1 so the
    # child reports failure.  The parent sees b"0" and returns False.
    assert ns.namespace_sandbox_available() is False


def test_namespace_sandbox_available_swallows_oserror(monkeypatch):
    monkeypatch.setattr(ns.platform, "system", lambda: "Linux")

    def fake_cdll(*args, **kwargs):
        raise OSError(1, "Cannot load libc")

    monkeypatch.setattr(ns.ctypes, "CDLL", fake_cdll)
    assert ns.namespace_sandbox_available() is False


def test_namespace_sandbox_available_passes_explicit_libc(monkeypatch):
    monkeypatch.setattr(ns.platform, "system", lambda: "Linux")

    fake = FakeLibc()

    def fake_cdll(*args, **kwargs):
        return fake

    monkeypatch.setattr(ns.ctypes, "CDLL", fake_cdll)
    monkeypatch.setattr(os, "fork", None)  # type: ignore[assignment]

    # When an explicit libc is supplied it is used instead of the default.
    assert ns.namespace_sandbox_available(libc=fake) is False
    assert fake.unshare_calls == []
    assert fake.mount_calls == []


# ---------------------------------------------------------------------------
# Mount-tree helpers (Phase 2)
# ---------------------------------------------------------------------------


def test_prepare_empty_directory_masking_returns_true_on_empty_paths():
    assert ns.prepare_empty_directory_masking([]) is True


def test_prepare_empty_directory_masking_returns_false_when_namespaces_denied(
    monkeypatch,
):
    monkeypatch.setattr(ns.platform, "system", lambda: "Linux")

    fake = FakeLibc()

    def fake_cdll(*args, **kwargs):
        return fake

    monkeypatch.setattr(ns.ctypes, "CDLL", fake_cdll)

    def fake_available(libc=None):
        return False

    monkeypatch.setattr(ns, "_namespace_available", fake_available)
    assert ns.prepare_empty_directory_masking(["/usr/include"], libc=fake) is False
    assert fake.unshare_calls == []


def test_prepare_empty_directory_masking_enters_namespace_when_available(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(ns.platform, "system", lambda: "Linux")

    fake = FakeLibc()
    fake.unshare_returns = 0

    def fake_unshare(self, flags):
        self.unshare_calls.append(int(flags))
        return 0

    fake.unshare = fake_unshare

    def fake_cdll(*args, **kwargs):
        return fake

    monkeypatch.setattr(ns.ctypes, "CDLL", fake_cdll)

    def fake_available(libc=None):
        return True

    monkeypatch.setattr(ns, "_namespace_available", fake_available)
    result = ns.prepare_empty_directory_masking(["/usr/include"], libc=fake)
    assert result is True
    assert fake.unshare_calls == [ns.CLONE_NEWUSER | ns.CLONE_NEWNS]
    assert fake.mount_calls == [
        (None, b"/", ns.MS_REC | ns.MS_PRIVATE)
    ]


def test_hide_directories_as_empty_returns_true_on_empty_paths():
    assert ns.hide_directories_as_empty([]) is True


def test_hide_directories_as_empty_binds_only_existing_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(ns.platform, "system", lambda: "Linux")

    fake = FakeLibc()
    fake.unshare_returns = 0

    def fake_unshare(self, flags):
        self.unshare_calls.append(int(flags))
        return 0

    fake.unshare = fake_unshare

    def fake_cdll(*args, **kwargs):
        return fake

    monkeypatch.setattr(ns.ctypes, "CDLL", fake_cdll)

    def fake_available(libc=None):
        return True

    monkeypatch.setattr(ns, "_namespace_available", fake_available)

    existing = tmp_path / "existing"
    existing.mkdir()
    missing = tmp_path / "missing"

    result = ns.hide_directories_as_empty(
        [str(existing), str(missing)], str(tmp_path), namespace_ready=True, libc=fake
    )
    assert result is True
    # Only the existing directory should be bind-mounted.
    assert len(fake.mount_calls) == 1
    source, target, flags = fake.mount_calls[0]
    assert flags == ns.MS_BIND
    assert source.endswith(b"spack-empty-host-dirs/0")
    assert target == os.fsencode(str(existing))


def test_hide_directories_as_empty_skips_when_namespace_unavailable(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(ns.platform, "system", lambda: "Linux")

    def fake_available(libc=None):
        return False

    monkeypatch.setattr(ns, "_namespace_available", fake_available)

    result = ns.hide_directories_as_empty(
        ["/usr/include"], str(tmp_path), namespace_ready=False
    )
    assert result is False


def test_hide_directories_as_empty_uses_explicit_libc(tmp_path, monkeypatch):
    monkeypatch.setattr(ns.platform, "system", lambda: "Linux")

    fake = FakeLibc()
    fake.unshare_returns = 0

    def fake_unshare(self, flags):
        self.unshare_calls.append(int(flags))
        return 0

    fake.unshare = fake_unshare

    def fake_cdll(*args, **kwargs):
        return fake

    monkeypatch.setattr(ns.ctypes, "CDLL", fake_cdll)

    def fake_available(libc=None):
        return False

    monkeypatch.setattr(ns, "_namespace_available", fake_available)

    # When namespace_ready is True, the probe is skipped and the explicit libc
    # is used for the bind mount.
    existing = tmp_path / "existing"
    existing.mkdir()
    result = ns.hide_directories_as_empty(
        [str(existing)], str(tmp_path), namespace_ready=True, libc=fake
    )
    assert result is True
    assert len(fake.mount_calls) == 1


# ---------------------------------------------------------------------------
# Re-entry guard
# ---------------------------------------------------------------------------


def test_enter_namespace_is_idempotent_within_process(tmp_path, monkeypatch):
    monkeypatch.setattr(ns.platform, "system", lambda: "Linux")

    fake = FakeLibc()
    fake.unshare_returns = 0

    def fake_unshare(self, flags):
        self.unshare_calls.append(int(flags))
        return 0

    fake.unshare = fake_unshare

    def fake_cdll(*args, **kwargs):
        return fake

    monkeypatch.setattr(ns.ctypes, "CDLL", fake_cdll)

    def fake_available(libc=None):
        return True

    monkeypatch.setattr(ns, "_namespace_available", fake_available)

    ns._enter_user_mount_namespace(fake)
    assert len(fake.unshare_calls) == 1
    # Second call should be a no-op.
    ns._enter_user_mount_namespace(fake)
    assert len(fake.unshare_calls) == 1


def test_probe_child_bypasses_reentry_guard(tmp_path, monkeypatch):
    """The probe child always runs the full setup, even if the parent already
    entered a namespace."""
    monkeypatch.setattr(ns.platform, "system", lambda: "Linux")

    fake = FakeLibc()
    fake.unshare_returns = 0

    def fake_unshare(self, flags):
        self.unshare_calls.append(int(flags))
        return 0

    fake.unshare = fake_unshare

    def fake_cdll(*args, **kwargs):
        return fake

    monkeypatch.setattr(ns.ctypes, "CDLL", fake_cdll)

    # Pretend the parent already entered a namespace.
    ns._namespace_entered_pids.add(os.getpid())

    # The probe child should still call unshare.
    assert ns._namespace_available(fake) is False  # fake returns 0 from unshare
    # The child called unshare even though the parent's PID is in the guard set.
    assert len(fake.unshare_calls) == 1


# ---------------------------------------------------------------------------
# Live kernel test
# ---------------------------------------------------------------------------


def test_live_namespace_probe():
    if sys.platform != "linux":
        pytest.skip("Linux-only live probe")
    if not ns.namespace_sandbox_available():
        pytest.skip("unprivileged user and mount namespaces are unavailable")
    # The probe succeeded — verify that the namespace constants are defined.
    assert ns.CLONE_NEWUSER == 0x10000000
    assert ns.CLONE_NEWNS == 0x00020000
    assert ns.MS_PRIVATE == 0x00040000
    assert ns.MS_REC == 0x00004000
    assert ns.MS_BIND == 0x00001000
