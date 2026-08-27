# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Contracts for confined concretizer workers."""

from .protocol import (
    PROTOCOL_VERSION,
    SEPARATELY,
    STRATEGIES,
    TOGETHER,
    WHEN_POSSIBLE,
    ConcretizerWorkerProtocolError,
    ConcretizerWorkerResponse,
    create_request,
    create_response,
    validate_request,
    validate_response,
)

__all__ = [
    "PROTOCOL_VERSION",
    "SEPARATELY",
    "STRATEGIES",
    "TOGETHER",
    "WHEN_POSSIBLE",
    "ConcretizerWorkerProtocolError",
    "ConcretizerWorkerResponse",
    "create_request",
    "create_response",
    "validate_request",
    "validate_response",
]
