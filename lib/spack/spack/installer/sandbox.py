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
from typing import Set

import spack.error
import spack.paths
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
)
#: Host paths required by selected system compilers.
HOST_COMPILER_READ_PATHS = ("/usr/include",)
#: Language virtuals whose concrete edges identify selected compiler drivers.
COMPILER_LANGUAGES = ("c", "cxx", "fortran")


def allow_compiler_paths(sandbox: spack.sandbox.Sandbox, spec: spack.spec.Spec) -> None:
    """Allow compiler paths selected by concrete language edges in a build DAG.

    Args:
        sandbox: Sandbox ruleset being prepared for the build.
        spec: Concrete root spec whose build dependency graph selects the compilers.
    """
    compiler_paths: Set[str] = set()
    for node in spec.traverse():
        for edge in node.edges_to_dependencies():
            selected_languages = set(edge.virtuals) & set(COMPILER_LANGUAGES)
            if not selected_languages:
                continue

            configured_compilers = (edge.spec.extra_attributes or {}).get("compilers", {})
            compiler_paths.update(
                configured_compilers[language]
                for language in selected_languages
                if configured_compilers.get(language)
            )

    for compiler_path in compiler_paths:
        sandbox.allow_read(compiler_path)
        real_compiler_path = Path(compiler_path).resolve()
        if real_compiler_path.exists() and str(real_compiler_path).startswith("/usr/"):
            for path in HOST_COMPILER_READ_PATHS:
                sandbox.allow_read(path)


def enable(config: dict, spec: spack.spec.Spec, stage_path: str) -> None:
    """Apply filesystem and network restrictions for a package's build phases.

    Args:
        config: Values from ``config:sandbox``.
        spec: Concrete package spec being built.
        stage_path: Package stage that remains writable during the build.
    """
    if not config.get("enable", False):
        return

    try:
        sandbox = spack.sandbox.get_sandbox()
    except spack.sandbox.SandboxError as error:
        raise spack.error.InstallError(f"Cannot enable build sandbox: {error}") from error

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
    sandbox.allow_read(spack.paths.prefix)
    sandbox.allow_read(spec.package.package_dir)
    sandbox.allow_read(os.path.join(spack.store.STORE.unpadded_root, "bin", "sbang"))
    for upstream_db in spack.store.STORE.upstreams or []:
        sandbox.allow_read(os.path.join(upstream_db.root, "bin", "sbang"))

    for path in HOST_RUNTIME_READ_PATHS:
        sandbox.allow_read(path)
    sandbox.allow_read(Path("/bin/sh"))
    allow_compiler_paths(sandbox, spec)

    for path in config.get("allow_read", []):
        sandbox.allow_read(path)
    for path in config.get("allow_write", []):
        sandbox.allow_write(path)

    try:
        sandbox.apply(block_network=not config.get("allow_network", True))
    except spack.sandbox.SandboxError as error:
        raise spack.error.InstallError(f"Cannot enable build sandbox: {error}") from error
