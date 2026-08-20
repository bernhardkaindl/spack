# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Trusted parent publication of recipe-free install metadata."""

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, List

import spack.error
import spack.hash_types as ht
import spack.util.spack_json as sjson
import spack.verify
from spack.installer.install_tree import (
    MAX_INSTALL_TREE_ENTRIES,
    InstallTreeError,
    install_tree_metadata,
    validate_install_tree_metadata,
)
from spack.installer.post_actions import PostActionError, validate_post_actions
from spack.solver.prepared_stage import source_plan_digest
from spack.solver.source_plan import SourcePlanError, validate_source_plan
from spack.spec import Spec

METADATA_DIRECTORY = ".spack"
SPEC_FILE = "spec.json"
MANIFEST_FILE = "install_manifest.json"
PROVENANCE_FILE = "sandbox_provenance.json"
PROVENANCE_SCHEMA_VERSION = 2
MAX_PROVENANCE_BYTES = 1024 * 1024
MAX_PROVENANCE_PHASES = 32
MAX_PROVENANCE_LOG_BYTES = 64 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PHASE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}")


class InstallMetadataError(spack.error.SpackError):
    """Raised when trusted install metadata cannot be published safely."""


def _sha256(value: Any, description: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise InstallMetadataError(f"invalid provenance {description}")
    return value


def read_install_provenance(prefix: Path) -> Dict[str, Any]:
    """Read bounded sandbox provenance without following its final path component."""
    prefix = Path(os.path.abspath(prefix))
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory = None
    descriptor = None
    try:
        directory = os.open(prefix / METADATA_DIRECTORY, directory_flags)
        descriptor = os.open(PROVENANCE_FILE, file_flags, dir_fd=directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PROVENANCE_BYTES:
            raise InstallMetadataError("invalid sandbox install provenance file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            payload = stream.read(MAX_PROVENANCE_BYTES + 1)
        if len(payload) > MAX_PROVENANCE_BYTES:
            raise InstallMetadataError("sandbox install provenance is too large")

        def reject_duplicate_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate provenance key")
                result[key] = value
            return result

        provenance = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except InstallMetadataError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise InstallMetadataError(f"cannot read install provenance: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
    if not isinstance(provenance, dict):
        raise InstallMetadataError("invalid sandbox install provenance")
    return provenance


def validate_install_provenance(spec: Spec, provenance: Dict[str, Any]) -> Dict[str, Any]:
    """Validate persisted provenance and bind it to a concrete Spec."""
    if not isinstance(spec, Spec) or not spec.concrete:
        raise InstallMetadataError("install provenance requires a concrete Spec")
    schema_version = provenance.get("schema_version") if isinstance(provenance, dict) else None
    if (
        not isinstance(provenance, dict)
        or set(provenance) != {"schema_version", "spec", "source_plan", "build", "parent"}
        or type(schema_version) is not int
        or schema_version not in (1, PROVENANCE_SCHEMA_VERSION)
    ):
        raise InstallMetadataError("invalid sandbox install provenance")
    try:
        spec_identity = provenance["spec"]
        if not isinstance(spec_identity, dict) or set(spec_identity) != {
            "dag_hash",
            "package_hash",
        }:
            raise InstallMetadataError("invalid provenance spec identity")
        if spec_identity["dag_hash"] != spec.dag_hash():
            raise InstallMetadataError("install provenance does not match the concrete spec")

        build = provenance["build"]
        expected_build_fields = {
            "protocol_version",
            "phases",
            "sandbox",
            "repositories",
            "source_plan_sha256",
            "prepared_stage",
            "install_tree",
            "log",
        }
        if provenance["schema_version"] >= 2:
            expected_build_fields.add("patch_method")
        if not isinstance(build, dict) or set(build) != expected_build_fields:
            raise InstallMetadataError("invalid provenance build record")
        if provenance["schema_version"] >= 2 and not isinstance(build["patch_method"], bool):
            raise InstallMetadataError("invalid provenance patch-method record")
        if (
            not isinstance(build["protocol_version"], int)
            or isinstance(build["protocol_version"], bool)
            or build["protocol_version"] < 1
        ):
            raise InstallMetadataError("invalid provenance protocol version")
        phases = build["phases"]
        if (
            not isinstance(phases, list)
            or not phases
            or len(phases) > MAX_PROVENANCE_PHASES
            or len(set(phases)) != len(phases)
            or any(
                not isinstance(phase, str) or _PHASE.fullmatch(phase) is None for phase in phases
            )
        ):
            raise InstallMetadataError("invalid provenance phases")
        sandbox = build["sandbox"]
        if (
            not isinstance(sandbox, dict)
            or set(sandbox)
            != {"backend", "abi_version", "filesystem_restricted", "tcp_restricted"}
            or sandbox["backend"] != "landlock"
            or not isinstance(sandbox["abi_version"], int)
            or isinstance(sandbox["abi_version"], bool)
            or sandbox["abi_version"] < 4
            or sandbox["filesystem_restricted"] is not True
            or sandbox["tcp_restricted"] is not True
        ):
            raise InstallMetadataError("invalid provenance sandbox record")
        plan = validate_source_plan(
            provenance["source_plan"],
            expected_provenance={
                "dag_hash": spec_identity["dag_hash"],
                "package_hash": spec_identity["package_hash"],
                "repositories": build["repositories"],
            },
        )
        _sha256(build["source_plan_sha256"], "SourcePlan digest")
        if source_plan_digest(plan) != build["source_plan_sha256"]:
            raise InstallMetadataError("invalid provenance SourcePlan digest")
        prepared_stage = build["prepared_stage"]
        if not isinstance(prepared_stage, dict) or set(prepared_stage) != {
            "initial_sha256",
            "final_sha256",
        }:
            raise InstallMetadataError("invalid provenance prepared-stage record")
        _sha256(prepared_stage["initial_sha256"], "initial prepared-stage digest")
        _sha256(prepared_stage["final_sha256"], "final prepared-stage digest")
        validate_install_tree_metadata(build["install_tree"])
        log = build["log"]
        if (
            not isinstance(log, dict)
            or set(log) != {"size", "sha256"}
            or not isinstance(log["size"], int)
            or isinstance(log["size"], bool)
            or log["size"] < 0
            or log["size"] > MAX_PROVENANCE_LOG_BYTES
        ):
            raise InstallMetadataError("invalid provenance build-log record")
        _sha256(log["sha256"], "build-log digest")

        parent = provenance["parent"]
        if not isinstance(parent, dict) or set(parent) != {"actions", "install_tree"}:
            raise InstallMetadataError("invalid provenance parent record")
        validate_post_actions(parent["actions"])
        validate_install_tree_metadata(parent["install_tree"])
    except (KeyError, TypeError, SourcePlanError, InstallTreeError, PostActionError) as error:
        raise InstallMetadataError(f"invalid sandbox install provenance: {error}") from error
    return provenance


def verify_install_provenance(spec: Spec, prefix: Path) -> Dict[str, Any]:
    """Verify and validate installed sandbox provenance without importing recipes."""
    if not isinstance(spec, Spec) or not spec.concrete:
        raise InstallMetadataError("install provenance requires a concrete Spec")
    installed_spec = spec.copy()
    installed_spec.set_prefix(str(Path(os.path.abspath(prefix))))
    if spack.verify.check_spec_manifest(installed_spec).has_errors():
        raise InstallMetadataError("install manifest failed provenance verification")
    return validate_install_provenance(spec, read_install_provenance(prefix))


def create_install_provenance(
    spec: Spec,
    source_plan: Dict[str, Any],
    build: Dict[str, Any],
    actions: List[str],
    action_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Create a path-free provenance record from parent-validated install results."""
    try:
        if build["dag_hash"] != spec.dag_hash():
            raise InstallMetadataError("build result does not match provenance spec")
        expected_provenance = {
            "dag_hash": spec.dag_hash(),
            "package_hash": build["package_hash"],
            "repositories": build["repositories"],
        }
        plan = validate_source_plan(source_plan, expected_provenance=expected_provenance)
        if source_plan_digest(plan) != build["source_plan_sha256"]:
            raise InstallMetadataError("source plan changed before provenance publication")
        action_names = [result["type"] for result in action_result["actions"]]
        if action_names != actions:
            raise InstallMetadataError("parent action results do not match the requested actions")
        build_log = build["build_log"]
        return {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "spec": {"dag_hash": spec.dag_hash(), "package_hash": build["package_hash"]},
            "source_plan": plan,
            "build": {
                "protocol_version": build["protocol_version"],
                "phases": build["phases"],
                "patch_method": build["patch_method"],
                "sandbox": build["sandbox"],
                "repositories": build["repositories"],
                "source_plan_sha256": build["source_plan_sha256"],
                "prepared_stage": {
                    "initial_sha256": build["initial_stage_sha256"],
                    "final_sha256": build["final_stage_sha256"],
                },
                "install_tree": build["install_tree"],
                "log": {"size": build_log["size"], "sha256": build_log["sha256"]},
            },
            "parent": {"actions": actions, "install_tree": action_result["install_tree"]},
        }
    except (KeyError, TypeError, SourcePlanError) as error:
        raise InstallMetadataError(f"cannot create install provenance: {error}") from error


def _write_provenance(provenance: Dict[str, Any], destination: Path) -> None:
    """Write canonical bounded provenance without exposing parent-only paths."""
    if (
        not isinstance(provenance, dict)
        or set(provenance) != {"schema_version", "spec", "source_plan", "build", "parent"}
        or provenance.get("schema_version") != PROVENANCE_SCHEMA_VERSION
    ):
        raise InstallMetadataError("invalid sandbox install provenance")
    try:
        payload = json.dumps(provenance, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise InstallMetadataError(f"cannot encode install provenance: {error}") from error
    if len(payload) > MAX_PROVENANCE_BYTES:
        raise InstallMetadataError("sandbox install provenance is too large")
    with destination.open("xb") as stream:
        stream.write(payload)


def _write_manifest(prefix: Path, destination: Path) -> None:
    """Write a Spack-compatible manifest without importing package recipes."""
    manifest = {}
    entries = 0
    for root, directories, files in os.walk(prefix):
        directories.sort()
        files.sort()
        for entry in directories + files:
            entries += 1
            if entries > MAX_INSTALL_TREE_ENTRIES:
                raise InstallMetadataError("install manifest exceeds the entry limit")
            path = Path(root) / entry
            manifest[str(path)] = spack.verify.create_manifest_entry(str(path))
    manifest[str(prefix)] = spack.verify.create_manifest_entry(str(prefix))
    with destination.open("x", encoding="utf-8") as stream:
        sjson.dump(manifest, stream)


def publish_install_metadata(
    spec: Spec, prefix: Path, expected_install_tree: Dict[str, Any], provenance: Dict[str, Any]
) -> Dict[str, Any]:
    """Verify worker output, publish trusted metadata, and identify the final tree."""
    if not isinstance(spec, Spec) or not spec.concrete:
        raise InstallMetadataError("install metadata requires a concrete Spec")
    prefix = Path(os.path.abspath(prefix))
    try:
        current_install_tree = install_tree_metadata(prefix)
    except InstallTreeError as error:
        raise InstallMetadataError(f"cannot verify install tree: {error}") from error
    if current_install_tree != expected_install_tree:
        raise InstallMetadataError("install tree changed before metadata publication")

    metadata_directory = prefix / METADATA_DIRECTORY
    try:
        metadata_directory.mkdir(mode=0o755)
        spec_path = metadata_directory / SPEC_FILE
        with spec_path.open("x", encoding="utf-8") as stream:
            spec.to_json(stream, hash=ht.dag_hash)
        provenance_path = metadata_directory / PROVENANCE_FILE
        _write_provenance(provenance, provenance_path)
        manifest_path = metadata_directory / MANIFEST_FILE
        _write_manifest(prefix, manifest_path)
        installed_spec = spec.copy()
        installed_spec.set_prefix(str(prefix))
        if spack.verify.check_spec_manifest(installed_spec).has_errors():
            raise InstallMetadataError("published install manifest failed verification")
        final_install_tree = install_tree_metadata(prefix)
    except (InstallTreeError, OSError, TypeError, ValueError) as error:
        raise InstallMetadataError(f"cannot publish install metadata: {error}") from error

    return {
        "spec_path": str(spec_path.relative_to(prefix)),
        "provenance_path": str(provenance_path.relative_to(prefix)),
        "manifest_path": str(manifest_path.relative_to(prefix)),
        "install_tree": final_install_tree,
    }
