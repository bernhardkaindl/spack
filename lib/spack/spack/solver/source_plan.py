# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Validation for declarative source plans produced by confined recipe workers.

The current schema supports checksummed URL sources, simply placed URL resources, and bounded
file patches. Validation is recipe-free so a trusted parent can reject malformed worker output
without importing package code.
"""

import base64
import binascii
import hashlib
import re
import urllib.parse
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional

import spack.error
import spack.fetch_strategy
import spack.hash_types as ht
import spack.patch
import spack.util.url

SOURCE_PLAN_SCHEMA_VERSION = 6
MAX_SOURCE_URLS = 32
MAX_RESOURCES = 32
MAX_RESOURCE_PLACEMENTS = 256
MAX_PATCHES = 32
MAX_PATCH_BYTES = 48 * 1024
MAX_PATCH_BYTES_TOTAL = 512 * 1024
MAX_SOURCE_PLAN_STRING = 4096
SUPPORTED_PATCH_ARCHIVE_EXTENSIONS = frozenset(
    (
        "tar",
        "tar.gz",
        "tgz",
        "tar.bz2",
        "tbz2",
        "tbz",
        "tar.xz",
        "txz",
        "TAR",
        "TAR.gz",
        "TAR.bz2",
        "TAR.xz",
        "zip",
        "whl",
        "gz",
        "bz2",
        "xz",
        "Z",
    )
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_DAG_HASH = re.compile(r"[a-z2-7]{32}")
_PACKAGE_HASH = re.compile(r"[a-z2-7]{52}={4}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]+")
_EXTENSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,31}")
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?\n")
_GIT_INDEX = re.compile(r"index [0-9a-f]+\.\.[0-9a-f]+(?: [0-7]{6})?\n")


class SourcePlanError(spack.error.SpackError):
    """Raised when a confined worker returns an invalid source plan."""


def _string(value: Any, description: str, *, pattern=None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SOURCE_PLAN_STRING
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise SourcePlanError(f"invalid {description}")
    return value


def _url(value: Any) -> str:
    url = _string(value, "source URL")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("file", "http", "https") or parsed.fragment:
        raise SourcePlanError("invalid source URL")
    if parsed.username is not None or parsed.password is not None:
        raise SourcePlanError("source URLs must not contain credentials")
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost") or not parsed.path.startswith("/"):
            raise SourcePlanError("invalid source URL")
    elif not parsed.netloc:
        raise SourcePlanError("invalid source URL")
    return url


def _relative_path(
    value: Any, description: str, *, allow_empty: bool = False, allow_dot: bool = False
) -> str:
    if allow_empty and value == "":
        return value
    if allow_dot and value == ".":
        return value
    path = _string(value, description)
    parsed = PurePosixPath(path)
    if (
        parsed.is_absolute()
        or "\\" in path
        or "\x00" in path
        or ".." in parsed.parts
        or str(parsed) != path
        or path in (".", "..")
    ):
        raise SourcePlanError(f"invalid {description}")
    return path


def _paths_overlap(first: str, second: str) -> bool:
    first_parts = PurePosixPath(first).parts
    second_parts = PurePosixPath(second).parts
    return (
        first_parts == second_parts[: len(first_parts)]
        or second_parts == first_parts[: len(second_parts)]
    )


def _resource_placement_for_plan(placement: Any) -> Any:
    if placement is None or isinstance(placement, str):
        return placement
    if not isinstance(placement, dict) or not placement:
        raise SourcePlanError("resource placement must be a string, mapping, or null")
    if any(
        not isinstance(source, str) or not isinstance(destination, str)
        for source, destination in placement.items()
    ):
        raise SourcePlanError("resource placement mapping paths must be strings")
    return [
        {"source": source, "destination": destination} for source, destination in placement.items()
    ]


def _patch_for_plan(patch: Any) -> Dict[str, Any]:
    common = {
        "owner": patch.owner,
        "sha256": patch.sha256,
        "level": patch.level,
        "working_dir": patch.working_dir,
        "reverse": patch.reverse,
    }
    if type(patch) is spack.patch.UrlPatch:
        extension = spack.util.url.extension_from_path(patch.url) if patch.archive_sha256 else None
        if patch.archive_sha256 and extension not in SUPPORTED_PATCH_ARCHIVE_EXTENSIONS:
            raise SourcePlanError("unsupported compressed URL patch extension")
        return {
            "kind": "url",
            **common,
            "url": patch.url,
            "archive_sha256": patch.archive_sha256,
            "extension": extension,
        }
    if type(patch) is not spack.patch.FilePatch or patch.path is None:
        raise SourcePlanError("only repository-local and URL file patches are supported")
    try:
        with open(patch.path, "rb") as stream:
            content = stream.read(MAX_PATCH_BYTES + 1)
    except OSError as error:
        raise SourcePlanError(f"cannot read repository patch: {error}") from error
    if len(content) > MAX_PATCH_BYTES:
        raise SourcePlanError("repository patch exceeds the size limit")
    sha256 = hashlib.sha256(content).hexdigest()
    if sha256 != patch.sha256:
        raise SourcePlanError("repository patch checksum changed during planning")
    targets = _validate_unified_diff(content, patch.level)
    return {
        "kind": "inline",
        **common,
        "targets": targets,
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def _patch_header_path(line: str, level: int) -> str:
    value = line[4:].rstrip("\n").split("\t", 1)[0]
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or ".." in path.parts
        or len(path.parts) <= level
    ):
        raise SourcePlanError("invalid unified patch target")
    return str(PurePosixPath(*path.parts[level:]))


def _validate_unified_diff(content: bytes, level: int) -> List[str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SourcePlanError("patch payload must be UTF-8 unified diff text") from error
    if "\x00" in text or "\r" in text:
        raise SourcePlanError("patch payload must use LF-terminated unified diff text")
    lines = text.splitlines(keepends=True)
    if not lines or any(not line.endswith("\n") for line in lines):
        raise SourcePlanError("patch payload must use LF-terminated unified diff text")
    targets = []
    index = 0
    while index < len(lines):
        git_targets = None
        if lines[index].startswith("diff --git "):
            fields = lines[index].rstrip("\n").split(" ")
            if len(fields) != 4:
                raise SourcePlanError("invalid Git patch preamble")
            git_targets = (
                _patch_header_path(f"--- {fields[2]}\n", level),
                _patch_header_path(f"+++ {fields[3]}\n", level),
            )
            if git_targets[0] != git_targets[1]:
                raise SourcePlanError("patch renames, creations, and deletions are unsupported")
            index += 1
            if index < len(lines) and lines[index].startswith("index "):
                if _GIT_INDEX.fullmatch(lines[index]) is None:
                    raise SourcePlanError("invalid Git patch index")
                index += 1
        if index >= len(lines):
            raise SourcePlanError("patch payload is missing a unified diff")
        if not lines[index].startswith("--- "):
            raise SourcePlanError("patch payload is not a supported unified diff")
        old_target = _patch_header_path(lines[index], level)
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise SourcePlanError("patch payload is missing a unified diff target")
        new_target = _patch_header_path(lines[index], level)
        if old_target != new_target:
            raise SourcePlanError("patch renames, creations, and deletions are unsupported")
        if git_targets is not None and git_targets != (old_target, new_target):
            raise SourcePlanError("Git patch preamble does not match unified diff targets")
        if old_target in targets:
            raise SourcePlanError("patch target must occur only once per payload")
        targets.append(old_target)
        index += 1
        hunks = 0
        while index < len(lines) and lines[index].startswith("@@ "):
            match = _HUNK.fullmatch(lines[index])
            if match is None:
                raise SourcePlanError("invalid unified patch hunk")
            old_remaining = int(match.group(2) or "1")
            new_remaining = int(match.group(4) or "1")
            index += 1
            while old_remaining or new_remaining:
                if index >= len(lines):
                    raise SourcePlanError("truncated unified patch hunk")
                prefix = lines[index][0]
                if prefix == " ":
                    old_remaining -= 1
                    new_remaining -= 1
                elif prefix == "-":
                    old_remaining -= 1
                elif prefix == "+":
                    new_remaining -= 1
                else:
                    raise SourcePlanError("invalid unified patch hunk body")
                if old_remaining < 0 or new_remaining < 0:
                    raise SourcePlanError("unified patch hunk exceeds declared size")
                index += 1
                if index < len(lines) and lines[index] == "\\ No newline at end of file\n":
                    index += 1
            hunks += 1
        if not hunks:
            raise SourcePlanError("patch target has no unified diff hunks")
    return targets


def _source_for_fetcher(fetcher: Any, description: str) -> Dict[str, Any]:
    if type(fetcher) is not spack.fetch_strategy.URLFetchStrategy:
        raise SourcePlanError(f"unsupported {description} fetch strategy")
    if fetcher.extra_options:
        raise SourcePlanError(f"{description} fetch options are unsupported")
    if not isinstance(fetcher.digest, str) or _SHA256.fullmatch(fetcher.digest) is None:
        raise SourcePlanError(f"{description} requires a SHA-256 checksum")
    source = {
        "kind": "url",
        "urls": list(fetcher.candidate_urls),
        "sha256": fetcher.digest,
        "expand": fetcher.expand_archive,
        "extension": fetcher.extension,
    }
    _validate_url_source(source, description)
    return source


def source_plan_for_spec(spec, repositories: Any) -> Dict[str, Any]:
    """Create a fixed-URL source plan from a concrete Spec inside a confined worker."""
    if not spec.concrete:
        raise SourcePlanError("source planning requires a concrete Spec")
    if spec.external:
        raise SourcePlanError("external packages are unsupported by source planning")

    package = spec.package
    source = _source_for_fetcher(package.fetcher, "source")
    needed_resources = package._get_needed_resources()
    if len(needed_resources) > MAX_RESOURCES:
        raise SourcePlanError("source plan has too many resources")
    resources = []
    for resource in needed_resources:
        placement = _resource_placement_for_plan(resource.placement)
        resource_source = _source_for_fetcher(resource.fetcher, "resource")
        if placement is None:
            if not resource_source["expand"]:
                raise SourcePlanError("implicit placement requires an expanding resource")
        resources.append(
            {
                "name": resource.name,
                "source": resource_source,
                "destination": resource.destination,
                "placement": placement,
            }
        )
    if len(spec.patches) > MAX_PATCHES:
        raise SourcePlanError("source plan has too many patches")
    patches = [_patch_for_plan(patch) for patch in spec.patches]

    package_hash = ht.package_hash(spec)
    plan = {
        "schema_version": SOURCE_PLAN_SCHEMA_VERSION,
        "provenance": {
            "dag_hash": spec.dag_hash(),
            "package_hash": package_hash,
            "repositories": repositories,
        },
        "source": source,
        "resources": resources,
        "patches": patches,
    }
    return validate_source_plan(plan)


def validate_source_plan(
    plan: Any, *, expected_provenance: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Validate and return a version-1 fixed-URL source plan."""
    if not isinstance(plan, dict) or set(plan) != {
        "schema_version",
        "provenance",
        "source",
        "resources",
        "patches",
    }:
        raise SourcePlanError("source plan has unexpected fields")
    schema_version = plan["schema_version"]
    if type(schema_version) is not int or schema_version not in (
        1,
        2,
        3,
        4,
        5,
        SOURCE_PLAN_SCHEMA_VERSION,
    ):
        raise SourcePlanError("unsupported source plan schema")

    provenance = plan["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {
        "dag_hash",
        "package_hash",
        "repositories",
    }:
        raise SourcePlanError("invalid source plan provenance")
    _string(provenance["dag_hash"], "DAG hash", pattern=_DAG_HASH)
    _string(provenance["package_hash"], "package hash", pattern=_PACKAGE_HASH)
    repositories = provenance["repositories"]
    if not isinstance(repositories, list) or not repositories or len(repositories) > 64:
        raise SourcePlanError("invalid source plan repositories")
    for repository in repositories:
        if not isinstance(repository, dict) or set(repository) != {
            "namespace",
            "package_api",
            "identity",
        }:
            raise SourcePlanError("invalid source plan repository")
        _string(repository["namespace"], "repository namespace", pattern=_IDENTIFIER)
        package_api = repository["package_api"]
        if (
            not isinstance(package_api, list)
            or len(package_api) != 2
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in package_api
            )
        ):
            raise SourcePlanError("invalid repository package API")
        _string(repository["identity"], "repository identity", pattern=_SHA256)
    if expected_provenance is not None and provenance != expected_provenance:
        raise SourcePlanError("source plan provenance does not match the request")

    _validate_url_source(plan["source"], "source")

    resources = plan["resources"]
    if schema_version == 1:
        if resources != []:
            raise SourcePlanError("source plan resources are unsupported by version 1")
    else:
        if not isinstance(resources, list) or len(resources) > MAX_RESOURCES:
            raise SourcePlanError("invalid source plan resources")
        names = []
        placement_entries = 0
        for resource in resources:
            if not isinstance(resource, dict) or set(resource) != {
                "name",
                "source",
                "destination",
                "placement",
            }:
                raise SourcePlanError("invalid source plan resource")
            names.append(_string(resource["name"], "resource name", pattern=_IDENTIFIER))
            _validate_url_source(resource["source"], "resource source")
            _relative_path(resource["destination"], "resource destination", allow_empty=True)
            placement = resource["placement"]
            if placement is None:
                if schema_version < 5 or resource["source"]["expand"] is not True:
                    raise SourcePlanError("invalid implicit resource placement")
            elif isinstance(placement, str):
                _relative_path(placement, "resource placement")
            elif schema_version >= 6 and isinstance(placement, list) and placement:
                placement_entries += len(placement)
                if placement_entries > MAX_RESOURCE_PLACEMENTS:
                    raise SourcePlanError("source plan has too many resource placements")
                sources = []
                destinations = []
                for mapping in placement:
                    if not isinstance(mapping, dict) or set(mapping) != {"source", "destination"}:
                        raise SourcePlanError("invalid resource placement mapping")
                    sources.append(
                        _relative_path(
                            mapping["source"], "resource placement source", allow_empty=True
                        )
                    )
                    destinations.append(
                        _relative_path(mapping["destination"], "resource placement destination")
                    )
                if any(
                    _paths_overlap(first, second)
                    for index, first in enumerate(sources)
                    for second in sources[index + 1 :]
                ):
                    raise SourcePlanError("resource placement sources overlap")
                if any(
                    _paths_overlap(first, second)
                    for index, first in enumerate(destinations)
                    for second in destinations[index + 1 :]
                ):
                    raise SourcePlanError("resource placement destinations overlap")
            else:
                raise SourcePlanError("invalid resource placement")
        if len(set(names)) != len(names):
            raise SourcePlanError("resource names must be unique")

    patches = plan["patches"]
    if schema_version in (1, 2):
        if patches != []:
            raise SourcePlanError(
                f"source plan patches are unsupported by version {schema_version}"
            )
    else:
        _validate_patches(patches, allow_url=schema_version >= 4)
    return plan


def _validate_patches(patches: Any, *, allow_url: bool) -> None:
    if not isinstance(patches, list) or len(patches) > MAX_PATCHES:
        raise SourcePlanError("invalid source plan patches")
    total_bytes = 0
    for patch in patches:
        if not isinstance(patch, dict):
            raise SourcePlanError("invalid source plan patch")
        common_fields = {"kind", "owner", "sha256", "level", "working_dir", "reverse"}
        kind = patch.get("kind")
        if kind == "inline":
            if set(patch) != common_fields | {"targets", "content_base64"}:
                raise SourcePlanError("invalid source plan patch")
        elif kind == "url" and allow_url:
            if set(patch) != common_fields | {"url", "archive_sha256", "extension"}:
                raise SourcePlanError("invalid source plan patch")
        else:
            raise SourcePlanError("unsupported patch kind")
        _string(patch["owner"], "patch owner", pattern=_IDENTIFIER)
        sha256 = _string(patch["sha256"], "patch SHA-256", pattern=_SHA256)
        level = patch["level"]
        if type(level) is not int or not 0 <= level <= 16:
            raise SourcePlanError("invalid patch level")
        _relative_path(patch["working_dir"], "patch working directory", allow_dot=True)
        if not isinstance(patch["reverse"], bool):
            raise SourcePlanError("invalid patch reverse policy")
        if kind == "url":
            _url(patch["url"])
            archive_sha256 = patch["archive_sha256"]
            if archive_sha256 is not None:
                _string(archive_sha256, "patch archive SHA-256", pattern=_SHA256)
            extension = patch["extension"]
            if extension is not None:
                _string(extension, "patch archive extension", pattern=_EXTENSION)
            if archive_sha256 is None and extension is not None:
                raise SourcePlanError("uncompressed URL patch cannot have an archive extension")
            if archive_sha256 is not None and extension not in SUPPORTED_PATCH_ARCHIVE_EXTENSIONS:
                raise SourcePlanError("unsupported compressed URL patch extension")
            continue
        targets = patch["targets"]
        if (
            not isinstance(targets, list)
            or not targets
            or len(targets) > 256
            or len(set(targets)) != len(targets)
        ):
            raise SourcePlanError("invalid source plan patch")
        for target in targets:
            _relative_path(target, "patch target")
        encoded = patch["content_base64"]
        if not isinstance(encoded, str) or not encoded:
            raise SourcePlanError("invalid patch payload")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise SourcePlanError("invalid patch payload") from error
        if base64.b64encode(content).decode("ascii") != encoded:
            raise SourcePlanError("patch payload is not canonically encoded")
        if not content or len(content) > MAX_PATCH_BYTES:
            raise SourcePlanError("patch payload exceeds the size limit")
        total_bytes += len(content)
        if total_bytes > MAX_PATCH_BYTES_TOTAL:
            raise SourcePlanError("patch payloads exceed the aggregate size limit")
        if hashlib.sha256(content).hexdigest() != sha256:
            raise SourcePlanError("patch payload checksum does not match")
        if _validate_unified_diff(content, level) != targets:
            raise SourcePlanError("patch targets do not match the payload")


def _validate_url_source(source: Any, description: str) -> None:
    if not isinstance(source, dict) or set(source) != {
        "kind",
        "urls",
        "sha256",
        "expand",
        "extension",
    }:
        raise SourcePlanError(f"invalid {description} description")
    if source["kind"] != "url":
        raise SourcePlanError(f"unsupported {description} kind")
    urls = source["urls"]
    if not isinstance(urls, list) or not urls or len(urls) > MAX_SOURCE_URLS:
        raise SourcePlanError(f"invalid {description} URLs")
    if len(set(_url(value) for value in urls)) != len(urls):
        raise SourcePlanError(f"{description} URLs must be unique")
    _string(source["sha256"], f"{description} SHA-256", pattern=_SHA256)
    if not isinstance(source["expand"], bool):
        raise SourcePlanError(f"invalid {description} expansion policy")
    extension = source["extension"]
    if extension is not None:
        _string(extension, f"{description} extension", pattern=_EXTENSION)
