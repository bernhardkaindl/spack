# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Network-supervised source staging for concrete package specs."""

import mimetypes
import os
import ssl
import sysconfig
from typing import Any, Dict, List, Optional, cast

import spack.caches
import spack.package_base
import spack.paths
import spack.repo
import spack.sandbox
import spack.spec
import spack.stage
import spack.store
import spack.util.filesystem as fs
import spack.util.sandbox
import spack.util.url
from spack.install_worker.request import create_request, validate_request
from spack.util.executable import which_string
from spack.util.ld_so_conf import host_dynamic_linker_search_paths
from spack.util.lock import FILE_TRACKER
from spack.util.proxy import DestinationPolicy

_STAGE_REQUEST_KEYS = {"acquire_lock", "patch", "path", "request"}
_STAGE_RESPONSE_KEYS = {"dag_hash", "path"}
_STAGE_TOOLS = ("tar", "unzip", "gzip", "gunzip", "bunzip2", "xz", "7z", "patch", "sh")


class StageWorkerError(ValueError):
    """A stage-worker request or response violates its bounded contract."""


def _validate_stage_request(request: Any):
    if not isinstance(request, dict) or set(request) != _STAGE_REQUEST_KEYS:
        raise StageWorkerError("stage-worker request has invalid fields")
    path = request["path"]
    if path is not None and (not isinstance(path, str) or not os.path.isabs(path)):
        raise StageWorkerError("stage-worker request has an invalid stage path")
    if type(request["patch"]) is not bool or type(request["acquire_lock"]) is not bool:
        raise StageWorkerError("stage-worker request has invalid operation flags")
    return validate_request(request["request"]), path, request["patch"], request["acquire_lock"]


def _stage_worker(request: Any) -> Dict[str, str]:
    """Restore a package after confinement and invoke its normal staging method."""
    spec, path, patch, acquire_lock = _validate_stage_request(request)
    package = spec.package
    setattr(package, "path", path)
    if acquire_lock:
        package.stage.keep = True
        with package.stage:
            package.do_patch() if patch else package.do_stage()
    else:
        package.do_patch() if patch else package.do_stage()
    return {"dag_hash": spec.dag_hash(), "path": package.stage.path}


def _recipe_read_roots() -> List[str]:
    roots = []
    for repository in spack.repo.PATH.repos:
        roots.append(repository.root)
        if repository.python_path:
            roots.append(repository.python_path)
    roots.extend(
        [
            spack.paths.etc_path,
            spack.paths.lib_path,
            spack.paths.system_config_path,
            spack.paths.user_config_path,
        ]
    )
    stdlib_path = sysconfig.get_path("stdlib")
    if stdlib_path:
        roots.append(stdlib_path)
    verify_paths = ssl.get_default_verify_paths()
    roots.extend(path for path in (verify_paths.cafile, verify_paths.capath) if path)
    roots.extend(path for path in mimetypes.knownfiles if os.path.exists(path))
    return roots


def _tool_runtime_roots(spec: spack.spec.Spec, tool_paths: List[str]) -> List[str]:
    roots = []
    canonical_tools = [os.path.realpath(path) for path in tool_paths]
    for node in spec.traverse():
        prefix = os.path.realpath(str(node.prefix))
        try:
            owns_tool = any(
                os.path.commonpath((path, prefix)) == prefix for path in canonical_tools
            )
        except ValueError:
            owns_tool = False
        if owns_tool:
            roots.append(str(node.prefix))
            roots.extend(
                str(dependency.prefix)
                for dependency in node.traverse(root=False, deptype=("link", "run"))
            )
    return roots


def _expansion_read_roots(spec: Optional[spack.spec.Spec] = None) -> List[str]:
    roots = list(host_dynamic_linker_search_paths())
    tool_paths = []
    for name in _STAGE_TOOLS:
        path = which_string(name)
        if path:
            tool_paths.append(path)
    roots.extend(tool_paths)
    if spec is not None:
        roots.extend(_tool_runtime_roots(spec, tool_paths))
    return roots


def _local_stage_read_roots(package: spack.package_base.PackageBase) -> List[str]:
    roots = []
    for stage in package.stage:
        urls = [getattr(getattr(stage, "default_fetcher", None), "url", None)]
        urls.extend(mirror.fetch_url for mirror in getattr(stage, "mirrors", ()))
        for url in urls:
            if url is None:
                continue
            path = spack.util.url.local_file_path(url)
            if path:
                roots.append(path)
    return roots


def _store_database_read_roots() -> List[str]:
    """Return local and upstream database directories needed for prefix queries."""
    databases = [spack.store.STORE.db] + list(spack.store.STORE.db.upstream_dbs)
    return [str(database.database_directory) for database in databases]


def _dependency_read_roots(spec: spack.spec.Spec) -> List[str]:
    """Return installed non-external dependency prefixes selected by the concrete DAG."""
    return [str(node.prefix) for node in spec.traverse(root=False) if not node.external]


def _stage_setup(read_roots: List[str], write_roots: List[str]) -> None:
    FILE_TRACKER.discard_after_fork()
    spack.store.reinitialize()
    try:
        from spack.oci import opener
        from spack.util import web
        from spack.util.s3 import s3_client_cache

        web.urlopen._instance = None
        cast(Any, opener.urlopen)._instance = None
        s3_client_cache.clear()
    except Exception as error:
        raise StageWorkerError(
            "stage worker setup reset network clients failed: {0}: {1}".format(
                type(error).__name__, error
            )
        ) from error
    try:
        spack.sandbox.restrict_stage_worker(read_roots, write_roots)
    except Exception as error:
        raise StageWorkerError(
            "stage worker setup restrict_stage_worker failed: {0}: {1}".format(
                type(error).__name__, error
            )
        ) from error


def _validate_stage_response(response: Any, dag_hash: str, expected_path: str) -> str:
    if (
        not isinstance(response, dict)
        or set(response) != _STAGE_RESPONSE_KEYS
        or response["dag_hash"] != dag_hash
        or not isinstance(response["path"], str)
        or not os.path.isabs(response["path"])
        or response["path"] != expected_path
    ):
        raise StageWorkerError("stage worker returned an invalid response")
    return response["path"]


def stage_package(
    package: spack.package_base.PackageBase, patch: bool = False, acquire_lock: bool = True
) -> str:
    """Stage ``package`` in a confined worker and return its retained stage path."""
    request = {
        "acquire_lock": acquire_lock,
        "patch": patch,
        "path": package.path,
        "request": create_request(package.spec),
    }
    global_stage_root = spack.stage.get_stage_root()
    stage_root = package.path or global_stage_root
    expected_stage_path = package.stage.path
    stage_lock = os.path.join(global_stage_root, ".lock")
    fetch_cache = spack.caches.fetch_cache_location()
    for root in (global_stage_root, stage_root, fetch_cache):
        fs.mkdirp(root)
    fs.touch(stage_lock)
    read_roots = (
        _recipe_read_roots()
        + _expansion_read_roots(package.spec)
        + _local_stage_read_roots(package)
        + _store_database_read_roots()
        + _dependency_read_roots(package.spec)
    )
    write_roots = [stage_root, fetch_cache]
    if acquire_lock and stage_root != global_stage_root:
        write_roots.append(stage_lock)
    response = spack.util.sandbox.run_json_worker_with_network(
        request,
        _stage_worker,
        DestinationPolicy.allow_any(),
        setup=lambda: _stage_setup(read_roots, write_roots),
    )
    return _validate_stage_response(response, package.spec.dag_hash(), expected_stage_path)
