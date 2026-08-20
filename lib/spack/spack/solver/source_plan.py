# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Validation for declarative source plans produced by confined recipe workers.

The current schema supports checksummed URL sources and simply placed URL resources. Validation
is recipe-free so a trusted parent can reject malformed worker output without importing package
code.
"""

import re
import urllib.parse
from pathlib import PurePosixPath
from typing import Any, Dict, Optional

import spack.error
import spack.fetch_strategy
import spack.hash_types as ht

SOURCE_PLAN_SCHEMA_VERSION = 2
MAX_SOURCE_URLS = 32
MAX_RESOURCES = 32
MAX_SOURCE_PLAN_STRING = 4096

_SHA256 = re.compile(r"[0-9a-f]{64}")
_DAG_HASH = re.compile(r"[a-z2-7]{32}")
_PACKAGE_HASH = re.compile(r"[a-z2-7]{52}={4}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]+")
_EXTENSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,31}")


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


def _relative_path(value: Any, description: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
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
        if not isinstance(resource.placement, str) or not resource.placement:
            raise SourcePlanError("resource placement must be an explicit non-empty string")
        resources.append(
            {
                "name": resource.name,
                "source": _source_for_fetcher(resource.fetcher, "resource"),
                "destination": resource.destination,
                "placement": resource.placement,
            }
        )
    if spec.patches:
        raise SourcePlanError("source plan patches are unsupported")

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
        "patches": [],
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
    if type(schema_version) is not int or schema_version not in (1, SOURCE_PLAN_SCHEMA_VERSION):
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
            _relative_path(resource["placement"], "resource placement")
        if len(set(names)) != len(names):
            raise SourcePlanError("resource names must be unique")

    if plan["patches"] != []:
        raise SourcePlanError("source plan patches are unsupported")
    return plan


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
