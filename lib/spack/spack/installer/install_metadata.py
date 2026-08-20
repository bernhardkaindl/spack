# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Trusted parent publication of recipe-free install metadata."""

import json
import os
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
)
from spack.solver.prepared_stage import source_plan_digest
from spack.solver.source_plan import SourcePlanError, validate_source_plan
from spack.spec import Spec

METADATA_DIRECTORY = ".spack"
SPEC_FILE = "spec.json"
MANIFEST_FILE = "install_manifest.json"
PROVENANCE_FILE = "sandbox_provenance.json"
PROVENANCE_SCHEMA_VERSION = 1
MAX_PROVENANCE_BYTES = 1024 * 1024


class InstallMetadataError(spack.error.SpackError):
    """Raised when trusted install metadata cannot be published safely."""


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
