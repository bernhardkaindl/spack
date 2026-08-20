# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Unit tests for Linux Landlock sandboxing in the new installer."""

import sys

import pytest

if sys.platform != "linux":
    pytest.skip("Landlock sandboxing is Linux only", allow_module_level=True)

import os
import pathlib
import tempfile
from types import SimpleNamespace
from typing import List, Tuple, cast

import spack.concretize
import spack.config
import spack.installer.sandbox
import spack.installer_dispatch
import spack.paths
import spack.sandbox
import spack.spec
import spack.store


class SpyLandlockSandbox(spack.sandbox.LandlockSandbox):
    """LandlockSandbox that records _syscall_* and _prctl_* calls."""

    def __init__(self, abi_version: int = 3) -> None:
        self._abi_version_override = abi_version
        super().__init__()
        self._fds: List[int] = []
        self.ruleset_fd = -1
        # (fs_flags, net_flags)
        self.create_ruleset_calls: List[Tuple[int, int]] = []
        # (ruleset_fd, allowed_access, path_fd)
        self.add_rule_calls: List[Tuple[int, int, int]] = []
        # (ruleset_fd, tsync_flag)
        self.restrict_self_calls: List[Tuple[int, int]] = []
        self.prctl_called: bool = False

    def __del__(self):
        for fd in self._fds:
            os.close(fd)

    def _new_fd(self) -> int:
        fd = os.open(os.devnull, os.O_RDONLY)
        self._fds.append(fd)
        return fd

    def _get_abi_version(self) -> int:
        return self._abi_version_override

    def _syscall_create_ruleset(self, handled_access_fs: int, handled_access_net: int) -> int:
        self.create_ruleset_calls.append((handled_access_fs, handled_access_net))
        self.ruleset_fd = self._new_fd()
        return self.ruleset_fd

    def _syscall_add_rule(self, ruleset_fd: int, allowed_access: int, path_fd: int) -> None:
        self.add_rule_calls.append((ruleset_fd, allowed_access, path_fd))

    def _syscall_restrict_self(self, ruleset_fd: int, tsync_flag: int) -> None:
        self.restrict_self_calls.append((ruleset_fd, tsync_flag))

    def _prctl_no_new_privs(self) -> None:
        self.prctl_called = True


@pytest.mark.parametrize(
    "config,expected",
    [
        ({}, True),
        ({"allow_network": True}, False),
        ({"allow_network": False}, True),
        ({"allow_network": True, "restrict_network": True}, True),
        ({"allow_network": False, "restrict_network": False}, False),
    ],
)
def test_network_restriction_compatibility(config, expected):
    assert spack.sandbox.network_restriction_enabled(config) is expected


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"defaults": {"policy": "deny"}}, (True, True)),
        ({"defaults": {"policy": "allow"}}, (False, False)),
        ({"defaults": {"policy": "allow", "allow": ["all"], "deny": ["network"]}}, (False, True)),
        (
            {"defaults": {"policy": "deny", "allow": ["network"], "deny": ["filesystem"]}},
            (True, False),
        ),
    ],
)
def test_resolve_sandbox_defaults(config, expected):
    assert spack.installer.sandbox.resolve_restrictions(config) == expected


def test_resolve_sandbox_overrides_use_first_resource_match():
    spec = spack.spec.Spec("pkg@2.0+shared %gcc@13 ^zlib@1.3")
    config = {
        "defaults": {"policy": "deny"},
        "overrides": [
            {"spec": "pkg@2: +shared %gcc@13: ^zlib@1:", "allow": ["network"]},
            {"spec": "pkg@2.0", "allow": ["filesystem"], "deny": ["network"]},
        ],
    }

    assert spack.installer.sandbox.resolve_restrictions(config, spec) == (False, False)


def test_landlock_sandbox_syscall_args(tmp_path: pathlib.Path):
    """Test that LandlockSandbox passes correct arguments to each syscall."""
    sandbox = SpyLandlockSandbox(abi_version=3)

    test_dir = tmp_path / "dir"
    test_dir.mkdir()
    test_file = test_dir / "file"
    test_file.touch()

    sandbox.allow_read(test_dir)
    sandbox.allow_write(test_file)
    sandbox.apply(restrict_filesystem=True, restrict_network=False)

    # Ruleset covers both read and write access; no network flags
    [(fs_flags, net_flags)] = sandbox.create_ruleset_calls
    assert fs_flags & spack.sandbox.FSAccess.READ_FILE
    assert fs_flags & spack.sandbox.FSAccess.WRITE_FILE
    assert net_flags == 0

    # One rule per path, both using the same ruleset fd
    assert len(sandbox.add_rule_calls) == 2
    for ruleset_fd, _access, path_fd in sandbox.add_rule_calls:
        assert ruleset_fd == sandbox.ruleset_fd
        assert path_fd > 0

    # Read-only directory: has READ_DIR, no WRITE_FILE
    dir_access = next(
        a for _, a, _ in sandbox.add_rule_calls if a & spack.sandbox.FSAccess.READ_DIR
    )
    assert not (dir_access & spack.sandbox.FSAccess.WRITE_FILE)

    # Write file: has WRITE_FILE, no READ_DIR (dir flags stripped for non-dirs)
    file_access = next(
        a for _, a, _ in sandbox.add_rule_calls if a & spack.sandbox.FSAccess.WRITE_FILE
    )
    assert not (file_access & spack.sandbox.FSAccess.READ_DIR)

    # RESTRICT_SELF gets the correct ruleset fd
    [(restrict_fd, tsync)] = sandbox.restrict_self_calls
    assert restrict_fd == sandbox.ruleset_fd
    assert tsync == 0  # ABI v3: no tsync flag

    assert sandbox.prctl_called


def test_landlock_sandbox_network_only_args():
    """Test that network-only mode sets network flags without filesystem rules."""
    sandbox = SpyLandlockSandbox(abi_version=4)
    sandbox.allow_read("/usr")
    sandbox.apply(restrict_filesystem=False, restrict_network=True)

    [(fs_flags, net_flags)] = sandbox.create_ruleset_calls
    assert fs_flags == 0
    assert net_flags & spack.sandbox.LANDLOCK_ACCESS_NET_CONNECT_TCP
    assert net_flags & spack.sandbox.LANDLOCK_ACCESS_NET_BIND_TCP
    assert not sandbox.add_rule_calls
    assert sandbox.prctl_called


def test_landlock_sandbox_no_restrictions_is_noop():
    sandbox = SpyLandlockSandbox(abi_version=4)
    sandbox.apply(restrict_filesystem=False, restrict_network=False)

    assert not sandbox.create_ruleset_calls
    assert not sandbox.prctl_called


class MockSandbox(spack.sandbox.Sandbox):
    """Record paths and network settings passed to the abstract sandbox interface."""

    def __init__(self):
        """Initialize empty call lists for each sandbox operation."""
        self.read_calls: List[Tuple[pathlib.Path, pathlib.Path]] = []
        self.write_calls: List[Tuple[pathlib.Path, pathlib.Path]] = []
        self.apply_calls: List[Tuple[bool, bool]] = []

    def _allow_read(self, original: pathlib.Path, resolved: pathlib.Path):
        self.read_calls.append((original, resolved))

    def _allow_write(self, original: pathlib.Path, resolved: pathlib.Path):
        self.write_calls.append((original, resolved))

    def apply(self, restrict_filesystem=True, restrict_network=False):
        self.apply_calls.append((restrict_filesystem, restrict_network))


def test_allow_selected_compiler_paths(tmp_path: pathlib.Path):
    """Allow selected C/C++ drivers once while leaving unselected Fortran inaccessible."""
    compiler_dir = tmp_path / "compiler" / "bin"
    compiler_dir.mkdir(parents=True)
    compiler_paths = {
        language: str(compiler_dir / executable)
        for language, executable in (("c", "cc"), ("cxx", "c++"), ("fortran", "fc"))
    }
    for path in compiler_paths.values():
        pathlib.Path(path).touch()

    compiler_spec = SimpleNamespace(extra_attributes={"compilers": compiler_paths})
    selected_edge = SimpleNamespace(spec=compiler_spec, virtuals=("c", "cxx"))
    node = SimpleNamespace(edges_to_dependencies=lambda: [selected_edge, selected_edge])
    spec = SimpleNamespace(traverse=lambda: [node])
    sandbox = MockSandbox()

    spack.installer.sandbox.allow_compiler_paths(sandbox, cast(spack.spec.Spec, spec))

    allowed = [resolved for _, resolved in sandbox.read_calls]
    assert pathlib.Path(compiler_paths["c"]).resolve() in allowed
    assert pathlib.Path(compiler_paths["cxx"]).resolve() in allowed
    assert pathlib.Path(compiler_paths["fortran"]).resolve() not in allowed
    assert len(allowed) == 2


def test_prepend_compiler_aliases(tmp_path, monkeypatch):
    compiler_dir = tmp_path / "compiler"
    compiler_dir.mkdir()
    compiler_paths = {
        "c": str(compiler_dir / "gcc"),
        "cxx": str(compiler_dir / "g++"),
        "fortran": str(compiler_dir / "gfortran"),
    }
    for path in compiler_paths.values():
        pathlib.Path(path).touch()

    compiler_spec = SimpleNamespace(extra_attributes={"compilers": compiler_paths})
    compiler_edge = SimpleNamespace(spec=compiler_spec, virtuals=("c", "cxx", "fortran"))
    node = SimpleNamespace(edges_to_dependencies=lambda: [compiler_edge])
    spec = SimpleNamespace(traverse=lambda: [node])
    monkeypatch.setenv("PATH", "/original/path")

    alias_dir = spack.installer.sandbox.prepend_compiler_aliases(
        cast(spack.spec.Spec, spec), str(tmp_path)
    )

    assert alias_dir is not None
    assert pathlib.Path(alias_dir, "cc").resolve() == pathlib.Path(compiler_paths["c"])
    for alias in ("c++", "g++", "clang++"):
        assert pathlib.Path(alias_dir, alias).resolve() == pathlib.Path(compiler_paths["cxx"])
    assert pathlib.Path(alias_dir, "gfortran").resolve() == pathlib.Path(compiler_paths["fortran"])
    assert os.environ["PATH"].split(os.pathsep) == [alias_dir, "/original/path"]


def test_prepend_compiler_aliases_omits_unselected_languages(tmp_path, monkeypatch):
    compiler = tmp_path / "compiler"
    compiler.touch()
    compiler_spec = SimpleNamespace(extra_attributes={"compilers": {"c": str(compiler)}})
    compiler_edge = SimpleNamespace(spec=compiler_spec, virtuals=("c",))
    node = SimpleNamespace(edges_to_dependencies=lambda: [compiler_edge])
    spec = SimpleNamespace(traverse=lambda: [node])
    monkeypatch.setenv("PATH", "/original/path")

    alias_dir = spack.installer.sandbox.prepend_compiler_aliases(
        cast(spack.spec.Spec, spec), str(tmp_path)
    )

    assert alias_dir is not None
    assert {path.name for path in pathlib.Path(alias_dir).iterdir()} == {"cc"}


def test_allow_direct_compiler_dependency_paths(tmp_path: pathlib.Path):
    """Allow every driver exposed by a direct compiler dependency."""
    compiler_paths = {}
    for language, executable in (("c", "gcc"), ("cxx", "g++"), ("fortran", "gfortran")):
        path = tmp_path / executable
        path.touch()
        compiler_paths[language] = str(path)

    compiler_spec = SimpleNamespace(extra_attributes={"compilers": compiler_paths})
    compiler_edge = SimpleNamespace(spec=compiler_spec, virtuals=())
    node = SimpleNamespace(edges_to_dependencies=lambda: [compiler_edge])
    spec = SimpleNamespace(traverse=lambda: [node])
    sandbox = MockSandbox()

    spack.installer.sandbox.allow_compiler_paths(sandbox, cast(spack.spec.Spec, spec))

    allowed = {resolved for _, resolved in sandbox.read_calls}
    assert allowed == {pathlib.Path(path).resolve() for path in compiler_paths.values()}


@pytest.mark.parametrize(
    "program",
    [
        "ld",
        "ar",
        "ranlib",
        "strip",
        "nm",
        "join",
        "true",
        "tbl",
        "mawk",
        "paste",
        "date",
        "head",
        "hostname",
        "arch",
        "comm",
        "egrep",
        "realpath",
        "split",
        "md5sum",
    ],
)
def test_compiler_support_paths_resolve_bare_tools_on_host_path(monkeypatch, program):
    """Resolve bare compiler tools through both the build PATH and the host default PATH."""
    assert program in spack.installer.sandbox.BUILD_TOOLS
    monkeypatch.setattr(spack.installer.sandbox, "BUILD_TOOLS", (program,))
    monkeypatch.setattr(spack.installer.sandbox, "COMPILER_FILES", ())
    monkeypatch.setattr(
        spack.installer.sandbox.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=f"{program}\n"),
    )

    def which(command, path=None):
        return f"/wrapper/{program}" if path is None else f"/usr/bin/{program}"

    monkeypatch.setattr(spack.installer.sandbox.shutil, "which", which)

    assert spack.installer.sandbox.compiler_support_paths("/usr/bin/g++") == [
        f"/wrapper/{program}",
        f"/usr/bin/{program}",
    ]


def test_enable_sandbox_paths(
    config, mock_packages, monkeypatch, temporary_store: spack.store.Store, tmp_path: pathlib.Path
):
    """Verify implicit and configured sandbox paths without exposing sibling prefixes."""
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

    # Create a symlink to verify original vs resolved path logic
    custom_read_target = tmp_path / "custom_read_target"
    custom_read_target.mkdir()
    custom_read_link = tmp_path / "custom_read_link"
    custom_read_link.symlink_to(custom_read_target)

    # Ensure the sbang exists
    temporary_store.install_sbang()
    sbang_file = pathlib.Path(temporary_store.unpadded_root) / "bin" / "sbang"

    config = {
        "enable": True,
        "restrict_filesystem": True,
        "restrict_network": True,
        "allow_read": [str(custom_read_link)],
        "allow_write": [str(custom_write)],
    }
    monkeypatch.setenv("PATH", "/original/path")

    spack.installer.sandbox.enable(config, spec, str(stage_path))

    allow_read_resolved = [c[1] for c in mock_sandbox.read_calls]
    for dep in spec.traverse(root=False):
        assert pathlib.Path(dep.prefix).resolve() in allow_read_resolved

    # Verify symlink resolution in read_calls
    assert custom_read_target.resolve() in allow_read_resolved
    assert (custom_read_link.absolute(), custom_read_target.resolve()) in mock_sandbox.read_calls

    # Verify sbang read
    assert sbang_file.resolve() in allow_read_resolved
    assert pathlib.Path(spack.paths.prefix).resolve() in allow_read_resolved
    assert pathlib.Path(spec.package.package_dir).resolve() in allow_read_resolved
    for path in spack.installer.sandbox.HOST_RUNTIME_READ_PATHS:
        resolved = pathlib.Path(path).resolve()
        if resolved.exists():
            assert resolved in allow_read_resolved
    assert pathlib.Path("/bin/sh").resolve() in allow_read_resolved

    allow_write_resolved = [c[1] for c in mock_sandbox.write_calls]
    assert pathlib.Path(spec.prefix).parent.resolve() not in allow_write_resolved
    assert stage_path.resolve() in allow_write_resolved
    assert pathlib.Path(spec.prefix).resolve() in allow_write_resolved
    assert custom_write.resolve() in allow_write_resolved
    assert pathlib.Path(tempfile.gettempdir()).resolve() in allow_write_resolved
    for path in spack.installer.sandbox.HOST_RUNTIME_WRITE_PATHS:
        resolved = pathlib.Path(path).resolve()
        if resolved.exists():
            assert resolved in allow_write_resolved

    assert mock_sandbox.apply_calls == [(True, True)]


def test_enable_sandbox_defaults_to_all_restrictions(mock_packages, monkeypatch, tmp_path):
    mock_sandbox = MockSandbox()
    monkeypatch.setattr(spack.sandbox, "get_sandbox", lambda: mock_sandbox)
    spec = spack.concretize.concretize_one("dependent-install")

    spack.installer.sandbox.enable({"enable": True}, spec, str(tmp_path))

    assert mock_sandbox.read_calls
    assert mock_sandbox.write_calls
    assert mock_sandbox.apply_calls == [(True, True)]


def test_enable_network_only_sandbox(mock_packages, monkeypatch, tmp_path: pathlib.Path):
    mock_sandbox = MockSandbox()
    monkeypatch.setattr(spack.sandbox, "get_sandbox", lambda: mock_sandbox)
    spec = spack.concretize.concretize_one("dependent-install")

    spack.installer.sandbox.enable(
        {"enable": True, "restrict_filesystem": False, "restrict_network": True},
        spec,
        str(tmp_path),
    )

    assert not mock_sandbox.read_calls
    assert not mock_sandbox.write_calls
    assert mock_sandbox.apply_calls == [(False, True)]


def test_enable_sandbox_without_restrictions_is_noop(mock_packages, monkeypatch, tmp_path):
    monkeypatch.setattr(
        spack.sandbox,
        "get_sandbox",
        lambda: pytest.fail("empty sandbox should not probe Landlock support"),
    )
    spec = spack.concretize.concretize_one("dependent-install")

    spack.installer.sandbox.enable(
        {"enable": True, "restrict_filesystem": False, "restrict_network": False},
        spec,
        str(tmp_path),
    )


@pytest.mark.parametrize(
    ("sandbox_config", "should_probe"),
    [
        ({"enable": False, "restrict_filesystem": True, "restrict_network": True}, False),
        ({"enable": True, "restrict_filesystem": False, "restrict_network": False}, False),
        ({"enable": True, "restrict_filesystem": False, "restrict_network": True}, True),
        ({"enable": True}, True),
    ],
)
def test_installer_dispatch_probes_active_sandbox(
    mock_packages, monkeypatch, sandbox_config, should_probe
):
    calls = []
    monkeypatch.setattr(spack.sandbox, "get_sandbox", lambda: calls.append(True))
    spec = spack.concretize.concretize_one("dependent-install")

    with spack.config.CONFIG.override("config:installer", "new"):
        with spack.config.CONFIG.override("config:sandbox", sandbox_config):
            spack.installer_dispatch.create_installer([spec.package])

    assert bool(calls) is should_probe


@pytest.mark.parametrize("mode", ["all", "root", "network", "filesystem"])
def test_installer_dispatch_probes_cli_sandbox(mock_packages, monkeypatch, mode):
    calls = []
    monkeypatch.setattr(spack.sandbox, "get_sandbox", lambda: calls.append(True))
    spec = spack.concretize.concretize_one("dependent-install")

    with spack.config.CONFIG.override("config:installer", "new"):
        with spack.config.CONFIG.override("config:sandbox:enable", False):
            spack.installer_dispatch.create_installer([spec.package], sandbox=mode)

    assert calls == [True]


def test_noop_sandbox_allows_old_installer(mock_packages):
    spec = spack.concretize.concretize_one("dependent-install")
    sandbox_config = {"enable": True, "restrict_filesystem": False, "restrict_network": False}

    with spack.config.CONFIG.override("config:installer", "old"):
        with spack.config.CONFIG.override("config:sandbox", sandbox_config):
            spack.installer_dispatch.create_installer([spec.package])


def test_cli_sandbox_rejects_old_installer(mock_packages):
    spec = spack.concretize.concretize_one("dependent-install")

    with spack.config.CONFIG.override("config:installer", "old"):
        with pytest.raises(spack.sandbox.SandboxError, match="only supported.*installer:new"):
            spack.installer_dispatch.create_installer([spec.package], sandbox="all")


def test_host_runtime_paths_include_network_configuration():
    assert "/etc/nsswitch.conf" in spack.installer.sandbox.HOST_RUNTIME_READ_PATHS
    assert "/etc/pki" in spack.installer.sandbox.HOST_RUNTIME_READ_PATHS
    assert "/etc/resolv.conf" in spack.installer.sandbox.HOST_RUNTIME_READ_PATHS
    assert "/etc/ssl" in spack.installer.sandbox.HOST_RUNTIME_READ_PATHS


def test_sandbox_network_blocking_requires_abi_v4():
    """Verify that network blocking rejects kernels without Landlock ABI v4 support."""
    sandbox = SpyLandlockSandbox(abi_version=3)

    with pytest.raises(
        spack.sandbox.SandboxError, match="Blocking network access requires Landlock ABI v4\\+"
    ):
        sandbox.apply(restrict_filesystem=False, restrict_network=True)
