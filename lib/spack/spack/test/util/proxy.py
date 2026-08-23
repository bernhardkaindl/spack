# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import base64
import contextlib
import errno
import http.client
import http.server
import os
import socket
import socketserver
import threading
import urllib.parse
from typing import Any, cast

import pytest

import spack.util.proxy as proxy_util
from spack.sandbox import SeccompData, SeccompNotification
from spack.util.proxy import (
    ConnectSupervisor,
    Destination,
    DestinationPolicy,
    LocalHTTPProxy,
    ProxyPolicyError,
    destination_from_url,
    global_address,
)


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class _UpstreamHandler(http.server.BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        self.requests.append((self.path, self.headers.get("Host")))
        body = b"proxied response"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@pytest.fixture
def upstream_server():
    _UpstreamHandler.requests = []
    server = _ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _proxy_address(local_proxy):
    parsed = urllib.parse.urlsplit(local_proxy.url)
    return parsed.hostname, parsed.port


def _proxy_connection(local_proxy):
    return http.client.HTTPConnection(*_proxy_address(local_proxy), timeout=2)


def _test_proxy(policy, **kwargs):
    return LocalHTTPProxy(
        policy, credential="test-proxy-token", address_allowed=lambda address: True, **kwargs
    )


def _proxy_headers(local_proxy):
    credential = "spack:{0}".format(local_proxy.credential).encode("ascii")
    return {
        "Proxy-Authorization": "Basic {0}".format(base64.b64encode(credential).decode("ascii"))
    }


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://example.com/archive.tar.gz", Destination("http", "example.com", 80)),
        ("HTTPS://Example.COM./path", Destination("https", "example.com", 443)),
        ("ftp://example.com:2121/file", Destination("ftp", "example.com", 2121)),
        ("https://bücher.example/file", Destination("https", "xn--bcher-kva.example", 443)),
    ],
)
def test_destination_from_url(url, expected):
    assert destination_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "example.com/file",
        "ssh://example.com/file",
        "https:///file",
        "https://user@example.com/file",
        "https://example.com:0/file",
        "https://example.com:not-a-port/file",
    ],
)
def test_destination_from_url_rejects_invalid_policy_input(url):
    with pytest.raises(ProxyPolicyError):
        destination_from_url(url)


def test_destination_policy_only_allows_granted_authorities():
    policy = DestinationPolicy.from_urls(
        ["https://example.com/one", "http://example.net:8080/two"]
    )

    assert policy.allows("HTTPS", "EXAMPLE.COM.")
    assert policy.allows("http", "example.net", 8080)
    assert not policy.allows("https", "example.com", 444)
    assert not policy.allows("https", "other.example.com")
    assert not policy.allows("ftp", "example.com")


def test_destination_policy_can_allow_any_supported_authority():
    policy = DestinationPolicy.allow_any()

    assert policy.allows("https", "arbitrary.example", 443)
    assert policy.allows("ftp", "another.example", 2121)
    assert not policy.allows("ssh", "arbitrary.example", 22)


def test_http_forward_proxy(upstream_server):
    upstream_host, upstream_port = upstream_server.server_address
    target = "http://{0}:{1}/archive?version=1".format(upstream_host, upstream_port)
    with _test_proxy(DestinationPolicy.from_urls([target])) as local_proxy:
        connection = _proxy_connection(local_proxy)
        headers = _proxy_headers(local_proxy)
        headers["Host"] = "untrusted.example"
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == b"proxied response"
        connection.close()

    assert _UpstreamHandler.requests == [
        ("/archive?version=1", "{0}:{1}".format(upstream_host, upstream_port))
    ]


def test_http_forward_proxy_denies_ungranted_destination(monkeypatch, upstream_server):
    upstream_host, upstream_port = upstream_server.server_address
    denied = []
    target = "http://{0}:{1}/archive".format(upstream_host, upstream_port)
    policy = DestinationPolicy.from_urls(["http://example.com/allowed"])
    local_proxy = _test_proxy(
        policy, denial_logger=lambda destination, reason: denied.append(destination)
    )
    monkeypatch.setattr(
        local_proxy,
        "_resolve_and_connect",
        lambda *args, **kwargs: pytest.fail("denied destination must not be resolved"),
    )
    with local_proxy:
        connection = _proxy_connection(local_proxy)
        connection.request("GET", target, headers=_proxy_headers(local_proxy))
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.close()

    assert denied == [Destination("http", upstream_host, upstream_port)]
    assert not _UpstreamHandler.requests


def test_proxy_requires_invocation_credential(upstream_server):
    upstream_host, upstream_port = upstream_server.server_address
    target = "http://{0}:{1}/archive".format(upstream_host, upstream_port)
    with _test_proxy(DestinationPolicy.from_urls([target])) as local_proxy:
        connection = _proxy_connection(local_proxy)
        connection.request("GET", target)
        response = connection.getresponse()
        assert response.status == 407
        assert response.getheader("Proxy-Authenticate") == 'Basic realm="Spack invocation"'
        response.read()
        connection.close()

        assert local_proxy.authenticated_url.startswith("http://spack:test-proxy-token@")

    assert not _UpstreamHandler.requests


@pytest.mark.parametrize("authority", ["example.com", "user@example.com:443", "example.com:0"])
def test_connect_proxy_rejects_malformed_authority(authority):
    with _test_proxy(DestinationPolicy.from_urls([])) as local_proxy:
        with contextlib.closing(socket.create_connection(_proxy_address(local_proxy))) as client:
            client.sendall(
                "CONNECT {0} HTTP/1.1\r\nHost: ignored.example\r\n{1}: {2}\r\n\r\n".format(
                    authority,
                    "Proxy-Authorization",
                    _proxy_headers(local_proxy)["Proxy-Authorization"],
                ).encode("ascii")
            )
            assert client.recv(4096).startswith(b"HTTP/1.1 400")


def test_connect_proxy_tunnels_bytes(upstream_server):
    upstream_host, upstream_port = upstream_server.server_address
    policy = DestinationPolicy.from_urls(["https://{0}:{1}/".format(upstream_host, upstream_port)])
    with _test_proxy(policy) as local_proxy:
        with contextlib.closing(socket.create_connection(_proxy_address(local_proxy))) as client:
            client.sendall(
                "CONNECT {0}:{1} HTTP/1.1\r\nHost: ignored.example\r\n{2}: {3}\r\n\r\n".format(
                    upstream_host,
                    upstream_port,
                    "Proxy-Authorization",
                    _proxy_headers(local_proxy)["Proxy-Authorization"],
                ).encode("ascii")
            )
            response = b""
            while b"\r\n\r\n" not in response:
                response += client.recv(4096)
            assert response.startswith(b"HTTP/1.1 200")

            client.sendall(
                b"GET /tunnel HTTP/1.1\r\nHost: through-tunnel\r\nConnection: close\r\n\r\n"
            )
            tunneled = b""
            while True:
                data = client.recv(4096)
                if not data:
                    break
                tunneled += data

    assert b"proxied response" in tunneled
    assert _UpstreamHandler.requests == [("/tunnel", "through-tunnel")]


def test_connect_proxy_ends_relay_when_peer_resets(monkeypatch):
    class ResetSocket:
        def recv(self, size):
            raise ConnectionResetError()

    with _test_proxy(DestinationPolicy.allow_any()) as local_proxy:
        handler = object.__new__(local_proxy._make_handler())
        handler.connection = ResetSocket()
        monkeypatch.setattr(
            proxy_util.select, "select", lambda *args: ([handler.connection], [], [])
        )

        handler._relay(socket.socket())


def test_passive_ftp_ignores_server_provided_address(monkeypatch):
    monkeypatch.setattr(proxy_util.ftplib.FTP, "makepasv", lambda self: ("203.0.113.10", 4321))

    class ControlSocket:
        def getpeername(self):
            return "192.0.2.20", 21

    ftp = proxy_util._PassiveFTP()
    ftp.sock = cast(Any, ControlSocket())
    assert ftp.makepasv() == ("192.0.2.20", 4321)


def test_ftp_gateway_returns_passive_transfer(monkeypatch):
    class DataSocket:
        def __init__(self):
            self.chunks = iter((b"archive data", b""))

        def recv(self, size):
            return next(self.chunks)

        def close(self):
            pass

    class FTP:
        def connect(self, host, port, timeout):
            assert (host, port) == ("203.0.113.20", 21)

        def login(self):
            pass

        def voidcmd(self, command):
            assert command == "TYPE I"

        def ntransfercmd(self, command):
            assert command == "RETR /archive.tar.gz"
            return DataSocket(), len(b"archive data")

        def voidresp(self):
            pass

        def quit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(proxy_util, "_PassiveFTP", FTP)
    getaddrinfo = proxy_util.socket.getaddrinfo
    monkeypatch.setattr(
        proxy_util.socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: (
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.20", 21))]
            if host == "example.com"
            else getaddrinfo(host, *args, **kwargs)
        ),
    )
    target = "ftp://example.com/archive.tar.gz"
    with _test_proxy(DestinationPolicy.from_urls([target])) as local_proxy:
        connection = _proxy_connection(local_proxy)
        connection.request("GET", target, headers=_proxy_headers(local_proxy))
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == b"archive data"
        connection.close()


@pytest.mark.parametrize(
    "address,expected",
    [
        ("127.0.0.1", False),
        ("10.0.0.1", False),
        ("169.254.169.254", False),
        ("::1", False),
        ("2001:4860:4860::8888", True),
        ("8.8.8.8", True),
    ],
)
def test_global_address(address, expected):
    assert global_address(address) is expected


def test_proxy_rejects_non_global_address_after_dns(monkeypatch):
    target = "http://metadata.example/latest"
    policy = DestinationPolicy.from_urls([target])
    denied = []
    getaddrinfo = proxy_util.socket.getaddrinfo
    monkeypatch.setattr(
        proxy_util.socket,
        "getaddrinfo",
        lambda host, *args, **kwargs: (
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))]
            if host == "metadata.example"
            else getaddrinfo(host, *args, **kwargs)
        ),
    )
    local_proxy = LocalHTTPProxy(
        policy,
        credential="test-proxy-token",
        denial_logger=lambda destination, reason: denied.append((destination, reason)),
    )
    local_proxy.bind()
    local_proxy.start()
    try:
        connection = _proxy_connection(local_proxy)
        connection.request("GET", target, headers=_proxy_headers(local_proxy))
        response = connection.getresponse()
        assert response.status == 502
        response.read()
        connection.close()
    finally:
        local_proxy.stop()

    assert denied == [
        (
            Destination("http", "metadata.example", 80),
            "destination resolved only to prohibited addresses",
        )
    ]


class _FakeSeccomp:
    def __init__(self, notification):
        self.notification = notification
        self.added_fds = []
        self.responses = []

    def _get_syscall_number(self, name):
        return {"connect": 42, "socket": 41}[name]

    def receive_notification(self, listener_fd):
        return self.notification

    def notification_is_valid(self, listener_fd, notification_id):
        return True

    def respond_to_notification(self, listener_fd, notification_id, value=0, error=0):
        self.responses.append((notification_id, value, error))

    def addfd_to_notification(
        self, listener_fd, notification_id, source_fd, flags=0, newfd=0, newfd_flags=0
    ):
        self.added_fds.append((notification_id, flags, newfd, newfd_flags))
        return 8


def _notification(pid=123, syscall=42, target_fd=7):
    data = SeccompData(nr=syscall, arch=0, instruction_pointer=0)
    data.args[0] = target_fd
    return SeccompNotification(id=99, pid=pid, flags=0, data=data)


def test_connect_supervisor_connects_shared_socket_to_proxy():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    worker_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    seccomp = _FakeSeccomp(_notification(target_fd=worker_socket.fileno()))
    supervisor = ConnectSupervisor(
        listener_fd=10,
        worker_pid=123,
        pidfd=11,
        proxy_address=listener.getsockname(),
        seccomp=cast(Any, seccomp),
        duplicate_fd=lambda pidfd, target_fd: os.dup(target_fd),
        thread_group_id=lambda thread_id, is_valid: 123,
    )
    try:
        supervisor.handle_once()
        accepted, _address = listener.accept()
        accepted.close()
        assert worker_socket.getpeername() == listener.getsockname()
        assert seccomp.responses == [(99, 0, 0)]
    finally:
        worker_socket.close()
        listener.close()


@pytest.mark.parametrize(
    "notification,expected_error",
    [(_notification(pid=456), errno.EPERM), (_notification(syscall=43), errno.ENOSYS)],
)
def test_connect_supervisor_rejects_unexpected_notification(notification, expected_error):
    seccomp = _FakeSeccomp(notification)
    supervisor = ConnectSupervisor(
        listener_fd=10,
        worker_pid=123,
        pidfd=11,
        proxy_address=("127.0.0.1", 1),
        seccomp=cast(Any, seccomp),
        duplicate_fd=lambda pidfd, target_fd: pytest.fail("descriptor must not be duplicated"),
        thread_group_id=lambda thread_id, is_valid: notification.pid,
    )

    supervisor.handle_once()

    assert seccomp.responses == [(99, 0, expected_error)]


def test_connect_supervisor_injects_tcp_socket():
    notification = _notification(syscall=41)
    notification.data.args[0] = socket.AF_INET
    notification.data.args[1] = socket.SOCK_STREAM | socket.SOCK_CLOEXEC
    notification.data.args[2] = 0
    seccomp = _FakeSeccomp(notification)
    supervisor = ConnectSupervisor(
        listener_fd=10,
        worker_pid=123,
        pidfd=11,
        proxy_address=("127.0.0.1", 1),
        seccomp=cast(Any, seccomp),
        thread_group_id=lambda thread_id, is_valid: 123,
    )

    supervisor.handle_once()

    assert seccomp.added_fds == [(99, proxy_util.SECCOMP_ADDFD_FLAG_SEND, 0, os.O_CLOEXEC)]
    assert not seccomp.responses


@pytest.mark.parametrize(
    "domain,socket_type,protocol",
    [
        (socket.AF_INET, socket.SOCK_DGRAM, 0),
        (socket.AF_INET6, socket.SOCK_STREAM, 0),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_UDP),
    ],
)
def test_connect_supervisor_denies_unsupported_socket(domain, socket_type, protocol):
    notification = _notification(syscall=41)
    notification.data.args[0] = domain
    notification.data.args[1] = socket_type
    notification.data.args[2] = protocol
    seccomp = _FakeSeccomp(notification)
    supervisor = ConnectSupervisor(
        listener_fd=10,
        worker_pid=123,
        pidfd=11,
        proxy_address=("127.0.0.1", 1),
        seccomp=cast(Any, seccomp),
        thread_group_id=lambda thread_id, is_valid: 123,
    )

    supervisor.handle_once()

    assert seccomp.responses == [(99, 0, errno.EPROTONOSUPPORT)]
    assert not seccomp.added_fds
