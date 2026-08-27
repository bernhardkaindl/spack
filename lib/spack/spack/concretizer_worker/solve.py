# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Launcher-neutral execution of the existing concretizer in a worker."""

import warnings
from typing import Any, Dict, Sequence

import spack.binary_distribution
import spack.config
import spack.error
import spack.repo
import spack.spec
import spack.store
import spack.util.sandbox
from spack.concretizer_worker.protocol import (
    ERROR_KINDS,
    TOGETHER,
    ConcretizerWorkerProtocolError,
    ConcretizerWorkerResponse,
    TestsType,
    create_error_response,
    create_request,
    create_response,
    validate_error_response,
    validate_request,
    validate_response,
)
from spack.util.lock import FILE_TRACKER


class ConcretizerWorkerError(spack.error.SpackError):
    """A validated Spack solver failure returned by the worker."""


class ConcretizerWorkerConfigError(spack.error.ConfigError):
    """A validated configuration failure returned by the worker."""


class ConcretizerWorkerUnsatisfiableSpecError(spack.error.UnsatisfiableSpecError):
    """A validated unsatisfiable-spec failure returned by the worker."""

    def __init__(self, message: str, long_message: Any = None) -> None:
        # The base initializer expects old-concretizer constraint operands, not a rendered message.
        spack.error.SpecError.__init__(self, message, long_message)
        self.provided = None
        self.required = None
        self.constraint_type = None


class ConcretizerWorkerUnknownPackageError(spack.repo.UnknownPackageError):
    """A validated unknown-package failure returned by the worker."""

    def __init__(self, message: str, long_message: Any = None) -> None:
        spack.error.SpackError.__init__(self, message, long_message)


def _error_response(error: spack.error.SpackError) -> Dict[str, Any]:
    if isinstance(error, spack.repo.UnknownPackageError):
        kind = "unknown_package"
    elif isinstance(error, spack.error.UnsatisfiableSpecError):
        kind = "unsatisfiable"
    elif isinstance(error, spack.error.SpecError):
        kind = "spec"
    elif isinstance(error, spack.error.ConfigError):
        kind = "config"
    else:
        kind = "spack"
    return create_error_response(kind, error.message, error.long_message)


def _worker_setup() -> None:
    """Recreate singleton state whose inherited descriptors were closed by the launcher."""
    FILE_TRACKER.discard_after_fork()
    spack.store.reinitialize()
    spack.binary_distribution.reinitialize_binary_index()


def solve_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Run one validated together solve and return structured concrete roots."""
    validated = validate_request(request)
    if validated.strategy != TOGETHER:
        raise ConcretizerWorkerProtocolError(
            "concretizer-worker solve supports only the together strategy"
        )

    # Importing the solver here keeps recipe evaluation on the worker side of a future setup hook.
    from spack.solver.asp import Solver

    try:
        with warnings.catch_warnings(record=True) as caught:
            result = Solver().solve(
                validated.specs, tests=validated.tests, allow_deprecated=validated.allow_deprecated
            )
    except spack.error.SpackError as error:
        return _error_response(error)
    return create_response(result.specs, warnings=[str(item.message) for item in caught])


def _raise_worker_error(response: Any) -> None:
    error = validate_error_response(response)
    if error is None:
        return
    exception_types = {
        "config": ConcretizerWorkerConfigError,
        "spack": ConcretizerWorkerError,
        "spec": spack.error.SpecError,
        "unknown_package": ConcretizerWorkerUnknownPackageError,
        "unsatisfiable": ConcretizerWorkerUnsatisfiableSpecError,
    }
    assert set(exception_types) == set(ERROR_KINDS)
    raise exception_types[error.kind](error.message, error.long_message)


def solve_in_worker(
    specs: Sequence[spack.spec.Spec], *, tests: TestsType = False, allow_deprecated: bool = False
) -> ConcretizerWorkerResponse:
    """Run an unchanged one-shot solve in an unconstrained forked worker.

    The worker inherits ``concretizer:timeout`` settings. The transport adds no competing
    deadline, so solver timeout and partial-answer behavior remain authoritative.
    """
    request = create_request(
        specs, tests=tests, allow_deprecated=allow_deprecated, strategy=TOGETHER
    )
    max_response_bytes = spack.config.CONFIG.get(
        "config:sandbox:concretizer:max_response_bytes",
        spack.util.sandbox.DEFAULT_STREAM_RESPONSE_BYTES,
    )
    response = spack.util.sandbox.run_json_worker_streaming(
        request, solve_request, setup=_worker_setup, max_response_bytes=max_response_bytes
    )
    _raise_worker_error(response)
    return validate_response(response, request)
