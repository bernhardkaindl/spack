# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Explicit experimental entry point for the worker-based installer."""

import argparse
import tempfile
import urllib.parse
from pathlib import Path
from typing import List, Tuple

import spack.store
from spack.installer.build_phase_worker import install_prepared_registered_sandboxed
from spack.installer.post_actions import SUPPORTED_POST_ACTIONS
from spack.solver.concretize_worker import concretize_one_sandboxed, plan_sources_sandboxed
from spack.solver.prepared_stage import SourceFetchPolicy, prepare_stage
from spack.util import tty

description = "install one spec with the experimental confined recipe worker"
section = "developer"
level = "long"


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("spec", help="single abstract spec to concretize and install")
    subparser.add_argument(
        "--repository",
        action="append",
        required=True,
        metavar="PATH",
        help="local package repository; repeat to define precedence order",
    )
    subparser.add_argument(
        "--source-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="allowed HTTP(S) source origin, for example https://example.com; repeat as needed",
    )
    subparser.add_argument(
        "--file-root",
        action="append",
        default=[],
        metavar="PATH",
        help="allowed root for file:// sources; repeat as needed",
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
        default=[],
        help="ordered allowlisted parent action; repeat in canonical order",
    )
    subparser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="timeout in seconds for each confined worker (default: 120)",
    )
    subparser.add_argument(
        "--no-repository-snapshots",
        action="store_true",
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


def sandbox_install(parser, args) -> None:
    """Run the explicit end-to-end worker-based installation workflow."""
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    try:
        fetch_policy = _source_fetch_policy(args.source_origin, args.file_root)
    except ValueError as error:
        parser.error(str(error))
    repositories = [str(Path(repository).resolve()) for repository in args.repository]
    snapshots = not args.no_repository_snapshots
    phases = args.phase or ["install"]

    tty.warn("sandbox-install is experimental and does not change normal spack install behavior")
    concrete = concretize_one_sandboxed(
        args.spec, repositories=repositories, timeout=args.timeout, repository_snapshots=snapshots
    )
    source_plan = plan_sources_sandboxed(
        concrete, repositories=repositories, timeout=args.timeout, repository_snapshots=snapshots
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
            timeout=args.timeout,
            keep_failed_prefix=args.keep_failed_prefix,
            log_path=args.log_file,
            post_actions=args.post_action,
        )
    registration = result["registration"]
    tty.msg("Installed {0} at {1}".format(concrete, registration["prefix"]))
