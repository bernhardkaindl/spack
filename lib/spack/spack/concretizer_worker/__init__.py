# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Contracts for confined concretizer workers."""

from .policy import (
    FALLBACK,
    WORKER,
    ConcretizerWorkerCapabilityError,
    ConcretizerWorkerSelection,
    select_execution,
)
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
    ConcretizerWorkerPackageError,
    ConcretizerWorkerUnknownPackageError,
    ConcretizerWorkerUnsatisfiableSpecError,
    solve_in_worker,
    solve_request,
)

__all__ = [
    "ERROR_KINDS",
    "FALLBACK",
    "PROTOCOL_VERSION",
    "SEPARATELY",
    "STRATEGIES",
    "TOGETHER",
    "WHEN_POSSIBLE",
    "WORKER",
    "ConcretizerWorkerCapabilityError",
    "ConcretizerWorkerConfigError",
    "ConcretizerWorkerError",
    "ConcretizerWorkerErrorResponse",
    "ConcretizerWorkerPackageError",
    "ConcretizerWorkerProtocolError",
    "ConcretizerWorkerResponse",
    "ConcretizerWorkerSelection",
    "ConcretizerWorkerUnknownPackageError",
    "ConcretizerWorkerUnsatisfiableSpecError",
    "create_error_response",
    "create_request",
    "create_response",
    "solve_in_worker",
    "solve_request",
    "select_execution",
    "validate_error_response",
    "validate_request",
    "validate_response",
]
