# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from pathlib import Path

import pytest

import spack.cmd.sandbox_install as command
from spack.main import SpackCommand

sandbox_install = SpackCommand("sandbox-install")


def test_source_fetch_policy_normalizes_explicit_authorities(tmp_path):
    policy = command._source_fetch_policy(
        ["https://EXAMPLE.com", "http://mirror.example:8080/"], [str(tmp_path)]
    )

    assert policy.https_origins == frozenset({("example.com", 443)})
    assert policy.http_origins == frozenset({("mirror.example", 8080)})
    assert policy.file_roots == (tmp_path.resolve(),)


@pytest.mark.parametrize(
    "origin", ["ftp://example.com", "https://user@example.com", "https://example.com/path"]
)
def test_source_fetch_policy_rejects_non_origin(origin):
    with pytest.raises(ValueError, match="scheme and authority"):
        command._source_fetch_policy([origin], [])


def test_sandbox_install_composes_explicit_workflow(monkeypatch, mutable_config, tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    source_root = tmp_path / "sources"
    source_root.mkdir()
    concrete = object()
    source_plan = {"provenance": {"identity": "plan"}}
    calls = []

    def concretize(spec, **kwargs):
        calls.append(("concretize", spec, kwargs))
        return concrete

    def plan(spec, **kwargs):
        calls.append(("plan", spec, kwargs))
        return source_plan

    def prepare(plan_data, destination, **kwargs):
        calls.append(("prepare", plan_data, destination, kwargs))
        destination.mkdir()
        return destination

    def install(spec, plan_data, prepared, phases, **kwargs):
        assert Path(prepared).is_dir()
        calls.append(("install", spec, plan_data, prepared, phases, kwargs))
        return {"registration": {"prefix": "/installed/prefix"}}

    monkeypatch.setattr(command, "concretize_one_sandboxed", concretize)
    monkeypatch.setattr(command, "plan_sources_sandboxed", plan)
    monkeypatch.setattr(command, "prepare_stage", prepare)
    monkeypatch.setattr(command, "install_prepared_registered_sandboxed", install)
    mutable_config.set(
        "config:sandbox_installer",
        {
            "repositories": ["/configured/repository"],
            "source_origins": ["https://configured.example"],
            "file_roots": ["/configured/sources"],
            "repository_snapshots": False,
            "phases": ["install"],
            "post_actions": [],
            "timeout": 30,
        },
    )

    output = sandbox_install(
        "example@1.0",
        "--repository",
        str(repository),
        "--file-root",
        str(source_root),
        "--phase",
        "configure",
        "--phase",
        "install",
        "--post-action",
        "drop_redundant_rpaths",
        "--repository-snapshots",
        "--timeout",
        "120",
    )

    repositories = [str(repository.resolve())]
    assert calls[0] == (
        "concretize",
        "example@1.0",
        {"repositories": repositories, "timeout": 120.0, "repository_snapshots": True},
    )
    assert calls[1] == (
        "plan",
        concrete,
        {"repositories": repositories, "timeout": 120.0, "repository_snapshots": True},
    )
    assert calls[2][3]["expected_provenance"] == source_plan["provenance"]
    assert calls[2][3]["fetch_policy"].file_roots == (source_root.resolve(),)
    assert calls[3][4] == ["configure", "install"]
    assert calls[3][5]["explicit"] is True
    assert calls[3][5]["post_actions"] == ["drop_redundant_rpaths"]
    assert "Installed" in output
    assert "/installed/prefix" in output


def test_sandbox_install_uses_isolated_config_policy(monkeypatch, mutable_config, tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    source_root = tmp_path / "sources"
    source_root.mkdir()
    mutable_config.set(
        "config:sandbox_installer",
        {
            "repositories": [str(repository)],
            "file_roots": [str(source_root)],
            "repository_snapshots": False,
            "phases": ["build", "install"],
            "post_actions": ["set_permissions"],
            "timeout": 45,
        },
    )
    concrete = object()
    plan = {"provenance": {}}
    observed = {}

    monkeypatch.setattr(command, "concretize_one_sandboxed", lambda spec, **kwargs: concrete)
    monkeypatch.setattr(command, "plan_sources_sandboxed", lambda spec, **kwargs: plan)
    monkeypatch.setattr(command, "prepare_stage", lambda *args, **kwargs: object())

    def install(spec, source_plan, prepared, phases, **kwargs):
        observed.update({"phases": phases, **kwargs})
        return {"registration": {"prefix": "/installed/prefix"}}

    monkeypatch.setattr(command, "install_prepared_registered_sandboxed", install)

    sandbox_install("example")

    assert observed["repositories"] == [str(repository.resolve())]
    assert observed["phases"] == ["build", "install"]
    assert observed["post_actions"] == ["set_permissions"]
    assert observed["timeout"] == 45


def test_sandbox_install_requires_repository_policy(mutable_config):
    mutable_config.set("config:sandbox_installer", {})
    output = sandbox_install("example", fail_on_error=False)

    assert sandbox_install.returncode == 2
    assert "at least one --repository" in output


def test_sandbox_install_rejects_policy_before_recipe_worker(monkeypatch, tmp_path):
    def reject_worker(*args, **kwargs):
        raise AssertionError("recipe worker started for invalid policy")

    monkeypatch.setattr(command, "concretize_one_sandboxed", reject_worker)
    output = sandbox_install(
        "example",
        "--repository",
        str(tmp_path),
        "--phase",
        "install",
        "--phase",
        "install",
        fail_on_error=False,
    )

    assert sandbox_install.returncode == 2
    assert "invalid worker phase list" in output


def test_sandbox_install_rejects_action_order_before_recipe_worker(
    monkeypatch, mutable_config, tmp_path
):
    mutable_config.set(
        "config:sandbox_installer",
        {"repositories": [str(tmp_path)], "post_actions": ["set_permissions", "sbang"]},
    )

    def reject_worker(*args, **kwargs):
        raise AssertionError("recipe worker started for invalid policy")

    monkeypatch.setattr(command, "concretize_one_sandboxed", reject_worker)
    output = sandbox_install("example", fail_on_error=False)

    assert sandbox_install.returncode == 2
    assert "canonical order" in output


def test_sandbox_install_rejects_nonpositive_timeout(tmp_path):
    output = sandbox_install(
        "example", "--repository", str(tmp_path), "--timeout", "0", fail_on_error=False
    )

    assert sandbox_install.returncode == 2
    assert "--timeout must be greater than zero" in output
