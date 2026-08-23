# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Policy and protocol support for invocation-scoped local network proxies."""

import base64
import contextlib
import errno
import fcntl
import ftplib
import hmac
import http.client
import http.server
import ipaddress
import os
import select
import socket
import socketserver
import threading
import urllib.parse
from typing import Callable, FrozenSet, Iterable, NamedTuple, Optional, Tuple

import spack.util.tty as tty
from spack.sandbox import SECCOMP_ADDFD_FLAG_SEND, SeccompNotification, SeccompSandbox, pidfd_getfd


class ProxyPolicyError(ValueError):
    """Raised when proxy policy input is malformed or unsupported."""


class Destination(NamedTuple):
    """Canonical network destination authorized by a proxy policy."""

    scheme: str
    host: str
    port: int


_DEFAULT_PORTS = {"ftp": 21, "http": 80, "https": 443}
_HOP_BY_HOP_HEADERS = frozenset(
    (
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    )
)
_RELAY_BUFFER_SIZE = 64 * 1024
_CONNECT_IN_PROGRESS = frozenset((errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK))
_PROXY_USERNAME = "spack"


def global_address(address: str) -> bool:
    """Return whether an address is globally routable."""
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def _thread_group_id(thread_id: int, is_valid: Callable[[], bool]) -> Optional[int]:
    """Return a blocked thread's process ID without trusting a reusable TID."""
    try:
        status_fd = os.open("/proc/{0}/status".format(thread_id), os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not is_valid():
            return None
        status = os.read(status_fd, 64 * 1024).decode("ascii", errors="replace")
        if not is_valid():
            return None
    finally:
        os.close(status_fd)
    for line in status.splitlines():
        if line.startswith("Tgid:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def destination_from_url(url: str) -> Destination:
    """Return the canonical destination named by an absolute fetch URL."""
    if not isinstance(url, str):
        raise ProxyPolicyError("proxy destination URL must be a string")

    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ProxyPolicyError("proxy destination URL has an invalid authority") from error

    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ProxyPolicyError("proxy destination URL has an unsupported scheme")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ProxyPolicyError("proxy destination URL must have an uncredentialed authority")

    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as error:
        raise ProxyPolicyError("proxy destination URL has an invalid hostname") from error
    if not host:
        raise ProxyPolicyError("proxy destination URL has an invalid hostname")

    if port is not None and port == 0:
        raise ProxyPolicyError("proxy destination URL has an invalid port")

    return Destination(
        scheme=scheme, host=host, port=port if port is not None else _DEFAULT_PORTS[scheme]
    )


class DestinationPolicy:
    """Immutable set of destinations granted by the trusted parent."""

    def __init__(self, destinations: Iterable[Destination], allow_any: bool = False):
        self._destinations: FrozenSet[Destination] = frozenset(destinations)
        self._allow_any = allow_any

    @classmethod
    def from_urls(cls, urls: Iterable[str]) -> "DestinationPolicy":
        """Create invocation policy from absolute URLs selected by a trusted parent."""
        return cls(destination_from_url(url) for url in urls)

    @classmethod
    def allow_any(cls) -> "DestinationPolicy":
        """Allow any syntactically valid authority; address policy remains independent."""
        return cls((), allow_any=True)

    def allows(self, scheme: str, host: str, port: Optional[int] = None) -> bool:
        """Return whether the canonicalized destination is in this policy."""
        normalized_scheme = scheme.lower()
        if normalized_scheme not in _DEFAULT_PORTS:
            return False
        try:
            normalized_host = host.encode("idna").decode("ascii").lower().rstrip(".")
        except (AttributeError, UnicodeError):
            return False
        destination = Destination(
            scheme=normalized_scheme,
            host=normalized_host,
            port=port if port is not None else _DEFAULT_PORTS[normalized_scheme],
        )
        return self._allow_any or destination in self._destinations


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _PassiveFTP(ftplib.FTP):
    """FTP client that ignores PASV-provided addresses to prevent FTP bounce."""

    def makepasv(self) -> Tuple[str, int]:
        _host, port = super().makepasv()
        if self.sock is None:
            raise ftplib.Error("FTP control connection is not established")
        return self.sock.getpeername()[0], port


class _ConnectedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection using a socket already resolved and connected by the proxy."""

    def __init__(self, destination: Destination, outbound: socket.socket, timeout: float):
        super().__init__(destination.host, destination.port, timeout=timeout)
        self._outbound = outbound

    def connect(self) -> None:
        self.sock = self._outbound


class LocalHTTPProxy:
    """Invocation-scoped HTTP forward and CONNECT proxy."""

    def __init__(
        self,
        policy: DestinationPolicy,
        credential: str,
        timeout: float = 30.0,
        denial_logger: Optional[Callable[[Destination, str], None]] = None,
        address_allowed: Callable[[str], bool] = global_address,
    ):
        if not credential:
            raise ValueError("proxy credential must not be empty")
        self.policy = policy
        self.credential = credential
        self.timeout = timeout
        self.denial_logger = denial_logger or self._log_denial
        self.address_allowed = address_allowed
        self._server: Optional[_ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "LocalHTTPProxy":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop()

    @property
    def url(self) -> str:
        """Return the loopback proxy URL after the server has started."""
        if self._server is None:
            raise RuntimeError("proxy is not running")
        host, port = self._server.server_address[:2]
        return "http://{0}:{1}".format(host, port)

    @property
    def authenticated_url(self) -> str:
        """Return the worker proxy URL with the invocation credential."""
        return "http://{0}:{1}@{2}".format(
            _PROXY_USERNAME, urllib.parse.quote(self.credential, safe=""), self.url[7:]
        )

    def bind(self) -> None:
        """Bind an ephemeral IPv4 loopback listener without starting a thread."""
        if self._server is not None:
            raise RuntimeError("proxy is already bound")
        handler = self._make_handler()
        self._server = _ThreadingHTTPServer(("127.0.0.1", 0), handler)

    def start(self) -> None:
        """Start serving on the bound or a new ephemeral loopback listener."""
        if self._thread is not None:
            raise RuntimeError("proxy is already running")
        if self._server is None:
            self.bind()
        assert self._server is not None
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.daemon = True
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and close the loopback listener."""
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        if thread is not None:
            server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join()

    def _log_denial(self, destination: Destination, reason: str) -> None:
        tty.warn(
            "proxy denied {0}://{1}:{2}: {3}".format(
                destination.scheme, destination.host, destination.port, reason
            )
        )

    def _resolve_and_connect(self, destination: Destination) -> socket.socket:
        """Resolve and connect to a policy-approved destination in the proxy."""
        last_error = None
        addresses = socket.getaddrinfo(destination.host, destination.port, type=socket.SOCK_STREAM)
        allowed_addresses = [
            address for address in addresses if self.address_allowed(address[4][0])
        ]
        if not allowed_addresses:
            self.denial_logger(destination, "destination resolved only to prohibited addresses")
            raise OSError(errno.EACCES, "proxy destination address is prohibited")
        for family, socktype, protocol, _canonical_name, address in allowed_addresses:
            outbound = socket.socket(family, socktype, protocol)
            outbound.settimeout(self.timeout)
            try:
                outbound.connect(address)
                return outbound
            except OSError as error:
                last_error = error
                outbound.close()
        if last_error is not None:
            raise last_error
        raise OSError("destination did not resolve to a TCP address")

    def _make_handler(self):
        proxy = self

        class ProxyRequestHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_CONNECT(self) -> None:
                if not self._authenticate():
                    return
                destination = self._connect_destination()
                if destination is None or not self._authorize(destination):
                    return
                try:
                    outbound = proxy._resolve_and_connect(destination)
                except OSError:
                    self.send_error(502, "proxy connection failed")
                    return
                with contextlib.closing(outbound):
                    self.send_response(200, "Connection Established")
                    self.end_headers()
                    self.close_connection = True
                    self._relay(outbound)

            def do_GET(self) -> None:
                self._forward()

            def do_HEAD(self) -> None:
                self._forward()

            def _forward(self) -> None:
                if not self._authenticate():
                    return
                try:
                    destination = destination_from_url(self.path)
                    parsed = urllib.parse.urlsplit(self.path)
                except ProxyPolicyError:
                    self.send_error(400, "absolute proxy URL required")
                    return
                if not self._authorize(destination):
                    return
                if destination.scheme == "ftp":
                    if self.command != "GET":
                        self.send_error(405, "FTP proxy requests require GET")
                        return
                    self._forward_ftp(destination, parsed)
                    return
                if destination.scheme != "http":
                    self.send_error(400, "HTTPS proxy requests require CONNECT")
                    return
                self._forward_http(destination, parsed)

            def _forward_http(
                self, destination: Destination, parsed: urllib.parse.SplitResult
            ) -> None:
                try:
                    outbound = proxy._resolve_and_connect(destination)
                except OSError:
                    self.send_error(502, "proxy connection failed")
                    return
                connection = _ConnectedHTTPConnection(destination, outbound, proxy.timeout)
                path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                headers = self._forward_headers(destination)
                try:
                    connection.request(self.command, path, headers=headers)
                    response = connection.getresponse()
                    self.send_response(response.status, response.reason)
                    for name, value in response.getheaders():
                        if name.lower() not in _HOP_BY_HOP_HEADERS:
                            self.send_header(name, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if self.command != "HEAD":
                        while True:
                            data = response.read(_RELAY_BUFFER_SIZE)
                            if not data:
                                break
                            self.wfile.write(data)
                except (http.client.HTTPException, OSError):
                    self.send_error(502, "proxy request failed")
                    self.close_connection = True
                finally:
                    connection.close()

            def _forward_ftp(
                self, destination: Destination, parsed: urllib.parse.SplitResult
            ) -> None:
                ftp = _PassiveFTP()
                data_socket = None
                try:
                    addresses = socket.getaddrinfo(
                        destination.host, destination.port, type=socket.SOCK_STREAM
                    )
                    allowed = [
                        address for address in addresses if proxy.address_allowed(address[4][0])
                    ]
                    if not allowed:
                        proxy.denial_logger(
                            destination, "destination resolved only to prohibited addresses"
                        )
                        self.send_error(403, "proxy destination address denied")
                        return
                    ftp.connect(allowed[0][4][0], destination.port, timeout=proxy.timeout)
                    ftp.login()
                    ftp.voidcmd("TYPE I")
                    path = urllib.parse.unquote(parsed.path)
                    data_socket, expected_size = ftp.ntransfercmd("RETR " + path)
                    self.send_response(200)
                    if expected_size is not None:
                        self.send_header("Content-Length", str(expected_size))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while True:
                        data = data_socket.recv(_RELAY_BUFFER_SIZE)
                        if not data:
                            break
                        self.wfile.write(data)
                    data_socket.close()
                    data_socket = None
                    ftp.voidresp()
                except (ftplib.Error, OSError):
                    if data_socket is None:
                        self.send_error(502, "FTP proxy request failed")
                    self.close_connection = True
                finally:
                    if data_socket is not None:
                        data_socket.close()
                    with contextlib.suppress(ftplib.Error, OSError):
                        ftp.quit()
                    ftp.close()

            def _connect_destination(self) -> Optional[Destination]:
                try:
                    parsed = urllib.parse.urlsplit("//" + self.path)
                    port = parsed.port
                except ValueError:
                    self.send_error(400, "invalid CONNECT authority")
                    return None
                if (
                    not parsed.hostname
                    or port is None
                    or port == 0
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.path
                    or parsed.query
                    or parsed.fragment
                ):
                    self.send_error(400, "invalid CONNECT authority")
                    return None
                try:
                    host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
                except UnicodeError:
                    self.send_error(400, "invalid CONNECT authority")
                    return None
                return Destination("https", host, port)

            def _authorize(self, destination: Destination) -> bool:
                if proxy.policy.allows(destination.scheme, destination.host, destination.port):
                    return True
                proxy.denial_logger(destination, "destination is not authorized")
                self.send_error(403, "proxy destination denied")
                return False

            def _authenticate(self) -> bool:
                encoded = base64.b64encode(
                    "{0}:{1}".format(_PROXY_USERNAME, proxy.credential).encode("utf-8")
                ).decode("ascii")
                expected = "Basic {0}".format(encoded)
                supplied = self.headers.get("Proxy-Authorization", "")
                if hmac.compare_digest(supplied, expected):
                    return True
                self.send_response(407, "Proxy Authentication Required")
                self.send_header("Proxy-Authenticate", 'Basic realm="Spack invocation"')
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                return False

            def _forward_headers(self, destination: Destination) -> dict:
                connection_tokens = {
                    token.strip().lower()
                    for token in self.headers.get("Connection", "").split(",")
                    if token.strip()
                }
                excluded = _HOP_BY_HOP_HEADERS | connection_tokens | {"host"}
                headers = {
                    name: value
                    for name, value in self.headers.items()
                    if name.lower() not in excluded
                }
                default_port = _DEFAULT_PORTS[destination.scheme]
                headers["Host"] = (
                    destination.host
                    if destination.port == default_port
                    else "{0}:{1}".format(destination.host, destination.port)
                )
                return headers

            def _relay(self, outbound: socket.socket) -> None:
                sockets = [self.connection, outbound]
                while True:
                    readable, _writable, _exceptional = select.select(
                        sockets, [], sockets, proxy.timeout
                    )
                    if not readable:
                        return
                    for source in readable:
                        try:
                            data = source.recv(_RELAY_BUFFER_SIZE)
                        except ConnectionResetError:
                            return
                        if not data:
                            return
                        target = outbound if source is self.connection else self.connection
                        try:
                            target.sendall(data)
                        except (BrokenPipeError, ConnectionResetError):
                            return

            def log_message(self, format, *args) -> None:
                tty.debug("local proxy: " + format % args)

        return ProxyRequestHandler


class ConnectSupervisor:
    """Complete notified worker ``connect`` calls only to a local proxy."""

    def __init__(
        self,
        listener_fd: int,
        worker_pid: int,
        pidfd: int,
        proxy_address: Tuple[str, int],
        timeout: float = 30.0,
        seccomp: Optional[SeccompSandbox] = None,
        duplicate_fd: Callable[[int, int], int] = pidfd_getfd,
        thread_group_id: Callable[[int, Callable[[], bool]], Optional[int]] = _thread_group_id,
    ):
        self.listener_fd = listener_fd
        self.worker_pid = worker_pid
        self.pidfd = pidfd
        self.proxy_address = proxy_address
        self.timeout = timeout
        self.seccomp = seccomp or SeccompSandbox()
        self.duplicate_fd = duplicate_fd
        self.thread_group_id = thread_group_id
        self._connect_syscall = self.seccomp._get_syscall_number("connect")
        self._socket_syscall = self.seccomp._get_syscall_number("socket")

    def handle_once(self) -> None:
        """Receive and answer one connect notification without continuing it."""
        notification = self.seccomp.receive_notification(self.listener_fd)
        if notification.data.nr == self._socket_syscall:
            self._handle_socket(notification)
            return
        error = self._handle(notification)
        try:
            self.seccomp.respond_to_notification(self.listener_fd, notification.id, error=error)
        except OSError as response_error:
            if response_error.errno != errno.ENOENT:
                raise

    def serve(self, stopped: threading.Event) -> None:
        """Handle notifications until ``stopped`` is set."""
        while not stopped.is_set():
            readable, _writable, _exceptional = select.select(
                [self.listener_fd], [], [self.listener_fd], 0.1
            )
            if readable:
                try:
                    self.handle_once()
                except OSError as error:
                    if error.errno == errno.ECANCELED:
                        return
                    raise

    def _handle(self, notification: SeccompNotification) -> int:
        if not self._notification_is_from_worker(notification):
            return errno.EPERM
        if notification.data.nr != self._connect_syscall:
            return errno.ENOSYS
        if not self.seccomp.notification_is_valid(self.listener_fd, notification.id):
            return errno.ENOENT

        target_fd = notification.data.args[0]
        try:
            duplicated_fd = self.duplicate_fd(self.pidfd, target_fd)
        except OSError as error:
            return error.errno or errno.EBADF
        try:
            return self._connect_to_proxy(duplicated_fd, notification.id)
        finally:
            os.close(duplicated_fd)

    def _handle_socket(self, notification: SeccompNotification) -> None:
        if not self._notification_is_from_worker(notification):
            self.seccomp.respond_to_notification(
                self.listener_fd, notification.id, error=errno.EPERM
            )
            return

        domain, socket_type, protocol = notification.data.args[:3]
        allowed_type_flags = socket.SOCK_STREAM | socket.SOCK_NONBLOCK | socket.SOCK_CLOEXEC
        if (
            domain != socket.AF_INET
            or socket_type & ~allowed_type_flags
            or socket_type & 0xF != socket.SOCK_STREAM
            or protocol not in (0, socket.IPPROTO_TCP)
        ):
            self.seccomp.respond_to_notification(
                self.listener_fd, notification.id, error=errno.EPROTONOSUPPORT
            )
            return

        created = socket.socket(domain, socket_type, protocol)
        try:
            newfd_flags = os.O_CLOEXEC if socket_type & socket.SOCK_CLOEXEC else 0
            self.seccomp.addfd_to_notification(
                self.listener_fd,
                notification.id,
                created.fileno(),
                flags=SECCOMP_ADDFD_FLAG_SEND,
                newfd_flags=newfd_flags,
            )
        finally:
            created.close()

    def _notification_is_from_worker(self, notification: SeccompNotification) -> bool:
        is_valid = lambda: self.seccomp.notification_is_valid(self.listener_fd, notification.id)
        return self.thread_group_id(notification.pid, is_valid) == self.worker_pid

    def _connect_to_proxy(self, duplicated_fd: int, notification_id: int) -> int:
        original_flags = fcntl.fcntl(duplicated_fd, fcntl.F_GETFL)
        connection = socket.socket(fileno=duplicated_fd)
        try:
            if connection.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
                return errno.EPROTOTYPE
            fcntl.fcntl(duplicated_fd, fcntl.F_SETFL, original_flags | os.O_NONBLOCK)
            result = connection.connect_ex(self.proxy_address)
            if result not in (0, errno.EISCONN) and result not in _CONNECT_IN_PROGRESS:
                return result
            if result in _CONNECT_IN_PROGRESS:
                _readable, writable, exceptional = select.select(
                    [], [connection], [connection], self.timeout
                )
                if not writable or exceptional:
                    return errno.ETIMEDOUT
                result = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if result:
                    return result
            if not self.seccomp.notification_is_valid(self.listener_fd, notification_id):
                return errno.ENOENT
            return 0
        except OSError as error:
            return error.errno or errno.EIO
        finally:
            fcntl.fcntl(duplicated_fd, fcntl.F_SETFL, original_flags)
            connection.detach()
