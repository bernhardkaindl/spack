# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Validation for declarative source plans produced by confined recipe workers.

The initial schema supports one checksummed URL archive and reserves resources and patches for
later schema revisions. Validation is recipe-free so a trusted parent can reject malformed worker
output without importing package code.
"""

import re
from typing import Any, Dict, Optional
import urllib.parse

import spack.error
import spack.fetch_strategy
import spack.hash_types as ht


SOURCE_PLAN_SCHEMA_VERSION = 1
MAX_SOURCE_URLS = 32
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


def source_plan_for_spec(spec, repositories: Any) -> Dict[str, Any]:
    """Create a fixed-URL source plan from a concrete Spec inside a confined worker."""
    if not spec.concrete:
        raise SourcePlanError("source planning requires a concrete Spec")
    if spec.external:
        raise SourcePlanError("external packages are unsupported by source planning")

    package = spec.package
    fetcher = package.fetcher
    if type(fetcher) is not spack.fetch_strategy.URLFetchStrategy:
        raise SourcePlanError("unsupported source fetch strategy")
    if fetcher.extra_options:
        raise SourcePlanError("source fetch options are unsupported")
    if package._get_needed_resources():
        raise SourcePlanError("source plan resources are unsupported")
    if spec.patches:
        raise SourcePlanError("source plan patches are unsupported")
    if not isinstance(fetcher.digest, str) or _SHA256.fullmatch(fetcher.digest) is None:
        raise SourcePlanError("source planning requires a SHA-256 checksum")

    package_hash = ht.package_hash(spec)
    plan = {
        "schema_version": SOURCE_PLAN_SCHEMA_VERSION,
        "provenance": {
            "dag_hash": spec.dag_hash(),
            "package_hash": package_hash,
            "repositories": repositories,
        },
        "source": {
            "kind": "url",
            "urls": list(fetcher.candidate_urls),
            "sha256": fetcher.digest,
            "expand": fetcher.expand_archive,
            "extension": fetcher.extension,
        },
        "resources": [],
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
    if type(plan["schema_version"]) is not int or plan["schema_version"] != (
        SOURCE_PLAN_SCHEMA_VERSION
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

    source = plan["source"]
    if not isinstance(source, dict) or set(source) != {
        "kind",
        "urls",
        "sha256",
        "expand",
        "extension",
    }:
        raise SourcePlanError("invalid source description")
    if source["kind"] != "url":
        raise SourcePlanError("unsupported source kind")
    urls = source["urls"]
    if not isinstance(urls, list) or not urls or len(urls) > MAX_SOURCE_URLS:
        raise SourcePlanError("invalid source URLs")
    if len(set(_url(value) for value in urls)) != len(urls):
        raise SourcePlanError("source URLs must be unique")
    _string(source["sha256"], "source SHA-256", pattern=_SHA256)
    if not isinstance(source["expand"], bool):
        raise SourcePlanError("invalid source expansion policy")
    extension = source["extension"]
    if extension is not None:
        _string(extension, "source extension", pattern=_EXTENSION)

    if plan["resources"] != []:
        raise SourcePlanError("source plan resources are unsupported")
    if plan["patches"] != []:
        raise SourcePlanError("source plan patches are unsupported")
    return plan