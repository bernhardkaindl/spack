# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Versioned structured messages for confined concretizer workers."""

from typing import Any, Dict, Iterable, List, NamedTuple, Sequence, Union

import spack.spec

PROTOCOL_VERSION = 1
TOGETHER = "together"
WHEN_POSSIBLE = "when_possible"
SEPARATELY = "separately"
STRATEGIES = (TOGETHER, WHEN_POSSIBLE, SEPARATELY)
_REQUEST_KEYS = {"allow_deprecated", "protocol", "specs", "strategy", "tests"}
_RESPONSE_KEYS = {"protocol", "results", "warnings"}
_RESULT_KEYS = {"dag_hash", "input", "spec"}
_ERROR_RESPONSE_KEYS = {"error", "protocol"}
_ERROR_KEYS = {"kind", "long_message", "message"}
ERROR_KINDS = ("config", "spack", "spec", "unknown_package", "unsatisfiable")
_MAX_WARNINGS = 1024
_MAX_WARNING_BYTES = 64 * 1024
_MAX_ERROR_BYTES = 1024 * 1024

TestsType = Union[bool, Iterable[str]]


class ConcretizerWorkerProtocolError(ValueError):
    """A concretizer-worker message violates the versioned contract."""


class ConcretizerWorkerRequest(NamedTuple):
    """Validated inputs for one concretization operation."""

    specs: List[spack.spec.Spec]
    tests: Union[bool, List[str]]
    allow_deprecated: bool
    strategy: str


class ConcretizerWorkerResponse(NamedTuple):
    """Validated concrete roots and diagnostics from one operation."""

    specs: List[spack.spec.Spec]
    warnings: List[str]


class ConcretizerWorkerErrorResponse(NamedTuple):
    """Validated allowlisted solver failure returned by a worker."""

    kind: str
    message: str
    long_message: Union[str, None]


def _tests_to_json(tests: TestsType) -> Union[bool, List[str]]:
    if type(tests) is bool:
        return tests
    if isinstance(tests, str):
        raise ConcretizerWorkerProtocolError("concretizer-worker tests must not be a string")
    try:
        result = list(tests)
    except TypeError as error:
        raise ConcretizerWorkerProtocolError(
            "concretizer-worker tests must be a boolean or iterable of package names"
        ) from error
    if not all(isinstance(name, str) and name for name in result):
        raise ConcretizerWorkerProtocolError(
            "concretizer-worker tests contain an invalid package name"
        )
    return result


def _validate_warnings(warnings: Any) -> List[str]:
    if not isinstance(warnings, list) or not all(isinstance(warning, str) for warning in warnings):
        raise ConcretizerWorkerProtocolError("concretizer-worker response has invalid warnings")
    if len(warnings) > _MAX_WARNINGS or any(
        len(warning.encode("utf-8")) > _MAX_WARNING_BYTES for warning in warnings
    ):
        raise ConcretizerWorkerProtocolError(
            "concretizer-worker response warnings exceed the diagnostic limit"
        )
    return warnings


def _validate_error_text(value: Any, field: str, optional: bool = False) -> Union[str, None]:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConcretizerWorkerProtocolError(
            "concretizer-worker error has invalid {0}".format(field)
        )
    if len(value.encode("utf-8")) > _MAX_ERROR_BYTES:
        raise ConcretizerWorkerProtocolError(
            "concretizer-worker error {0} exceeds the diagnostic limit".format(field)
        )
    return value


def create_request(
    specs: Sequence[spack.spec.Spec],
    *,
    tests: TestsType = False,
    allow_deprecated: bool = False,
    strategy: str = TOGETHER,
) -> Dict[str, Any]:
    """Create a JSON-compatible request without imposing a solve-size ceiling."""
    if not isinstance(specs, Sequence) or isinstance(specs, (str, bytes)) or not specs:
        raise ConcretizerWorkerProtocolError("concretizer-worker request requires input specs")
    if not all(isinstance(spec, spack.spec.Spec) for spec in specs):
        raise ConcretizerWorkerProtocolError("concretizer-worker request contains an invalid spec")
    request = {
        "allow_deprecated": allow_deprecated,
        "protocol": PROTOCOL_VERSION,
        "specs": [spec.to_dict() for spec in specs],
        "strategy": strategy,
        "tests": _tests_to_json(tests),
    }
    validate_request(request)
    return request


def validate_request(request: Any) -> ConcretizerWorkerRequest:
    """Validate structured worker input and restore its abstract native specs."""
    if not isinstance(request, dict) or set(request) != _REQUEST_KEYS:
        raise ConcretizerWorkerProtocolError("concretizer-worker request has invalid fields")
    if type(request["protocol"]) is not int or request["protocol"] != PROTOCOL_VERSION:
        raise ConcretizerWorkerProtocolError(
            "concretizer-worker request has an unsupported protocol"
        )
    if type(request["allow_deprecated"]) is not bool:
        raise ConcretizerWorkerProtocolError(
            "concretizer-worker request has invalid allow-deprecated data"
        )
    if request["strategy"] not in STRATEGIES:
        raise ConcretizerWorkerProtocolError("concretizer-worker request has an invalid strategy")
    tests = _tests_to_json(request["tests"])
    if not isinstance(request["specs"], list) or not request["specs"]:
        raise ConcretizerWorkerProtocolError("concretizer-worker request requires input specs")
    if not all(isinstance(spec, dict) for spec in request["specs"]):
        raise ConcretizerWorkerProtocolError("concretizer-worker request has invalid spec data")

    try:
        specs = [spack.spec.Spec.from_dict(spec) for spec in request["specs"]]
    except Exception as error:
        raise ConcretizerWorkerProtocolError(
            "concretizer-worker request contains an invalid spec"
        ) from error
    return ConcretizerWorkerRequest(specs, tests, request["allow_deprecated"], request["strategy"])


def create_response(
    specs: Sequence[spack.spec.Spec], warnings: Iterable[str] = ()
) -> Dict[str, Any]:
    """Create a JSON-compatible response without imposing a concrete-DAG size ceiling."""
    if not isinstance(specs, Sequence) or isinstance(specs, (str, bytes)) or not specs:
        raise ConcretizerWorkerProtocolError("concretizer-worker response has invalid specs")
    if not all(isinstance(spec, spack.spec.Spec) and spec.concrete for spec in specs):
        raise ConcretizerWorkerProtocolError("concretizer-worker response requires concrete specs")
    if isinstance(warnings, str):
        raise ConcretizerWorkerProtocolError("concretizer-worker response has invalid warnings")
    try:
        warning_list = list(warnings)
    except TypeError as error:
        raise ConcretizerWorkerProtocolError(
            "concretizer-worker response has invalid warnings"
        ) from error
    _validate_warnings(warning_list)

    return {
        "protocol": PROTOCOL_VERSION,
        "results": [
            {"dag_hash": spec.dag_hash(), "input": index, "spec": spec.to_dict()}
            for index, spec in enumerate(specs)
        ],
        "warnings": warning_list,
    }


def create_error_response(
    kind: str, message: str, long_message: Union[str, None] = None
) -> Dict[str, Any]:
    """Create a bounded allowlisted solver-error response."""
    if kind not in ERROR_KINDS:
        raise ConcretizerWorkerProtocolError("concretizer-worker error has an invalid kind")
    error = {
        "kind": kind,
        "long_message": _validate_error_text(long_message, "long message", optional=True),
        "message": _validate_error_text(message, "message"),
    }
    return {"error": error, "protocol": PROTOCOL_VERSION}


def validate_error_response(response: Any) -> Union[ConcretizerWorkerErrorResponse, None]:
    """Return a validated solver error, or ``None`` for a non-error response."""
    if not isinstance(response, dict) or set(response) != _ERROR_RESPONSE_KEYS:
        return None
    if type(response["protocol"]) is not int or response["protocol"] != PROTOCOL_VERSION:
        raise ConcretizerWorkerProtocolError(
            "concretizer-worker error has an unsupported protocol"
        )
    error = response["error"]
    if not isinstance(error, dict) or set(error) != _ERROR_KEYS:
        raise ConcretizerWorkerProtocolError("concretizer-worker error has invalid fields")
    if error["kind"] not in ERROR_KINDS:
        raise ConcretizerWorkerProtocolError("concretizer-worker error has an invalid kind")
    message = _validate_error_text(error["message"], "message")
    assert isinstance(message, str)
    return ConcretizerWorkerErrorResponse(
        error["kind"],
        message,
        _validate_error_text(error["long_message"], "long message", optional=True),
    )


def validate_response(response: Any, request: Any) -> ConcretizerWorkerResponse:
    """Validate a structured response and restore concrete roots in request order."""
    validated_request = validate_request(request)
    if not isinstance(response, dict) or set(response) != _RESPONSE_KEYS:
        raise ConcretizerWorkerProtocolError("concretizer-worker response has invalid fields")
    if type(response["protocol"]) is not int or response["protocol"] != PROTOCOL_VERSION:
        raise ConcretizerWorkerProtocolError(
            "concretizer-worker response has an unsupported protocol"
        )
    warnings = _validate_warnings(response["warnings"])
    if not isinstance(response["results"], list):
        raise ConcretizerWorkerProtocolError("concretizer-worker response has invalid results")
    if len(response["results"]) != len(validated_request.specs):
        raise ConcretizerWorkerProtocolError(
            "concretizer-worker response does not match its request"
        )

    specs = []
    for expected_index, result in enumerate(response["results"]):
        if not isinstance(result, dict) or set(result) != _RESULT_KEYS:
            raise ConcretizerWorkerProtocolError(
                "concretizer-worker response has invalid result fields"
            )
        if type(result["input"]) is not int or result["input"] != expected_index:
            raise ConcretizerWorkerProtocolError(
                "concretizer-worker response has invalid input ordering"
            )
        if not isinstance(result["dag_hash"], str) or not result["dag_hash"]:
            raise ConcretizerWorkerProtocolError(
                "concretizer-worker response has an invalid DAG hash"
            )
        if not isinstance(result["spec"], dict):
            raise ConcretizerWorkerProtocolError(
                "concretizer-worker response has invalid spec data"
            )
        try:
            spec = spack.spec.Spec.from_dict(result["spec"])
        except Exception as error:
            raise ConcretizerWorkerProtocolError(
                "concretizer-worker response contains an invalid spec"
            ) from error
        if not spec.concrete:
            raise ConcretizerWorkerProtocolError(
                "concretizer-worker response requires concrete specs"
            )
        if spec.dag_hash() != result["dag_hash"]:
            raise ConcretizerWorkerProtocolError(
                "concretizer-worker response DAG hash does not match its spec"
            )
        specs.append(spec)

    return ConcretizerWorkerResponse(specs, list(warnings))
