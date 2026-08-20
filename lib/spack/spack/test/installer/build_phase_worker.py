# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Tests for provenance-bound build phases over trusted prepared source trees."""

import contextlib
import copy
import hashlib
import io
import os
import socket
import stat
import tarfile
from pathlib import Path

import pytest

import spack.config
import spack.hooks
import spack.installer.build_phase_worker
import spack.repo
import spack.spec
import spack.verify
from spack.installer.build_phase_worker import (
    SandboxedBuildPhaseError,
    install_prepared_registered_sandboxed,
    install_prepared_sandboxed,
    run_build_phase_sandboxed,
)
from spack.installer.install_tree import InstallTreeError, install_tree_metadata
from spack.solver.concretize_worker import concretize_one_sandboxed, plan_sources_sandboxed
from spack.solver.prepared_stage import (
    PreparedStage,
    SourceFetchPolicy,
    prepare_stage,
    source_plan_digest,
)


def _write_source_archive(path: Path) -> str:
    """Write a minimal source archive and return its SHA-256 checksum."""
    contents = b"prepared source\n"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("project/README")
        info.size = len(contents)
        archive.addfile(info, io.BytesIO(contents))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _add_build_recipe(repo_builder, name: str, source_url: str, checksum: str, body: str) -> None:
    """Create a fixed-source test recipe with the supplied class body."""
    repo_builder.add_package(name)
    recipe = Path(repo_builder._recipe_filename(name))
    text = recipe.read_text(encoding="utf-8")
    text = text.replace(
        '    url = "http://www.example.com/root-1.0.tar.gz"', f"    url = {source_url!r}"
    )
    text = text.replace(
        "    version(\"1.0\", sha256='abcde')",
        f'    version("1.0", sha256={checksum!r}, url={source_url!r})',
    )
    recipe.write_text(text + "\n" + body, encoding="utf-8")


def _prepare_build(repo_builder, mock_packages_repo, tmp_path, name, body):
    """Concretize, plan, and prepare one generated package for a build test."""
    source = tmp_path / f"{name}.tar.gz"
    checksum = _write_source_archive(source)
    _add_build_recipe(repo_builder, name, source.as_uri(), checksum, body)
    repositories = [repo_builder.root, mock_packages_repo]
    concrete = concretize_one_sandboxed(f"{name}@1.0", repositories=repositories)
    plan = plan_sources_sandboxed(concrete, repositories=repositories)
    prepared = prepare_stage(
        plan,
        tmp_path / "prepared",
        expected_provenance=plan["provenance"],
        fetch_policy=SourceFetchPolicy(file_roots=(tmp_path,)),
    )
    return concrete, plan, prepared, repositories


@pytest.mark.use_package_hash
def test_run_prepared_build_phase_sandboxed(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path, monkeypatch
):
    """Run setup, one phase, and its callback without importing recipes in the parent."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-build-phase",
        """    def install(self, spec, prefix):
        \"\"\"Write phase outputs to the allowed prefix and prepared source tree.\"\"\"
        import os
        from pathlib import Path
        Path(prefix).joinpath("installed.txt").write_text("installed")
        Path(prefix).joinpath("short-spec.txt").write_text(os.environ["SPACK_SHORT_SPEC"])
        Path(self.stage.source_path).joinpath("phase.txt").write_text("phase ran")

    @run_after("install")
    def after_install(self):
        \"\"\"Record execution of the confined package-local callback.\"\"\"
        from pathlib import Path
        Path(self.prefix).joinpath("callback.txt").write_text("callback ran")
""",
    )

    def reject_parent_package_import(*args, **kwargs):
        """Fail if the trusted parent attempts to resolve the package class."""
        raise AssertionError("trusted parent imported recipe code")

    monkeypatch.setattr(spack.repo.PATH, "get_pkg_class", reject_parent_package_import)
    prefix = tmp_path / "prefix"
    response = run_build_phase_sandboxed(
        concrete, plan, prepared, "install", prefix=prefix, repositories=repositories
    )

    assert response["phase"] == "install"
    assert (prefix / "installed.txt").read_text(encoding="utf-8") == "installed"
    assert "sandbox-build-phase" in (prefix / "short-spec.txt").read_text(encoding="utf-8")
    assert (prefix / "callback.txt").read_text(encoding="utf-8") == "callback ran"
    assert (prepared.path / "phase.txt").read_text(encoding="utf-8") == "phase ran"


@pytest.mark.use_package_hash
def test_build_output_uses_dedicated_bounded_log(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path
):
    """Keep recipe stdout and stderr out of the JSON response and identify the build log."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-build-log",
        """    def install(self, spec, prefix):
        \"\"\"Emit output that must not corrupt the worker protocol.\"\"\"
        import sys
        from pathlib import Path
        print("phase stdout")
        print("phase stderr", file=sys.stderr)
        Path(prefix).joinpath("installed.txt").write_text("installed")
""",
    )
    log_path = tmp_path / "build.log"

    response = run_build_phase_sandboxed(
        concrete,
        plan,
        prepared,
        "install",
        prefix=tmp_path / "prefix",
        repositories=repositories,
        log_path=log_path,
    )

    contents = log_path.read_bytes()
    assert b"phase stdout" in contents
    assert b"phase stderr" in contents
    assert response["build_log"] == {
        "path": str(log_path),
        "size": len(contents),
        "sha256": hashlib.sha256(contents).hexdigest(),
    }


@pytest.mark.use_package_hash
def test_build_log_does_not_replace_existing_file(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path
):
    """Reject an existing parent log path before creating the build prefix."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-build-log-existing",
        """    def install(self, spec, prefix):
        \"\"\"Provide a phase that must not run when log creation fails.\"\"\"
        pass
""",
    )
    log_path = tmp_path / "build.log"
    log_path.write_text("existing", encoding="utf-8")

    with pytest.raises(SandboxedBuildPhaseError, match="cannot create build log"):
        run_build_phase_sandboxed(
            concrete,
            plan,
            prepared,
            "install",
            prefix=tmp_path / "prefix",
            repositories=repositories,
            log_path=log_path,
        )

    assert log_path.read_text(encoding="utf-8") == "existing"
    assert not (tmp_path / "prefix").exists()


@pytest.mark.use_package_hash
def test_build_response_binds_install_tree(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path
):
    """Bind regular files, directories, modes, and symlinks to the build response."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-install-tree",
        """    def install(self, spec, prefix):
        \"\"\"Create representative install-tree entries.\"\"\"
        import os
        from pathlib import Path
        binary = Path(prefix).joinpath("bin")
        binary.mkdir()
        executable = binary.joinpath("tool")
        executable.write_text("tool")
        executable.chmod(0o755)
        os.symlink("tool", binary.joinpath("tool-link"))
""",
    )
    prefix = tmp_path / "prefix"

    response = run_build_phase_sandboxed(
        concrete, plan, prepared, "install", prefix=prefix, repositories=repositories
    )

    assert response["install_tree"] == install_tree_metadata(prefix)


@pytest.mark.use_package_hash
def test_install_prepared_rolls_back_unverified_tree(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path, monkeypatch
):
    """Restore the old prefix when parent and worker install-tree identities differ."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-install-tree-mismatch",
        """    def install(self, spec, prefix):
        \"\"\"Create output that must not commit without parent verification.\"\"\"
        from pathlib import Path
        Path(prefix).joinpath("new.txt").write_text("new")
""",
    )
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "original.txt").write_text("original", encoding="utf-8")

    def mismatched_install_tree(path):
        """Return validly shaped metadata that cannot match the worker result."""
        metadata = install_tree_metadata(path)
        return {**metadata, "sha256": "0" * 64}

    monkeypatch.setattr(
        spack.installer.build_phase_worker, "install_tree_metadata", mismatched_install_tree
    )
    with pytest.raises(SandboxedBuildPhaseError, match="changed after worker verification"):
        install_prepared_sandboxed(
            concrete, plan, prepared, ["install"], prefix=prefix, repositories=repositories
        )

    assert (prefix / "original.txt").read_text(encoding="utf-8") == "original"
    assert not (prefix / "new.txt").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX special files")
def test_install_tree_metadata_rejects_special_files(tmp_path):
    """Fail closed when an install tree contains an unsupported special file."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "regular").write_text("regular", encoding="utf-8")
    fifo = prefix / "fifo"
    os.mkfifo(fifo)

    with pytest.raises(InstallTreeError, match="unsupported install-tree entry: fifo"):
        install_tree_metadata(prefix)


@pytest.mark.use_package_hash
def test_install_prepared_publishes_trusted_metadata(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path, monkeypatch
):
    """Publish database- and verification-compatible metadata without parent recipe import."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-install-metadata",
        """    def install(self, spec, prefix):
        \"\"\"Create one installed file for the trusted manifest.\"\"\"
        from pathlib import Path
        Path(prefix).joinpath("installed.txt").write_text("installed")
""",
    )

    def reject_parent_package_import(*args, **kwargs):
        """Fail if trusted metadata publication resolves a package class."""
        raise AssertionError("trusted parent imported recipe code")

    monkeypatch.setattr(spack.repo.PATH, "get_pkg_class", reject_parent_package_import)
    prefix = tmp_path / "prefix"
    response = install_prepared_sandboxed(
        concrete, plan, prepared, ["install"], prefix=prefix, repositories=repositories
    )

    metadata = response["install_metadata"]
    assert metadata["spec_path"] == ".spack/spec.json"
    assert metadata["manifest_path"] == ".spack/install_manifest.json"
    assert metadata["install_tree"] == install_tree_metadata(prefix)
    installed_spec = spack.spec.Spec.from_json((prefix / metadata["spec_path"]).read_text())
    assert installed_spec.dag_hash() == concrete.dag_hash()
    installed_spec.set_prefix(str(prefix))
    assert not spack.verify.check_spec_manifest(installed_spec).has_errors()


@pytest.mark.use_package_hash
def test_install_prepared_rejects_package_metadata(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path
):
    """Roll back when a package attempts to occupy the trusted metadata directory."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-install-metadata-conflict",
        """    def install(self, spec, prefix):
        \"\"\"Create a reserved metadata path that the parent must reject.\"\"\"
        from pathlib import Path
        metadata = Path(prefix).joinpath(".spack")
        metadata.mkdir()
        metadata.joinpath("untrusted").write_text("untrusted")
        Path(prefix).joinpath("installed.txt").write_text("installed")
""",
    )
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "original.txt").write_text("original", encoding="utf-8")

    with pytest.raises(SandboxedBuildPhaseError, match="cannot publish install metadata"):
        install_prepared_sandboxed(
            concrete, plan, prepared, ["install"], prefix=prefix, repositories=repositories
        )

    assert (prefix / "original.txt").read_text(encoding="utf-8") == "original"
    assert not (prefix / ".spack").exists()


@pytest.mark.use_package_hash
def test_install_prepared_registers_under_parent_lock(
    concretize_scope, mock_packages_repo, repo_builder, temporary_store, tmp_path, monkeypatch
):
    """Commit a database-ready prefix at the store projection without parent recipe import."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-install-registered",
        """    def install(self, spec, prefix):
        \"\"\"Create installed content for parent registration.\"\"\"
        from pathlib import Path
        Path(prefix).joinpath("installed.txt").write_text("installed")
""",
    )

    def reject_parent_package_import(*args, **kwargs):
        """Fail if parent registration resolves recipe code."""
        raise AssertionError("trusted parent imported recipe code")

    lock_active = False
    original_write_lock = temporary_store.prefix_locker.write_lock
    original_add = temporary_store.db.add

    @contextlib.contextmanager
    def observed_write_lock(spec):
        """Record when the real per-spec write lock is held."""
        nonlocal lock_active
        with original_write_lock(spec):
            lock_active = True
            try:
                yield
            finally:
                lock_active = False

    def observed_add(*args, **kwargs):
        """Require registration to happen while the prefix lock is held."""
        assert lock_active
        return original_add(*args, **kwargs)

    monkeypatch.setattr(spack.repo.PATH, "get_pkg_class", reject_parent_package_import)
    monkeypatch.setattr(temporary_store.prefix_locker, "write_lock", observed_write_lock)
    monkeypatch.setattr(temporary_store.db, "add", observed_add)
    response = install_prepared_registered_sandboxed(
        concrete,
        plan,
        prepared,
        ["install"],
        store=temporary_store,
        repositories=repositories,
        explicit=True,
    )

    prefix = Path(temporary_store.layout.path_for_spec(concrete))
    record = temporary_store.db.get_record(concrete)
    assert record is not None and record.installed and record.explicit
    assert response["registration"] == {
        "dag_hash": concrete.dag_hash(),
        "explicit": True,
        "prefix": str(prefix),
    }
    temporary_store.layout.ensure_installed(concrete)
    assert (prefix / "installed.txt").read_text(encoding="utf-8") == "installed"


@pytest.mark.use_package_hash
def test_install_prepared_rolls_back_database_failure(
    concretize_scope, mock_packages_repo, repo_builder, temporary_store, tmp_path, monkeypatch
):
    """Restore the previous projected prefix when parent database registration fails."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-install-register-failure",
        """    def install(self, spec, prefix):
        \"\"\"Create replacement content that must not survive a database failure.\"\"\"
        from pathlib import Path
        Path(prefix).joinpath("new.txt").write_text("new")
""",
    )
    prefix = Path(temporary_store.layout.path_for_spec(concrete))
    prefix.mkdir(parents=True)
    (prefix / "original.txt").write_text("original", encoding="utf-8")

    def fail_registration(*args, **kwargs):
        """Simulate a failed trusted database transaction."""
        raise RuntimeError("database failed")

    monkeypatch.setattr(temporary_store.db, "add", fail_registration)
    with pytest.raises(SandboxedBuildPhaseError, match="database failed"):
        install_prepared_registered_sandboxed(
            concrete, plan, prepared, ["install"], store=temporary_store, repositories=repositories
        )

    assert (prefix / "original.txt").read_text(encoding="utf-8") == "original"
    assert not (prefix / "new.txt").exists()


@pytest.mark.use_package_hash
def test_install_prepared_runs_typed_permission_action(
    concretize_scope, mock_packages_repo, repo_builder, temporary_store, tmp_path
):
    """Apply an allowlisted parent permission action before metadata and registration."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-install-permissions",
        """    def install(self, spec, prefix):
        \"\"\"Create modes and a symlink for trusted normalization.\"\"\"
        import os
        from pathlib import Path
        binary = Path(prefix).joinpath("bin")
        binary.mkdir(mode=0o700)
        tool = binary.joinpath("tool")
        tool.write_text("tool")
        tool.chmod(0o700)
        binary.joinpath("data").write_text("data")
        os.symlink("tool", binary.joinpath("tool-link"))
""",
    )

    with spack.config.CONFIG.override(
        "packages:all:permissions:read", "world"
    ), spack.config.CONFIG.override("packages:all:permissions:write", "user"):
        response = install_prepared_registered_sandboxed(
            concrete,
            plan,
            prepared,
            ["install"],
            store=temporary_store,
            repositories=repositories,
            post_actions=["set_permissions"],
        )

    prefix = Path(temporary_store.layout.path_for_spec(concrete))
    assert stat.S_IMODE((prefix / "bin").stat().st_mode) == 0o2755
    assert stat.S_IMODE((prefix / "bin" / "tool").stat().st_mode) == 0o755
    assert stat.S_IMODE((prefix / "bin" / "data").stat().st_mode) == 0o644
    assert os.readlink(prefix / "bin" / "tool-link") == "tool"
    assert response["post_actions"]["actions"] == [
        {
            "type": "set_permissions",
            "entries": 4,
            "file_mode": 0o755,
            "directory_mode": 0o2755,
            "group": None,
        }
    ]
    assert response["install_metadata"]["install_tree"] == install_tree_metadata(prefix)


@pytest.mark.use_package_hash
def test_install_prepared_rejects_unknown_post_action(
    concretize_scope, mock_packages_repo, repo_builder, temporary_store, tmp_path
):
    """Reject an untyped privileged action and restore the previous prefix."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-install-action-rejected",
        """    def install(self, spec, prefix):
        \"\"\"Create output that must roll back after action validation.\"\"\"
        from pathlib import Path
        Path(prefix).joinpath("new.txt").write_text("new")
        Path(self.stage.source_path).joinpath("worker-ran").write_text("worker ran")
""",
    )
    prefix = Path(temporary_store.layout.path_for_spec(concrete))
    prefix.mkdir(parents=True)
    (prefix / "original.txt").write_text("original", encoding="utf-8")

    with pytest.raises(SandboxedBuildPhaseError, match="invalid parent post-action list"):
        install_prepared_registered_sandboxed(
            concrete,
            plan,
            prepared,
            ["install"],
            store=temporary_store,
            repositories=repositories,
            post_actions=["run_all_hooks"],
        )

    assert (prefix / "original.txt").read_text(encoding="utf-8") == "original"
    assert not (prefix / "new.txt").exists()
    assert not (prepared.path / "worker-ran").exists()


@pytest.mark.use_package_hash
def test_install_prepared_rejects_unsafe_permission_action(
    concretize_scope, mock_packages_repo, repo_builder, temporary_store, tmp_path
):
    """Reject unsafe SUID permissions and restore the previous prefix."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-install-unsafe-permissions",
        """    def install(self, spec, prefix):
        \"\"\"Create a SUID file that must not become group writable.\"\"\"
        from pathlib import Path
        tool = Path(prefix).joinpath("tool")
        tool.write_text("tool")
        tool.chmod(0o4700)
""",
    )
    prefix = Path(temporary_store.layout.path_for_spec(concrete))
    prefix.mkdir(parents=True)
    (prefix / "original.txt").write_text("original", encoding="utf-8")

    with spack.config.CONFIG.override(
        "packages:all:permissions:read", "world"
    ), spack.config.CONFIG.override("packages:all:permissions:write", "group"):
        with pytest.raises(SandboxedBuildPhaseError, match="writable SUID"):
            install_prepared_registered_sandboxed(
                concrete,
                plan,
                prepared,
                ["install"],
                store=temporary_store,
                repositories=repositories,
                post_actions=["set_permissions"],
            )

    assert (prefix / "original.txt").read_text(encoding="utf-8") == "original"
    assert not (prefix / "tool").exists()


@pytest.mark.use_package_hash
def test_build_phase_rejects_modified_prepared_stage(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path
):
    """Reject prepared source content changed after trusted preparation."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-build-tampered-stage",
        """    def install(self, spec, prefix):
        \"\"\"Provide a no-op phase that must never run for modified input.\"\"\"
        pass
""",
    )
    (prepared.path / "tampered").write_text("changed", encoding="utf-8")

    with pytest.raises(SandboxedBuildPhaseError, match="identity does not match"):
        run_build_phase_sandboxed(
            concrete,
            plan,
            prepared,
            "install",
            prefix=tmp_path / "prefix",
            repositories=repositories,
        )

    assert not (tmp_path / "prefix").exists()


@pytest.mark.use_package_hash
def test_build_phase_rejects_unknown_phase(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path
):
    """Reject a requested phase that the package builder does not declare."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-build-unknown-phase",
        """    def install(self, spec, prefix):
        \"\"\"Provide the package's sole declared phase.\"\"\"
        pass
""",
    )

    with pytest.raises(SandboxedBuildPhaseError, match="does not declare phase"):
        run_build_phase_sandboxed(
            concrete,
            plan,
            prepared,
            "configure",
            prefix=tmp_path / "prefix",
            repositories=repositories,
        )


@pytest.mark.use_package_hash
def test_build_phase_cannot_write_outside_allowed_paths(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path
):
    """Deny recipe writes outside prepared source, prefix, and private state."""
    outside = tmp_path / "outside"
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-build-denied-write",
        f"""    def install(self, spec, prefix):
        \"\"\"Attempt a host write that Landlock must deny.\"\"\"
        from pathlib import Path
        Path({str(outside)!r}).write_text("modified")
""",
    )

    with pytest.raises(SandboxedBuildPhaseError, match="Permission denied"):
        run_build_phase_sandboxed(
            concrete,
            plan,
            prepared,
            "install",
            prefix=tmp_path / "prefix",
            repositories=repositories,
        )

    assert not outside.exists()


@pytest.mark.use_package_hash
def test_build_phase_cannot_connect_tcp(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path
):
    """Deny TCP connections initiated by a recipe build phase."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    host, port = listener.getsockname()
    try:
        concrete, plan, prepared, repositories = _prepare_build(
            repo_builder,
            mock_packages_repo,
            tmp_path,
            "sandbox-build-denied-network",
            f"""    def install(self, spec, prefix):
        \"\"\"Attempt a TCP connection that Landlock must deny.\"\"\"
        import socket
        socket.create_connection(({host!r}, {port}))
""",
        )

        with pytest.raises(SandboxedBuildPhaseError, match="Permission denied"):
            run_build_phase_sandboxed(
                concrete,
                plan,
                prepared,
                "install",
                prefix=tmp_path / "prefix",
                repositories=repositories,
            )
    finally:
        listener.close()


@pytest.mark.use_package_hash
def test_build_phase_times_out(concretize_scope, mock_packages_repo, repo_builder, tmp_path):
    """Terminate a build phase that exceeds its wall-clock deadline."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-build-timeout",
        """    def install(self, spec, prefix):
        \"\"\"Run indefinitely so the parent must enforce its timeout.\"\"\"
        while True:
            pass
""",
    )

    with pytest.raises(SandboxedBuildPhaseError, match="timed out"):
        run_build_phase_sandboxed(
            concrete,
            plan,
            prepared,
            "install",
            prefix=tmp_path / "prefix",
            repositories=repositories,
            timeout=0.2,
        )


@pytest.mark.use_package_hash
def test_build_phase_recomputes_package_hash(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path
):
    """Reject mutually consistent transported metadata with a forged package hash."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-build-package-hash",
        """    def install(self, spec, prefix):
        \"\"\"Provide a no-op phase that forged provenance must not reach.\"\"\"
        pass
""",
    )
    forged_hash = "a" * 52 + "===="
    setattr(concrete, "_package_hash", forged_hash)
    forged_plan = copy.deepcopy(plan)
    forged_plan["provenance"]["package_hash"] = forged_hash
    forged_prepared = PreparedStage(
        path=prepared.path,
        source_plan_sha256=source_plan_digest(forged_plan),
        content_sha256=prepared.content_sha256,
    )

    with pytest.raises(SandboxedBuildPhaseError, match="package hash does not match"):
        run_build_phase_sandboxed(
            concrete,
            forged_plan,
            forged_prepared,
            "install",
            prefix=tmp_path / "prefix",
            repositories=repositories,
        )


@pytest.mark.use_package_hash
def test_install_prepared_sandboxed_commits_ordered_phases(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path, monkeypatch
):
    """Commit a replacement prefix after all confined phases succeed in order."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-install-commit",
        """    def configure(self, spec, prefix):
        \"\"\"Record the first confined phase.\"\"\"
        from pathlib import Path
        Path(prefix).joinpath("order.txt").write_text("configure\\n")

    def install(self, spec, prefix):
        \"\"\"Record the second confined phase.\"\"\"
        from pathlib import Path
        with Path(prefix).joinpath("order.txt").open("a") as stream:
            stream.write("install\\n")

class GenericBuilder(Builder):
    \"\"\"Adapt the generated package to two ordered phases.\"\"\"
    phases = ("configure", "install")

    def configure(self, pkg, spec, prefix):
        \"\"\"Forward configure to the package implementation.\"\"\"
        pkg.configure(spec, prefix)

    def install(self, pkg, spec, prefix):
        \"\"\"Forward install to the package implementation.\"\"\"
        pkg.install(spec, prefix)
""",
    )
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "original.txt").write_text("original", encoding="utf-8")

    def reject_global_hook(*args, **kwargs):
        """Fail if untyped global install hooks run in the parent."""
        raise AssertionError("global install hook crossed the typed boundary")

    monkeypatch.setattr(spack.hooks, "pre_install", reject_global_hook)
    monkeypatch.setattr(spack.hooks, "post_install", reject_global_hook)
    response = install_prepared_sandboxed(
        concrete,
        plan,
        prepared,
        ["configure", "install"],
        prefix=prefix,
        repositories=repositories,
    )

    assert response["phases"] == ["configure", "install"]
    assert (prefix / "order.txt").read_text(encoding="utf-8") == "configure\ninstall\n"
    assert not (prefix / "original.txt").exists()


@pytest.mark.use_package_hash
def test_install_prepared_sandboxed_rolls_back_failed_phases(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path
):
    """Restore an existing prefix when a later confined phase fails."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-install-rollback",
        """    def configure(self, spec, prefix):
        \"\"\"Create partial output before the failing phase.\"\"\"
        from pathlib import Path
        Path(prefix).joinpath("partial.txt").write_text("partial")

    def install(self, spec, prefix):
        \"\"\"Fail so the parent must restore the original prefix.\"\"\"
        raise RuntimeError("install failed")

class GenericBuilder(Builder):
    \"\"\"Adapt the generated package to two ordered phases.\"\"\"
    phases = ("configure", "install")

    def configure(self, pkg, spec, prefix):
        \"\"\"Forward configure to the package implementation.\"\"\"
        pkg.configure(spec, prefix)

    def install(self, pkg, spec, prefix):
        \"\"\"Forward install to the package implementation.\"\"\"
        pkg.install(spec, prefix)
""",
    )
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "original.txt").write_text("original", encoding="utf-8")

    with pytest.raises(SandboxedBuildPhaseError, match="install failed"):
        install_prepared_sandboxed(
            concrete,
            plan,
            prepared,
            ["configure", "install"],
            prefix=prefix,
            repositories=repositories,
        )

    assert (prefix / "original.txt").read_text(encoding="utf-8") == "original"
    assert not (prefix / "partial.txt").exists()


@pytest.mark.use_package_hash
def test_install_prepared_sandboxed_prevalidates_all_phases(
    concretize_scope, mock_packages_repo, repo_builder, tmp_path
):
    """Reject an unknown later phase before an earlier phase can mutate state."""
    concrete, plan, prepared, repositories = _prepare_build(
        repo_builder,
        mock_packages_repo,
        tmp_path,
        "sandbox-install-prevalidate",
        """    def configure(self, spec, prefix):
        \"\"\"Create a marker only if phase prevalidation is incomplete.\"\"\"
        from pathlib import Path
        Path(self.stage.source_path).joinpath("configured.txt").write_text("configured")

class GenericBuilder(Builder):
    \"\"\"Expose configure while leaving the requested missing phase undeclared.\"\"\"
    phases = ("configure",)

    def configure(self, pkg, spec, prefix):
        \"\"\"Forward configure to the package implementation.\"\"\"
        pkg.configure(spec, prefix)
""",
    )

    with pytest.raises(SandboxedBuildPhaseError, match="does not declare phase: missing"):
        install_prepared_sandboxed(
            concrete,
            plan,
            prepared,
            ["configure", "missing"],
            prefix=tmp_path / "prefix",
            repositories=repositories,
        )

    assert not (prepared.path / "configured.txt").exists()
    assert not (tmp_path / "prefix").exists()
