# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import base64
import os
import socket
import struct
import threading
import time
import urllib.parse

import pytest

import spack.sandbox
import spack.util.sandbox
from spack.util.proxy import DestinationPolicy


def _echo_worker(request):
    return {"echo": request["value"]}


def _failing_worker(request):
    print("untrusted diagnostic output")
    raise RuntimeError("untrusted failure")


def _writing_worker(request):
    print("untrusted terminal output")
    return request


def _sleeping_worker(request):
    time.sleep(1)
    return request


def test_json_worker_transfers_primitive_request_and_response():
    response = spack.util.sandbox.run_json_worker({"value": "hello"}, _echo_worker)
    assert response == {"echo": "hello"}


def test_json_worker_hides_child_standard_output(capsys):
    assert spack.util.sandbox.run_json_worker({"value": "hello"}, _writing_worker) == {
        "value": "hello"
    }
    assert capsys.readouterr().out == ""


def test_json_worker_closes_inherited_descriptors(tmp_path):
    inherited_path = tmp_path / "inherited"
    inherited_fd = os.open(str(inherited_path), os.O_CREAT | os.O_RDWR, 0o600)

    def worker(request):
        try:
            os.fstat(inherited_fd)
        except OSError:
            inherited_open = False
        else:
            inherited_open = True
        return {"inherited_open": inherited_open, "stdin_is_empty": not os.read(0, 1)}

    try:
        result = spack.util.sandbox.run_json_worker({}, worker)
    finally:
        os.close(inherited_fd)

    assert result == {"inherited_open": False, "stdin_is_empty": True}


def test_json_worker_runs_setup_in_child_before_worker(tmp_path):
    marker = tmp_path / "setup-pid"

    def setup():
        marker.write_text(str(os.getpid()))

    def worker(request):
        return {"setup_pid": int(marker.read_text()), "worker_pid": os.getpid()}

    result = spack.util.sandbox.run_json_worker({}, worker, setup)
    assert result["setup_pid"] == result["worker_pid"]
    assert result["worker_pid"] != os.getpid()


def test_json_worker_reports_child_exception_and_traceback():
    with pytest.raises(
        spack.util.sandbox.JsonWorkerError, match="RuntimeError: untrusted failure"
    ) as error:
        spack.util.sandbox.run_json_worker({}, _failing_worker)
    message = str(error.value)
    assert "_failing_worker" in message
    assert "untrusted diagnostic output" in message


def test_json_worker_reaps_child_when_request_is_invalid():
    with pytest.raises(spack.util.sandbox.JsonWorkerError, match="not JSON compatible"):
        spack.util.sandbox.run_json_worker({"value": object()}, _echo_worker)


def test_json_worker_times_out_and_reaps_child():
    start = time.monotonic()
    with pytest.raises(spack.util.sandbox.JsonWorkerError, match="timed out"):
        spack.util.sandbox.run_json_worker({}, _sleeping_worker, timeout=0.05)
    assert time.monotonic() - start < 0.5


@pytest.mark.parametrize("payload", [b'{"field": 1, "field": 2}', None])
def test_json_worker_rejects_invalid_messages(payload):
    read_fd, write_fd = os.pipe()
    try:
        if payload is None:
            os.write(write_fd, struct.pack(">Q", spack.util.sandbox.MAX_MESSAGE_BYTES + 1))
        else:
            os.write(write_fd, struct.pack(">Q", len(payload)) + payload)
        with pytest.raises(spack.util.sandbox.JsonWorkerError):
            spack.util.sandbox._read_message(read_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_listener_fd_number_handshake():
    parent, child = socket.socketpair()
    completed = []

    def send_listener_number():
        spack.util.sandbox._send_listener_fd_number(child, 42)
        completed.append(True)

    thread = threading.Thread(target=send_listener_number)
    thread.start()
    try:
        assert spack.util.sandbox._receive_listener_fd_number(parent, 1) == 42
        assert not completed
        parent.sendall(b"1")
        thread.join(timeout=1)
        assert completed == [True]
    finally:
        parent.close()
        child.close()


def test_network_worker_connects_only_to_local_proxy():
    if not spack.sandbox.network_supervision_available():
        pytest.skip("seccomp network supervision is unavailable")

    def worker(request):
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            connection.settimeout(5)
            connection.connect(("127.0.0.1", 9))
            parsed = urllib.parse.urlsplit(os.environ["HTTP_PROXY"])
            credential = "{0}:{1}".format(parsed.username, parsed.password).encode("utf-8")
            authorization = base64.b64encode(credential).decode("ascii")
            headers = (
                "Host: ignored\r\nProxy-Authorization: Basic {0}\r\n".format(authorization)
                + "Connection: close\r\n\r\n"
            )
            connection.sendall(
                b"GET http://127.0.0.1:9/probe HTTP/1.1\r\n" + headers.encode("ascii")
            )
            return {"response": connection.recv(1024).decode("ascii", "replace")}
        finally:
            connection.close()

    result = spack.util.sandbox.run_json_worker_with_network(
        {}, worker, DestinationPolicy.allow_any(), timeout=10
    )

    assert result["response"].startswith("HTTP/1.1 502")


def test_network_worker_injects_authenticated_proxy_environment(monkeypatch):
    if not spack.sandbox.network_supervision_available():
        pytest.skip("seccomp network supervision is unavailable")
    monkeypatch.setenv("NO_PROXY", "localhost")

    def worker(request):
        return {name: os.environ.get(name) for name in ("HTTP_PROXY", "http_proxy", "NO_PROXY")}

    result = spack.util.sandbox.run_json_worker_with_network(
        {}, worker, DestinationPolicy.allow_any(), timeout=10
    )

    assert result["HTTP_PROXY"] == result["http_proxy"]
    assert result["NO_PROXY"] is None
    parsed = urllib.parse.urlsplit(result["HTTP_PROXY"])
    assert parsed.scheme == "http"
    assert parsed.username == "spack"
    assert parsed.password
    assert parsed.hostname == "127.0.0.1"
