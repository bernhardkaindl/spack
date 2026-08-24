# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import pytest

import spack.concretize
import spack.install_worker as install_worker
import spack.sandbox
import spack.spec
from spack.install_worker.request import PROTOCOL_VERSION
from spack.util.sandbox import MAX_MESSAGE_BYTES


def test_request_round_trip(mock_packages):
    spec = spack.concretize.concretize_one("dependent-install")

    request = install_worker.create_request(spec)
    restored = install_worker.validate_request(request)

    assert request["protocol"] == PROTOCOL_VERSION
    assert restored.dag_hash() == spec.dag_hash()
    assert restored.to_dict() == spec.to_dict()


def test_request_requires_concrete_spec():
    with pytest.raises(install_worker.InstallWorkerRequestError, match="concrete spec"):
        install_worker.create_request(spack.spec.Spec("zlib"))


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"dag_hash": "hash", "protocol": PROTOCOL_VERSION, "spec": {}, "extra": True},
        {"dag_hash": "hash", "protocol": True, "spec": {}},
        {"dag_hash": "hash", "protocol": PROTOCOL_VERSION + 1, "spec": {}},
        {"dag_hash": "", "protocol": PROTOCOL_VERSION, "spec": {}},
        {"dag_hash": "hash", "protocol": PROTOCOL_VERSION, "spec": []},
    ],
)
def test_request_rejects_malformed_input(payload):
    with pytest.raises(install_worker.InstallWorkerRequestError):
        install_worker.validate_request(payload)


def test_request_rejects_oversized_input():
    request = {
        "dag_hash": "hash",
        "protocol": PROTOCOL_VERSION,
        "spec": {"padding": "x" * MAX_MESSAGE_BYTES},
    }

    with pytest.raises(install_worker.InstallWorkerRequestError, match="byte limit"):
        install_worker.validate_request(request)


def test_request_rejects_mismatched_hash(mock_packages):
    spec = spack.concretize.concretize_one("dependent-install")
    request = install_worker.create_request(spec)
    request["dag_hash"] = "a" * len(request["dag_hash"])

    with pytest.raises(install_worker.InstallWorkerRequestError, match="does not match"):
        install_worker.validate_request(request)


def test_selects_worker_when_capabilities_are_available(monkeypatch):
    monkeypatch.setattr(spack.sandbox, "recipe_import_sandbox_available", lambda: True)
    monkeypatch.setattr(spack.sandbox, "network_supervision_available", lambda: True)

    selection = install_worker.select_execution()

    assert selection.mode == install_worker.WORKER
    assert selection.diagnostic is None


def test_selects_recipe_fallback_with_diagnostic(monkeypatch):
    monkeypatch.setattr(spack.sandbox, "recipe_import_sandbox_available", lambda: False)
    monkeypatch.setattr(spack.sandbox, "sandbox_fallback_allowed", lambda: True)

    selection = install_worker.select_execution()

    assert selection.mode == install_worker.FALLBACK
    assert selection.diagnostic is not None
    assert "recipe_import_sandbox_available() returned False" in selection.diagnostic


def test_fails_when_recipe_fallback_is_disabled(monkeypatch):
    monkeypatch.setattr(spack.sandbox, "recipe_import_sandbox_available", lambda: False)
    monkeypatch.setattr(spack.sandbox, "sandbox_fallback_allowed", lambda: False)

    with pytest.raises(
        install_worker.InstallWorkerCapabilityError,
        match=r"recipe_import_sandbox_available\(\) returned False.*allow_fallback is false",
    ):
        install_worker.select_execution()


def test_selects_network_fallback_with_diagnostic(monkeypatch):
    monkeypatch.setattr(spack.sandbox, "recipe_import_sandbox_available", lambda: True)
    monkeypatch.setattr(spack.sandbox, "network_supervision_available", lambda: False)
    monkeypatch.setattr(spack.sandbox, "sandbox_fallback_allowed", lambda: True)

    selection = install_worker.select_execution()

    assert selection.mode == install_worker.FALLBACK
    assert selection.diagnostic is not None
    assert "network_supervision_available() returned False" in selection.diagnostic


def test_fails_when_recipe_probe_fails(monkeypatch):
    def unavailable():
        raise spack.sandbox.SandboxError("Landlock unavailable")

    monkeypatch.setattr(spack.sandbox, "recipe_import_sandbox_available", unavailable)
    monkeypatch.setattr(spack.sandbox, "sandbox_fallback_allowed", lambda: False)

    with pytest.raises(
        install_worker.InstallWorkerCapabilityError,
        match=r"recipe_import_sandbox_available\(\) failed: Landlock unavailable",
    ):
        install_worker.select_execution()


def test_probe_error_uses_configured_fallback(monkeypatch):
    def unavailable():
        raise OSError("pidfd unavailable")

    monkeypatch.setattr(spack.sandbox, "recipe_import_sandbox_available", lambda: True)
    monkeypatch.setattr(spack.sandbox, "network_supervision_available", unavailable)
    monkeypatch.setattr(spack.sandbox, "sandbox_fallback_allowed", lambda: True)

    selection = install_worker.select_execution()

    assert selection.mode == install_worker.FALLBACK
    assert selection.diagnostic is not None
    assert (
        "network_supervision_available() failed: OSError: pidfd unavailable"
        in selection.diagnostic
    )


def test_fails_when_network_fallback_is_disabled(monkeypatch):
    monkeypatch.setattr(spack.sandbox, "recipe_import_sandbox_available", lambda: True)
    monkeypatch.setattr(spack.sandbox, "network_supervision_available", lambda: False)
    monkeypatch.setattr(spack.sandbox, "sandbox_fallback_allowed", lambda: False)

    with pytest.raises(
        install_worker.InstallWorkerCapabilityError,
        match=r"network_supervision_available\(\) returned False.*allow_fallback is false",
    ):
        install_worker.select_execution()
