# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Unit tests for the namespace sandbox capability probe (Phase 1).

These tests only exercise ``spack.sandbox.namespace_sandbox_available()`` and
the disposable-child probing pattern. Mount-tree setup, backend selection, and
installer integration are later phases and are not imported here, so this file
cannot accidentally depend on uncommitted follow-up APIs.
"""

import sys

import pytest

if sys.platform != "linux":
    pytest.skip("Namespace sandboxing is Linux only", allow_module_level=True)

import spack.sandbox


def test_namespace_sandbox_available_is_false_off_linux(monkeypatch):
    monkeypatch.setattr(spack.sandbox.platform, "system", lambda: "Windows")
    assert spack.sandbox.namespace_sandbox_available() is False


def test_namespace_sandbox_available_reports_probe_result(monkeypatch):
    monkeypatch.setattr(spack.sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(spack.sandbox, "_namespace_available", lambda libc: True)
    assert spack.sandbox.namespace_sandbox_available() is True

    monkeypatch.setattr(spack.sandbox, "_namespace_available", lambda libc: False)
    assert spack.sandbox.namespace_sandbox_available() is False


def test_namespace_sandbox_available_swallows_probe_errors(monkeypatch):
    monkeypatch.setattr(spack.sandbox.platform, "system", lambda: "Linux")

    def boom(libc):
        raise OSError("unshare failed")

    monkeypatch.setattr(spack.sandbox, "_namespace_available", boom)
    assert spack.sandbox.namespace_sandbox_available() is False
