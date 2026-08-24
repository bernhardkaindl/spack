# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Automatic capability selection for install workers."""

from typing import NamedTuple, Optional

import spack.sandbox

WORKER = "worker"
FALLBACK = "fallback"


class InstallWorkerSelection(NamedTuple):
    """Selected execution mode and an optional fallback diagnostic."""

    mode: str
    diagnostic: Optional[str]


class InstallWorkerCapabilityError(spack.sandbox.SandboxError):
    """Required install-worker confinement is unavailable."""


def _fallback_or_raise(diagnostic: str) -> InstallWorkerSelection:
    if spack.sandbox.sandbox_fallback_allowed():
        return InstallWorkerSelection(FALLBACK, diagnostic)
    raise InstallWorkerCapabilityError(
        "Install worker capability probe {0}; config:sandbox:allow_fallback is false".format(
            diagnostic
        )
    )


def select_execution() -> InstallWorkerSelection:
    """Select confined worker execution or the configured trusted fallback."""
    try:
        recipe_confinement = spack.sandbox.recipe_import_sandbox_available()
    except spack.sandbox.SandboxError as error:
        return _fallback_or_raise(
            "spack.sandbox.recipe_import_sandbox_available() failed: {0}".format(error)
        )

    if not recipe_confinement:
        return _fallback_or_raise("spack.sandbox.recipe_import_sandbox_available() returned False")

    try:
        network_supervision = spack.sandbox.network_supervision_available()
    except Exception as error:
        return _fallback_or_raise(
            "spack.sandbox.network_supervision_available() failed: {0}: {1}".format(
                type(error).__name__, error
            )
        )
    if network_supervision:
        return InstallWorkerSelection(WORKER, None)
    return _fallback_or_raise("spack.sandbox.network_supervision_available() returned False")
