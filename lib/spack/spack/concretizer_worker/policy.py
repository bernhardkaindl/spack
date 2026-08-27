# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Automatic capability selection for concretizer workers."""

from typing import NamedTuple, Optional

import spack.sandbox

WORKER = "worker"
FALLBACK = "fallback"


class ConcretizerWorkerSelection(NamedTuple):
    """Selected execution mode and an optional fallback diagnostic."""

    mode: str
    diagnostic: Optional[str]


class ConcretizerWorkerCapabilityError(spack.sandbox.SandboxError):
    """Required concretizer-worker confinement is unavailable."""


def _fallback_or_raise(diagnostic: str) -> ConcretizerWorkerSelection:
    if spack.sandbox.sandbox_fallback_allowed():
        return ConcretizerWorkerSelection(FALLBACK, diagnostic)
    raise ConcretizerWorkerCapabilityError(
        "Concretizer worker capability probe {0}; config:sandbox:allow_fallback is false".format(
            diagnostic
        )
    )


def select_execution() -> ConcretizerWorkerSelection:
    """Select confined worker execution or the configured trusted fallback."""
    try:
        available = spack.sandbox.recipe_import_sandbox_available()
    except spack.sandbox.SandboxError as error:
        return _fallback_or_raise(
            "spack.sandbox.recipe_import_sandbox_available() failed: {0}".format(error)
        )
    if available:
        return ConcretizerWorkerSelection(WORKER, None)
    return _fallback_or_raise("spack.sandbox.recipe_import_sandbox_available() returned False")
