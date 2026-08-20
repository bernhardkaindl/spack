# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from pathlib import Path
import socket
import sys
from typing import Any, cast

import pytest

import spack.sandbox
import spack.solver.concretize_worker as concretize_worker_module
from spack.solver.concretize_worker import (
    SandboxedConcretizationError,
    _load_response,
    concretize_one_sandboxed,
)
from spack.solver.repository_snapshot import (
    RepositorySnapshotError,
    create_repository_snapshot,
    repository_digest,
    snapshot_root,
)


def _prepend_recipe_code(repo_builder, package, code):
    recipe = Path(repo_builder._recipe_filename(package))
    recipe.write_text(f"{code}\n{recipe.read_text(encoding='utf-8')}", encoding="utf-8")


def test_repository_snapshot_has_deterministic_identity(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "repo.yaml").write_text("repo: {}\n", encoding="utf-8")
    (source / "packages").mkdir()
    (source / "packages" / "package.py").write_text("value = 1\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("ignored\n", encoding="utf-8")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "package.pyc").write_bytes(b"ignored")
    destination = snapshot_root(tmp_path / "snapshots", 0, "test.nested", (2, 0))

    identity = create_repository_snapshot(source, destination)

    assert destination == tmp_path / "snapshots" / "0" / "spack_repo" / "test" / "nested"
    assert identity == repository_digest(destination)
    assert not (destination / ".git").exists()
    (source / "packages" / "package.py").write_text("value = 2\n", encoding="utf-8")
    assert repository_digest(destination) == identity


def test_repository_snapshot_rejects_symlinks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "target").write_text("content\n", encoding="utf-8")
    (source / "link").symlink_to("target")

    with pytest.raises(RepositorySnapshotError, match="symlink"):
        create_repository_snapshot(source, tmp_path / "snapshot")


def test_response_rejects_duplicate_keys():
    with pytest.raises(SandboxedConcretizationError, match="duplicate JSON key"):
        _load_response(b'{"protocol_version":1,"ok":true,"ok":false}')


def test_concretize_one_sandboxed_round_trip(
    concretize_scope, mock_packages_repo, repo_builder
):
    repo_builder.add_package("sandbox-dependency")
    repo_builder.add_package(
        "sandbox-root", dependencies=[("sandbox-dependency", None, None)]
    )

    concrete = concretize_one_sandboxed(
        "sandbox-root@1.0",
        repositories=[repo_builder.root, mock_packages_repo],
    )

    assert concrete.concrete
    assert concrete.name == "sandbox-root"
    assert concrete["sandbox-dependency"].concrete
    assert concrete.namespace == repo_builder.namespace


def test_concretization_can_use_live_repositories_without_copying(
    concretize_scope, mock_packages_repo, repo_builder, monkeypatch
):
    repo_builder.add_package("sandbox-live-repository")

    def fail_if_called(*args, **kwargs):
        pytest.fail("repository snapshot creation should be disabled")

    monkeypatch.setattr(concretize_worker_module, "create_repository_snapshot", fail_if_called)

    concrete = concretize_one_sandboxed(
        "sandbox-live-repository@1.0",
        repositories=[repo_builder.root, mock_packages_repo],
        repository_snapshots=False,
    )

    assert concrete.concrete


def test_live_repository_change_is_rejected_before_concretization(
    concretize_scope, mock_packages_repo, repo_builder, monkeypatch
):
    repo_builder.add_package("sandbox-live-mutation")
    recipe = Path(repo_builder._recipe_filename("sandbox-live-mutation"))
    repository_root = Path(repo_builder.root).resolve()
    repository_digest = concretize_worker_module.repository_digest
    mutated = False

    def digest_then_mutate(root):
        nonlocal mutated
        identity = repository_digest(root)
        if root == repository_root and not mutated:
            recipe.write_text("raise RuntimeError('repository changed')\n", encoding="utf-8")
            mutated = True
        return identity

    monkeypatch.setattr(concretize_worker_module, "repository_digest", digest_then_mutate)

    with pytest.raises(SandboxedConcretizationError, match="repository identity mismatch"):
        concretize_one_sandboxed(
            "sandbox-live-mutation@1.0",
            repositories=[repo_builder.root, mock_packages_repo],
            repository_snapshots=False,
        )


def test_repository_snapshots_option_must_be_boolean(mock_packages_repo):
    with pytest.raises(SandboxedConcretizationError, match="must be a boolean"):
        concretize_one_sandboxed(
            "trivial-install-test-package",
            repositories=[mock_packages_repo],
            repository_snapshots=cast(Any, "false"),
        )


def test_concretization_uses_snapshot_after_source_changes(
    concretize_scope, mock_packages_repo, repo_builder, monkeypatch
):
    repo_builder.add_package("sandbox-snapshot-source")
    recipe = Path(repo_builder._recipe_filename("sandbox-snapshot-source"))
    create_snapshot = concretize_worker_module.create_repository_snapshot

    def create_then_mutate(source, destination):
        identity = create_snapshot(source, destination)
        recipe.write_text("raise RuntimeError('source repository was used')\n", encoding="utf-8")
        return identity

    monkeypatch.setattr(
        concretize_worker_module, "create_repository_snapshot", create_then_mutate
    )

    concrete = concretize_one_sandboxed(
        "sandbox-snapshot-source@1.0", repositories=[repo_builder.root, mock_packages_repo]
    )

    assert concrete.concrete


def test_recipe_import_cannot_modify_repository_snapshot(
    concretize_scope, mock_packages_repo, repo_builder
):
    repo_builder.add_package("sandbox-snapshot-write")
    _prepend_recipe_code(
        repo_builder,
        "sandbox-snapshot-write",
        "from pathlib import Path\nPath(__file__).write_text('modified')",
    )

    with pytest.raises(SandboxedConcretizationError, match="Permission denied"):
        concretize_one_sandboxed(
            "sandbox-snapshot-write@1.0",
            repositories=[repo_builder.root, mock_packages_repo],
        )


def test_repository_identity_mismatch_fails_before_concretization(
    concretize_scope, mock_packages_repo, repo_builder, monkeypatch
):
    repo_builder.add_package("sandbox-manifest-mismatch")
    create_snapshot = concretize_worker_module.create_repository_snapshot

    def create_with_wrong_identity(source, destination):
        create_snapshot(source, destination)
        return "0" * 64

    monkeypatch.setattr(
        concretize_worker_module, "create_repository_snapshot", create_with_wrong_identity
    )

    with pytest.raises(SandboxedConcretizationError, match="repository identity mismatch"):
        concretize_one_sandboxed(
            "sandbox-manifest-mismatch@1.0",
            repositories=[repo_builder.root, mock_packages_repo],
        )


def test_worker_binds_ordered_repository_identities(
    concretize_scope, mock_packages_repo, repo_builder, monkeypatch
):
    repo_builder.add_package("sandbox-overlay-order")
    validate_success = concretize_worker_module._validate_success
    observed = []

    def validate_and_record(response, requested, repositories):
        observed.extend(response["repositories"])
        return validate_success(response, requested, repositories)

    monkeypatch.setattr(concretize_worker_module, "_validate_success", validate_and_record)

    concrete = concretize_one_sandboxed(
        "sandbox-overlay-order@1.0", repositories=[repo_builder.root, mock_packages_repo]
    )

    assert concrete.concrete
    assert [identity["namespace"] for identity in observed] == [
        repo_builder.namespace,
        mock_packages_repo.namespace,
    ]
    assert all(len(identity["identity"]) == 64 for identity in observed)


def test_parent_rejects_inconsistent_package_hash_provenance(
    concretize_scope, mock_packages_repo, repo_builder, monkeypatch
):
    repo_builder.add_package("sandbox-package-hash")
    load_response = concretize_worker_module._load_response

    def load_with_altered_package_hash(data):
        response = load_response(data)
        if response.get("ok"):
            response["package_hashes"][0]["package_hash"] = "a" * 52 + "===="
        return response

    monkeypatch.setattr(concretize_worker_module, "_load_response", load_with_altered_package_hash)

    with pytest.raises(SandboxedConcretizationError, match="package hash provenance"):
        concretize_one_sandboxed(
            "sandbox-package-hash@1.0", repositories=[repo_builder.root, mock_packages_repo]
        )


def test_recipe_import_cannot_write_outside_private_state(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path
):
    sentinel = tmp_path / "outside-sandbox"
    repo_builder.add_package("sandbox-write")
    _prepend_recipe_code(
        repo_builder,
        "sandbox-write",
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('modified')",
    )

    with pytest.raises(SandboxedConcretizationError, match="Permission denied"):
        concretize_one_sandboxed(
            "sandbox-write@1.0", repositories=[repo_builder.root, mock_packages_repo]
        )

    assert not sentinel.exists()


def test_recipe_import_can_write_inside_private_state(
    concretize_scope, mock_packages_repo, repo_builder
):
    repo_builder.add_package("sandbox-private-write")
    _prepend_recipe_code(
        repo_builder,
        "sandbox-private-write",
        "from pathlib import Path\n(Path.home() / 'recipe-output').write_text('allowed')",
    )

    concrete = concretize_one_sandboxed(
        "sandbox-private-write@1.0", repositories=[repo_builder.root, mock_packages_repo]
    )

    assert concrete.concrete


def test_recipe_import_cannot_connect_tcp(
    concretize_scope, mock_packages_repo, repo_builder
):
    try:
        sandbox = spack.sandbox.get_sandbox()
    except spack.sandbox.SandboxError as error:
        pytest.skip(str(error))
    if sandbox.abi_version < 4:
        pytest.skip(f"TCP restrictions require Landlock ABI 4+, found ABI {sandbox.abi_version}")

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        repo_builder.add_package("sandbox-network")
        _prepend_recipe_code(
            repo_builder,
            "sandbox-network",
            f"import socket\nsocket.create_connection(('127.0.0.1', {port}))",
        )

        with pytest.raises(SandboxedConcretizationError, match="Permission denied"):
            concretize_one_sandboxed(
                "sandbox-network@1.0", repositories=[repo_builder.root, mock_packages_repo]
            )


def test_recipe_import_timeout(concretize_scope, mock_packages_repo, repo_builder):
    repo_builder.add_package("sandbox-timeout")
    _prepend_recipe_code(repo_builder, "sandbox-timeout", "while True:\n    pass")

    with pytest.raises(SandboxedConcretizationError, match="timed out"):
        concretize_one_sandboxed(
            "sandbox-timeout@1.0",
            repositories=[repo_builder.root, mock_packages_repo],
            timeout=0.25,
        )


def test_hash_reference_is_rejected(mock_packages_repo):
    with pytest.raises(SandboxedConcretizationError, match="hash references"):
        concretize_one_sandboxed("/abc123", repositories=[mock_packages_repo])


def test_recipe_diagnostic_output_is_bounded(
    concretize_scope, mock_packages_repo, repo_builder
):
    repo_builder.add_package("sandbox-output")
    _prepend_recipe_code(repo_builder, "sandbox-output", "print('x' * (3 * 1024 * 1024))")

    with pytest.raises(SandboxedConcretizationError, match="diagnostic output"):
        concretize_one_sandboxed(
            "sandbox-output@1.0", repositories=[repo_builder.root, mock_packages_repo]
        )


def test_recipe_descendants_are_killed(
    concretize_scope, mock_packages_repo, repo_builder
):
    with socket.socket(type=socket.SOCK_DGRAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.settimeout(1.25)
        port = listener.getsockname()[1]
        child_code = (
            "import socket,time; time.sleep(0.75); "
            f"socket.socket(type=socket.SOCK_DGRAM).sendto(b'alive', ('127.0.0.1', {port}))"
        )
        repo_builder.add_package("sandbox-descendant")
        _prepend_recipe_code(
            repo_builder,
            "sandbox-descendant",
            "import subprocess,sys\n"
            f"subprocess.Popen([{sys.executable!r}, '-c', {child_code!r}])",
        )

        concrete = concretize_one_sandboxed(
            "sandbox-descendant@1.0", repositories=[repo_builder.root, mock_packages_repo]
        )
        assert concrete.concrete
        with pytest.raises(socket.timeout):
            listener.recv(16)