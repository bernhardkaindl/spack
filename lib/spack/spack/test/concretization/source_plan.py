# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import base64
import copy
import hashlib
from pathlib import Path

import pytest

import spack.concretize
import spack.repo
import spack.solver.concretize_worker as concretize_worker_module
from spack.solver.concretize_worker import (
    SandboxedConcretizationError,
    concretize_one_sandboxed,
    plan_sources_sandboxed,
)
from spack.solver.source_plan import SourcePlanError, source_plan_for_spec, validate_source_plan


@pytest.fixture
def source_plan():
    return {
        "schema_version": 1,
        "provenance": {
            "dag_hash": "a" * 32,
            "package_hash": "b" * 52 + "====",
            "repositories": [
                {"namespace": "builtin.mock", "package_api": [2, 1], "identity": "c" * 64}
            ],
        },
        "source": {
            "kind": "url",
            "urls": ["https://example.com/archive.tar.gz"],
            "sha256": "d" * 64,
            "expand": True,
            "extension": "tar.gz",
        },
        "resources": [],
        "patches": [],
    }


def resource_description():
    return {
        "name": "headers",
        "source": {
            "kind": "url",
            "urls": ["https://example.com/headers.tar.gz"],
            "sha256": "e" * 64,
            "expand": True,
            "extension": None,
        },
        "destination": "vendor",
        "placement": "headers",
    }


def patch_description(content=b"--- a/file\n+++ b/file\n@@ -1 +1 @@\n-before\n+after\n"):
    return {
        "kind": "inline",
        "owner": "test.patch-owner",
        "sha256": hashlib.sha256(content).hexdigest(),
        "level": 1,
        "working_dir": ".",
        "reverse": False,
        "targets": ["file"],
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def url_patch_description():
    return {
        "kind": "url",
        "owner": "test.patch-owner",
        "sha256": "e" * 64,
        "level": 1,
        "working_dir": ".",
        "reverse": False,
        "url": "https://example.com/fix.patch",
        "archive_sha256": None,
        "extension": None,
    }


def test_validate_fixed_url_source_plan(source_plan):
    assert validate_source_plan(source_plan) is source_plan
    assert (
        validate_source_plan(
            source_plan, expected_provenance=copy.deepcopy(source_plan["provenance"])
        )
        is source_plan
    )


def test_validate_url_resource_source_plan(source_plan):
    source_plan["schema_version"] = 2
    source_plan["resources"] = [resource_description()]

    assert validate_source_plan(source_plan) is source_plan


def test_validate_implicit_resource_placement(source_plan):
    source_plan["schema_version"] = 5
    resource = resource_description()
    resource["placement"] = None
    source_plan["resources"] = [resource]

    assert validate_source_plan(source_plan) is source_plan


def test_validate_resource_mapping_placement(source_plan):
    source_plan["schema_version"] = 6
    resource = resource_description()
    resource["placement"] = [
        {"source": "include/api.h", "destination": "headers/api.h"},
        {"source": "lib", "destination": "vendor/lib"},
    ]
    source_plan["resources"] = [resource]

    assert validate_source_plan(source_plan) is source_plan


@pytest.mark.parametrize(
    "schema_version,placement,match",
    [
        (5, [{"source": "include", "destination": "headers"}], "placement"),
        (6, [], "placement"),
        (6, [{"source": "../include", "destination": "headers"}], "source"),
        (6, [{"source": "include", "destination": ""}], "destination"),
        (
            6,
            [
                {"source": "include", "destination": "headers"},
                {"source": "include/api.h", "destination": "api.h"},
            ],
            "sources overlap",
        ),
        (
            6,
            [
                {"source": "include", "destination": "headers"},
                {"source": "lib", "destination": "headers/lib"},
            ],
            "destinations overlap",
        ),
    ],
)
def test_source_plan_rejects_invalid_resource_mapping(
    source_plan, schema_version, placement, match
):
    source_plan["schema_version"] = schema_version
    resource = resource_description()
    resource["placement"] = placement
    source_plan["resources"] = [resource]

    with pytest.raises(SourcePlanError, match=match):
        validate_source_plan(source_plan)


def test_legacy_source_plan_rejects_implicit_resource_placement(source_plan):
    source_plan["schema_version"] = 4
    resource = resource_description()
    resource["placement"] = None
    source_plan["resources"] = [resource]

    with pytest.raises(SourcePlanError, match="implicit resource placement"):
        validate_source_plan(source_plan)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda resource: resource.update(extra=True), "resource"),
        (lambda resource: resource.update(name="../headers"), "resource name"),
        (lambda resource: resource.update(destination="../escape"), "destination"),
        (lambda resource: resource.update(placement="/escape"), "placement"),
        (lambda resource: resource["source"].update(sha256="md5"), "SHA-256"),
        (lambda resource: resource["source"].update(urls=[]), "URLs"),
    ],
)
def test_source_plan_rejects_malformed_url_resource(source_plan, mutation, match):
    source_plan["schema_version"] = 2
    resource = resource_description()
    mutation(resource)
    source_plan["resources"] = [resource]

    with pytest.raises(SourcePlanError, match=match):
        validate_source_plan(source_plan)


def test_source_plan_rejects_duplicate_resource_names(source_plan):
    source_plan["schema_version"] = 2
    source_plan["resources"] = [resource_description(), resource_description()]

    with pytest.raises(SourcePlanError, match="names must be unique"):
        validate_source_plan(source_plan)


def test_source_plan_rejects_too_many_resources(source_plan):
    source_plan["schema_version"] = 2
    source_plan["resources"] = []
    for index in range(33):
        resource = resource_description()
        resource["name"] = f"resource-{index}"
        source_plan["resources"].append(resource)

    with pytest.raises(SourcePlanError, match="resources"):
        validate_source_plan(source_plan)


def test_validate_inline_patch_source_plan(source_plan):
    source_plan["schema_version"] = 3
    source_plan["patches"] = [patch_description()]

    assert validate_source_plan(source_plan) is source_plan


def test_validate_url_patch_source_plan(source_plan):
    source_plan["schema_version"] = 4
    source_plan["patches"] = [url_patch_description()]

    assert validate_source_plan(source_plan) is source_plan


def test_validate_compressed_url_patch_source_plan(source_plan):
    source_plan["schema_version"] = 4
    patch = url_patch_description()
    patch.update(url="https://example.com/fix.tar.gz", archive_sha256="f" * 64, extension="tar.gz")
    source_plan["patches"] = [patch]

    assert validate_source_plan(source_plan) is source_plan


def test_source_plan_accepts_unix_compress_url_patch(source_plan):
    source_plan["schema_version"] = 4
    patch = url_patch_description()
    patch.update(url="https://example.com/fix.patch.Z", archive_sha256="f" * 64, extension="Z")
    source_plan["patches"] = [patch]

    assert validate_source_plan(source_plan) is source_plan


def test_source_plan_v3_rejects_url_patch(source_plan):
    source_plan["schema_version"] = 3
    source_plan["patches"] = [url_patch_description()]

    with pytest.raises(SourcePlanError, match="patch kind"):
        validate_source_plan(source_plan)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda patch: patch.update(extra=True), "source plan patch"),
        (lambda patch: patch.update(kind="url"), "patch kind"),
        (lambda patch: patch.update(level=-1), "patch level"),
        (lambda patch: patch.update(level=17), "patch level"),
        (lambda patch: patch.update(working_dir="../escape"), "working directory"),
        (lambda patch: patch.update(reverse=1), "reverse policy"),
        (lambda patch: patch.update(targets=["other"]), "targets do not match"),
        (lambda patch: patch.update(content_base64="not base64"), "patch payload"),
        (lambda patch: patch.update(sha256="0" * 64), "checksum"),
    ],
)
def test_source_plan_rejects_malformed_inline_patch(source_plan, mutation, match):
    source_plan["schema_version"] = 3
    patch = patch_description()
    mutation(patch)
    source_plan["patches"] = [patch]

    with pytest.raises(SourcePlanError, match=match):
        validate_source_plan(source_plan)


def test_source_plan_rejects_non_unified_patch(source_plan):
    source_plan["schema_version"] = 3
    source_plan["patches"] = [patch_description(b"1c\nmalicious\n.\n")]

    with pytest.raises(SourcePlanError, match="unified diff"):
        validate_source_plan(source_plan)


def test_source_plan_accepts_git_unified_patch(source_plan):
    content = (
        b"diff --git a/file b/file\n"
        b"index 1111111..2222222 100644\n"
        b"--- a/file\n"
        b"+++ b/file\n"
        b"@@ -1 +1 @@\n"
        b"-before\n"
        b"+after\n"
    )
    source_plan["schema_version"] = 3
    source_plan["patches"] = [patch_description(content)]

    assert validate_source_plan(source_plan) is source_plan


def test_source_plan_rejects_git_patch_target_mismatch(source_plan):
    content = b"diff --git a/other b/other\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-before\n+after\n"
    source_plan["schema_version"] = 3
    source_plan["patches"] = [patch_description(content)]

    with pytest.raises(SourcePlanError, match="preamble does not match"):
        validate_source_plan(source_plan)


def test_source_plan_rejects_mismatched_provenance(source_plan):
    expected = copy.deepcopy(source_plan["provenance"])
    expected["dag_hash"] = "z" * 32
    with pytest.raises(SourcePlanError, match="does not match"):
        validate_source_plan(source_plan, expected_provenance=expected)


@pytest.mark.parametrize(
    "url",
    [
        "relative/archive.tar.gz",
        "ssh://example.com/archive.tar.gz",
        "https://user:secret@example.com/archive.tar.gz",
        "https://example.com/archive.tar.gz#fragment",
        "file://remotehost/archive.tar.gz",
    ],
)
def test_source_plan_rejects_unsafe_url(source_plan, url):
    source_plan["source"]["urls"] = [url]
    with pytest.raises(SourcePlanError, match="source URL"):
        validate_source_plan(source_plan)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda plan: plan.update(extra=True), "unexpected fields"),
        (lambda plan: plan.update(schema_version=True), "unsupported.*schema"),
        (lambda plan: plan["source"].update(kind="git"), "unsupported source kind"),
        (lambda plan: plan["source"].update(sha256=""), "source SHA-256"),
        (lambda plan: plan["source"].update(urls=[]), "source URLs"),
        (lambda plan: plan["provenance"].update(repositories=[]), "repositories"),
        (lambda plan: plan.update(resources=[{"destination": "../escape"}]), "unsupported"),
        (lambda plan: plan.update(patches=[{"working_dir": "/escape"}]), "unsupported"),
    ],
)
def test_source_plan_rejects_unsupported_or_malformed_data(source_plan, mutation, match):
    plan = copy.deepcopy(source_plan)
    mutation(plan)
    with pytest.raises(SourcePlanError, match=match):
        validate_source_plan(plan)


@pytest.mark.use_package_hash
def test_source_plan_for_fixed_url_recipe(concretize_scope, mock_packages_repo, repo_builder):
    repo_builder.add_package("source-plan-fixed-url")
    recipe = Path(repo_builder._recipe_filename("source-plan-fixed-url"))
    recipe.write_text(
        recipe.read_text(encoding="utf-8")
        + "\n"
        + '    url = "https://example.com/source-plan-1.0.tar.gz"\n'
        + f'    version("2.0", sha256={"e" * 64!r})\n',
        encoding="utf-8",
    )
    repositories = [
        {"namespace": repo_builder.namespace, "package_api": [2, 0], "identity": "f" * 64}
    ]

    with spack.repo.use_repositories(repo_builder.root, mock_packages_repo):
        concrete = spack.concretize.concretize_one("source-plan-fixed-url@2.0")
        plan = source_plan_for_spec(concrete, repositories)

    assert plan["source"] == {
        "kind": "url",
        "urls": ["https://example.com/source-plan-2.0.tar.gz"],
        "sha256": "e" * 64,
        "expand": True,
        "extension": None,
    }
    assert validate_source_plan(plan, expected_provenance=plan["provenance"]) is plan


@pytest.mark.use_package_hash
def test_source_plan_for_url_resource(concretize_scope, mock_packages_repo, repo_builder):
    repo_builder.add_package("source-plan-resource")
    recipe = Path(repo_builder._recipe_filename("source-plan-resource"))
    recipe.write_text(
        recipe.read_text(encoding="utf-8")
        + "\n"
        + '    url = "https://example.com/source-plan-resource-1.0.tar.gz"\n'
        + f'    version("1.0", sha256={"d" * 64!r})\n'
        + "    resource(\n"
        + '        name="headers",\n'
        + '        url="https://example.com/headers.tar.gz",\n'
        + f"        sha256={'e' * 64!r},\n"
        + '        destination="vendor",\n'
        + '        placement="headers",\n'
        + '        when="@1.0",\n'
        + "    )\n",
        encoding="utf-8",
    )
    repositories = [
        {"namespace": repo_builder.namespace, "package_api": [2, 0], "identity": "f" * 64}
    ]

    with spack.repo.use_repositories(repo_builder.root, mock_packages_repo):
        concrete = spack.concretize.concretize_one("source-plan-resource@1.0")
        plan = source_plan_for_spec(concrete, repositories)

    assert plan["schema_version"] == 6
    assert plan["source"]["urls"] == ["https://example.com/source-plan-resource-1.0.tar.gz"]
    assert plan["resources"] == [resource_description()]


@pytest.mark.use_package_hash
def test_source_plan_for_implicit_resource_placement(
    concretize_scope, mock_packages_repo, repo_builder
):
    repo_builder.add_package("source-plan-implicit-resource")
    recipe = Path(repo_builder._recipe_filename("source-plan-implicit-resource"))
    recipe.write_text(
        recipe.read_text(encoding="utf-8")
        + "\n"
        + '    url = "https://example.com/source-plan-implicit-resource-1.0.tar.gz"\n'
        + f'    version("1.0", sha256={"d" * 64!r})\n'
        + "    resource(\n"
        + '        name="headers",\n'
        + '        url="https://example.com/headers.tar.gz",\n'
        + f"        sha256={'e' * 64!r},\n"
        + '        destination="vendor",\n'
        + '        when="@1.0",\n'
        + "    )\n",
        encoding="utf-8",
    )
    repositories = [
        {"namespace": repo_builder.namespace, "package_api": [2, 0], "identity": "f" * 64}
    ]

    with spack.repo.use_repositories(repo_builder.root, mock_packages_repo):
        concrete = spack.concretize.concretize_one("source-plan-implicit-resource@1.0")
        plan = source_plan_for_spec(concrete, repositories)

    assert plan["schema_version"] == 6
    assert plan["resources"][0]["placement"] is None


@pytest.mark.use_package_hash
def test_source_plan_for_repository_patch(concretize_scope, mock_packages_repo, repo_builder):
    repo_builder.add_package("source-plan-patch")
    recipe = Path(repo_builder._recipe_filename("source-plan-patch"))
    patch = recipe.with_name("fix.patch")
    content = b"--- a/file\n+++ b/file\n@@ -1 +1 @@\n-before\n+after\n"
    patch.write_bytes(content)
    recipe.write_text(
        recipe.read_text(encoding="utf-8")
        + "\n"
        + '    url = "https://example.com/source-plan-patch-1.0.tar.gz"\n'
        + f'    version("1.0", sha256={"d" * 64!r})\n'
        + '    patch("fix.patch", level=1, working_dir="src", reverse=True)\n',
        encoding="utf-8",
    )
    repositories = [
        {"namespace": repo_builder.namespace, "package_api": [2, 0], "identity": "f" * 64}
    ]

    with spack.repo.use_repositories(repo_builder.root, mock_packages_repo):
        concrete = spack.concretize.concretize_one("source-plan-patch@1.0")
        plan = source_plan_for_spec(concrete, repositories)

    assert plan["patches"] == [
        {
            **patch_description(content),
            "owner": f"{repo_builder.namespace}.source-plan-patch",
            "working_dir": "src",
            "reverse": True,
        }
    ]


@pytest.mark.use_package_hash
def test_source_plan_for_url_patch(concretize_scope, mock_packages_repo, repo_builder):
    repo_builder.add_package("source-plan-url-patch")
    recipe = Path(repo_builder._recipe_filename("source-plan-url-patch"))
    recipe.write_text(
        recipe.read_text(encoding="utf-8")
        + "\n"
        + '    url = "https://example.com/source-plan-url-patch-1.0.tar.gz"\n'
        + f'    version("1.0", sha256={"d" * 64!r})\n'
        + '    patch("https://example.com/fix.patch", sha256="'
        + "e" * 64
        + '")\n',
        encoding="utf-8",
    )
    repositories = [
        {"namespace": repo_builder.namespace, "package_api": [2, 0], "identity": "f" * 64}
    ]

    with spack.repo.use_repositories(repo_builder.root, mock_packages_repo):
        concrete = spack.concretize.concretize_one("source-plan-url-patch@1.0")
        plan = source_plan_for_spec(concrete, repositories)

    assert plan["patches"] == [
        {**url_patch_description(), "owner": f"{repo_builder.namespace}.source-plan-url-patch"}
    ]


@pytest.mark.use_package_hash
def test_source_plan_for_compressed_url_patch(concretize_scope, mock_packages_repo, repo_builder):
    repo_builder.add_package("source-plan-compressed-patch")
    recipe = Path(repo_builder._recipe_filename("source-plan-compressed-patch"))
    recipe.write_text(
        recipe.read_text(encoding="utf-8")
        + "\n"
        + '    url = "https://example.com/source-plan-compressed-patch-1.0.tar.gz"\n'
        + f'    version("1.0", sha256={"d" * 64!r})\n'
        + '    patch("https://example.com/fix.tar.gz", sha256="'
        + "e" * 64
        + '", archive_sha256="'
        + "f" * 64
        + '")\n',
        encoding="utf-8",
    )
    repositories = [
        {"namespace": repo_builder.namespace, "package_api": [2, 0], "identity": "f" * 64}
    ]

    with spack.repo.use_repositories(repo_builder.root, mock_packages_repo):
        concrete = spack.concretize.concretize_one("source-plan-compressed-patch@1.0")
        plan = source_plan_for_spec(concrete, repositories)

    assert plan["patches"] == [
        {
            **url_patch_description(),
            "owner": f"{repo_builder.namespace}.source-plan-compressed-patch",
            "url": "https://example.com/fix.tar.gz",
            "archive_sha256": "f" * 64,
            "extension": "tar.gz",
        }
    ]


@pytest.mark.use_package_hash
def test_source_plan_accepts_package_patch_method(
    concretize_scope, mock_packages_repo, repo_builder
):
    repo_builder.add_package("source-plan-patch-method")
    recipe = Path(repo_builder._recipe_filename("source-plan-patch-method"))
    recipe.write_text(
        recipe.read_text(encoding="utf-8")
        + "\n"
        + '    url = "https://example.com/source-plan-patch-method-1.0.tar.gz"\n'
        + f'    version("1.0", sha256={"d" * 64!r})\n'
        + "    def patch(self):\n"
        + "        pass\n",
        encoding="utf-8",
    )

    with spack.repo.use_repositories(repo_builder.root, mock_packages_repo):
        concrete = spack.concretize.concretize_one("source-plan-patch-method@1.0")
        plan = source_plan_for_spec(
            concrete,
            [{"namespace": repo_builder.namespace, "package_api": [2, 0], "identity": "f" * 64}],
        )

    assert plan["patches"] == []


@pytest.mark.use_package_hash
def test_source_plan_normalizes_resource_dictionary_placement(
    concretize_scope, mock_packages_repo, repo_builder
):
    repo_builder.add_package("source-plan-resource-dict")
    recipe = Path(repo_builder._recipe_filename("source-plan-resource-dict"))
    recipe.write_text(
        recipe.read_text(encoding="utf-8")
        + "\n"
        + '    url = "https://example.com/source-plan-resource-dict-1.0.tar.gz"\n'
        + f'    version("1.0", sha256={"d" * 64!r})\n'
        + "    resource(\n"
        + '        name="headers",\n'
        + '        url="https://example.com/headers.tar.gz",\n'
        + f"        sha256={'e' * 64!r},\n"
        + '        placement={"include/api.h": "headers/api.h", "lib": "vendor/lib"},\n'
        + '        when="@1.0",\n'
        + "    )\n",
        encoding="utf-8",
    )

    repositories = [
        {"namespace": repo_builder.namespace, "package_api": [2, 0], "identity": "f" * 64}
    ]

    with spack.repo.use_repositories(repo_builder.root, mock_packages_repo):
        concrete = spack.concretize.concretize_one("source-plan-resource-dict@1.0")
        plan = source_plan_for_spec(concrete, repositories)

    assert plan["resources"][0]["placement"] == [
        {"source": "include/api.h", "destination": "headers/api.h"},
        {"source": "lib", "destination": "vendor/lib"},
    ]


@pytest.mark.use_package_hash
def test_plan_sources_sandboxed(concretize_scope, mock_packages_repo, repo_builder, monkeypatch):
    repo_builder.add_package("sandbox-source-plan")
    recipe = Path(repo_builder._recipe_filename("sandbox-source-plan"))
    recipe.write_text(
        recipe.read_text(encoding="utf-8")
        + "\n"
        + '    url = "https://example.com/sandbox-source-plan-1.0.tar.gz"\n'
        + f'    version("1.0", sha256={"d" * 64!r})\n',
        encoding="utf-8",
    )
    repositories = [repo_builder.root, mock_packages_repo]

    concrete = concretize_one_sandboxed("sandbox-source-plan@1.0", repositories=repositories)

    def reject_parent_package_import(*args, **kwargs):
        raise AssertionError("trusted parent imported recipe code")

    monkeypatch.setattr(spack.repo.PATH, "get_pkg_class", reject_parent_package_import)
    plan = plan_sources_sandboxed(concrete, repositories=repositories)

    assert plan["provenance"]["dag_hash"] == concrete.dag_hash()
    assert plan["source"]["urls"] == ["https://example.com/sandbox-source-plan-1.0.tar.gz"]
    assert plan["source"]["sha256"] == "d" * 64


@pytest.mark.use_package_hash
def test_plan_sources_sandboxed_rejects_fetch_options(
    concretize_scope, mock_packages_repo, repo_builder
):
    repo_builder.add_package("sandbox-source-options")
    recipe = Path(repo_builder._recipe_filename("sandbox-source-options"))
    recipe.write_text(
        recipe.read_text(encoding="utf-8")
        + "\n"
        + '    url = "https://example.com/sandbox-source-options-1.0.tar.gz"\n'
        + f'    version("1.0", sha256={"c" * 64!r}, fetch_options={{"timeout": 5}})\n',
        encoding="utf-8",
    )
    repositories = [repo_builder.root, mock_packages_repo]

    concrete = concretize_one_sandboxed("sandbox-source-options@1.0", repositories=repositories)
    with pytest.raises(SandboxedConcretizationError, match="fetch options are unsupported"):
        plan_sources_sandboxed(concrete, repositories=repositories)


@pytest.mark.use_package_hash
def test_plan_sources_sandboxed_rejects_tampered_provenance(
    concretize_scope, mock_packages_repo, repo_builder, monkeypatch
):
    repo_builder.add_package("sandbox-source-tamper")
    recipe = Path(repo_builder._recipe_filename("sandbox-source-tamper"))
    recipe.write_text(
        recipe.read_text(encoding="utf-8")
        + "\n"
        + '    url = "https://example.com/sandbox-source-tamper-1.0.tar.gz"\n'
        + f'    version("1.0", sha256={"b" * 64!r})\n',
        encoding="utf-8",
    )
    repositories = [repo_builder.root, mock_packages_repo]
    concrete = concretize_one_sandboxed("sandbox-source-tamper@1.0", repositories=repositories)
    load_response = concretize_worker_module._load_response

    def load_with_altered_provenance(data):
        response = load_response(data)
        if response.get("source_plan"):
            response["source_plan"]["provenance"]["dag_hash"] = "a" * 32
        return response

    monkeypatch.setattr(concretize_worker_module, "_load_response", load_with_altered_provenance)
    with pytest.raises(SandboxedConcretizationError, match="provenance does not match"):
        plan_sources_sandboxed(concrete, repositories=repositories)
