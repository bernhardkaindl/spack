# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Trusted parent publication of recipe-free install metadata."""

import os
from pathlib import Path
from typing import Any, Dict

import spack.error
import spack.hash_types as ht
import spack.util.spack_json as sjson
import spack.verify
from spack.installer.install_tree import (
    MAX_INSTALL_TREE_ENTRIES,
    InstallTreeError,
    install_tree_metadata,
)
from spack.spec import Spec

METADATA_DIRECTORY = ".spack"
SPEC_FILE = "spec.json"
MANIFEST_FILE = "install_manifest.json"


class InstallMetadataError(spack.error.SpackError):
    """Raised when trusted install metadata cannot be published safely."""


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
    spec: Spec, prefix: Path, expected_install_tree: Dict[str, Any]
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
        "manifest_path": str(manifest_path.relative_to(prefix)),
        "install_tree": final_install_tree,
    }
