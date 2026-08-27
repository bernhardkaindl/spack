# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Bounded byte-only process transport for sandbox command workers."""

import contextlib
import io
import json
import os
import secrets
import select
import signal
import socket
import struct
import tempfile
import threading
import time
import traceback
from typing import Any, Callable, Dict, Generator, Iterable, Optional, Set, Tuple

MAX_MESSAGE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_STREAM_RESPONSE_BYTES = 1024 * 1024 * 1024
_MESSAGE_PREFIX_BYTES = 8
_MAX_FAILURE_DIAGNOSTIC_BYTES = 1024 * 1024
_STREAM_FRAME_BYTES = 64 * 1024


class JsonWorkerError(RuntimeError):
    """The sandbox worker did not produce a valid successful response."""


class _BoundedTextBuffer(io.StringIO):
    """Capture Python diagnostic output without exceeding its response budget."""

    def __init__(self, limit: int):
        super().__init__()
        self.limit = limit
        self.size = 0

    def write(self, text: str) -> int:
        encoded = text.encode("utf-8", errors="replace")
        remaining = self.limit - self.size
        if remaining > 0:
            captured = encoded[:remaining].decode("utf-8", errors="ignore")
            super().write(captured)
            self.size += len(captured.encode("utf-8"))
        return len(text)


def _bounded_text(text: str, limit: int = _MAX_FAILURE_DIAGNOSTIC_BYTES) -> str:
    """Return UTF-8 text truncated to at most ``limit`` encoded bytes."""
    encoded = text.encode("utf-8", errors="replace")
    return encoded[:limit].decode("utf-8", errors="ignore")


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JsonWorkerError("sandbox worker message contains duplicate keys")
        result[key] = value
    return result


def _remaining_time(deadline: Optional[float]) -> Optional[float]:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise JsonWorkerError("sandbox worker timed out")
    return remaining


def _wait_for_fd(fd: int, readable: bool, deadline: Optional[float]) -> None:
    if deadline is None:
        return
    read_fds = [fd] if readable else []
    write_fds = [] if readable else [fd]
    ready = select.select(read_fds, write_fds, [], _remaining_time(deadline))
    if not ready[0] and not ready[1]:
        raise JsonWorkerError("sandbox worker timed out")


def _write_all(fd: int, data: bytes, deadline: Optional[float] = None) -> None:
    while data:
        _wait_for_fd(fd, readable=False, deadline=deadline)
        try:
            written = os.write(fd, data)
        except BlockingIOError:
            continue
        if written == 0:
            raise JsonWorkerError("sandbox worker pipe closed during write")
        data = data[written:]


def _read_exact(fd: int, size: int, deadline: Optional[float] = None) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        _wait_for_fd(fd, readable=True, deadline=deadline)
        try:
            chunk = os.read(fd, min(remaining, 64 * 1024))
        except BlockingIOError:
            continue
        if not chunk:
            raise JsonWorkerError("sandbox worker message is truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_message(fd: int, message: Any, deadline: Optional[float] = None) -> None:
    try:
        payload = json.dumps(message, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise JsonWorkerError("sandbox worker message is not JSON compatible") from error
    if len(payload) > MAX_MESSAGE_BYTES:
        raise JsonWorkerError("sandbox worker message exceeds the byte limit")
    _write_all(fd, struct.pack(">Q", len(payload)), deadline)
    _write_all(fd, payload, deadline)


def _read_message(fd: int, deadline: Optional[float] = None) -> Any:
    size = struct.unpack(">Q", _read_exact(fd, _MESSAGE_PREFIX_BYTES, deadline))[0]
    if size > MAX_MESSAGE_BYTES:
        raise JsonWorkerError("sandbox worker message exceeds the byte limit")
    try:
        return json.loads(
            _read_exact(fd, size, deadline).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise JsonWorkerError("sandbox worker message is not valid JSON") from error


def _write_stream_message(fd: int, message: Any, deadline: Optional[float] = None) -> None:
    """Write one JSON value as a sequence of bounded frames followed by an empty frame."""
    encoder = json.JSONEncoder(allow_nan=False, separators=(",", ":"))
    buffered = bytearray()
    try:
        for text in encoder.iterencode(message):
            buffered.extend(text.encode("utf-8"))
            while len(buffered) >= _STREAM_FRAME_BYTES:
                frame = bytes(buffered[:_STREAM_FRAME_BYTES])
                del buffered[:_STREAM_FRAME_BYTES]
                _write_all(fd, struct.pack(">Q", len(frame)), deadline)
                _write_all(fd, frame, deadline)
    except (TypeError, ValueError) as error:
        raise JsonWorkerError("sandbox worker message is not JSON compatible") from error

    if buffered:
        _write_all(fd, struct.pack(">Q", len(buffered)), deadline)
        _write_all(fd, bytes(buffered), deadline)
    _write_all(fd, struct.pack(">Q", 0), deadline)


def _read_stream_message(
    fd: int,
    deadline: Optional[float] = None,
    *,
    spool_to_disk: bool = False,
    max_total_bytes: Optional[int] = None,
) -> Any:
    """Read bounded JSON frames, optionally limiting total transport resources."""
    stream = (
        tempfile.SpooledTemporaryFile(max_size=MAX_MESSAGE_BYTES)
        if spool_to_disk
        else io.BytesIO()
    )
    total = 0
    try:
        while True:
            size = struct.unpack(">Q", _read_exact(fd, _MESSAGE_PREFIX_BYTES, deadline))[0]
            if size == 0:
                break
            if size > _STREAM_FRAME_BYTES:
                raise JsonWorkerError("sandbox worker frame exceeds the byte limit")
            total += size
            if max_total_bytes is not None and total > max_total_bytes:
                raise JsonWorkerError("sandbox worker response exceeds the resource limit")
            stream.write(_read_exact(fd, size, deadline))

        stream.seek(0)
        return json.load(stream, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise JsonWorkerError("sandbox worker message is not valid JSON") from error
    finally:
        stream.close()


def _redirect_standard_streams() -> None:
    input_fd = os.open(os.devnull, os.O_RDONLY)
    output_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(input_fd, 0)
        os.dup2(output_fd, 1)
        os.dup2(output_fd, 2)
    finally:
        for fd in (input_fd, output_fd):
            if fd > 2:
                os.close(fd)


def _close_inherited_fds(keep: Set[int]) -> None:
    for fd_directory in ("/proc/self/fd", "/dev/fd"):
        try:
            names = os.listdir(fd_directory)
        except OSError:
            continue
        for name in names:
            try:
                fd = int(name)
            except ValueError:
                continue
            if fd not in keep:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return
    raise JsonWorkerError("sandbox worker cannot enumerate inherited descriptors")


def _set_nonblocking(fd: int) -> None:
    import fcntl

    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


def _terminate_worker(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _run_child(
    request_fd: int,
    response_fd: int,
    worker: Callable[[Any], Any],
    setup: Optional[Callable[[], None]],
    keep_fds: Optional[Set[int]] = None,
) -> None:
    diagnostics = _BoundedTextBuffer(_MAX_FAILURE_DIAGNOSTIC_BYTES)
    try:
        os.setsid()
        _redirect_standard_streams()
        _close_inherited_fds({0, 1, 2, request_fd, response_fd} | (keep_fds or set()))
        with contextlib.redirect_stdout(diagnostics), contextlib.redirect_stderr(diagnostics):
            if setup:
                setup()
            result = worker(_read_message(request_fd))
        _write_message(response_fd, {"ok": True, "result": result})
    except BaseException as error:
        try:
            _write_message(
                response_fd,
                {
                    "error": _bounded_text(f"{type(error).__name__}: {error}"),
                    "ok": False,
                    "output": _bounded_text(diagnostics.getvalue()),
                    "traceback": _bounded_text(traceback.format_exc()),
                },
            )
        except BaseException:
            pass
        os._exit(1)
    finally:
        os.close(request_fd)
        os.close(response_fd)
    os._exit(0)


def _run_stream_child(
    request_fd: int,
    response_fd: int,
    worker: Callable[[Any], Any],
    setup: Optional[Callable[[], None]],
) -> None:
    """Run a child using scalable framed JSON transport."""
    diagnostics = _BoundedTextBuffer(_MAX_FAILURE_DIAGNOSTIC_BYTES)
    try:
        os.setsid()
        _redirect_standard_streams()
        _close_inherited_fds({0, 1, 2, request_fd, response_fd})
        with contextlib.redirect_stdout(diagnostics), contextlib.redirect_stderr(diagnostics):
            if setup:
                setup()
            result = worker(_read_stream_message(request_fd))
        _write_stream_message(response_fd, {"ok": True, "result": result})
    except BaseException as error:
        try:
            _write_stream_message(
                response_fd,
                {
                    "error": _bounded_text(f"{type(error).__name__}: {error}"),
                    "ok": False,
                    "output": _bounded_text(diagnostics.getvalue()),
                    "traceback": _bounded_text(traceback.format_exc()),
                },
            )
        except BaseException:
            pass
        os._exit(1)
    finally:
        os.close(request_fd)
        os.close(response_fd)
    os._exit(0)


class _StreamingJsonWorker:
    """Parent-owned handle for one scalable JSON worker."""

    def __init__(
        self,
        pid: int,
        response_fd: int,
        deadline: Optional[float],
        max_response_bytes: Optional[int],
    ) -> None:
        self.pid = pid
        self.response_fd = response_fd
        self.deadline = deadline
        self.max_response_bytes = max_response_bytes
        self.reaped = False

    def terminate(self) -> None:
        """Terminate and reap this worker unless it was already collected."""
        if self.reaped:
            return
        _terminate_worker(self.pid)
        os.close(self.response_fd)
        os.waitpid(self.pid, 0)
        self.reaped = True

    def collect(self) -> Any:
        """Read, validate, and reap this worker's response."""
        response = None
        status = None
        completed = False
        try:
            response = _read_stream_message(
                self.response_fd,
                self.deadline,
                spool_to_disk=True,
                max_total_bytes=self.max_response_bytes,
            )
            completed = True
        finally:
            if not completed:
                _terminate_worker(self.pid)
            os.close(self.response_fd)
            _, status = os.waitpid(self.pid, 0)
            self.reaped = True

        if not isinstance(response, dict):
            raise JsonWorkerError("sandbox worker failed")
        if set(response) == {"ok", "result"} and response["ok"] is True and status == 0:
            return response["result"]
        if set(response) == {"error", "ok", "output", "traceback"} and response["ok"] is False:
            if all(isinstance(response[key], str) for key in ("error", "output", "traceback")):
                message = "sandbox worker failed: {}\n{}{}".format(
                    response["error"], response["output"], response["traceback"]
                )
                raise JsonWorkerError(message)
        raise JsonWorkerError("sandbox worker failed")


def _launch_json_worker_streaming(
    request: Any,
    worker: Callable[[Any], Any],
    setup: Optional[Callable[[], None]],
    timeout: Optional[float],
    max_response_bytes: Optional[int],
) -> _StreamingJsonWorker:
    """Launch one scalable JSON worker and return its parent-owned handle."""
    child_request_fd, request_fd = os.pipe()
    response_fd, child_response_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(request_fd)
        os.close(response_fd)
        _run_stream_child(child_request_fd, child_response_fd, worker, setup)

    os.close(child_request_fd)
    os.close(child_response_fd)
    deadline = time.monotonic() + timeout if timeout is not None else None
    handle = _StreamingJsonWorker(pid, response_fd, deadline, max_response_bytes)
    try:
        _set_nonblocking(request_fd)
        try:
            _write_stream_message(request_fd, request, deadline)
        finally:
            os.close(request_fd)
    except BaseException:
        handle.terminate()
        raise
    return handle


def run_json_worker(
    request: Any,
    worker: Callable[[Any], Any],
    setup: Optional[Callable[[], None]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """Run ``worker`` in a POSIX child with bounded JSON-only transport."""
    if not hasattr(os, "fork"):
        raise JsonWorkerError("sandbox worker launcher is unsupported on this platform")
    if timeout <= 0:
        raise ValueError("sandbox worker timeout must be positive")

    child_request_fd, request_fd = os.pipe()
    response_fd, child_response_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(request_fd)
        os.close(response_fd)
        _run_child(child_request_fd, child_response_fd, worker, setup)

    os.close(child_request_fd)
    os.close(child_response_fd)
    response = None
    status = None
    completed = False
    deadline = time.monotonic() + timeout
    try:
        _set_nonblocking(request_fd)
        _set_nonblocking(response_fd)
        try:
            _write_message(request_fd, request, deadline)
        finally:
            os.close(request_fd)

        response = _read_message(response_fd, deadline)
        completed = True
    finally:
        if not completed:
            _terminate_worker(pid)
        os.close(response_fd)
        _, status = os.waitpid(pid, 0)

    if not isinstance(response, dict):
        raise JsonWorkerError("sandbox worker failed")
    if set(response) == {"ok", "result"} and response["ok"] is True and status == 0:
        return response["result"]
    if set(response) == {"error", "ok", "output", "traceback"} and response["ok"] is False:
        if all(isinstance(response[key], str) for key in ("error", "output", "traceback")):
            message = "sandbox worker failed: {}\n{}{}".format(
                response["error"], response["output"], response["traceback"]
            )
            raise JsonWorkerError(message)
    raise JsonWorkerError("sandbox worker failed")


def run_json_worker_streaming(
    request: Any,
    worker: Callable[[Any], Any],
    setup: Optional[Callable[[], None]] = None,
    timeout: Optional[float] = None,
    max_response_bytes: Optional[int] = DEFAULT_STREAM_RESPONSE_BYTES,
) -> Any:
    """Run a POSIX child using scalable JSON split across bounded transport frames.

    ``timeout`` is an optional resource policy. ``max_response_bytes`` has a large finite default
    so an untrusted worker cannot exhaust parent storage; callers may raise it or explicitly pass
    ``None`` when another host-enforced quota provides the total resource bound.
    """
    if not hasattr(os, "fork"):
        raise JsonWorkerError("sandbox worker launcher is unsupported on this platform")
    if timeout is not None and timeout <= 0:
        raise ValueError("sandbox worker timeout must be positive")
    if max_response_bytes is not None and max_response_bytes <= 0:
        raise ValueError("sandbox worker response resource limit must be positive")

    return _launch_json_worker_streaming(
        request, worker, setup, timeout, max_response_bytes
    ).collect()


def map_json_workers_streaming(
    requests: Iterable[Any],
    worker: Callable[[Any], Any],
    *,
    processes: int,
    setup: Optional[Callable[[], None]] = None,
    timeout: Optional[float] = None,
    max_response_bytes: Optional[int] = DEFAULT_STREAM_RESPONSE_BYTES,
) -> Generator[Tuple[int, Any], None, None]:
    """Run bounded scalable JSON workers and yield responses as workers finish."""
    if not hasattr(os, "fork"):
        raise JsonWorkerError("sandbox worker launcher is unsupported on this platform")
    if processes <= 0:
        raise ValueError("sandbox worker process count must be positive")
    if timeout is not None and timeout <= 0:
        raise ValueError("sandbox worker timeout must be positive")
    if max_response_bytes is not None and max_response_bytes <= 0:
        raise ValueError("sandbox worker response resource limit must be positive")

    indexed_requests = iter(enumerate(requests))
    active: Dict[int, Tuple[int, _StreamingJsonWorker]] = {}

    def launch_next() -> bool:
        try:
            index, request = next(indexed_requests)
        except StopIteration:
            return False
        handle = _launch_json_worker_streaming(request, worker, setup, timeout, max_response_bytes)
        active[handle.response_fd] = (index, handle)
        return True

    try:
        for _ in range(processes):
            if not launch_next():
                break

        while active:
            deadlines = [handle.deadline for _, handle in active.values() if handle.deadline]
            wait = _remaining_time(min(deadlines)) if deadlines else None
            ready, _, _ = select.select(list(active), [], [], wait)
            if not ready:
                raise JsonWorkerError("sandbox worker timed out")
            for response_fd in ready:
                index, handle = active.pop(response_fd)
                response = handle.collect()
                launch_next()
                yield index, response
    finally:
        for _, handle in active.values():
            handle.terminate()


def _send_listener_fd_number(control: socket.socket, fd: int) -> None:
    control.sendall(str(fd).encode("ascii"))
    if control.recv(1) != b"1":
        raise JsonWorkerError("network worker listener was not acknowledged")


def _receive_listener_fd_number(control: socket.socket, timeout: float) -> int:
    control.settimeout(timeout)
    message = control.recv(32)
    try:
        listener_fd = int(message.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        diagnostic = message.decode("utf-8", errors="replace")
        raise JsonWorkerError("network worker setup failed: {0}".format(diagnostic))
    if listener_fd < 0:
        raise JsonWorkerError("network worker returned an invalid listener descriptor")
    return listener_fd


def _configure_proxy_environment(proxy_url: str) -> None:
    """Replace inherited proxy routing with the invocation's local proxy."""
    proxy_names = ("http_proxy", "https_proxy", "ftp_proxy", "all_proxy", "no_proxy")
    for name in proxy_names:
        os.environ.pop(name, None)
        os.environ.pop(name.upper(), None)
    for name in ("http_proxy", "https_proxy", "ftp_proxy"):
        os.environ[name] = proxy_url
        os.environ[name.upper()] = proxy_url


def run_json_worker_with_network(
    request: Any,
    worker: Callable[[Any], Any],
    proxy_policy,
    setup: Optional[Callable[[], None]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    """Run a JSON worker whose TCP sockets are connected only to a local proxy."""
    if not hasattr(os, "fork"):
        raise JsonWorkerError("network worker launcher is unsupported on this platform")
    if timeout <= 0:
        raise ValueError("sandbox worker timeout must be positive")

    import spack.sandbox
    from spack.util.proxy import ConnectSupervisor, LocalHTTPProxy

    local_proxy = LocalHTTPProxy(
        proxy_policy, credential=secrets.token_urlsafe(32), timeout=timeout
    )
    local_proxy.bind()
    proxy_url = local_proxy.authenticated_url
    proxy_address = local_proxy._server.server_address[:2]

    child_request_fd, request_fd = os.pipe()
    response_fd, child_response_fd = os.pipe()
    control_parent, control_child = socket.socketpair()
    pid = os.fork()
    if pid == 0:
        os.close(request_fd)
        os.close(response_fd)
        control_parent.close()

        def network_setup() -> None:
            try:
                seccomp = spack.sandbox.SeccompSandbox()
                listener_fd = seccomp.network_listener()
                try:
                    _send_listener_fd_number(control_child, listener_fd)
                finally:
                    os.close(listener_fd)
                seccomp.deny_network_bypass()
                control_child.close()
                _configure_proxy_environment(proxy_url)
                if setup:
                    setup()
            except BaseException as error:
                with contextlib.suppress(OSError):
                    control_child.send(repr(error).encode("utf-8")[:1024])
                raise

        _run_child(
            child_request_fd,
            child_response_fd,
            worker,
            network_setup,
            keep_fds={control_child.fileno()},
        )

    os.close(child_request_fd)
    os.close(child_response_fd)
    control_child.close()
    listener_fd = -1
    pidfd = -1
    stop_supervisor = threading.Event()
    supervisor_errors = []
    supervisor_thread = None
    response = None
    status = None
    completed = False
    deadline = time.monotonic() + timeout
    try:
        local_proxy.start()
        pidfd = spack.sandbox.pidfd_open(pid)
        child_listener_fd = _receive_listener_fd_number(control_parent, _remaining_time(deadline))
        listener_fd = spack.sandbox.pidfd_getfd(pidfd, child_listener_fd)
        control_parent.sendall(b"1")
        supervisor = ConnectSupervisor(listener_fd, pid, pidfd, proxy_address, timeout=timeout)

        def supervise() -> None:
            try:
                supervisor.serve(stop_supervisor)
            except BaseException as error:
                supervisor_errors.append(error)
                _terminate_worker(pid)

        supervisor_thread = threading.Thread(target=supervise)
        supervisor_thread.daemon = True
        supervisor_thread.start()

        _set_nonblocking(request_fd)
        _set_nonblocking(response_fd)
        try:
            _write_message(request_fd, request, deadline)
        finally:
            os.close(request_fd)
            request_fd = -1
        response = _read_message(response_fd, deadline)
        completed = True
    finally:
        if not completed:
            _terminate_worker(pid)
        if request_fd >= 0:
            os.close(request_fd)
        os.close(response_fd)
        control_parent.close()
        _, status = os.waitpid(pid, 0)
        stop_supervisor.set()
        if supervisor_thread is not None:
            supervisor_thread.join(timeout=1)
        if listener_fd >= 0:
            os.close(listener_fd)
        if pidfd >= 0:
            os.close(pidfd)
        local_proxy.stop()

    if supervisor_errors:
        raise JsonWorkerError("network supervisor failed: {0}".format(supervisor_errors[0]))
    if not isinstance(response, dict):
        raise JsonWorkerError("sandbox worker failed")
    if set(response) == {"ok", "result"} and response["ok"] is True and status == 0:
        return response["result"]
    if set(response) == {"error", "ok", "output", "traceback"} and response["ok"] is False:
        if all(isinstance(response[key], str) for key in ("error", "output", "traceback")):
            message = "sandbox worker failed: {}\n{}{}".format(
                response["error"], response["output"], response["traceback"]
            )
            raise JsonWorkerError(message)
    raise JsonWorkerError("sandbox worker failed")
