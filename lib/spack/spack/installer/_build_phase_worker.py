# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Fresh-exec entry point for experimental prepared-stage build phases."""

import json
import os
from pathlib import Path
import re
import resource
from types import SimpleNamespace
import sys
from typing import Any, Dict


PROTOCOL_VERSION = 3
MAX_PHASES = 32
MAX_REQUEST_BYTES = 4 * 1024 * 1024
_PHASE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}")


class WorkerRequestError(Exception):
    """Raised when the fresh worker receives an invalid request."""

    pass


def _spack_library_path() -> str:
    """Return the trusted Spack library root needed after isolated startup."""
    return str(Path(__file__).resolve().parents[2])


def _reject_duplicate_keys(pairs):
    """Build a JSON object while rejecting ambiguous duplicate keys."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkerRequestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_request() -> Dict[str, Any]:
    """Read and validate one bounded build-phase request from standard input."""
    data = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(data) > MAX_REQUEST_BYTES:
        raise WorkerRequestError("request is too large")
    try:
        request = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(WorkerRequestError(value)),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise WorkerRequestError(f"invalid request JSON: {error}") from error
    expected = {
        "protocol_version",
        "spec",
        "source_plan",
        "source_plan_sha256",
        "prepared_stage",
        "prepared_stage_sha256",
        "prefix",
        "phases",
        "repositories",
        "platform",
        "state_directory",
    }
    if not isinstance(request, dict) or set(request) != expected:
        raise WorkerRequestError("request has unexpected fields")
    if request["protocol_version"] != PROTOCOL_VERSION:
        raise WorkerRequestError("unsupported protocol version")
    if not isinstance(request["spec"], dict) or not isinstance(request["source_plan"], dict):
        raise WorkerRequestError("spec and source plan must be objects")
    for key in ("source_plan_sha256", "prepared_stage_sha256"):
        if (
            not isinstance(request[key], str)
            or re.fullmatch(r"[0-9a-f]{64}", request[key]) is None
        ):
            raise WorkerRequestError(f"invalid {key}")
    phases = request["phases"]
    if (
        not isinstance(phases, list)
        or not phases
        or len(phases) > MAX_PHASES
        or len(set(phases)) != len(phases)
        or any(not isinstance(phase, str) or _PHASE.fullmatch(phase) is None for phase in phases)
    ):
        raise WorkerRequestError("invalid phase list")
    if not isinstance(request["repositories"], list) or not request["repositories"]:
        raise WorkerRequestError("repositories must be a non-empty list")
    if not isinstance(request["platform"], str) or not request["platform"]:
        raise WorkerRequestError("platform must be a non-empty string")
    for key in ("prepared_stage", "prefix", "state_directory"):
        path = Path(request[key])
        if not path.is_absolute() or not path.is_dir():
            raise WorkerRequestError(f"{key} must be an existing absolute directory")
        request[key] = str(path.resolve(strict=True))
    return request


def _apply_resource_limits() -> None:
    """Apply conservative CPU, descriptor, and output-file limits."""
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))


def _apply_sandbox(request: Dict[str, Any]) -> int:
    """Apply Landlock before enabling repositories or importing recipe code."""
    sys.path.insert(0, _spack_library_path())
    from spack.sandbox import LandlockSandbox, get_sandbox

    sandbox = get_sandbox()
    if not isinstance(sandbox, LandlockSandbox):
        raise RuntimeError("Landlock sandbox is unavailable")
    sandbox.allow_read("/")
    sandbox.allow_write(request["prepared_stage"])
    sandbox.allow_write(request["prefix"])
    sandbox.allow_write(request["state_directory"])
    sandbox.apply(restrict_filesystem=True, restrict_network=True)
    return sandbox.abi_version


def _configure_state(request: Dict[str, Any]):
    """Install private configuration, cache, store, and verified repositories."""
    import spack.caches
    import spack.config
    import spack.repo
    import spack.store
    import spack.util.file_cache
    from spack.solver.repository_snapshot import repository_digest

    state = request["state_directory"]
    scope = spack.config.InternalConfigScope(
        "sandboxed-build-phase",
        data={
            "config": {
                "build_stage": [os.path.join(state, "stage")],
                "install_tree": {"root": os.path.join(state, "store")},
                "locks": False,
                "misc_cache": os.path.join(state, "cache"),
            },
            "upstreams": {},
        },
    )
    spack.config.CONFIG = spack.config.create_from(scope)
    cache = spack.util.file_cache.FileCache(os.path.join(state, "cache"), enable_lock=False)
    spack.caches.MISC_CACHE = cache

    repositories = []
    identities = []
    for description in request["repositories"]:
        if not isinstance(description, dict) or set(description) != {
            "root",
            "namespace",
            "package_api",
            "identity",
        }:
            raise WorkerRequestError("invalid repository description")
        root = Path(description["root"])
        if not root.is_absolute():
            raise WorkerRequestError("repository roots must be absolute")
        root = root.resolve(strict=True)
        if repository_digest(root) != description["identity"]:
            raise WorkerRequestError("repository identity mismatch")
        repository = spack.repo.Repo(str(root), cache=cache)
        if repository.namespace != description["namespace"]:
            raise WorkerRequestError("repository namespace changed after validation")
        if list(repository.package_api) != description["package_api"]:
            raise WorkerRequestError("repository API changed after validation")
        repositories.append(repository)
        identities.append(
            {
                "namespace": repository.namespace,
                "package_api": list(repository.package_api),
                "identity": description["identity"],
            }
        )
    spack.repo.enable_repo(spack.repo.RepoPath(*repositories))
    spack.store.reinitialize()
    return identities


def _run_phases(request: Dict[str, Any], repositories):
    """Verify build provenance and execute declared phases under confinement."""
    import spack.build_environment
    import spack.builder
    import spack.platforms
    from spack.solver.prepared_stage import prepared_stage_digest, source_plan_digest
    from spack.solver.source_plan import validate_source_plan
    from spack.spec import Spec

    spec = Spec.from_dict(request["spec"])
    if not spec.concrete:
        raise WorkerRequestError("build Spec is not concrete")
    root = request["spec"]["spec"]["nodes"][0]
    expected_provenance = {
        "dag_hash": root["hash"],
        "package_hash": root["package_hash"],
        "repositories": repositories,
    }
    validate_source_plan(request["source_plan"], expected_provenance=expected_provenance)
    if source_plan_digest(request["source_plan"]) != request["source_plan_sha256"]:
        raise WorkerRequestError("source plan identity mismatch")
    initial_stage_sha256 = prepared_stage_digest(Path(request["prepared_stage"]))
    if initial_stage_sha256 != request["prepared_stage_sha256"]:
        raise WorkerRequestError("prepared stage identity mismatch")

    platform = spack.platforms.by_name(request["platform"])
    if platform is None or platform.name != spec.architecture.platform:
        raise WorkerRequestError("build platform mismatch")
    spec.set_prefix(request["prefix"])
    package = spec.package
    if package.content_hash() != root["package_hash"]:
        raise WorkerRequestError("package hash does not match the verified repository")
    package.stage = SimpleNamespace(
        path=request["prepared_stage"], source_path=request["prepared_stage"]
    )
    spack.build_environment.setup_package(package, dirty=False)
    builder = spack.builder.create(package)
    phases = {phase.name: phase for phase in builder}
    selected = []
    for phase_name in request["phases"]:
        phase = phases.get(phase_name)
        if phase is None:
            raise WorkerRequestError(f"package does not declare phase: {phase_name}")
        selected.append(phase)
    os.chdir(request["prepared_stage"])
    for phase in selected:
        phase.execute()
    return {
        "phases": request["phases"],
        "dag_hash": root["hash"],
        "package_hash": root["package_hash"],
        "source_plan_sha256": request["source_plan_sha256"],
        "initial_stage_sha256": initial_stage_sha256,
        "final_stage_sha256": prepared_stage_digest(Path(request["prepared_stage"])),
    }


def _response(response_fd: int, ok: bool, **kwargs) -> None:
    """Write one compact protocol response to the dedicated parent descriptor."""
    response = {"protocol_version": PROTOCOL_VERSION, "ok": ok, **kwargs}
    data = json.dumps(response, allow_nan=False, separators=(",", ":")).encode("utf-8")
    while data:
        written = os.write(response_fd, data)
        if written == 0:
            raise RuntimeError("cannot write worker response")
        data = data[written:]


def main() -> None:
    """Run request validation, confinement, verification, and phase execution."""
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit("usage: _build_phase_worker.py RESPONSE_FD")
    response_fd = int(sys.argv[1])
    if response_fd < 3:
        raise SystemExit("response descriptor must not be standard input or output")
    os.set_inheritable(response_fd, False)
    phase = "validate"
    try:
        request = _read_request()
        _apply_resource_limits()
        phase = "sandbox"
        abi_version = _apply_sandbox(request)
        phase = "verify"
        repositories = _configure_state(request)
        phase = "build"
        result = _run_phases(request, repositories)
        phase = "serialize"
        _response(
            response_fd,
            True,
            sandbox={
                "backend": "landlock",
                "abi_version": abi_version,
                "filesystem_restricted": True,
                "tcp_restricted": True,
            },
            repositories=repositories,
            **result,
        )
    except BaseException as error:
        _response(
            response_fd,
            False,
            error={"phase": phase, "type": type(error).__name__, "message": str(error)},
        )


if __name__ == "__main__":
    main()
