# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Contracts for confined concretizer workers."""

from .protocol import (
    ERROR_KINDS,
    PROTOCOL_VERSION,
    SEPARATELY,
    STRATEGIES,
    TOGETHER,
    WHEN_POSSIBLE,
    ConcretizerWorkerErrorResponse,
    ConcretizerWorkerProtocolError,
    ConcretizerWorkerResponse,
    create_error_response,
    create_request,
    create_response,
    validate_error_response,
    validate_request,
    validate_response,
)
from .solve import (
    ConcretizerWorkerConfigError,
    ConcretizerWorkerError,
    ConcretizerWorkerUnknownPackageError,
    ConcretizerWorkerUnsatisfiableSpecError,
    solve_in_worker,
    solve_request,
)

__all__ = [
    "ERROR_KINDS",
    "PROTOCOL_VERSION",
    "SEPARATELY",
    "STRATEGIES",
    "TOGETHER",
    "WHEN_POSSIBLE",
    "ConcretizerWorkerConfigError",
    "ConcretizerWorkerError",
    "ConcretizerWorkerErrorResponse",
    "ConcretizerWorkerProtocolError",
    "ConcretizerWorkerResponse",
    "ConcretizerWorkerUnknownPackageError",
    "ConcretizerWorkerUnsatisfiableSpecError",
    "create_error_response",
    "create_request",
    "create_response",
    "solve_in_worker",
    "solve_request",
    "validate_error_response",
    "validate_request",
    "validate_response",
]
