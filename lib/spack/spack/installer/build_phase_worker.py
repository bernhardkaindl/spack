# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Experimental launcher for one phase over a trusted prepared source tree."""

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Union

import spack.error
import spack.repo
from spack.solver.concretize_worker import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_STDERR_BYTES,
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


PROTOCOL_VERSION = 1
_PHASE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}")


class SandboxedBuildPhaseError(spack.error.SpackError):
    """Raised when the prepared-stage build worker fails."""


def _worker_command() -> List[str]:
    """Return an isolated Python command for the fresh build-phase worker."""
    worker = Path(__file__).with_name("_build_phase_worker.py")
    return [sys.executable, "-I", "-S", "-B", str(worker)]


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


def _validate_response(
    response: Dict[str, Any],
    *,
    phase: str,
    spec_data: Dict[str, Any],
    source_plan_sha256: str,
    initial_stage_sha256: str,
    prepared_stage: Path,
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
        "phase",
        "dag_hash",
        "package_hash",
        "source_plan_sha256",
        "initial_stage_sha256",
        "final_stage_sha256",
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
        response["phase"] != phase
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
    return response


def run_build_phase_sandboxed(
    spec: Spec,
    source_plan: Dict[str, Any],
    prepared_stage: PreparedStage,
    phase: str,
    *,
    prefix: Path,
    repositories: List[Union[str, spack.repo.Repo]],
    timeout: float = 120.0,
) -> Dict[str, Any]:
    """Run one declared build phase in a fresh Landlock-confined process."""
    if not isinstance(spec, Spec) or not spec.concrete:
        raise SandboxedBuildPhaseError("build phase requires a concrete Spec")
    architecture = spec.architecture
    if architecture is None:
        raise SandboxedBuildPhaseError("build phase requires a concrete architecture")
    if not isinstance(phase, str) or _PHASE.fullmatch(phase) is None:
        raise SandboxedBuildPhaseError("invalid build phase name")
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
    prefix.mkdir(mode=0o700)

    with tempfile.TemporaryDirectory(prefix="spack-build-phase-sandbox-") as workspace:
        state_directory = Path(workspace) / "state"
        state_directory.mkdir(mode=0o700)
        for name in ("cache", "config", "stage", "store", "tmp"):
            (state_directory / name).mkdir(mode=0o700)
        repositories_payload = _repository_payload(repositories, Path(workspace) / "repositories")
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
            "phase": phase,
            "repositories": repositories_payload,
            "platform": architecture.platform,
            "state_directory": str(state_directory),
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
                    f"sandboxed build phase timed out after {timeout:g} seconds"
                ) from timeout_error
            stdout_file.seek(0)
            stdout = stdout_file.read(MAX_RESPONSE_BYTES + 1)
            stderr_file.seek(0)
            stderr = stderr_file.read(MAX_STDERR_BYTES + 1)
        if len(stdout) > MAX_RESPONSE_BYTES:
            raise SandboxedBuildPhaseError("worker response is too large")
        if len(stderr) > MAX_STDERR_BYTES:
            raise SandboxedBuildPhaseError("worker diagnostic output is too large")
        if process.returncode != 0 and not stdout:
            detail = stderr.decode("utf-8", errors="replace")[-2000:].strip()
            raise SandboxedBuildPhaseError(
                f"worker exited with status {process.returncode}: {detail}"
            )
        response = _load_response(stdout)
        return _validate_response(
            response,
            phase=phase,
            spec_data=spec_data,
            source_plan_sha256=plan_sha256,
            initial_stage_sha256=initial_stage_sha256,
            prepared_stage=stage_path,
            repositories=repositories_payload,
        )
