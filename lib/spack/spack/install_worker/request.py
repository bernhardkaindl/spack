# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Bounded native-spec requests for install workers."""

import json
from typing import Any, Dict

import spack.spec
from spack.util.sandbox import MAX_MESSAGE_BYTES

PROTOCOL_VERSION = 1
_REQUEST_KEYS = {"dag_hash", "protocol", "spec"}


class InstallWorkerRequestError(ValueError):
    """An install-worker request is malformed or does not identify its spec."""


def _encoded_size(request: Dict[str, Any]) -> int:
    try:
        return len(json.dumps(request, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as error:
        raise InstallWorkerRequestError("install-worker request is not JSON compatible") from error


def create_request(spec: spack.spec.Spec) -> Dict[str, Any]:
    """Create and validate a request for an already-concrete native Spack spec."""
    if not isinstance(spec, spack.spec.Spec) or not spec.concrete:
        raise InstallWorkerRequestError("install-worker request requires a concrete spec")
    request = {"protocol": PROTOCOL_VERSION, "dag_hash": spec.dag_hash(), "spec": spec.to_dict()}
    validate_request(request)
    return request


def validate_request(request: Any) -> spack.spec.Spec:
    """Validate a bounded request and return its concrete native Spack spec."""
    if not isinstance(request, dict) or set(request) != _REQUEST_KEYS:
        raise InstallWorkerRequestError("install-worker request has invalid fields")
    if type(request["protocol"]) is not int or request["protocol"] != PROTOCOL_VERSION:
        raise InstallWorkerRequestError("install-worker request has an unsupported protocol")
    if not isinstance(request["dag_hash"], str) or not request["dag_hash"]:
        raise InstallWorkerRequestError("install-worker request has an invalid DAG hash")
    if not isinstance(request["spec"], dict):
        raise InstallWorkerRequestError("install-worker request has invalid spec data")
    if _encoded_size(request) > MAX_MESSAGE_BYTES:
        raise InstallWorkerRequestError("install-worker request exceeds the byte limit")

    try:
        spec = spack.spec.Spec.from_dict(request["spec"])
    except Exception as error:
        raise InstallWorkerRequestError(
            "install-worker request contains an invalid spec"
        ) from error
    if not spec.concrete:
        raise InstallWorkerRequestError("install-worker request requires a concrete spec")
    if spec.dag_hash() != request["dag_hash"]:
        raise InstallWorkerRequestError("install-worker request DAG hash does not match its spec")
    return spec
