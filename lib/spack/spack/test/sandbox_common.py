# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Platform-agnostic unit tests for the abstract Sandbox interface and installer wiring."""

import os
import pathlib
import sys
import tempfile
from typing import List, Tuple

import pytest

import spack.concretize
import spack.sandbox
import spack.sandbox_namespaces
import spack.store
from spack.installer.build import _enable_sandbox


class MockSandbox(spack.sandbox.Sandbox):
    def __init__(self):
        self.read_calls: List[Tuple[pathlib.Path, pathlib.Path]] = []
        self.write_calls: List[Tuple[pathlib.Path, pathlib.Path]] = []
        self.apply_calls: List[bool] = []

    def _allow_read(self, original: pathlib.Path, resolved: pathlib.Path):
        self.read_calls.append((original, resolved))

    def _allow_write(self, original: pathlib.Path, resolved: pathlib.Path):
        self.write_calls.append((original, resolved))

    def apply(self, block_network=False):
        self.apply_calls.append(block_network)


def test_allow_read_reports_both_the_requested_and_resolved_path(tmp_path: pathlib.Path):
    """Backends need the resolved path to apply a rule, and the original to report on it.

    Rules are keyed by resolved path so that two names for one directory collapse to a single
    rule instead of racing each other.
    """
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError as e:
        # Windows only permits this under Developer Mode or with elevation.
        pytest.skip(f"cannot create symlinks on this host: {e}")

    sandbox = MockSandbox()
    sandbox.allow_read(link)

    assert sandbox.read_calls == [(link.absolute(), target.resolve())]


def test_enable_sandbox_paths(
    config, mock_packages, monkeypatch, temporary_store: spack.store.Store, tmp_path: pathlib.Path
):
    """Test that _enable_sandbox in the installer calls allow_read/allow_write correctly."""
    mock_sandbox = MockSandbox()
    monkeypatch.setattr(spack.sandbox, "get_sandbox", lambda: mock_sandbox)

    spec = spack.concretize.concretize_one("dependent-install")

    # Create prefix directories so resolved.exists() passes
    pathlib.Path(spec.prefix).mkdir(parents=True, exist_ok=True)
    for dep in spec.traverse(root=False):
        pathlib.Path(dep.prefix).mkdir(parents=True, exist_ok=True)

    stage_path = tmp_path / "stage"
    stage_path.mkdir()

    custom_write = tmp_path / "custom_write"
    custom_write.mkdir()

    custom_read = tmp_path / "custom_read"
    custom_read.mkdir()

    # sbang (a shebang-length workaround) is a POSIX-only concept; install_sbang() is a no-op
    # on Windows, so there is nothing to grant read access to on that platform.
    temporary_store.install_sbang()
    sbang_file = pathlib.Path(temporary_store.unpadded_root) / "bin" / "sbang"

    config = {
        "enable": True,
        "allow_read": [str(custom_read)],
        "allow_write": [str(custom_write)],
        "allow_network": True,
    }

    _enable_sandbox(config, spec, str(stage_path))

    allow_read_resolved = [c[1] for c in mock_sandbox.read_calls]
    for dep in spec.traverse(root=False):
        assert pathlib.Path(dep.prefix).resolve() in allow_read_resolved

    assert custom_read.resolve() in allow_read_resolved

    # Verify sbang read (sbang doesn't exist on Windows; see comment above)
    if sys.platform != "win32":
        assert sbang_file.resolve() in allow_read_resolved

    allow_write_resolved = [c[1] for c in mock_sandbox.write_calls]
    assert stage_path.resolve() in allow_write_resolved
    assert pathlib.Path(spec.prefix).resolve() in allow_write_resolved
    assert custom_write.resolve() in allow_write_resolved
    assert pathlib.Path(tempfile.gettempdir()).resolve() in allow_write_resolved

    assert mock_sandbox.apply_calls == [False]


class MockNamespaceSandbox(spack.sandbox_namespaces.NamespaceSandbox):
    """NamespaceSandbox that records mount-tree preparation instead of mounting."""

    def __init__(self):
        super().__init__()
        self.prepare_mount_tree_calls = []
        self.apply_calls: List[bool] = []

    def _allow_read(self, original: pathlib.Path, resolved: pathlib.Path):
        pass

    def _allow_write(self, original: pathlib.Path, resolved: pathlib.Path):
        pass

    def prepare_mount_tree(self, hidden_dirs, stage_path):
        self.prepare_mount_tree_calls.append((list(hidden_dirs), stage_path))
        return True

    def drop_mount_authority(self):
        return True

    def apply(self, block_network=False):
        self.apply_calls.append(block_network)


def test_enable_sandbox_prepares_namespace_mount_tree(
    config, mock_packages, monkeypatch, temporary_store: spack.store.Store, tmp_path: pathlib.Path
):
    """The namespace backend hides host directories before Landlock is applied.

    ``_enable_sandbox`` must call ``prepare_mount_tree`` on the selected
    NamespaceSandbox with the default hidden directories, and the returned
    Landlock-visible tree is what ``apply`` then confines.
    """
    mock_sandbox = MockNamespaceSandbox()
    monkeypatch.setattr(spack.sandbox, "get_sandbox", lambda: mock_sandbox)

    spec = spack.concretize.concretize_one("dependent-install")
    pathlib.Path(spec.prefix).mkdir(parents=True, exist_ok=True)
    for dep in spec.traverse(root=False):
        pathlib.Path(dep.prefix).mkdir(parents=True, exist_ok=True)

    stage_path = tmp_path / "stage"
    stage_path.mkdir()

    _enable_sandbox({"enable": True, "allow_network": True}, spec, str(stage_path))

    assert mock_sandbox.prepare_mount_tree_calls == [(["/usr/share/aclocal"], str(stage_path))]
    assert mock_sandbox.apply_calls == [False]


def test_default_hide_as_empty_dirs_skips_external_autoconf():
    """An external autoconf keeps access to its own host macro directory."""
    import types

    from spack.installer.build import default_hide_as_empty_dirs

    def fake_spec(dependencies):
        spec = types.SimpleNamespace()
        spec.traverse = lambda root=True: iter(dependencies)
        return spec

    external = types.SimpleNamespace(name="autoconf", external=True)
    assert default_hide_as_empty_dirs(fake_spec([external])) == []

    not_external = types.SimpleNamespace(name="autoconf", external=False)
    assert default_hide_as_empty_dirs(fake_spec([not_external])) == ["/usr/share/aclocal"]


def test_namespace_filesystem_policy_from_installer_inputs(monkeypatch, tmp_path: pathlib.Path):
    from types import SimpleNamespace

    from spack.installer.build import namespace_filesystem_policy_from_inputs

    dependency_prefix = tmp_path / "dependency"
    dependency_prefix.mkdir()
    external_prefix = tmp_path / "external"
    external_prefix.mkdir()
    install_prefix = tmp_path / "prefix"
    install_prefix.mkdir()
    stage_path = tmp_path / "stage"
    stage_path.mkdir()
    temporary_path = tmp_path / "temporary"
    temporary_path.mkdir()
    custom_read = tmp_path / "custom-read"
    custom_read.mkdir()
    custom_write = tmp_path / "custom-write"
    custom_write.mkdir()
    store_root = tmp_path / "store"
    sbang = store_root / "bin" / "sbang"
    sbang.parent.mkdir(parents=True)
    sbang.touch()

    dependencies = [
        SimpleNamespace(name="dependency", external=False, prefix=dependency_prefix),
        SimpleNamespace(name="external", external=True, prefix=external_prefix),
    ]
    spec = SimpleNamespace(prefix=install_prefix, traverse=lambda root=True: iter(dependencies))
    monkeypatch.setattr(spack.store.STORE, "unpadded_root", str(store_root))
    monkeypatch.setattr(spack.store.STORE, "upstreams", None)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temporary_path))

    policy = namespace_filesystem_policy_from_inputs(
        {"allow_read": [str(custom_read)], "allow_write": [str(custom_write)]},
        spec,
        str(stage_path),
    )

    read_only_targets = {pathlib.Path(mount.target) for mount in policy.read_only_mounts}
    assert read_only_targets == {
        dependency_prefix.resolve(),
        custom_read.resolve(),
        sbang.resolve(),
    }
    read_write_targets = {pathlib.Path(mount.target) for mount in policy.read_write_mounts}
    assert read_write_targets == {
        stage_path.resolve(),
        install_prefix.resolve(),
        temporary_path.resolve(),
        custom_write.resolve(),
        pathlib.Path(os.devnull).resolve(),
    }
    assert external_prefix.resolve() not in read_only_targets
