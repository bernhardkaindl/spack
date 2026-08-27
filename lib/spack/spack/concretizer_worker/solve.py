# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Launcher-neutral execution of the existing concretizer in a worker."""

import functools
import os
import pathlib
import sysconfig
import warnings
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

import spack.binary_distribution
import spack.caches
import spack.compilers.config
import spack.compilers.libraries
import spack.config
import spack.error
import spack.paths
import spack.platforms
import spack.repo
import spack.sandbox
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

if TYPE_CHECKING:
    from spack.solver.reuse import SpecFiltersFactory


class ConcretizerWorkerError(spack.error.SpackError):
    """A validated Spack solver failure returned by the worker."""


class ConcretizerWorkerConfigError(spack.error.ConfigError):
    """A validated configuration failure returned by the worker."""


class ConcretizerWorkerPackageError(spack.error.PackageError):
    """A validated package failure returned by the worker."""


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
    from spack.solver.asp import InvalidVersionError

    if isinstance(error, spack.repo.UnknownPackageError):
        kind = "unknown_package"
    elif isinstance(error, InvalidVersionError):
        kind = "invalid_version"
    elif isinstance(error, spack.error.UnsatisfiableSpecError):
        kind = "unsatisfiable"
    elif isinstance(error, spack.error.SpecError):
        kind = "spec"
    elif isinstance(error, spack.error.ConfigError):
        kind = "config"
    elif isinstance(error, spack.error.PackageError):
        kind = "package"
    else:
        kind = "spack"
    return create_error_response(kind, error.message, error.long_message)


def _worker_setup(read_roots: List[str], write_roots: List[str]) -> None:
    """Recreate singleton state whose inherited descriptors were closed by the launcher."""
    FILE_TRACKER.discard_after_fork()
    spack.store.reinitialize()
    spack.binary_distribution.reinitialize_binary_index()
    spack.sandbox.restrict_concretizer_worker(read_roots, write_roots)


def _worker_paths() -> Tuple[List[str], List[str]]:
    """Derive worker filesystem capabilities from trusted active Spack state."""
    from spack.solver.asp import ConcretizationCache

    misc_cache = pathlib.Path(spack.caches.misc_cache_location())
    concretization_cache = ConcretizationCache().root
    misc_cache.mkdir(parents=True, exist_ok=True)
    concretization_cache.mkdir(parents=True, exist_ok=True)

    read_roots = [spack.paths.lib_path]
    python_paths = list(sysconfig.get_paths().values())
    read_roots.extend(path for path in python_paths if path and os.path.exists(path))
    for repository in spack.repo.PATH.repos:
        read_roots.append(repository.root)
        if repository.python_path:
            read_roots.append(repository.python_path)
    for scope in spack.config.CONFIG.active_scopes:
        path = getattr(scope, "path", None)
        if path:
            read_roots.append(str(path))

    for root in ("/lib", "/lib64", "/usr/lib", "/usr/lib64", "/etc/ld.so.cache"):
        if os.path.exists(root):
            read_roots.append(root)

    write_roots = [str(misc_cache), str(concretization_cache)]
    return list(dict.fromkeys(read_roots)), list(dict.fromkeys(write_roots))


def _preflight_compiler_properties(
    configured_compilers: List[spack.spec.Spec], local_store_specs: List[spack.spec.Spec]
) -> None:
    """Populate properties for configured and installed compiler candidates."""
    if not spack.platforms.using_libc_compatibility():
        return
    compiler_names = set(spack.compilers.config.supported_compilers())
    installed_compilers = [spec for spec in local_store_specs if spec.name in compiler_names]
    seen_compilers = set()
    for compiler in configured_compilers + installed_compilers:
        dag_hash = compiler.dag_hash()
        if dag_hash in seen_compilers:
            continue
        seen_compilers.add(dag_hash)
        spack.compilers.libraries.CompilerPropertyDetector(compiler).compiler_verbose_output()


def solve_request(
    request: Dict[str, Any], specs_factory: Optional["SpecFiltersFactory"] = None
) -> Dict[str, Any]:
    """Run one validated together solve and return structured concrete roots."""
    validated = validate_request(request)
    if validated.strategy != TOGETHER:
        raise ConcretizerWorkerProtocolError(
            "concretizer-worker solve supports only the together strategy"
        )

    # Importing the solver here keeps recipe evaluation on the worker side of a future setup hook.
    from spack.solver.asp import Solver
    from spack.solver.reuse import use_buildcache_snapshot, use_local_store_snapshot

    try:
        with use_buildcache_snapshot(validated.buildcache_specs), use_local_store_snapshot(
            validated.local_store_specs,
            set(validated.local_external_origin_hashes),
            validated.local_deprecated_for,
        ):
            with warnings.catch_warnings(record=True) as caught:
                result = Solver(specs_factory=specs_factory).solve(
                    validated.specs,
                    tests=validated.tests,
                    allow_deprecated=validated.allow_deprecated,
                )
    except spack.error.SpackError as error:
        return _error_response(error)
    except AssertionError as error:
        return create_error_response("assertion", str(error))
    return create_response(result.specs, warnings=[str(item.message) for item in caught])


def _raise_worker_error(response: Any) -> None:
    error = validate_error_response(response)
    if error is None:
        return
    from spack.solver.asp import InvalidVersionError, UnsatisfiableSpecError

    exception_types = {
        "assertion": AssertionError,
        "config": ConcretizerWorkerConfigError,
        "invalid_version": InvalidVersionError,
        "package": ConcretizerWorkerPackageError,
        "spack": ConcretizerWorkerError,
        "spec": spack.error.SpecError,
        "unknown_package": ConcretizerWorkerUnknownPackageError,
        "unsatisfiable": UnsatisfiableSpecError,
    }
    assert set(exception_types) == set(ERROR_KINDS)
    if error.kind in ("assertion", "invalid_version", "unsatisfiable"):
        raise exception_types[error.kind](error.message)
    raise exception_types[error.kind](error.message, error.long_message)


def solve_in_worker(
    specs: Sequence[spack.spec.Spec],
    *,
    tests: TestsType = False,
    allow_deprecated: bool = False,
    factory: Optional["SpecFiltersFactory"] = None,
) -> ConcretizerWorkerResponse:
    """Run an unchanged one-shot solve in an unconstrained forked worker.

    The worker inherits ``concretizer:timeout`` settings. The transport adds no competing
    deadline, so solver timeout and partial-answer behavior remain authoritative.
    """
    from spack.bootstrap import ensure_clingo_importable_or_raise
    from spack.solver.reuse import buildcache_reuse_enabled, local_store_snapshot

    ensure_clingo_importable_or_raise()
    configured_compilers = spack.compilers.config.all_compilers()
    local_store_specs, local_external_origin_hashes, local_deprecated_for = local_store_snapshot(
        spack.config.CONFIG
    )
    _preflight_compiler_properties(configured_compilers, local_store_specs)
    buildcache_specs = (
        spack.binary_distribution.update_cache_and_get_specs()
        if buildcache_reuse_enabled(spack.config.CONFIG)
        else []
    )
    read_roots, write_roots = _worker_paths()
    request = create_request(
        specs,
        tests=tests,
        allow_deprecated=allow_deprecated,
        strategy=TOGETHER,
        buildcache_specs=buildcache_specs,
        local_store_specs=local_store_specs,
        local_external_origin_hashes=sorted(local_external_origin_hashes),
        local_deprecated_for=local_deprecated_for,
    )
    max_response_bytes = spack.config.CONFIG.get(
        "config:sandbox:concretizer:max_response_bytes",
        spack.util.sandbox.DEFAULT_STREAM_RESPONSE_BYTES,
    )
    response = spack.util.sandbox.run_json_worker_streaming(
        request,
        functools.partial(solve_request, specs_factory=factory),
        setup=functools.partial(_worker_setup, read_roots, write_roots),
        max_response_bytes=max_response_bytes,
    )
    _raise_worker_error(response)
    return validate_response(response, request)
