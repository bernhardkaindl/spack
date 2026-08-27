# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import pytest

import spack.concretizer_worker as concretizer_worker
import spack.sandbox


def test_selects_worker_when_confinement_is_available(monkeypatch):
    monkeypatch.setattr(spack.sandbox, "recipe_import_sandbox_available", lambda: True)

    selection = concretizer_worker.select_execution()

    assert selection == concretizer_worker.ConcretizerWorkerSelection(
        concretizer_worker.WORKER, None
    )


def test_selects_configured_fallback(monkeypatch):
    monkeypatch.setattr(spack.sandbox, "recipe_import_sandbox_available", lambda: False)
    monkeypatch.setattr(spack.sandbox, "sandbox_fallback_allowed", lambda: True)

    selection = concretizer_worker.select_execution()

    assert selection.mode == concretizer_worker.FALLBACK
    assert "returned False" in selection.diagnostic


def test_rejects_unavailable_worker_without_fallback(monkeypatch):
    monkeypatch.setattr(spack.sandbox, "recipe_import_sandbox_available", lambda: False)
    monkeypatch.setattr(spack.sandbox, "sandbox_fallback_allowed", lambda: False)

    with pytest.raises(
        concretizer_worker.ConcretizerWorkerCapabilityError, match="allow_fallback"
    ):
        concretizer_worker.select_execution()


def test_probe_failure_uses_configured_fallback(monkeypatch):
    def unavailable():
        raise spack.sandbox.SandboxError("Landlock unavailable")

    monkeypatch.setattr(spack.sandbox, "recipe_import_sandbox_available", unavailable)
    monkeypatch.setattr(spack.sandbox, "sandbox_fallback_allowed", lambda: True)

    selection = concretizer_worker.select_execution()

    assert selection.mode == concretizer_worker.FALLBACK
    assert "Landlock unavailable" in selection.diagnostic
