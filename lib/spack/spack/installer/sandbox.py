# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Build sandbox policy for the new installer.

This module translates a concrete build spec and ``config:sandbox`` settings into rules for the
platform sandbox implementation in :mod:`spack.sandbox`.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

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
    "/etc/group",
    "/etc/hosts",
    "/etc/magic",
    "/etc/mime.types",
    "/etc/nsswitch.conf",
    "/etc/passwd",
    "/etc/pki",
    "/etc/resolv.conf",
    "/etc/ssl",
)
#: Host paths required by runtime facilities that create kernel-backed objects.
#: POSIX prescribes /dev/shm is present and writable for shared memory objects.
#: If not available, e.g. Python's multiprocessing is compiled without SemLock
#: support, which causes failures in programs requiring multiprocessing.SemLock.
HOST_RUNTIME_WRITE_PATHS = ("/dev/shm",)
#: Host paths required by selected system compilers.
HOST_COMPILER_READ_PATHS = ("/usr/include",)
#: Language virtuals whose concrete edges identify selected compiler drivers.
COMPILER_LANGUAGES = ("c", "cxx", "fortran")
#: Conventional executable names redirected to the selected language driver.
COMPILER_ALIASES = {"c": ("cc",), "cxx": ("c++", "g++", "clang++"), "fortran": ("gfortran",)}


def selected_compiler_paths(spec: spack.spec.Spec) -> Dict[str, str]:
    """Return the first selected compiler driver for each language in a concrete build DAG."""
    compiler_paths = {}
    for node in spec.traverse():
        for edge in node.edges_to_dependencies():
            configured_compilers = (edge.spec.extra_attributes or {}).get("compilers", {})
            selected_languages = set(edge.virtuals) & set(COMPILER_LANGUAGES)
            if not selected_languages and not edge.virtuals:
                selected_languages = set(configured_compilers)
            for language in selected_languages:
                if configured_compilers.get(language):
                    compiler_paths.setdefault(language, configured_compilers[language])
    return compiler_paths


def prepend_compiler_aliases(spec: spack.spec.Spec, stage_path: str) -> Optional[str]:
    """Prepend stage-local aliases for the selected language compiler drivers."""
    compiler_paths = selected_compiler_paths(spec)
    if not any(language in compiler_paths for language in COMPILER_ALIASES):
        return None

    alias_dir = tempfile.mkdtemp(prefix=".spack-sandbox-bin-", dir=stage_path)
    for language, aliases in COMPILER_ALIASES.items():
        compiler_path = compiler_paths.get(language)
        if compiler_path:
            for alias in aliases:
                os.symlink(compiler_path, os.path.join(alias_dir, alias))
    os.environ["PATH"] = os.pathsep.join((alias_dir, os.environ.get("PATH", "")))
    return alias_dir


#: Subordinate executables that compiler drivers may invoke.
COMPILER_PROGS = ("cc1", "cc1plus", "f951", "collect2", "lto1", "lto-wrapper", "cpp")
BINUTILS_PROGS = ("as", "ld", "ar", "ranlib", "strip", "nm")
STAGING_PROGS = ("tar", "gzip", "bzip2", "xz", "unzip")
COREUTILS_INSTALL_PROGS = ("cat", "chmod", "cp", "install", "ln", "mkdir", "mv", "rm", "rmdir")
FILE_PROGS = ("comm", "cut", "head", "join", "ls", "paste", "split", "tail", "touch", "wc")
COREUTILS_SYS = ("arch", "date", "env", "hostname", "id", "pwd", "sleep", "uname", "ldd")
COREUTILS_TOOLS = ("basename", "dirname", "echo", "expr", "realpath", "sort", "tr", "uniq")
COMMON_UTILS = ("cmp", "diff", "file", "find", "mktemp", "git", "md5sum", "true")
COMMON_TOOLS = ("egrep", "grep", "hexdump", "od", "tbl", "which", "xargs")
SCRIPT_INTERPRETERS = ("awk", "gawk", "mawk", "nawk", "bash", "perl", "sed")
BUILD_TOOLS = (
    COMPILER_PROGS
    + STAGING_PROGS
    + BINUTILS_PROGS
    + COREUTILS_INSTALL_PROGS
    + FILE_PROGS
    + COREUTILS_SYS
    + COREUTILS_TOOLS
    + SCRIPT_INTERPRETERS
    + COMMON_UTILS
    + COMMON_TOOLS
)
#: Support files that compiler drivers may pass to subordinate tools.
COMPILER_FILES = ("liblto_plugin.so",)


def compiler_support_paths(compiler_path: str) -> List[str]:
    """Return support programs and files reported by a selected compiler driver.

    Args:
        compiler_path: Absolute path to the configured compiler driver.

    Returns:
        Absolute paths to compiler frontends, assembler/linker programs, and plugins that exist
        outside the compiler driver's own sandbox rule.
    """
    result = []
    for program in BUILD_TOOLS:
        try:
            completed = subprocess.run(
                [compiler_path, f"-print-prog-name={program}"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError:
            continue

        if completed.returncode != 0:
            continue
        reported_path = completed.stdout.strip()
        if not reported_path or reported_path == program:
            for search_path in (None, os.defpath):
                resolved = shutil.which(program, path=search_path)
                if resolved:
                    result.append(resolved)
            continue
        if os.path.isabs(reported_path):
            result.append(reported_path)

    for filename in COMPILER_FILES:
        try:
            completed = subprocess.run(
                [compiler_path, f"-print-file-name={filename}"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError:
            continue

        reported_path = completed.stdout.strip()
        if (
            completed.returncode == 0
            and reported_path != filename
            and os.path.isabs(reported_path)
        ):
            result.append(reported_path)
    return result


def allow_compiler_paths(sandbox: spack.sandbox.Sandbox, spec: spack.spec.Spec) -> None:
    """Allow compiler paths selected by concrete language edges in a build DAG.

    Args:
        sandbox: Sandbox ruleset being prepared for the build.
        spec: Concrete root spec whose build dependency graph selects the compilers.
    """
    for compiler_path in set(selected_compiler_paths(spec).values()):
        sandbox.allow_read(compiler_path)
        for program_path in compiler_support_paths(compiler_path):
            sandbox.allow_read(program_path)
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

    restrict_filesystem = config.get("restrict_filesystem", True)
    restrict_network = spack.sandbox.network_restriction_enabled(config)
    if not restrict_filesystem and not restrict_network:
        return

    try:
        sandbox = spack.sandbox.get_sandbox()
    except spack.sandbox.SandboxError as error:
        raise spack.error.InstallError(f"Cannot enable build sandbox: {error}") from error

    if restrict_filesystem:
        prepend_compiler_aliases(spec, stage_path)
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
        for path in HOST_RUNTIME_WRITE_PATHS:
            sandbox.allow_write(path)
        sandbox.allow_read(Path("/bin/sh"))
        allow_compiler_paths(sandbox, spec)

        for path in config.get("allow_read", []):
            sandbox.allow_read(path)
        for path in config.get("allow_write", []):
            sandbox.allow_write(path)

    try:
        sandbox.apply(restrict_filesystem=restrict_filesystem, restrict_network=restrict_network)
    except spack.sandbox.SandboxError as error:
        raise spack.error.InstallError(f"Cannot enable build sandbox: {error}") from error
