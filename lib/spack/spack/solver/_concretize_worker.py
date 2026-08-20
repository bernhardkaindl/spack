# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Fresh-exec entry point for experimental sandboxed concretization.

Keep the pre-Landlock import surface in this module limited to the standard library and trusted
Spack sandbox implementation. Recipe repositories are configured and enabled only after the
irreversible Landlock ruleset has been applied.
"""

import contextlib
import json
import os
from pathlib import Path
import resource
import sys
from typing import Any, Dict


PROTOCOL_VERSION = 3
MAX_REQUEST_BYTES = 4 * 1024 * 1024


class WorkerRequestError(Exception):
    pass


def _spack_library_path() -> str:
    return str(Path(__file__).resolve().parents[2])


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkerRequestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_request() -> Dict[str, Any]:
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
    if not isinstance(request, dict):
        raise WorkerRequestError("request must be a JSON object")
    expected = {
        "protocol_version",
        "operation",
        "spec",
        "tests",
        "configuration",
        "repositories",
        "clingo_paths",
        "diagnostic_predicates",
        "platform",
        "state_directory",
    }
    if set(request) != expected:
        raise WorkerRequestError("request has unexpected fields")
    if request["protocol_version"] != PROTOCOL_VERSION:
        raise WorkerRequestError("unsupported protocol version")
    if request["operation"] not in ("concretize_one", "diagnose_concretization"):
        raise WorkerRequestError("unsupported operation")
    if not isinstance(request["spec"], str) or not request["spec"]:
        raise WorkerRequestError("spec must be a non-empty string")
    if not isinstance(request["tests"], (bool, list)):
        raise WorkerRequestError("tests must be a boolean or list")
    if not isinstance(request["configuration"], dict):
        raise WorkerRequestError("configuration must be an object")
    if not isinstance(request["repositories"], list) or not request["repositories"]:
        raise WorkerRequestError("repositories must be a non-empty list")
    if not isinstance(request["clingo_paths"], list):
        raise WorkerRequestError("clingo_paths must be a list")
    diagnostic_predicates = request["diagnostic_predicates"]
    if (
        not isinstance(diagnostic_predicates, list)
        or len(diagnostic_predicates) > 32
        or any(
            not isinstance(name, str)
            or not name
            or len(name) > 128
            or not name.replace("_", "a").isalnum()
            for name in diagnostic_predicates
        )
    ):
        raise WorkerRequestError("diagnostic_predicates must be a bounded list of names")
    if request["operation"] == "concretize_one" and diagnostic_predicates:
        raise WorkerRequestError("diagnostic predicates require the diagnostic operation")
    if not isinstance(request["platform"], str) or not request["platform"]:
        raise WorkerRequestError("platform must be a non-empty string")
    state_directory = Path(request["state_directory"])
    if not state_directory.is_absolute() or not state_directory.is_dir():
        raise WorkerRequestError("state_directory must be an existing absolute directory")
    request["state_directory"] = str(state_directory.resolve(strict=True))
    return request


def _apply_resource_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))


def _apply_sandbox(state_directory: str) -> int:
    sys.path.insert(0, _spack_library_path())
    from spack.sandbox import LandlockSandbox, get_sandbox

    sandbox = get_sandbox()
    if not isinstance(sandbox, LandlockSandbox):
        raise RuntimeError("Landlock sandbox is unavailable")
    sandbox.allow_read("/")
    sandbox.allow_write(state_directory)
    sandbox.apply(restrict_filesystem=True, restrict_network=True)
    return sandbox.abi_version


def _private_configuration(request: Dict[str, Any]):
    import spack.config

    data = request["configuration"]
    state = request["state_directory"]
    config = data.setdefault("config", {})
    config.update(
        {
            "build_stage": [os.path.join(state, "stage")],
            "install_tree": {"root": os.path.join(state, "store")},
            "locks": False,
            "misc_cache": os.path.join(state, "cache"),
            "source_cache": os.path.join(state, "source-cache"),
        }
    )
    data["upstreams"] = {}
    concretizer = data.setdefault("concretizer", {})
    concretizer["reuse"] = False
    concretizer.setdefault("concretization_cache", {})["enable"] = False
    scope = spack.config.InternalConfigScope("sandboxed-concretization", data=data)
    return spack.config.create_from(scope)


def _configure_state(request: Dict[str, Any]):
    for path in request["clingo_paths"]:
        if not isinstance(path, str) or not os.path.isabs(path):
            raise WorkerRequestError("clingo paths must be absolute strings")
        sys.path.insert(0, path)

    import importlib

    importlib.import_module("clingo")
    import spack.caches
    import spack.config
    import spack.repo
    import spack.store
    import spack.util.file_cache
    from spack.solver.repository_snapshot import repository_digest

    spack.config.CONFIG = _private_configuration(request)
    cache = spack.util.file_cache.FileCache(
        os.path.join(request["state_directory"], "cache"), enable_lock=False
    )
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


def _concretize(request: Dict[str, Any]):
    import spack.concretize

    os.environ["SPACK_CONCRETIZER_REQUIRE_CHECKSUM"] = "yes"
    with contextlib.redirect_stdout(sys.stderr):
        spec = spack.concretize.concretize_one(request["spec"], tests=request["tests"])
    spec_data = spec.to_dict()
    package_hashes = [
        {"dag_hash": node["hash"], "package_hash": node["package_hash"]}
        for node in spec_data["spec"]["nodes"]
    ]
    return spec_data, package_hashes


def _diagnose(request: Dict[str, Any]):
    from spack.solver.concretize_diagnostics import concretization_fingerprint

    os.environ["SPACK_CONCRETIZER_REQUIRE_CHECKSUM"] = "yes"
    with contextlib.redirect_stdout(sys.stderr):
        return concretization_fingerprint(
            request["spec"],
            tests=request["tests"],
            excerpt_predicates=request["diagnostic_predicates"],
        )


def _response(ok: bool, **kwargs) -> None:
    response = {"protocol_version": PROTOCOL_VERSION, "ok": ok, **kwargs}
    sys.stdout.write(json.dumps(response, allow_nan=False, separators=(",", ":")))
    sys.stdout.flush()


def main() -> None:
    phase = "validate"
    try:
        request = _read_request()
        _apply_resource_limits()
        phase = "sandbox"
        abi_version = _apply_sandbox(request["state_directory"])
        phase = "prepare"
        import spack.platforms

        platform = spack.platforms.by_name(request["platform"])
        if platform is None:
            raise WorkerRequestError(f"unsupported platform: {request['platform']}")
        with spack.platforms.use_platform(platform):
            repositories = _configure_state(request)
            sandbox = {
                "backend": "landlock",
                "abi_version": abi_version,
                "filesystem_restricted": True,
                "tcp_restricted": True,
            }
            if request["operation"] == "diagnose_concretization":
                phase = "diagnose"
                diagnostics = _diagnose(request)
                phase = "serialize"
                _response(
                    True,
                    sandbox=sandbox,
                    repositories=repositories,
                    diagnostics=diagnostics,
                )
            else:
                phase = "concretize"
                spec, package_hashes = _concretize(request)
                phase = "serialize"
                _response(
                    True,
                    sandbox=sandbox,
                    repositories=repositories,
                    package_hashes=package_hashes,
                    spec=spec,
                )
    except BaseException as error:
        _response(
            False,
            error={"phase": phase, "type": type(error).__name__, "message": str(error)},
        )


if __name__ == "__main__":
    main()