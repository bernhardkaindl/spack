# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Explicit experimental entry point for the worker-based installer."""

import argparse
import re
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Tuple

import spack.config
import spack.store
from spack.installer.build_phase_worker import install_prepared_registered_sandboxed
from spack.installer.post_actions import (
    SUPPORTED_POST_ACTIONS,
    PostActionError,
    validate_post_actions,
)
from spack.solver.concretize_worker import concretize_one_sandboxed, plan_sources_sandboxed
from spack.solver.prepared_stage import SourceFetchPolicy, prepare_stage
from spack.util import tty

description = "install one spec with the experimental confined recipe worker"
section = "developer"
level = "long"
MAX_PHASES = 32
_PHASE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}")


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("spec", help="single abstract spec to concretize and install")
    subparser.add_argument(
        "--repository",
        action="append",
        metavar="PATH",
        help="local package repository; replaces configured repository order when specified",
    )
    subparser.add_argument(
        "--source-origin",
        action="append",
        metavar="ORIGIN",
        help="allowed HTTP(S) source origin; replaces configured origins when specified",
    )
    subparser.add_argument(
        "--file-root",
        action="append",
        metavar="PATH",
        help="allowed file:// root; replaces configured roots when specified",
    )
    subparser.add_argument(
        "--phase",
        action="append",
        metavar="NAME",
        help="ordered worker phase; repeat as needed (default: install)",
    )
    subparser.add_argument(
        "--post-action",
        action="append",
        choices=SUPPORTED_POST_ACTIONS,
        help="ordered parent action; replaces configured actions when specified",
    )
    subparser.add_argument(
        "--timeout", type=float, help="timeout for each worker; replaces the configured value"
    )
    snapshots = subparser.add_mutually_exclusive_group()
    snapshots.add_argument(
        "--repository-snapshots",
        action="store_true",
        dest="repository_snapshots",
        default=None,
        help="copy repositories before recipe import",
    )
    snapshots.add_argument(
        "--no-repository-snapshots",
        action="store_false",
        dest="repository_snapshots",
        help="use live repositories with weaker point-in-time integrity",
    )
    subparser.add_argument(
        "--keep-failed-prefix", action="store_true", help="retain a failed prefix for debugging"
    )
    subparser.add_argument("--log-file", type=Path, help="parent-owned bounded build log path")


def _origin(value: str) -> Tuple[str, Tuple[str, int]]:
    parsed = urllib.parse.urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("source origin has an invalid port") from error
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("source origin must contain only an HTTP(S) scheme and authority")
    return parsed.scheme, (
        parsed.hostname.lower(),
        port or (443 if parsed.scheme == "https" else 80),
    )


def _source_fetch_policy(origins: List[str], file_roots: List[str]) -> SourceFetchPolicy:
    https_origins = set()
    http_origins = set()
    for value in origins:
        scheme, origin = _origin(value)
        (https_origins if scheme == "https" else http_origins).add(origin)
    return SourceFetchPolicy(
        https_origins=frozenset(https_origins),
        http_origins=frozenset(http_origins),
        file_roots=tuple(Path(root).resolve() for root in file_roots),
    )


def _policy(args) -> Dict[str, Any]:
    """Resolve explicit command values over the isolated worker-installer profile."""
    configured = spack.config.CONFIG.get("config:sandbox_installer", {}) or {}

    def value(argument, name, default):
        return argument if argument is not None else configured.get(name, default)

    return {
        "repositories": value(args.repository, "repositories", []),
        "source_origins": value(args.source_origin, "source_origins", []),
        "file_roots": value(args.file_root, "file_roots", []),
        "repository_snapshots": value(args.repository_snapshots, "repository_snapshots", True),
        "phases": value(args.phase, "phases", ["install"]),
        "post_actions": value(args.post_action, "post_actions", []),
        "timeout": value(args.timeout, "timeout", 120.0),
    }


def sandbox_install(parser, args) -> None:
    """Run the explicit end-to-end worker-based installation workflow."""
    policy = _policy(args)
    if not policy["repositories"]:
        parser.error(
            "at least one --repository or config:sandbox_installer:repositories entry is required"
        )
    if policy["timeout"] <= 0:
        parser.error("--timeout must be greater than zero")
    phases = policy["phases"]
    if (
        not isinstance(phases, list)
        or not phases
        or len(phases) > MAX_PHASES
        or len(set(phases)) != len(phases)
        or any(not isinstance(phase, str) or _PHASE.fullmatch(phase) is None for phase in phases)
    ):
        parser.error("invalid worker phase list")
    try:
        validate_post_actions(policy["post_actions"])
    except PostActionError as error:
        parser.error(str(error))
    try:
        fetch_policy = _source_fetch_policy(policy["source_origins"], policy["file_roots"])
    except ValueError as error:
        parser.error(str(error))
    repositories = [str(Path(repository).resolve()) for repository in policy["repositories"]]

    tty.warn("sandbox-install is experimental and does not change normal spack install behavior")
    concrete = concretize_one_sandboxed(
        args.spec,
        repositories=repositories,
        timeout=policy["timeout"],
        repository_snapshots=policy["repository_snapshots"],
    )
    source_plan = plan_sources_sandboxed(
        concrete,
        repositories=repositories,
        timeout=policy["timeout"],
        repository_snapshots=policy["repository_snapshots"],
    )
    with tempfile.TemporaryDirectory(prefix="spack-sandbox-stage-") as temporary:
        prepared_stage = prepare_stage(
            source_plan,
            Path(temporary) / "prepared",
            expected_provenance=source_plan["provenance"],
            fetch_policy=fetch_policy,
        )
        result = install_prepared_registered_sandboxed(
            concrete,
            source_plan,
            prepared_stage,
            phases,
            store=spack.store.STORE,
            repositories=repositories,
            explicit=True,
            timeout=policy["timeout"],
            keep_failed_prefix=args.keep_failed_prefix,
            log_path=args.log_file,
            post_actions=policy["post_actions"],
        )
    registration = result["registration"]
    tty.msg("Installed {0} at {1}".format(concrete, registration["prefix"]))
