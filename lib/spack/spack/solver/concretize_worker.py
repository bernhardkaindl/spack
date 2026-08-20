# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Experimental launcher for concretizing recipes in a disposable sandbox.

This proof of concept confines complete single-spec concretization to a fresh Linux process. It
uses Landlock to deny direct file-content and filesystem-topology writes outside private worker
state and, on ABI v4 or newer, TCP bind/connect operations. Requests and results use bounded JSON;
the parent never unpickles worker-controlled data.

This is not complete malicious-code isolation. The worker can read files available to the invoking
user, and Landlock does not restrict every metadata operation, UDP, Unix sockets and other IPC,
signals, or all process behavior. Package hashes are produced by the worker and are not
independently attested until a later repository-provenance protocol is implemented.
"""

import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Union

import spack.config
import spack.error
import spack.hash_types as ht
import spack.repo
from spack.active_environment import active_environment
from spack.solver.repository_snapshot import (
    RepositorySnapshotError,
    create_repository_snapshot,
    repository_digest,
    snapshot_root,
)
from spack.spec import Spec
from spack.version.common import is_git_version


PROTOCOL_VERSION = 2
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_STDERR_BYTES = 2 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_ITEMS = 50000
MAX_JSON_STRING = 65536


class SandboxedConcretizationError(spack.error.SpackError):
    """Raised when the sandboxed concretization worker fails."""


def _json_bytes(value: Dict[str, Any], limit: int) -> bytes:
    try:
        result = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise SandboxedConcretizationError(f"request is not valid JSON data: {error}") from error
    if len(result) > limit:
        raise SandboxedConcretizationError(f"JSON message exceeds {limit} bytes")
    return result


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_response(data: bytes) -> Dict[str, Any]:
    if len(data) > MAX_RESPONSE_BYTES:
        raise SandboxedConcretizationError("worker response is too large")
    try:
        result = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise SandboxedConcretizationError(f"invalid worker response: {error}") from error
    if not isinstance(result, dict):
        raise SandboxedConcretizationError("worker response must be a JSON object")
    _validate_json_value(result)
    return result


def _validate_json_value(value: Any, depth: int = 0) -> int:
    if depth > MAX_JSON_DEPTH:
        raise SandboxedConcretizationError("worker response exceeds maximum JSON depth")
    if isinstance(value, str):
        if len(value) > MAX_JSON_STRING:
            raise SandboxedConcretizationError("worker response contains an oversized string")
        return 1
    if value is None or isinstance(value, (bool, int, float)):
        return 1
    if isinstance(value, list):
        count = 1 + sum(_validate_json_value(item, depth + 1) for item in value)
    elif isinstance(value, dict):
        count = 1 + sum(
            _validate_json_value(key, depth + 1) + _validate_json_value(item, depth + 1)
            for key, item in value.items()
        )
    else:
        raise SandboxedConcretizationError("worker response contains a non-JSON value")
    if count > MAX_JSON_ITEMS:
        raise SandboxedConcretizationError("worker response contains too many JSON values")
    return count


def _configuration_payload() -> Dict[str, Any]:
    result = {}
    for section in ("config", "concretizer", "packages", "compilers"):
        value = spack.config.CONFIG.deepcopy_as_builtin(section)
        if value:
            result[section] = value
    return result


def _repository_payload(
    repositories: List[Union[str, spack.repo.Repo]], snapshot_base: Path
) -> List[Dict[str, Any]]:
    result = []
    for index, repository in enumerate(repositories):
        repo = (
            repository
            if isinstance(repository, spack.repo.Repo)
            else spack.repo.from_path(repository)
        )
        source = Path(repo.root).resolve(strict=True)
        root = snapshot_root(snapshot_base, index, repo.namespace, repo.package_api)
        try:
            identity = create_repository_snapshot(source, root)
        except RepositorySnapshotError as error:
            raise SandboxedConcretizationError(
                f"cannot snapshot repository {repo.namespace}: {error}"
            ) from error
        result.append(
            {
                "root": str(root),
                "namespace": repo.namespace,
                "package_api": list(repo.package_api),
                "identity": identity,
            }
        )
    if not result:
        raise SandboxedConcretizationError("at least one local repository is required")
    return result


def _clingo_paths() -> List[str]:
    module = importlib.util.find_spec("clingo")
    if module is None:
        from spack.bootstrap import (
            ensure_bootstrap_configuration,
            ensure_clingo_importable_or_raise,
        )

        with ensure_bootstrap_configuration():
            ensure_clingo_importable_or_raise()
        module = importlib.util.find_spec("clingo")
    if module is None or module.origin is None:
        raise SandboxedConcretizationError("clingo must be importable before launching the worker")
    origin = Path(module.origin).resolve()
    return [str(origin.parent.parent if origin.name == "__init__.py" else origin.parent)]


def _worker_command() -> List[str]:
    worker = Path(__file__).with_name("_concretize_worker.py")
    return [sys.executable, "-I", "-S", "-B", str(worker)]


def _sanitized_environment(state_directory: str) -> Dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH", "LD_PRELOAD", "SPACK_ENV"}
    }
    environment.update(
        {
            "HOME": state_directory,
            "TMPDIR": os.path.join(state_directory, "tmp"),
            "SPACK_USER_CACHE_PATH": os.path.join(state_directory, "cache"),
            "SPACK_USER_CONFIG_PATH": os.path.join(state_directory, "config"),
        }
    )
    return environment


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.kill()
    process.wait()


def _validate_spec_payload(spec_data: Any) -> None:
    if not isinstance(spec_data, dict) or set(spec_data) != {"spec"}:
        raise SandboxedConcretizationError("worker returned an invalid spec payload")
    body = spec_data["spec"]
    if not isinstance(body, dict) or set(body) != {"_meta", "nodes"}:
        raise SandboxedConcretizationError("worker returned an invalid spec document")
    if body["_meta"] != {"version": 5}:
        raise SandboxedConcretizationError("worker returned an unsupported spec format")
    nodes = body["nodes"]
    if not isinstance(nodes, list) or not nodes or len(nodes) > 10000:
        raise SandboxedConcretizationError("worker returned an invalid spec graph")
    hashes = set()
    edge_count = 0
    for node in nodes:
        if (
            not isinstance(node, dict)
            or not isinstance(node.get("name"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", node["name"])
        ):
            raise SandboxedConcretizationError("worker returned an invalid spec node")
        dag_hash = node.get("hash")
        if not isinstance(dag_hash, str) or not re.fullmatch(r"[a-z2-7]{32}", dag_hash):
            raise SandboxedConcretizationError("worker returned an invalid DAG hash")
        package_hash = node.get("package_hash")
        if not isinstance(package_hash, str) or not re.fullmatch(
            r"[a-z2-7]{52}={4}", package_hash
        ):
            raise SandboxedConcretizationError("worker returned an invalid package hash")
        if dag_hash in hashes:
            raise SandboxedConcretizationError("worker returned duplicate DAG hashes")
        hashes.add(dag_hash)
        if node.get("external"):
            raise SandboxedConcretizationError("external specs are unsupported by this PoC")
        version = node.get("version")
        if isinstance(version, str) and is_git_version(version):
            raise SandboxedConcretizationError("Git versions are unsupported by this PoC")
        parameters = node.get("parameters", {})
        if not isinstance(parameters, dict) or "dev_path" in parameters:
            raise SandboxedConcretizationError("develop specs are unsupported by this PoC")
        dependencies = node.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise SandboxedConcretizationError("worker returned invalid dependencies")
        edge_count += len(dependencies)
    if edge_count > 50000:
        raise SandboxedConcretizationError("worker returned too many dependency edges")

    incoming = {dag_hash: 0 for dag_hash in hashes}
    graph = {node["hash"]: [] for node in nodes}
    for node in nodes:
        for dependency in node.get("dependencies", []):
            if not isinstance(dependency, dict) or not isinstance(dependency.get("hash"), str):
                raise SandboxedConcretizationError("worker returned an invalid dependency edge")
            dependency_hash = dependency["hash"]
            if dependency_hash not in hashes:
                raise SandboxedConcretizationError("worker returned a dangling dependency edge")
            graph[node["hash"]].append(dependency_hash)
            incoming[dependency_hash] += 1
    roots = [dag_hash for dag_hash, count in incoming.items() if count == 0]
    if roots != [nodes[0]["hash"]]:
        raise SandboxedConcretizationError("worker returned a disconnected spec graph")
    visited = set()
    active = set()

    def visit(dag_hash):
        if dag_hash in active:
            raise SandboxedConcretizationError("worker returned a cyclic spec graph")
        if dag_hash in visited:
            return
        active.add(dag_hash)
        for dependency_hash in graph[dag_hash]:
            visit(dependency_hash)
        active.remove(dag_hash)
        visited.add(dag_hash)

    visit(roots[0])
    if visited != hashes:
        raise SandboxedConcretizationError("worker returned a disconnected spec graph")


def _validate_success(
    response: Dict[str, Any], requested: Spec, repositories: List[Dict[str, Any]]
) -> Spec:
    if response.get("protocol_version") != PROTOCOL_VERSION:
        raise SandboxedConcretizationError("worker returned an unsupported protocol version")
    if response.get("ok") is not True:
        error = response.get("error")
        if not isinstance(error, dict):
            raise SandboxedConcretizationError("worker failed without a structured error")
        phase = error.get("phase", "unknown")
        error_type = error.get("type", "Error")
        message = error.get("message", "sandboxed concretization failed")
        raise SandboxedConcretizationError(
            f"worker failed during {phase} ({error_type}): {message}"
        )
    if set(response) != {
        "protocol_version",
        "ok",
        "sandbox",
        "repositories",
        "package_hashes",
        "spec",
    }:
        raise SandboxedConcretizationError("worker success response has unexpected fields")
    sandbox = response["sandbox"]
    if not isinstance(sandbox, dict) or set(sandbox) != {
        "backend",
        "abi_version",
        "filesystem_restricted",
        "tcp_restricted",
    }:
        raise SandboxedConcretizationError("worker returned invalid sandbox metadata")
    if (
        sandbox["backend"] != "landlock"
        or not isinstance(sandbox["abi_version"], int)
        or sandbox["abi_version"] < 4
        or sandbox["filesystem_restricted"] is not True
        or sandbox["tcp_restricted"] is not True
    ):
        raise SandboxedConcretizationError("worker did not apply the required restrictions")
    expected_repositories = [
        {
            "namespace": repository["namespace"],
            "package_api": repository["package_api"],
            "identity": repository["identity"],
        }
        for repository in repositories
    ]
    if response["repositories"] != expected_repositories:
        raise SandboxedConcretizationError("worker returned inconsistent repository identities")
    for repository in repositories:
        try:
            identity = repository_digest(Path(repository["root"]))
        except RepositorySnapshotError as error:
            raise SandboxedConcretizationError(
                f"cannot verify repository snapshot: {error}"
            ) from error
        if identity != repository["identity"]:
            raise SandboxedConcretizationError("repository snapshot changed during concretization")
    spec_data = response["spec"]
    _validate_spec_payload(spec_data)
    expected_package_hashes = [
        {"dag_hash": node["hash"], "package_hash": node["package_hash"]}
        for node in spec_data["spec"]["nodes"]
    ]
    if response["package_hashes"] != expected_package_hashes:
        raise SandboxedConcretizationError("worker returned inconsistent package hash provenance")
    try:
        spec = Spec.from_dict(spec_data)
    except Exception as error:
        raise SandboxedConcretizationError(
            f"worker returned an unreadable spec: {error}"
        ) from error
    nodes = spec_data["spec"]["nodes"]
    concrete_nodes = list(spec.traverse())
    if not spec.concrete or len(concrete_nodes) != len(nodes):
        raise SandboxedConcretizationError("worker returned an incomplete concrete spec")
    for concrete_node in concrete_nodes:
        concrete_node.clear_caches(ignore=(ht.package_hash.attr,))
    if any(node["hash"] != concrete.dag_hash() for node, concrete in zip(nodes, concrete_nodes)):
        raise SandboxedConcretizationError("worker returned inconsistent DAG hashes")
    if spec.name != requested.name or not spec.satisfies(requested):
        raise SandboxedConcretizationError("worker returned a spec unrelated to the request")
    return spec


def concretize_one_sandboxed(
    spec: Union[str, Spec],
    *,
    repositories: List[Union[str, spack.repo.Repo]],
    tests: Union[bool, List[str]] = False,
    timeout: float = 120.0,
) -> Spec:
    """Concretize one abstract spec in an experimental Landlock-confined worker.

    This initial API intentionally rejects active environments, concrete and hash-reference inputs,
    develop and external specs, and Git versions. It does not alter normal Spack concretization or
    installation entry points.
    """
    if active_environment() is not None:
        raise SandboxedConcretizationError("active environments are unsupported by this PoC")
    if isinstance(spec, Spec) and spec.concrete:
        raise SandboxedConcretizationError("concrete inputs are unsupported by this PoC")
    spec_string = str(spec)
    requested = Spec(spec_string)
    if any(node.abstract_hash for node in requested.traverse()):
        raise SandboxedConcretizationError("hash references are unsupported by this PoC")
    if any("dev_path" in node.variants for node in requested.traverse()):
        raise SandboxedConcretizationError("develop specs are unsupported by this PoC")
    with tempfile.TemporaryDirectory(prefix="spack-concretize-sandbox-") as workspace:
        state_directory = os.path.join(workspace, "state")
        os.mkdir(state_directory, mode=0o700)
        for name in ("cache", "config", "store", "stage", "source-cache", "tmp"):
            os.mkdir(os.path.join(state_directory, name), mode=0o700)
        repositories_payload = _repository_payload(
            repositories, Path(workspace) / "repositories"
        )
        request = {
            "protocol_version": PROTOCOL_VERSION,
            "operation": "concretize_one",
            "spec": spec_string,
            "tests": tests,
            "configuration": _configuration_payload(),
            "repositories": repositories_payload,
            "clingo_paths": _clingo_paths(),
            "state_directory": state_directory,
        }
        request_bytes = _json_bytes(request, MAX_REQUEST_BYTES)
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                _worker_command(),
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                close_fds=True,
                start_new_session=True,
                env=_sanitized_environment(state_directory),
            )
            timeout_error = None
            try:
                process.communicate(request_bytes, timeout=timeout)
            except subprocess.TimeoutExpired as error:
                timeout_error = error
            finally:
                _kill_process_group(process)
            if timeout_error is not None:
                raise SandboxedConcretizationError(
                    f"sandboxed concretization timed out after {timeout:g} seconds"
                ) from timeout_error
            stdout_file.seek(0)
            stdout = stdout_file.read(MAX_RESPONSE_BYTES + 1)
            stderr_file.seek(0)
            stderr = stderr_file.read(MAX_STDERR_BYTES + 1)
        if len(stdout) > MAX_RESPONSE_BYTES:
            raise SandboxedConcretizationError("worker response is too large")
        if len(stderr) > MAX_STDERR_BYTES:
            raise SandboxedConcretizationError(
                f"worker diagnostic output exceeds {MAX_STDERR_BYTES} bytes"
            )
        if process.returncode != 0 and not stdout:
            detail = stderr.decode("utf-8", errors="replace")[-2000:].strip()
            raise SandboxedConcretizationError(
                f"worker exited with status {process.returncode}: {detail}"
            )
        return _validate_success(_load_response(stdout), requested, repositories_payload)