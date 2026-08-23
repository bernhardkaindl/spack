# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import argparse
import functools
import re
import ssl
import sys
import sysconfig
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import spack.config
import spack.paths
import spack.repo
import spack.sandbox
import spack.spec
import spack.stage
import spack.util.lang
import spack.util.parallel
import spack.util.sandbox
import spack.util.string
import spack.util.web as web_util
from spack.cmd.common import arguments
from spack.package_base import (
    ManualDownloadRequiredError,
    PackageBase,
    deprecated_version,
    preferred_version,
)
from spack.util import tty
from spack.util.editor import editor
from spack.util.format import get_version_lines
from spack.util.proxy import DestinationPolicy
from spack.version import StandardVersion, Version

description = "checksum available versions of a package"
section = "packaging"
level = "long"


def setup_parser(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "--keep-stage",
        action="store_true",
        default=False,
        help="don't clean up staging area when command completes",
    )
    subparser.add_argument(
        "--batch",
        "-b",
        action="store_true",
        default=False,
        help="don't ask which versions to checksum",
    )
    subparser.add_argument(
        "--latest",
        "-l",
        action="store_true",
        default=False,
        help="checksum the latest available version",
    )
    subparser.add_argument(
        "--preferred",
        "-p",
        action="store_true",
        default=False,
        help="checksum the known Spack preferred version",
    )
    modes_parser = subparser.add_mutually_exclusive_group()
    modes_parser.add_argument(
        "--add-to-package",
        "-a",
        action="store_true",
        default=False,
        help="add new versions to package",
    )
    modes_parser.add_argument(
        "--verify", action="store_true", default=False, help="verify known package checksums"
    )
    subparser.add_argument("package", help="name or spec (e.g. ``cmake`` or ``cmake@3.18``)")
    subparser.add_argument(
        "versions",
        nargs="*",
        help="checksum these specific versions (if omitted, Spack searches for remote versions)",
    )
    arguments.add_common_arguments(subparser, ["jobs"])
    subparser.epilog = (
        "examples:\n"
        "  `spack checksum zlib@1.2` autodetects versions 1.2.0 to 1.2.13 from the remote\n"
        "  `spack checksum zlib 1.2.13` checksums exact version 1.2.13 directly without search\n"
    )


def _checksum_direct_url_discovery(request: Dict[str, Any]) -> Dict[str, Any]:
    """Import a recipe and return direct URL candidates without network access."""
    spec = spack.spec.Spec(request["package"])
    pkg: PackageBase = spack.repo.PATH.get_pkg_class(spec.name)(spec)
    versions = [StandardVersion.from_string(version) for version in request["versions"]]
    if request["preferred"]:
        versions.append(preferred_version(pkg))

    return {
        "deprecated": [str(version) for version in versions if deprecated_version(pkg, version)],
        "download_instr": pkg.download_instr,
        "fetch_options": pkg.fetch_options,
        "manual_download": pkg.manual_download,
        "name": pkg.name,
        "urls": {str(version): pkg.all_urls_for_version(version) for version in versions},
        "versions": {
            str(version): attributes.get("sha256") for version, attributes in pkg.versions.items()
        },
    }


def _checksum_network_discovery(request: Dict[str, Any]) -> Dict[str, Any]:
    """Discover checksum URLs sequentially inside a network-supervised worker."""
    spec = spack.spec.Spec(request["package"])
    pkg: PackageBase = spack.repo.PATH.get_pkg_class(spec.name)(spec)
    spack.util.parallel.ENABLE_PARALLELISM = False

    versions = [StandardVersion.from_string(version) for version in request["versions"]]
    remote_versions = None
    with spack.config.CONFIG.override("config:url_fetch_method", "urllib"):
        if request["latest"]:
            remote_versions = pkg.fetch_remote_versions(concurrency=1)
            if remote_versions:
                versions.append(max(remote_versions))
        if request["preferred"]:
            versions.append(preferred_version(pkg))

        urls = {}
        deprecated = []
        for version in versions:
            if deprecated_version(pkg, version):
                deprecated.append(str(version))
            url = pkg.find_valid_url_for_version(version)
            if url is None:
                if remote_versions is None:
                    remote_versions = pkg.fetch_remote_versions(concurrency=1)
                url = remote_versions.get(version)
            if url is not None:
                urls[str(version)] = url

        if not versions:
            if remote_versions is None:
                remote_versions = pkg.fetch_remote_versions(concurrency=1)
            urls = {str(version): url for version, url in remote_versions.items()}

        changed = []
        for version_string, url in list(urls.items()):
            version = StandardVersion.from_string(version_string)
            possible_urls = pkg.all_urls_for_version(version)
            if url not in possible_urls:
                for possible_url in possible_urls:
                    if web_util.url_exists(possible_url):
                        urls[version_string] = possible_url
                        break
                else:
                    changed.append(version_string)

    return {
        "deprecated": deprecated,
        "download_instr": pkg.download_instr,
        "fetch_options": pkg.fetch_options,
        "manual_download": pkg.manual_download,
        "name": pkg.name,
        "url_changed": changed,
        "urls": urls,
        "versions": {
            str(version): attributes.get("sha256") for version, attributes in pkg.versions.items()
        },
    }


def _checksum_network_fetch(request: Dict[str, Any]) -> Dict[str, str]:
    """Fetch and checksum archives sequentially inside a network-supervised worker."""
    urls = {StandardVersion.from_string(version): url for version, url in request["urls"].items()}
    spack.util.parallel.ENABLE_PARALLELISM = False
    with spack.config.CONFIG.override("config:url_fetch_method", "urllib"):
        hashes = spack.stage.get_checksums_for_versions(
            urls,
            request["name"],
            keep_stage=request["keep_stage"],
            fetch_options=request["fetch_options"],
            concurrency=1,
        )
    return {str(version): checksum for version, checksum in hashes.items()}


def _recipe_import_read_roots() -> List[str]:
    roots = []
    for repo in spack.repo.PATH.repos:
        roots.append(repo.root)
        if repo.python_path:
            roots.append(repo.python_path)
    return roots + [
        spack.paths.etc_path,
        spack.paths.lib_path,
        spack.paths.system_config_path,
        spack.paths.user_config_path,
    ]


def _network_worker_setup():
    verify_paths = ssl.get_default_verify_paths()
    read_roots = _recipe_import_read_roots()
    for path in (verify_paths.cafile, verify_paths.capath):
        if path:
            read_roots.append(path)
    stdlib_path = sysconfig.get_path("stdlib")
    if stdlib_path:
        read_roots.append(stdlib_path)
    spack.sandbox.restrict_network_worker(read_roots, write_roots=[spack.stage.get_stage_root()])


def _network_checksum_package(args):
    request = {
        "latest": args.latest,
        "package": args.package,
        "preferred": args.preferred,
        "versions": args.versions,
    }
    response = spack.util.sandbox.run_json_worker_with_network(
        request,
        _checksum_network_discovery,
        DestinationPolicy.allow_any(),
        setup=_network_worker_setup,
    )
    if not isinstance(response, dict) or set(response) != {
        "deprecated",
        "download_instr",
        "fetch_options",
        "manual_download",
        "name",
        "url_changed",
        "urls",
        "versions",
    }:
        raise ValueError("checksum network worker returned an invalid response")
    if (
        not isinstance(response["name"], str)
        or response["name"] != spack.spec.Spec(args.package).name
        or not isinstance(response["manual_download"], bool)
    ):
        raise ValueError("checksum network worker returned an invalid response")
    if response["manual_download"]:
        raise ManualDownloadRequiredError(response["download_instr"])
    for key in ("deprecated", "url_changed"):
        if not isinstance(response[key], list) or not all(
            isinstance(version, str) for version in response[key]
        ):
            raise ValueError("checksum network worker returned an invalid response")
    if not isinstance(response["fetch_options"], dict) or not isinstance(response["urls"], dict):
        raise ValueError("checksum network worker returned an invalid response")

    urls = {}
    for version, url in response["urls"].items():
        if not isinstance(version, str) or not isinstance(url, str):
            raise ValueError("checksum network worker returned an invalid response")
        urls[StandardVersion.from_string(version)] = url
    versions = {}
    if not isinstance(response["versions"], dict):
        raise ValueError("checksum network worker returned an invalid response")
    for version, sha256 in response["versions"].items():
        if not isinstance(version, str) or sha256 is not None and not isinstance(sha256, str):
            raise ValueError("checksum network worker returned an invalid response")
        versions[StandardVersion.from_string(version)] = {"sha256": sha256}
    for version in response["deprecated"]:
        tty.warn(f"Version {version} is deprecated")
    package = SimpleNamespace(
        fetch_options=response["fetch_options"], name=response["name"], versions=versions
    )
    changed = {StandardVersion.from_string(version) for version in response["url_changed"]}
    return package, urls, changed


def _direct_checksum_package(args) -> Optional[Tuple[SimpleNamespace, Dict[StandardVersion, str]]]:
    """Return worker-derived direct URLs, or ``None`` for the existing command path."""
    if args.latest or (not args.versions and not args.preferred):
        return None
    if not spack.sandbox.recipe_import_sandbox_available():
        return None

    request = {"package": args.package, "preferred": args.preferred, "versions": args.versions}
    response = spack.util.sandbox.run_json_worker(
        request,
        _checksum_direct_url_discovery,
        setup=functools.partial(
            spack.sandbox.restrict_recipe_import, repository_roots=_recipe_import_read_roots()
        ),
    )
    if not isinstance(response, dict) or set(response) != {
        "deprecated",
        "download_instr",
        "fetch_options",
        "manual_download",
        "name",
        "urls",
        "versions",
    }:
        raise ValueError("checksum worker returned an invalid response")
    if (
        not isinstance(response["name"], str)
        or response["name"] != spack.spec.Spec(args.package).name
        or not isinstance(response["manual_download"], bool)
        or not isinstance(response["urls"], dict)
    ):
        raise ValueError("checksum worker returned an invalid response")
    if response["manual_download"]:
        raise ManualDownloadRequiredError(response["download_instr"])

    for version in response["deprecated"]:
        if not isinstance(version, str):
            raise ValueError("checksum worker returned an invalid response")
        tty.warn(f"Version {version} is deprecated")

    url_dict = {}
    for version, urls in response["urls"].items():
        if (
            not isinstance(version, str)
            or not isinstance(urls, list)
            or not all(isinstance(url, str) for url in urls)
        ):
            raise ValueError("checksum worker returned an invalid response")
        for url in urls:
            if web_util.url_exists(url):
                url_dict[StandardVersion.from_string(version)] = url
                break
        else:
            return None

    versions = {}
    if not isinstance(response["versions"], dict) or not isinstance(
        response["fetch_options"], dict
    ):
        raise ValueError("checksum worker returned an invalid response")
    for version, sha256 in response["versions"].items():
        if not isinstance(version, str) or sha256 is not None and not isinstance(sha256, str):
            raise ValueError("checksum worker returned an invalid response")
        versions[StandardVersion.from_string(version)] = {"sha256": sha256}
    package = SimpleNamespace(
        fetch_options=response["fetch_options"], name=response["name"], versions=versions
    )
    return package, url_dict


def checksum(parser, args):
    spec = spack.spec.Spec(args.package)
    if spack.sandbox.network_supervision_available():
        pkg, url_dict, changed = _network_checksum_package(args)
        return _checksum_urls(pkg, spec, args, url_dict, changed, network_worker=True)
    if not spack.sandbox.sandbox_fallback_allowed():
        raise spack.sandbox.SandboxError(
            "Checksum network supervision is unavailable and sandbox fallback is disabled"
        )
    direct = _direct_checksum_package(args)
    if direct is not None:
        pkg, url_dict = direct
        return _checksum_urls(pkg, spec, args, url_dict, set())
    return _checksum_in_process(args)


def _checksum_in_process(args):
    spec = spack.spec.Spec(args.package)

    # Get the package we're going to generate checksums for
    pkg: PackageBase = spack.repo.PATH.get_pkg_class(spec.name)(spec)

    # Skip manually downloaded packages
    if pkg.manual_download:
        raise ManualDownloadRequiredError(pkg.download_instr)

    versions = [StandardVersion.from_string(v) for v in args.versions]

    # Define placeholder for remote versions. This'll help reduce redundant work if we need to
    # check for the existence of remote versions more than once.
    remote_versions: Optional[Dict[StandardVersion, str]] = None

    # Add latest version if requested
    if args.latest:
        remote_versions = pkg.fetch_remote_versions(concurrency=args.jobs)
        if len(remote_versions) > 0:
            versions.append(max(remote_versions.keys()))

    # Add preferred version if requested (todo: exclude git versions)
    if args.preferred:
        versions.append(preferred_version(pkg))

    # Store a dict of the form version -> URL
    url_dict: Dict[StandardVersion, str] = {}

    for version in versions:
        if deprecated_version(pkg, version):
            tty.warn(f"Version {version} is deprecated")

        url = pkg.find_valid_url_for_version(version)
        if url is not None:
            url_dict[version] = url
            continue
        # If we get here, it's because no valid url was provided by the package. Do expensive
        # fallback to try to recover
        if remote_versions is None:
            remote_versions = pkg.fetch_remote_versions(concurrency=args.jobs)
        if version in remote_versions:
            url_dict[version] = remote_versions[version]

    if len(versions) <= 0:
        if remote_versions is None:
            remote_versions = pkg.fetch_remote_versions(concurrency=args.jobs)
        url_dict = remote_versions

    # A spidered URL can differ from the package.py *computed* URL, pointing to different tarballs.
    # For example, GitHub release pages sometimes have multiple tarballs with different shasum:
    # - releases/download/1.0/<pkg>-1.0.tar.gz (uploaded tarball)
    # - archive/refs/tags/1.0.tar.gz           (generated tarball)
    # We wanna ensure that `spack checksum` and `spack install` ultimately use the same URL, so
    # here we check whether the crawled and computed URLs disagree, and if so, prioritize the
    # former if that URL exists (just sending a HEAD request that is).
    url_changed_for_version = set()
    for version, url in url_dict.items():
        possible_urls = pkg.all_urls_for_version(version)
        if url not in possible_urls:
            for possible_url in possible_urls:
                if web_util.url_exists(possible_url):
                    url_dict[version] = possible_url
                    break
            else:
                url_changed_for_version.add(version)

    return _checksum_urls(pkg, spec, args, url_dict, url_changed_for_version)


def _checksum_urls(pkg, spec, args, url_dict, url_changed_for_version, network_worker=False):
    if not url_dict:
        tty.die(f"Could not find any remote versions for {pkg.name}")
    elif len(url_dict) > 1 and not args.batch and sys.stdin.isatty():
        filtered_url_dict = spack.stage.interactive_version_filter(
            url_dict,
            pkg.versions,
            url_changes=url_changed_for_version,
            initial_verion_filter=spec.versions,
        )
        if not filtered_url_dict:
            exit(0)
        url_dict = filtered_url_dict
    else:
        tty.info(f"Found {spack.util.string.plural(len(url_dict), 'version')} of {pkg.name}")

    if network_worker:
        response = spack.util.sandbox.run_json_worker_with_network(
            {
                "fetch_options": pkg.fetch_options,
                "keep_stage": args.keep_stage,
                "name": pkg.name,
                "urls": {str(version): url for version, url in url_dict.items()},
            },
            _checksum_network_fetch,
            DestinationPolicy.allow_any(),
            setup=_network_worker_setup,
        )
        if not isinstance(response, dict) or not all(
            isinstance(version, str) and isinstance(checksum, str)
            for version, checksum in response.items()
        ):
            raise ValueError("checksum network worker returned an invalid response")
        version_hashes = {
            StandardVersion.from_string(version): checksum
            for version, checksum in response.items()
        }
    else:
        version_hashes = spack.stage.get_checksums_for_versions(
            url_dict, pkg.name, keep_stage=args.keep_stage, fetch_options=pkg.fetch_options
        )

    if args.verify:
        print_checksum_status(pkg, version_hashes)
        sys.exit(0)

    # convert dict into package.py version statements
    version_lines = get_version_lines(version_hashes)
    print()
    print(version_lines)
    print()

    if args.add_to_package:
        path = spack.repo.PATH.filename_for_package_name(pkg.name)
        num_versions_added = add_versions_to_pkg(path, version_lines)
        tty.msg(f"Added {num_versions_added} new versions to {pkg.name} in {path}")
        if not args.batch and sys.stdin.isatty():
            editor(path)


def print_checksum_status(pkg: PackageBase, version_hashes: dict):
    """
    Verify checksums present in version_hashes against those present
    in the package's instructions.

    Args:
        pkg (spack.package_base.PackageBase): A package class for a given package in Spack.
        version_hashes (dict): A dictionary of the form: version -> checksum.

    """
    results = []
    num_verified = 0
    failed = False

    max_len = max(len(str(v)) for v in version_hashes)
    num_total = len(version_hashes)

    for version, sha in version_hashes.items():
        if version not in pkg.versions:
            msg = "No previous checksum"
            status = "-"

        elif sha == pkg.versions[version]["sha256"]:
            msg = "Correct"
            status = "="
            num_verified += 1

        else:
            msg = sha
            status = "x"
            failed = True

        results.append("{0:{1}}  {2} {3}".format(str(version), max_len, f"[{status}]", msg))

    # Display table of checksum results.
    tty.msg(
        f"Verified {num_verified} of {num_total}", "", *spack.util.lang.elide_list(results), ""
    )

    # Terminate at the end of function to prevent additional output.
    if failed:
        print()
        tty.die("Invalid checksums found.")


def _update_version_statements(package_src: str, version_lines: str) -> Tuple[int, str]:
    """Returns a tuple of number of versions added and the package's modified contents."""
    num_versions_added = 0
    version_statement_re = re.compile(r"([\t ]+version\([^\)]*\))")
    version_re = re.compile(r'[\t ]+version\(\s*"([^"]+)"[^\)]*\)')

    # Split rendered version lines into tuple of (version, version_line)
    # We reverse sort here to make sure the versions match the version_lines
    new_versions = []
    for ver_line in version_lines.split("\n"):
        match = version_re.match(ver_line)
        if match:
            new_versions.append((Version(match.group(1)), ver_line))

    split_contents = version_statement_re.split(package_src)

    for i, subsection in enumerate(split_contents):
        # If there are no more versions to add we should exit
        if len(new_versions) <= 0:
            break

        # Check if the section contains a version
        contents_version = version_re.match(subsection)
        if contents_version is not None:
            parsed_version = Version(contents_version.group(1))

            if parsed_version < new_versions[0][0]:
                split_contents[i:i] = [new_versions.pop(0)[1], "  # FIXME", "\n"]
                num_versions_added += 1

            elif parsed_version == new_versions[0][0]:
                new_versions.pop(0)

    return num_versions_added, "".join(split_contents)


def add_versions_to_pkg(path: str, version_lines: str) -> int:
    """Add new versions to a package.py file. Returns the number of versions added."""
    with open(path, "r", encoding="utf-8") as f:
        package_src = f.read()
    num_versions_added, package_src = _update_version_statements(package_src, version_lines)
    if num_versions_added > 0:
        with open(path, "w", encoding="utf-8") as f:
            f.write(package_src)
    return num_versions_added
