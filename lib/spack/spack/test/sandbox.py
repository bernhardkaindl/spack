# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""Unit tests for Linux Landlock sandboxing in the new installer."""

import array
import sys

import pytest

if sys.platform != "linux":
    pytest.skip("Landlock sandboxing is Linux only", allow_module_level=True)

import os
import pathlib
import socket
import tempfile
from types import SimpleNamespace
from typing import List, Tuple, cast

import spack.concretize
import spack.installer.build
import spack.sandbox
import spack.store
from spack.installer.build import _enable_sandbox
from spack.util.executable import which_string
from spack.util.sandbox import run_json_worker


def test_exec_notification_reports_path_and_continues():
    parent, child = socket.socketpair()
    executable = sys.executable
    pid = os.fork()
    if pid == 0:
        parent.close()
        listener_fd = spack.sandbox.SeccompSandbox().exec_listener()
        descriptors = array.array("i", [listener_fd])
        child.sendmsg([b"1"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptors)])
        child.close()
        os.execv(executable, [executable, "-c", "pass"])
        os._exit(1)

    child.close()
    listener_fd = -1
    try:
        _, ancillary, _, _ = parent.recvmsg(1, socket.CMSG_SPACE(array.array("i").itemsize))
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                descriptors = array.array("i")
                descriptors.frombytes(data[: descriptors.itemsize])
                listener_fd = descriptors[0]
                break
        assert listener_fd >= 0

        seccomp = spack.sandbox.SeccompSandbox()
        notification = seccomp.receive_notification(listener_fd)
        assert os.fsdecode(seccomp.executable_path(notification)) == executable
        seccomp.continue_notification(listener_fd, notification.id)
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0
    finally:
        parent.close()
        if listener_fd >= 0:
            os.close(listener_fd)
        try:
            os.kill(pid, 9)
        except OSError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass


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


def test_landlock_sandbox_syscall_args(tmp_path: pathlib.Path):
    """Test that LandlockSandbox passes correct arguments to each syscall."""
    sandbox = SpyLandlockSandbox(abi_version=3)

    test_dir = tmp_path / "dir"
    test_dir.mkdir()
    test_file = test_dir / "file"
    test_file.touch()

    sandbox.allow_read(test_dir)
    sandbox.allow_write(test_file)
    sandbox.apply(block_network=False)

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


def test_landlock_sandbox_network_uses_internal_seccomp(monkeypatch):
    """Test that network blocking uses seccomp instead of Landlock TCP rules."""
    sandbox = SpyLandlockSandbox(abi_version=4)
    seccomp = MockSeccompSandbox()
    monkeypatch.setattr(spack.sandbox, "SeccompSandbox", lambda: seccomp)
    sandbox.apply(block_network=True)

    [(_, net_flags)] = sandbox.create_ruleset_calls
    assert net_flags == 0
    assert seccomp.apply_calls == 1
    assert seccomp.block_sockets
    assert sandbox.prctl_called


def test_recipe_import_sandbox_policy(monkeypatch):
    sandbox = MockSandbox()
    monkeypatch.setattr(spack.sandbox, "get_sandbox", lambda: sandbox)
    rlimits = []
    monkeypatch.setattr(
        spack.sandbox, "set_recipe_import_rlimits", lambda limit: rlimits.append(limit)
    )

    spack.sandbox.restrict_recipe_import(["/tmp", "/var"])

    assert sandbox.read_calls == [
        (pathlib.Path("/tmp").absolute(), pathlib.Path("/tmp").resolve()),
        (pathlib.Path("/var").absolute(), pathlib.Path("/var").resolve()),
    ]
    assert sandbox.write_calls == []
    assert sandbox.apply_calls == [(True, True, True, False)]
    assert rlimits == [1024 * 1024 * 1024]


def test_network_worker_sandbox_policy(monkeypatch):
    sandbox = MockSandbox()
    monkeypatch.setattr(spack.sandbox, "get_sandbox", lambda: sandbox)
    rlimits = []
    monkeypatch.setattr(
        spack.sandbox, "set_network_worker_rlimits", lambda limit: rlimits.append(limit)
    )

    spack.sandbox.restrict_network_worker(["/tmp"], ["/var"])

    assert sandbox.read_calls == [
        (pathlib.Path("/tmp").absolute(), pathlib.Path("/tmp").resolve())
    ]
    assert sandbox.write_calls == [
        (pathlib.Path("/var").absolute(), pathlib.Path("/var").resolve())
    ]
    assert sandbox.apply_calls == [(False, True, True, False)]
    assert rlimits == [1024 * 1024 * 1024]


def test_stage_worker_sandbox_policy(monkeypatch):
    sandbox = MockSandbox()
    monkeypatch.setattr(spack.sandbox, "get_sandbox", lambda: sandbox)
    rlimits = []
    monkeypatch.setattr(spack.sandbox, "set_stage_worker_rlimits", lambda: rlimits.append(True))

    spack.sandbox.restrict_stage_worker(["/tmp", "/bin/tar"], ["/var"])

    assert sandbox.read_calls == [
        (pathlib.Path("/tmp").absolute(), pathlib.Path("/tmp").resolve()),
        (pathlib.Path("/bin/tar").absolute(), pathlib.Path("/bin/tar").resolve()),
    ]
    assert sandbox.write_calls == [
        (pathlib.Path("/var").absolute(), pathlib.Path("/var").resolve())
    ]
    assert sandbox.apply_calls == [(False, False, True, False)]
    assert rlimits == [True]


def test_unlimited_worker_rlimits_only_disable_core_dumps(monkeypatch):
    setrlimit_calls = []
    monkeypatch.setattr(
        spack.sandbox.resource,
        "setrlimit",
        lambda kind, limits: setrlimit_calls.append((kind, limits)),
    )
    monkeypatch.setattr(
        spack.sandbox.resource,
        "getrlimit",
        lambda kind: pytest.fail("unlimited workers must not inspect memory rlimits"),
    )

    spack.sandbox.set_stage_worker_rlimits()
    spack.sandbox.set_build_worker_rlimits()

    assert setrlimit_calls == [
        (spack.sandbox.resource.RLIMIT_CORE, (0, 0)),
        (spack.sandbox.resource.RLIMIT_CORE, (0, 0)),
    ]


def test_recipe_import_sandbox_availability_honors_fallback(monkeypatch):
    monkeypatch.setattr(
        spack.sandbox,
        "get_recipe_import_sandbox",
        lambda: (_ for _ in ()).throw(spack.sandbox.SandboxError("unavailable")),
    )
    monkeypatch.setattr(spack.sandbox, "sandbox_fallback_allowed", lambda: True)

    assert not spack.sandbox.recipe_import_sandbox_available()


def test_sandbox_fallback_config(mutable_config):
    assert not spack.sandbox.sandbox_fallback_allowed()

    mutable_config.set("config:sandbox:allow_fallback", True)

    assert spack.sandbox.sandbox_fallback_allowed()


def test_recipe_import_sandbox_non_linux_uses_configured_fallback(monkeypatch):
    monkeypatch.setattr(spack.sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(spack.sandbox, "sandbox_fallback_allowed", lambda: True)
    monkeypatch.setattr(
        spack.sandbox,
        "get_recipe_import_sandbox",
        lambda: pytest.fail("Landlock should not be probed"),
    )
    monkeypatch.setattr(
        spack.sandbox, "SeccompSandbox", lambda: pytest.fail("seccomp should not be probed")
    )

    assert not spack.sandbox.recipe_import_sandbox_available()


def test_recipe_import_sandbox_non_linux_fails_without_fallback(monkeypatch):
    monkeypatch.setattr(spack.sandbox.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(spack.sandbox, "sandbox_fallback_allowed", lambda: False)

    with pytest.raises(spack.sandbox.SandboxError, match="only supported on Linux"):
        spack.sandbox.recipe_import_sandbox_available()


def test_recipe_import_sandbox_allows_pre_v4_landlock(monkeypatch):
    sandbox = MockSandbox()
    sandbox.abi_version = 1
    monkeypatch.setattr(spack.sandbox, "get_recipe_import_sandbox", lambda: sandbox)

    assert spack.sandbox.recipe_import_sandbox_available()


def test_recipe_import_sandbox_falls_back_without_seccomp(monkeypatch):
    sandbox = MockSandbox()
    sandbox.network_isolation_result = False
    monkeypatch.setattr(spack.sandbox, "get_sandbox", lambda: sandbox)
    monkeypatch.setattr(spack.sandbox, "sandbox_fallback_allowed", lambda: True)

    assert not spack.sandbox.recipe_import_sandbox_available()


def test_recipe_import_sandbox_requires_full_network_isolation(monkeypatch):
    sandbox = MockSandbox()
    sandbox.network_isolation_result = False
    monkeypatch.setattr(spack.sandbox, "get_sandbox", lambda: sandbox)
    monkeypatch.setattr(spack.sandbox, "sandbox_fallback_allowed", lambda: False)

    with pytest.raises(spack.sandbox.SandboxError, match="network isolation"):
        spack.sandbox.recipe_import_sandbox_available()


def test_landlock_sandbox_uses_tcp_fallback_when_seccomp_load_fails(monkeypatch):
    sandbox = SpyLandlockSandbox(abi_version=4)
    seccomp = FailingSeccompSandbox()
    monkeypatch.setattr(spack.sandbox, "SeccompSandbox", lambda: seccomp)

    sandbox.apply(block_network=True, allow_tcp_network_fallback=True)
    assert [net_flags for _, net_flags in sandbox.create_ruleset_calls] == [0, 3]
    assert seccomp.apply_calls == 1


class MockSandbox(spack.sandbox.Sandbox):
    def __init__(self):
        self.abi_version = 4
        self.read_calls: List[Tuple[pathlib.Path, pathlib.Path]] = []
        self.write_calls: List[Tuple[pathlib.Path, pathlib.Path]] = []
        self.apply_calls = []
        self.network_isolation_result = True

    def _allow_read(self, original: pathlib.Path, resolved: pathlib.Path):
        self.read_calls.append((original, resolved))

    def _allow_write(self, original: pathlib.Path, resolved: pathlib.Path):
        self.write_calls.append((original, resolved))

    def network_isolation_available(self, allow_tcp_network_fallback=False):
        return self.network_isolation_result

    def apply(
        self,
        block_network=False,
        block_process=False,
        block_ipc=False,
        allow_tcp_network_fallback=False,
    ):
        self.apply_calls.append(
            (block_network, block_process, block_ipc, allow_tcp_network_fallback)
        )


class MockSeccompSandbox:
    def __init__(self):
        self.apply_calls = 0
        self.block_sockets = None
        self.block_process = None
        self.block_ipc = None

    def apply(self, block_sockets=True, block_process=False, block_ipc=False):
        self.apply_calls += 1
        self.block_sockets = block_sockets
        self.block_process = block_process
        self.block_ipc = block_ipc


class FailingSeccompSandbox(MockSeccompSandbox):
    def apply(self, block_sockets=True, block_process=False, block_ipc=False):
        super().apply(block_sockets, block_process, block_ipc)
        raise OSError("seccomp_load failed")


@pytest.mark.parametrize(
    "allow_network,expected_block_network", [(None, True), (False, True), (True, False)]
)
def test_enable_sandbox_paths(
    config,
    mock_packages,
    monkeypatch,
    temporary_store: spack.store.Store,
    tmp_path: pathlib.Path,
    allow_network,
    expected_block_network,
):
    """Test that _enable_sandbox in the installer calls allow_read/allow_write correctly."""
    mock_sandbox = MockSandbox()
    monkeypatch.setattr(spack.sandbox, "get_sandbox", lambda: mock_sandbox)
    build_rlimits = []
    monkeypatch.setattr(
        spack.sandbox, "set_build_worker_rlimits", lambda: build_rlimits.append(True)
    )
    compiler_specs = []
    monkeypatch.setattr(
        spack.installer.build,
        "allow_compiler_paths",
        lambda sandbox, spec: compiler_specs.append(spec),
    )

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
        "allow_read": [str(custom_read_link)],
        "allow_write": [str(custom_write)],
        "whitelists": {"tools": {"allow": ["true"], "specs": ["dependent-install"]}},
    }
    if allow_network is not None:
        config["allow_network"] = allow_network

    _enable_sandbox(config, spec, str(stage_path))

    allow_read_resolved = [c[1] for c in mock_sandbox.read_calls]
    for dep in spec.traverse(root=False):
        assert pathlib.Path(dep.prefix).resolve() in allow_read_resolved

    # Verify symlink resolution in read_calls
    assert custom_read_target.resolve() in allow_read_resolved
    assert (custom_read_link.absolute(), custom_read_target.resolve()) in mock_sandbox.read_calls

    # Verify sbang read
    assert sbang_file.resolve() in allow_read_resolved
    assert pathlib.Path(which_string("true")).resolve() in allow_read_resolved
    for path in spack.installer.build.HOST_RUNTIME_READ_PATHS:
        assert pathlib.Path(path).resolve() in allow_read_resolved
    assert pathlib.Path("/bin/sh").resolve() in allow_read_resolved
    assert compiler_specs == [spec]

    allow_write_resolved = [c[1] for c in mock_sandbox.write_calls]
    assert stage_path.resolve() in allow_write_resolved
    assert pathlib.Path(spec.prefix).resolve() in allow_write_resolved
    assert custom_write.resolve() in allow_write_resolved
    assert pathlib.Path(tempfile.gettempdir()).resolve() in allow_write_resolved

    assert mock_sandbox.apply_calls == [(expected_block_network, False, False, False)]
    assert build_rlimits == [True]


def test_enable_sandbox_proxy_uses_network_listener(
    mock_packages, monkeypatch, temporary_store, tmp_path
):
    mock_sandbox = MockSandbox()
    monkeypatch.setattr(spack.sandbox, "get_sandbox", lambda: mock_sandbox)
    monkeypatch.setattr(spack.sandbox, "set_build_worker_rlimits", lambda: None)
    monkeypatch.setattr(spack.installer.build, "allow_compiler_paths", lambda sandbox, spec: None)

    class NetworkSeccomp:
        denied_bypass = False
        included_exec = None

        def network_listener(self, include_exec=False):
            self.included_exec = include_exec
            return 42

        def deny_network_bypass(self):
            self.denied_bypass = True

    seccomp = NetworkSeccomp()
    monkeypatch.setattr(spack.sandbox, "SeccompSandbox", lambda: seccomp)
    for name in ("http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "no_proxy"):
        monkeypatch.setenv(name, "inherited")
        monkeypatch.setenv(name.upper(), "inherited")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "inherited")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "0")
    spec = spack.concretize.concretize_one("trivial-install-test-package")
    pathlib.Path(spec.prefix).mkdir(parents=True, exist_ok=True)
    stage_path = tmp_path / "stage"
    stage_path.mkdir()
    temporary_store.install_sbang()

    listeners = _enable_sandbox(
        {"enable": True, "learning": {"enabled": True}},
        spec,
        str(stage_path),
        "http://spack:secret@127.0.0.1:1234",
    )

    assert listeners.exec_fd is None
    assert listeners.network_fd == 42
    assert seccomp.denied_bypass
    assert seccomp.included_exec is True
    assert mock_sandbox.apply_calls == [(False, False, False, False)]
    assert os.environ["https_proxy"] == "http://spack:secret@127.0.0.1:1234"
    assert os.environ["GIT_CONFIG_GLOBAL"] == os.devnull
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"


def test_compiler_support_paths_queries_all_build_tools(monkeypatch):
    queries = []

    class CompletedProcess:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    def run(command, **kwargs):
        queries.append(command[1])
        return CompletedProcess(command[1].split("=", 1)[1])

    monkeypatch.setattr(spack.installer.build.subprocess, "run", run)
    monkeypatch.setattr(
        spack.installer.build.shutil,
        "which",
        lambda program, path=None: (
            "/wrapper/{0}".format(program) if path is None else "/host/{0}".format(program)
        ),
    )

    paths = spack.installer.build.compiler_support_paths("/usr/bin/cc")

    assert queries == [
        "-print-prog-name={0}".format(program) for program in spack.installer.build.BUILD_PROGRAMS
    ] + [
        "-print-file-name={0}".format(filename)
        for filename in spack.installer.build.COMPILER_FILES
    ]
    assert set(paths) == {
        prefix + program
        for program in spack.installer.build.BUILD_PROGRAMS
        for prefix in ("/wrapper/", "/host/")
    }


def test_allow_git_support_paths_uses_configured_exec_path(monkeypatch):
    sandbox = MockSandbox()
    monkeypatch.setattr(spack.installer.build.shutil, "which", lambda program: "/usr/bin/git")
    completed = SimpleNamespace(returncode=0, stdout="/usr/lib/git-core\n")
    monkeypatch.setattr(
        spack.installer.build.subprocess, "run", lambda command, **kwargs: completed
    )

    spack.installer.build.allow_git_support_paths(sandbox)

    assert pathlib.Path("/usr/lib/git-core").resolve() in [
        resolved for _original, resolved in sandbox.read_calls
    ]


def test_allow_selected_compiler_paths(tmp_path: pathlib.Path, monkeypatch):
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
    monkeypatch.setattr(spack.installer.build, "compiler_support_paths", lambda path: [])

    spack.installer.build.allow_compiler_paths(sandbox, cast(spack.spec.Spec, spec))

    allowed = [resolved for _, resolved in sandbox.read_calls]
    assert pathlib.Path(compiler_paths["c"]).resolve() in allowed
    assert pathlib.Path(compiler_paths["cxx"]).resolve() in allowed
    assert pathlib.Path(compiler_paths["fortran"]).resolve() not in allowed
    assert len(allowed) == 2


def test_sandbox_tcp_network_fallback_requires_abi_v4(monkeypatch):
    """Test that the Landlock TCP fallback requires ABI v4."""
    sandbox = SpyLandlockSandbox(abi_version=3)
    monkeypatch.setattr(
        spack.sandbox, "SeccompSandbox", lambda: (_ for _ in ()).throw(OSError("unavailable"))
    )

    with pytest.raises(spack.sandbox.SandboxError, match="Seccomp sandboxing is unavailable"):
        sandbox.apply(block_network=True, allow_tcp_network_fallback=True)


def test_recipe_import_sandbox_denies_writes_and_socket_communication(tmp_path):
    try:
        sandbox = spack.sandbox.get_recipe_import_sandbox()
        if not sandbox.network_isolation_available():
            pytest.skip("recipe-import network isolation is unavailable")
    except spack.sandbox.SandboxError as error:
        pytest.skip(str(error))

    write_path = tmp_path / "denied"

    def worker(request):
        try:
            write_path.write_text("denied")
        except OSError as error:
            write_errno = error.errno
        else:
            write_errno = None

        socket_errnos = []
        for family, socket_type, protocol in (
            (socket.AF_INET, socket.SOCK_STREAM, 0),
            (socket.AF_INET, socket.SOCK_DGRAM, 0),
            (socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP),
            (socket.AF_UNIX, socket.SOCK_STREAM, 0),
        ):
            try:
                network_socket = socket.socket(family, socket_type, protocol)
            except OSError as error:
                socket_errnos.append(error.errno)
            else:
                network_socket.close()
                socket_errnos.append(None)

        return {"socket_errnos": socket_errnos, "write_errno": write_errno}

    repository_root = pathlib.Path(__file__).parents[4]
    result = run_json_worker(
        {}, worker, setup=lambda: spack.sandbox.restrict_recipe_import([repository_root])
    )
    assert result["write_errno"] in (1, 13)
    assert all(error_number in (1, 13) for error_number in result["socket_errnos"])
