# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Experimental launcher for build phases over a trusted prepared source tree."""

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import spack.error
import spack.repo
import spack.store
import spack.util.lang
from spack.installer.install_metadata import (
    InstallMetadataError,
    create_install_provenance,
    publish_install_metadata,
)
from spack.installer.install_tree import (
    InstallTreeError,
    install_tree_metadata,
    validate_install_tree_metadata,
)
from spack.installer.post_actions import (
    PostActionError,
    run_post_actions,
    validate_post_actions,
    validate_sbang_path,
)
from spack.solver.concretize_worker import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    SandboxedConcretizationError,
    _json_bytes,
    _kill_process_group,
    _load_response,
    _repository_payload,
    _sanitized_environment,
    _validate_spec_payload,
)
from spack.solver.prepared_stage import PreparedStage, prepared_stage_digest, source_plan_digest
from spack.solver.repository_snapshot import RepositorySnapshotError, repository_digest
from spack.solver.source_plan import SourcePlanError, validate_source_plan
from spack.spec import Spec

PROTOCOL_VERSION = 4
MAX_PHASES = 32
MAX_BUILD_LOG_BYTES = 64 * 1024 * 1024
_PHASE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}")


class SandboxedBuildPhaseError(spack.error.SpackError):
    """Raised when the prepared-stage build worker fails."""


def _worker_command(response_fd: int) -> List[str]:
    """Return an isolated Python command for the fresh build-phase worker."""
    worker = Path(__file__).with_name("_build_phase_worker.py")
    return [sys.executable, "-I", "-S", "-B", str(worker), str(response_fd)]


def _build_log_metadata(path: Path) -> Dict[str, Any]:
    """Return trusted identity metadata for one bounded build log."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BUILD_LOG_BYTES:
                raise SandboxedBuildPhaseError("worker build log is too large")
            digest.update(chunk)
    return {"path": str(path), "size": size, "sha256": digest.hexdigest()}


def _repository_identities(repositories: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip repository paths from descriptions that cross back from the worker."""
    return [
        {
            "namespace": repository["namespace"],
            "package_api": repository["package_api"],
            "identity": repository["identity"],
        }
        for repository in repositories
    ]


def _validate_install_tree_metadata(metadata: Any) -> None:
    """Validate bounded install-tree metadata returned by the worker."""
    try:
        validate_install_tree_metadata(metadata)
    except InstallTreeError:
        raise SandboxedBuildPhaseError("worker returned invalid install-tree metadata")


def _validate_response(
    response: Dict[str, Any],
    *,
    phases: List[str],
    spec_data: Dict[str, Any],
    source_plan_sha256: str,
    initial_stage_sha256: str,
    prepared_stage: Path,
    prefix: Path,
    repositories: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate a successful worker response and its post-phase identities."""
    if response.get("protocol_version") != PROTOCOL_VERSION:
        raise SandboxedBuildPhaseError("worker returned an unsupported protocol version")
    if response.get("ok") is not True:
        error = response.get("error")
        if not isinstance(error, dict):
            raise SandboxedBuildPhaseError("worker failed without a structured error")
        raise SandboxedBuildPhaseError(
            "worker failed during {} ({}): {}".format(
                error.get("phase", "unknown"),
                error.get("type", "Error"),
                error.get("message", "sandboxed build phase failed"),
            )
        )
    expected_fields = {
        "protocol_version",
        "ok",
        "sandbox",
        "repositories",
        "phases",
        "dag_hash",
        "package_hash",
        "source_plan_sha256",
        "initial_stage_sha256",
        "final_stage_sha256",
        "install_tree",
    }
    if set(response) != expected_fields:
        raise SandboxedBuildPhaseError("worker success response has unexpected fields")
    sandbox = response["sandbox"]
    if not isinstance(sandbox, dict) or set(sandbox) != {
        "backend",
        "abi_version",
        "filesystem_restricted",
        "tcp_restricted",
    }:
        raise SandboxedBuildPhaseError("worker returned invalid sandbox metadata")
    if (
        sandbox["backend"] != "landlock"
        or not isinstance(sandbox["abi_version"], int)
        or sandbox["abi_version"] < 4
        or sandbox["filesystem_restricted"] is not True
        or sandbox["tcp_restricted"] is not True
    ):
        raise SandboxedBuildPhaseError("worker did not apply the required restrictions")
    if response["repositories"] != _repository_identities(repositories):
        raise SandboxedBuildPhaseError("worker returned inconsistent repository identities")
    for repository in repositories:
        try:
            identity = repository_digest(Path(repository["root"]))
        except RepositorySnapshotError as error:
            raise SandboxedBuildPhaseError(
                f"cannot verify repository contents: {error}"
            ) from error
        if identity != repository["identity"]:
            raise SandboxedBuildPhaseError("repository contents changed during the build phase")
    root = spec_data["spec"]["nodes"][0]
    if (
        response["phases"] != phases
        or response["dag_hash"] != root["hash"]
        or response["package_hash"] != root["package_hash"]
        or response["source_plan_sha256"] != source_plan_sha256
        or response["initial_stage_sha256"] != initial_stage_sha256
    ):
        raise SandboxedBuildPhaseError("worker returned inconsistent build provenance")
    final_stage_sha256 = response["final_stage_sha256"]
    if not isinstance(final_stage_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", final_stage_sha256
    ):
        raise SandboxedBuildPhaseError("worker returned an invalid prepared-stage identity")
    if prepared_stage_digest(prepared_stage) != final_stage_sha256:
        raise SandboxedBuildPhaseError("prepared stage changed after worker verification")
    _validate_install_tree_metadata(response["install_tree"])
    try:
        parent_install_tree = install_tree_metadata(prefix)
    except InstallTreeError as error:
        raise SandboxedBuildPhaseError(f"cannot verify install tree: {error}") from error
    if response["install_tree"] != parent_install_tree:
        raise SandboxedBuildPhaseError("install tree changed after worker verification")
    return response


def run_build_phases_sandboxed(
    spec: Spec,
    source_plan: Dict[str, Any],
    prepared_stage: PreparedStage,
    phases: List[str],
    *,
    prefix: Path,
    repositories: List[Union[str, spack.repo.Repo]],
    timeout: float = 120.0,
    log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run ordered declared build phases and capture output in a parent-owned log."""
    if not isinstance(spec, Spec) or not spec.concrete:
        raise SandboxedBuildPhaseError("build phase requires a concrete Spec")
    architecture = spec.architecture
    if architecture is None:
        raise SandboxedBuildPhaseError("build phase requires a concrete architecture")
    if (
        not isinstance(phases, list)
        or not phases
        or len(phases) > MAX_PHASES
        or len(set(phases)) != len(phases)
        or any(not isinstance(phase, str) or _PHASE.fullmatch(phase) is None for phase in phases)
    ):
        raise SandboxedBuildPhaseError("invalid build phase list")
    spec_data = spec.to_dict()
    try:
        _validate_spec_payload(spec_data)
    except SandboxedConcretizationError as error:
        raise SandboxedBuildPhaseError(str(error)) from error
    stage_path = prepared_stage.path.resolve(strict=True)
    initial_stage_sha256 = prepared_stage_digest(stage_path)
    if initial_stage_sha256 != prepared_stage.content_sha256:
        raise SandboxedBuildPhaseError("prepared stage identity does not match its contents")
    plan_sha256 = source_plan_digest(source_plan)
    if plan_sha256 != prepared_stage.source_plan_sha256:
        raise SandboxedBuildPhaseError("prepared stage does not match the source plan")
    prefix = prefix.resolve()
    if prefix.exists():
        raise SandboxedBuildPhaseError("build prefix must not already exist")
    prefix.parent.mkdir(parents=True, exist_ok=True)
    if log_path is None:
        log_fd, log_name = tempfile.mkstemp(
            prefix=f".{prefix.name}.spack-build-", suffix=".log", dir=prefix.parent
        )
        log_path = Path(log_name).resolve()
    else:
        log_path = log_path.resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        except OSError as error:
            raise SandboxedBuildPhaseError(f"cannot create build log: {error}") from error
    log_file = os.fdopen(log_fd, "wb")

    try:
        prefix.mkdir(mode=0o700)
        with tempfile.TemporaryDirectory(prefix="spack-build-phase-sandbox-") as workspace:
            state_directory = Path(workspace) / "state"
            state_directory.mkdir(mode=0o700)
            for name in ("cache", "config", "stage", "store", "tmp"):
                (state_directory / name).mkdir(mode=0o700)
            repositories_payload = _repository_payload(
                repositories, Path(workspace) / "repositories"
            )
            identities = _repository_identities(repositories_payload)
            root = spec_data["spec"]["nodes"][0]
            expected_provenance = {
                "dag_hash": root["hash"],
                "package_hash": root["package_hash"],
                "repositories": identities,
            }
            try:
                validate_source_plan(source_plan, expected_provenance=expected_provenance)
            except SourcePlanError as error:
                raise SandboxedBuildPhaseError(f"invalid source plan: {error}") from error
            request = {
                "protocol_version": PROTOCOL_VERSION,
                "spec": spec_data,
                "source_plan": source_plan,
                "source_plan_sha256": plan_sha256,
                "prepared_stage": str(stage_path),
                "prepared_stage_sha256": initial_stage_sha256,
                "prefix": str(prefix),
                "phases": phases,
                "repositories": repositories_payload,
                "platform": architecture.platform,
                "state_directory": str(state_directory),
            }
            request_bytes = _json_bytes(request, MAX_REQUEST_BYTES)
            with log_file, tempfile.TemporaryFile() as response_file:
                response_fd = response_file.fileno()
                process = subprocess.Popen(
                    _worker_command(response_fd),
                    stdin=subprocess.PIPE,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    pass_fds=(response_fd,),
                    close_fds=True,
                    start_new_session=True,
                    env=_sanitized_environment(str(state_directory)),
                )
                timeout_error = None
                try:
                    process.communicate(request_bytes, timeout=timeout)
                except subprocess.TimeoutExpired as error:
                    timeout_error = error
                finally:
                    _kill_process_group(process)
                if timeout_error is not None:
                    raise SandboxedBuildPhaseError(
                        f"sandboxed build phases timed out after {timeout:g} seconds"
                    ) from timeout_error
                response_file.seek(0)
                response_bytes = response_file.read(MAX_RESPONSE_BYTES + 1)
            if len(response_bytes) > MAX_RESPONSE_BYTES:
                raise SandboxedBuildPhaseError("worker response is too large")
            if process.returncode != 0 and not response_bytes:
                raise SandboxedBuildPhaseError(f"worker exited with status {process.returncode}")
            response = _load_response(response_bytes)
            validated = _validate_response(
                response,
                phases=phases,
                spec_data=spec_data,
                source_plan_sha256=plan_sha256,
                initial_stage_sha256=initial_stage_sha256,
                prepared_stage=stage_path,
                prefix=prefix,
                repositories=repositories_payload,
            )
        return {**validated, "build_log": _build_log_metadata(log_path)}
    except BaseException:
        if not log_file.closed:
            log_file.close()
        raise


def run_build_phase_sandboxed(
    spec: Spec,
    source_plan: Dict[str, Any],
    prepared_stage: PreparedStage,
    phase: str,
    *,
    prefix: Path,
    repositories: List[Union[str, spack.repo.Repo]],
    timeout: float = 120.0,
    log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run one declared build phase in a fresh Landlock-confined process."""
    response = run_build_phases_sandboxed(
        spec,
        source_plan,
        prepared_stage,
        [phase],
        prefix=prefix,
        repositories=repositories,
        timeout=timeout,
        log_path=log_path,
    )
    return {**response, "phase": phase}


def install_prepared_sandboxed(
    spec: Spec,
    source_plan: Dict[str, Any],
    prepared_stage: PreparedStage,
    phases: List[str],
    *,
    prefix: Path,
    repositories: List[Union[str, spack.repo.Repo]],
    timeout: float = 120.0,
    keep_failed_prefix: bool = False,
    log_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run confined phases with parent-owned atomic prefix commit or rollback.

    This additive API intentionally does not run global hooks or update the store database. Those
    privileged parent actions require a separate typed integration boundary.
    """
    from spack.installer.build import PrefixPivoter

    prefix = prefix.resolve()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with PrefixPivoter(str(prefix), keep_prefix=keep_failed_prefix):
        return _install_prepared_sandboxed(
            spec,
            source_plan,
            prepared_stage,
            phases,
            prefix=prefix,
            repositories=repositories,
            timeout=timeout,
            log_path=log_path,
        )


def _install_prepared_sandboxed(
    spec: Spec,
    source_plan: Dict[str, Any],
    prepared_stage: PreparedStage,
    phases: List[str],
    *,
    prefix: Path,
    repositories: List[Union[str, spack.repo.Repo]],
    timeout: float,
    log_path: Optional[Path],
    post_actions: Optional[List[str]] = None,
    sbang_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build and publish trusted metadata inside a parent-owned prefix transaction."""
    response = run_build_phases_sandboxed(
        spec,
        source_plan,
        prepared_stage,
        phases,
        prefix=prefix,
        repositories=repositories,
        timeout=timeout,
        log_path=log_path,
    )
    try:
        actions = [] if post_actions is None else post_actions
        action_result = run_post_actions(
            spec, prefix, actions, response["install_tree"], sbang_path=sbang_path
        )
        provenance = create_install_provenance(spec, source_plan, response, actions, action_result)
        metadata = publish_install_metadata(
            spec, prefix, action_result["install_tree"], provenance
        )
    except (InstallMetadataError, PostActionError) as error:
        raise SandboxedBuildPhaseError(str(error)) from error
    return {**response, "post_actions": action_result, "install_metadata": metadata}


def install_prepared_registered_sandboxed(
    spec: Spec,
    source_plan: Dict[str, Any],
    prepared_stage: PreparedStage,
    phases: List[str],
    *,
    store: spack.store.Store,
    repositories: List[Union[str, spack.repo.Repo]],
    explicit: bool = False,
    timeout: float = 120.0,
    keep_failed_prefix: bool = False,
    log_path: Optional[Path] = None,
    post_actions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Install at the store projection under its prefix lock and register it."""
    from spack.installer.build import PrefixPivoter

    unwrapped_store = spack.util.lang.ensure_unwrapped(store)
    if not isinstance(unwrapped_store, spack.store.Store):
        raise SandboxedBuildPhaseError("registered install requires a Store")
    store = unwrapped_store
    actions = [] if post_actions is None else post_actions
    try:
        validate_post_actions(actions)
    except PostActionError as error:
        raise SandboxedBuildPhaseError(str(error)) from error
    if "sbang" in actions:
        sbang_path = Path(store.unpadded_root) / "bin" / "sbang"
        try:
            validate_sbang_path(sbang_path)
            store.install_sbang()
        except (spack.error.SpackError, OSError, KeyError) as error:
            raise SandboxedBuildPhaseError(
                "cannot prepare sbang post-action: {0}".format(error)
            ) from error
    else:
        sbang_path = None
    prefix = Path(store.layout.path_for_spec(spec)).resolve()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with store.prefix_locker.write_lock(spec):
        with PrefixPivoter(str(prefix), keep_prefix=keep_failed_prefix):
            response = _install_prepared_sandboxed(
                spec,
                source_plan,
                prepared_stage,
                phases,
                prefix=prefix,
                repositories=repositories,
                timeout=timeout,
                log_path=log_path,
                post_actions=post_actions,
                sbang_path=sbang_path,
            )
            try:
                store.db.add(spec, explicit=explicit)
            except Exception as error:
                raise SandboxedBuildPhaseError(
                    f"cannot register prepared installation: {error}"
                ) from error
            return {
                **response,
                "registration": {
                    "dag_hash": spec.dag_hash(),
                    "explicit": explicit,
                    "prefix": str(prefix),
                },
            }
