# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Build sandbox policy for the new installer.

This module translates a concrete build spec and ``config:sandbox`` settings into rules for the
platform sandbox implementation in :mod:`spack.sandbox`.
"""

import os
import tempfile
from pathlib import Path

import spack.error
import spack.sandbox
import spack.spec
import spack.store

#: Host paths required by dynamically linked build tools at runtime.
HOST_RUNTIME_READ_PATHS = (
    "/lib",
    "/lib64",
    "/usr/lib",
    "/usr/lib64",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
    "/proc/cpuinfo",
    "/etc/debian_version",
    "/etc/mime.types",
    "/etc/nsswitch.conf",
    "/etc/pki",
    "/etc/resolv.conf",
    "/etc/ssl",
)


def enable(config: dict, spec: spack.spec.Spec, stage_path: str) -> None:
    """Apply filesystem and network restrictions for a package's build phases.

    Args:
        config: Values from ``config:sandbox``.
        spec: Concrete package spec being built.
        stage_path: Package stage that remains writable during the build.
    """
    if not config.get("enable", False):
        return

    restrict_filesystem = config.get("restrict_filesystem", True)
    restrict_network = spack.sandbox.network_restriction_enabled(config)
    if not restrict_filesystem and not restrict_network:
        return

    try:
        sandbox = spack.sandbox.get_sandbox()
    except spack.sandbox.SandboxError as error:
        raise spack.error.InstallError(f"Cannot enable build sandbox: {error}") from error

    if restrict_filesystem:
        for dependency in spec.traverse(root=False):
            if not dependency.external:
                sandbox.allow_read(dependency.prefix)

        sandbox.allow_write(stage_path)
        sandbox.allow_write(spec.prefix)

        # POSIX prescribes /tmp and /dev/null are present. In the future we can consider setting
        # TMPPATH to a sibling of the stage path to isolate concurrent builds better.
        sandbox.allow_write(tempfile.gettempdir())
        sandbox.allow_write(os.devnull)

        # Allow read access to sbang, which might be needed to run build scripts.
        sandbox.allow_read(os.path.join(spack.store.STORE.unpadded_root, "bin", "sbang"))
        for upstream_db in spack.store.STORE.upstreams or []:
            sandbox.allow_read(os.path.join(upstream_db.root, "bin", "sbang"))

        for path in HOST_RUNTIME_READ_PATHS:
            sandbox.allow_read(path)
        sandbox.allow_read(Path("/bin/sh"))

        for path in config.get("allow_read", []):
            sandbox.allow_read(path)
        for path in config.get("allow_write", []):
            sandbox.allow_write(path)

    try:
        sandbox.apply(restrict_filesystem=restrict_filesystem, restrict_network=restrict_network)
    except spack.sandbox.SandboxError as error:
        raise spack.error.InstallError(f"Cannot enable build sandbox: {error}") from error
