# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import http.server
import multiprocessing
import os
import socket
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

import spack.caches
import spack.concretize
import spack.config
import spack.install_worker.stage
import spack.package_base
import spack.sandbox
import spack.stage
import spack.util.proxy
import spack.util.sandbox
from spack.fetch_strategy import FsCache, URLFetchStrategy
from spack.install_worker.stage import (
    StageWorkerError,
    _dependency_read_roots,
    _expansion_read_roots,
    _local_stage_read_roots,
    _stage_setup,
    _store_database_read_roots,
    _tool_runtime_roots,
    _validate_stage_response,
    stage_package,
)
from spack.util.proxy import DestinationPolicy

pytestmark = pytest.mark.usefixtures("install_mockery", "mock_packages")


def _serve_archive(archive, status, request_log, ready, stopped):
    class ArchiveHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            with open(request_log, "a") as stream:
                stream.write(self.path + "\n")
            if status.value != 200:
                self.send_error(status.value)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(archive)))
            self.end_headers()
            self.wfile.write(archive)

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), ArchiveHandler)
    server.timeout = 0.1
    ready.send(server.server_address[1])
    ready.close()
    try:
        while not stopped.is_set():
            server.handle_request()
    finally:
        server.server_close()


class _ArchiveServer:
    def __init__(self, port, status, request_log):
        self.url = "http://127.0.0.1:{0}/archive.tar.gz".format(port)
        self._status = status
        self._request_log = request_log

    @property
    def status(self):
        return self._status.value

    @status.setter
    def status(self, value):
        self._status.value = value

    @property
    def requests(self):
        with open(self._request_log) as stream:
            return stream.read().splitlines()


@pytest.fixture
def archive_server(mock_archive, monkeypatch, tmp_path):
    with open(mock_archive.archive_file, "rb") as stream:
        archive = stream.read()
    request_log = str(tmp_path / "requests")
    with open(request_log, "w"):
        pass
    status = multiprocessing.Value("i", 200)
    stopped = multiprocessing.Event()
    ready, child_ready = multiprocessing.Pipe(duplex=False)
    process = multiprocessing.Process(
        target=_serve_archive, args=(archive, status, request_log, child_ready, stopped)
    )
    process.start()
    child_ready.close()
    if not ready.poll(5):
        process.terminate()
        process.join()
        pytest.fail("archive server did not start")
    server = _ArchiveServer(ready.recv(), status, request_log)
    ready.close()
    try:
        proxy_type = spack.util.proxy.LocalHTTPProxy
        monkeypatch.setattr(
            spack.util.proxy,
            "LocalHTTPProxy",
            lambda policy, credential, timeout: proxy_type(
                policy,
                credential=credential,
                timeout=timeout,
                address_allowed=lambda address: True,
            ),
        )
        yield server
    finally:
        stopped.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join()


def _package_for_url(url, monkeypatch, tmp_path, checksum=None):
    monkeypatch.setattr(
        spack.package_base.PackageBase, "fetcher", URLFetchStrategy(url=url, checksum=checksum)
    )
    monkeypatch.setattr(spack.caches, "FETCH_CACHE", FsCache(str(tmp_path / "cache")))
    package = spack.concretize.concretize_one("trivial-install-test-package").package
    setattr(package, "path", str(tmp_path / "selected-stage"))
    return package


def test_stage_package_accepts_no_code_package(tmp_path):
    package = spack.concretize.concretize_one("python-venv").package
    setattr(package, "path", str(tmp_path / "selected-stage"))

    assert _local_stage_read_roots(package) == []
    assert stage_package(package) == package.stage.path


def test_stage_package_fetches_and_expands_through_proxy(archive_server, monkeypatch, tmp_path):
    if not spack.sandbox.network_supervision_available():
        pytest.skip("seccomp network supervision is unavailable")
    package = _package_for_url(archive_server.url, monkeypatch, tmp_path)

    stage_path = stage_package(package)

    assert stage_path == package.path
    assert (tmp_path / "selected-stage" / "spack-src" / "configure").is_file()


def test_stage_package_reads_parent_selected_local_source(mock_archive, monkeypatch, tmp_path):
    package = _package_for_url(mock_archive.url, monkeypatch, tmp_path)

    stage_path = stage_package(package)

    assert stage_path == package.path
    assert (tmp_path / "selected-stage" / "spack-src" / "configure").is_file()


def test_stage_package_expands_cached_tar_xz(monkeypatch, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "configure").write_text("#!/bin/sh\n")
    archive = tmp_path / "libxml2.tar.xz"
    with tarfile.open(str(archive), "w:xz") as stream:
        stream.add(str(source), arcname="libxml2")
    package = _package_for_url(archive.as_uri(), monkeypatch, tmp_path)

    stage_path = stage_package(package)

    assert stage_path == package.path
    assert (tmp_path / "selected-stage" / "spack-src" / "configure").is_file()


def test_stage_package_patches_under_parent_stage_lock(mock_archive, monkeypatch, tmp_path):
    monkeypatch.setattr(
        spack.package_base.PackageBase,
        "fetcher",
        URLFetchStrategy(url=mock_archive.url, checksum=None),
    )
    monkeypatch.setattr(spack.caches, "FETCH_CACHE", FsCache(str(tmp_path / "cache")))
    package = spack.concretize.concretize_one("patch-a-dependency")["libelf"].package
    setattr(package, "path", str(tmp_path / "selected-stage"))

    with package.stage:
        stage_path = stage_package(package, patch=True, acquire_lock=False)
        configure = tmp_path / "selected-stage" / "spack-src" / "configure"
        assert "Patched!" in configure.read_text()

    assert stage_path == package.path


@pytest.mark.parametrize("acquire_lock", [False, True])
def test_stage_package_grants_lock_write_only_when_acquired(
    mock_archive, monkeypatch, tmp_path, acquire_lock
):
    package = _package_for_url(mock_archive.url, monkeypatch, tmp_path)
    captured_read_roots = []
    captured_write_roots = []

    def capture_setup(read_roots, write_roots):
        captured_read_roots.extend(read_roots)
        captured_write_roots.extend(write_roots)

    def run_worker(request, worker, proxy_policy, setup):
        setup()
        return {"dag_hash": package.spec.dag_hash(), "path": package.stage.path}

    monkeypatch.setattr(spack.install_worker.stage, "_stage_setup", capture_setup)
    monkeypatch.setattr(spack.util.sandbox, "run_json_worker_with_network", run_worker)

    stage_package(package, acquire_lock=acquire_lock)

    stage_lock = os.path.join(spack.stage.get_stage_root(), ".lock")
    assert set(_store_database_read_roots()).issubset(captured_read_roots)
    assert set(_dependency_read_roots(package.spec)).issubset(captured_read_roots)
    assert (stage_lock in captured_write_roots) is acquire_lock


def test_stage_package_preserves_mirror_and_fetch_failure(
    archive_server, monkeypatch, mutable_config, tmp_path
):
    if not spack.sandbox.network_supervision_available():
        pytest.skip("seccomp network supervision is unavailable")
    archive_server.status = 404
    mutable_config.set("mirrors", {"broken": archive_server.url.rsplit("/", 1)[0]})
    package = _package_for_url(archive_server.url, monkeypatch, tmp_path)

    with pytest.raises(spack.util.sandbox.JsonWorkerError, match=r"(?s)FetchError:.*404"):
        stage_package(package)
    assert "/archive.tar.gz" in archive_server.requests
    assert any(path != "/archive.tar.gz" for path in archive_server.requests)


def test_stage_package_preserves_checksum_failure(archive_server, monkeypatch, tmp_path):
    if not spack.sandbox.network_supervision_available():
        pytest.skip("seccomp network supervision is unavailable")
    package = _package_for_url(archive_server.url, monkeypatch, tmp_path, checksum="0" * 64)

    with spack.config.CONFIG.override("config:checksum", True), pytest.raises(
        spack.util.sandbox.JsonWorkerError, match="ChecksumError:"
    ):
        stage_package(package)


def test_stage_policy_limits_process_network_ipc_and_writes(tmp_path):
    if not spack.sandbox.network_supervision_available():
        pytest.skip("seccomp network supervision is unavailable")

    selected = tmp_path / "selected"
    selected.mkdir()
    sibling = tmp_path / "sibling"
    sibling.mkdir()

    def worker(request):
        results = {}
        try:
            with open(request["sibling"], "w") as stream:
                stream.write("denied")
        except OSError as error:
            results["write_errno"] = error.errno

        for name, family, socket_type in (
            ("udp", socket.AF_INET, socket.SOCK_DGRAM),
            ("unix", socket.AF_UNIX, socket.SOCK_STREAM),
        ):
            try:
                connection = socket.socket(family, socket_type)
            except OSError as error:
                results[name + "_errno"] = error.errno
            else:
                connection.close()

        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            connection.connect(("127.0.0.1", 9))
            results["tcp_peer"] = connection.getpeername()[1]
        finally:
            connection.close()

        try:
            socket.getaddrinfo("install-worker.invalid", 80)
        except socket.gaierror as error:
            results["dns_error"] = str(error)

        tar = subprocess.Popen([request["tar"], "--version"], stdout=subprocess.PIPE)
        tar.communicate()
        results["tar_status"] = tar.returncode
        try:
            subprocess.Popen([request["unselected"]]).wait()
        except OSError as error:
            results["process_errno"] = error.errno
        return results

    read_roots = _expansion_read_roots()
    tar = next(path for path in read_roots if os.path.basename(path) == "tar")
    unselected = "/usr/bin/true"
    assert unselected not in read_roots
    result = spack.util.sandbox.run_json_worker_with_network(
        {"sibling": str(sibling / "denied"), "tar": tar, "unselected": unselected},
        worker,
        DestinationPolicy.allow_any(),
        setup=lambda: spack.sandbox.restrict_stage_worker(read_roots, [str(selected)]),
        timeout=10,
    )

    assert result["write_errno"] == 13
    assert result["udp_errno"] != 0
    assert result["unix_errno"] != 0
    assert result["tcp_peer"] != 9
    assert result["dns_error"]
    assert result["tar_status"] == 0
    assert result["process_errno"] == 13
    assert not (sibling / "denied").exists()


def test_expansion_roots_include_gzip_helper_chain(monkeypatch):
    tools = {name: "/tools/{0}".format(name) for name in spack.install_worker.stage._STAGE_TOOLS}
    monkeypatch.setattr(spack.install_worker.stage, "which_string", tools.get)
    monkeypatch.setattr(spack.install_worker.stage, "host_dynamic_linker_search_paths", lambda: [])

    read_roots = _expansion_read_roots()

    assert "/tools/gzip" in read_roots
    assert "/tools/gunzip" in read_roots
    assert "/tools/sh" in read_roots


def test_tool_runtime_roots_include_selected_tool_and_dependency_closure(tmp_path):
    tar_prefix = tmp_path / "tar"
    tar = tar_prefix / "bin" / "tar"
    tar.parent.mkdir(parents=True)
    tar.touch()
    libiconv = SimpleNamespace(prefix=tmp_path / "libiconv")
    unrelated = SimpleNamespace(prefix=tmp_path / "unrelated")
    owner = SimpleNamespace(prefix=tar_prefix, traverse=lambda **kwargs: [libiconv])
    spec = SimpleNamespace(traverse=lambda: [owner, unrelated])

    roots = _tool_runtime_roots(spec, [str(tar)])

    assert roots == [str(owner.prefix), str(libiconv.prefix)]


def test_stage_setup_failure_names_operation(monkeypatch):
    def fail_setup(read_roots, write_roots):
        raise spack.sandbox.SandboxError("Landlock setup failed")

    monkeypatch.setattr(spack.sandbox, "restrict_stage_worker", fail_setup)

    with pytest.raises(
        spack.util.sandbox.JsonWorkerError,
        match=(
            "stage worker setup restrict_stage_worker failed: SandboxError: Landlock setup failed"
        ),
    ):
        spack.util.sandbox.run_json_worker(
            {}, lambda request: {}, setup=lambda: _stage_setup([], [])
        )


def test_stage_setup_reinitializes_store_before_confinement(monkeypatch):
    events = []
    monkeypatch.setattr(
        spack.install_worker.stage.FILE_TRACKER,
        "discard_after_fork",
        lambda: events.append("locks"),
    )
    monkeypatch.setattr(
        spack.install_worker.stage.spack.store, "reinitialize", lambda: events.append("store")
    )
    monkeypatch.setattr(
        spack.sandbox,
        "restrict_stage_worker",
        lambda read_roots, write_roots: events.append("confine"),
    )

    _stage_setup([], [])

    assert events == ["locks", "store", "confine"]


def test_stage_response_must_match_parent_selected_path():
    with pytest.raises(StageWorkerError, match="stage worker returned an invalid response"):
        _validate_stage_response(
            {"dag_hash": "expected", "path": "/tmp/unexpected"}, "expected", "/tmp/selected"
        )
