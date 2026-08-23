..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Network Supervisor Contract
===========================

This page defines the network boundary for download-capable build workers.
It is separate from recipe-import confinement: recipe-import workers remain network-denied.

Implementation status
---------------------

The implemented network supervisor provides:

* an invocation-scoped HTTP forward proxy and HTTPS ``CONNECT`` tunnel;
* an immutable scheme, IDNA-hostname, and port authority policy;
* proxy-side DNS and outbound HTTP connections;
* an HTTP-to-passive-FTP gateway that pins data connections to the control peer;
* seccomp user-notification mediation for worker IPv4 TCP ``socket`` and
   ``connect`` calls;
* pidfd duplication of worker notification and socket descriptors, with worker
   identity validation;
* a random per-invocation HTTP Basic proxy credential in sanitized proxy
   environment URLs;
* denial of listener, descriptor-passing, and socket-creation bypasses;
* resolved-address rejection for non-global destinations; and
* sequential ``spack checksum`` remote discovery and ``Stage`` archive fetch
   workers.

Verification performed
----------------------

Focused real-kernel testing starts a confined worker.
The worker attempts to connect to ``127.0.0.1:9``.
That endpoint is unrelated to the invocation proxy.
The supervisor redirects the worker socket to the invocation-local proxy.
The proxy rejects the original loopback authority.

An end-to-end ``spack checksum -l libdrm`` run verifies HTTPS discovery and archive fetching through the proxy.

The boundary remains incomplete for general download workflows.
See :doc:`roadmap` for installation work.
See :doc:`design-decisions` for policy questions.

Trust boundary
--------------

A less-trusted download worker never owns a socket capable of directly reaching the network.
Its only TCP destination is a trusted proxy listening on a private local endpoint.
The proxy is outside worker confinement.
It is the only component that owns network-capable sockets.

The trusted supervisor injects ``HTTP_PROXY`` and ``HTTPS_PROXY``.
Supported fetchers send destination names to the proxy.
They do not resolve them in the worker.

The supervisor:

* sets lowercase aliases required by selected fetchers;
* removes inherited proxy settings and ``NO_PROXY`` bypasses; and
* installs only supervisor-selected proxy URLs.

Each URL includes a random credential from the trusted launcher.
The proxy requires matching ``Proxy-Authorization`` before request parsing or destination resolution.
The credential excludes unrelated local clients.
It cannot exclude the confined worker because the worker receives it.
Proxy URLs never contain recipe-controlled credentials or options.

The resulting data path is::

   confined worker
      |
      | HTTP forwarding or CONNECT to the injected proxy URL
      | seccomp-notified connect completed only to the local proxy
      v
   trusted local proxy
      |
      | canonical name and port policy, denial logging, then DNS
      v
   proxy-owned outbound TCP connection
      |
      v
   permitted Internet destination

The worker treats destination names, URLs, and redirects as proxy-protocol data.
The proxy performs DNS and owns the outbound connection.
The worker receives neither a DNS capability nor a direct Internet address.

Destination policy
------------------

The trusted parent supplies a policy for each invocation.
The current policy grants canonical scheme, host, and port.
URL paths do not participate in authorization.
Denied authorities are rejected before DNS.

The checksum integration permits arbitrary public source authorities.
This mode centralizes DNS and outbound sockets in the proxy.
It is not a destination whitelist.
It remains separate from the no-network recipe URL-evaluation worker.

Future install and resource-download integration may use an administrator URL whitelist or configured source mirrors.
Path prefixes, configuration precedence, and mirror interactions need separate design.
See :doc:`design-decisions`.

Proxy protocol
--------------

The proxy implements HTTP forwarding and ``CONNECT``.

For HTTP, it:

* parses the absolute-form target;
* validates the canonical authority;
* replaces the untrusted ``Host`` header;
* resolves the name; and
* forwards the request.

The proxy does not follow redirects itself.
The worker's client follows them as normal HTTP behavior.
For current checksum policy, an authorized source may redirect without separate redirect authorization.
The proxy still authenticates the new request and performs DNS.
It rejects non-global resolved addresses.
Future restricted-policy redirect semantics are an open design decision.

For HTTPS and other supported tunnels, the proxy parses the ``CONNECT`` authority.
It validates canonical host and port, resolves the host, and connects before reporting success.
It relays bytes without terminating TLS.
HTTPS remains end-to-end between the worker client and destination.
IP literals, alternate spellings, and DNS results cannot bypass name and port policy.

The proxy supports passive FTP for the initial fetch path.
It validates and resolves the URL destination before opening a control connection.
It uses ``EPSV`` or ``PASV`` and opens the data connection itself.

Passive responses cannot select arbitrary addresses or ports.
A distinct data destination requires canonicalization and policy checks.
Active FTP is unsupported and fails closed.

Denied destinations and malformed requests receive bounded protocol errors.
Logs include canonical host, port, protocol, and denial reason.
Logs exclude credentials, URL user information, query strings, and secrets.
DNS failures, disallowed addresses, connection failures, and protocol timeouts fail closed.

Worker-to-proxy connection
--------------------------

The worker filter returns ``SECCOMP_RET_USER_NOTIF`` for ``connect``.
The launcher gives the trusted supervisor the listener before worker network requests begin.
For each valid notification, the supervisor connects the worker socket to the configured local proxy endpoint.
Worker-controlled memory never selects an Internet destination.

The supervisor duplicates the worker socket with ``pidfd_getfd``.
The duplicate refers to the same underlying socket.
The supervisor connects it to the local proxy and returns ``0``.
The original descriptor is connected without executing the original syscall.

The supervisor maps validation, descriptor access, and local connection failures to an appropriate errno.
It revalidates notifications before acting on worker state.
It closes duplicate descriptors after transfer or failure.

Required invariant
------------------

Every successful worker TCP connection terminates at the configured local proxy.
Every Internet connection originates in that proxy.
No worker ``connect`` continues.
No worker-controlled address can select another local service.
The proxy authorizes canonical name and port before DNS.
It checks the resolved address before connecting.

The filter denies or mediates other potential connection paths.
These include:

* DNS transports and UDP;
* Unix sockets and ``socketpair``;
* ``accept``, ``bind``, and ``sendto``; and
* multiplexed socket syscalls.

The initial capability supports only the syscall and address-family set needed for the proxy.
Unsupported cases fail closed.

Lifecycle and compatibility
---------------------------

The trusted parent launches the supervisor and proxy.
It owns their listener descriptors for the worker lifetime.
The supervisor injects the sanitized proxy environment before confinement.
The proxy authenticates each request to exclude unrelated local clients.

The supervisor:

* bounds concurrent notifications;
* validates process identity before using a notification; and
* fails the worker when the listener, worker, proxy, or protocol is invalid.

Exit and timeout cleanup closes notification, proxy, duplicate, and transferred descriptors.
It terminates invocation-scoped proxy work.

The capability is Linux-specific.
It requires kernel and libseccomp support for user notification.
It also requires the selected descriptor-transfer mechanism.
When unavailable, a command retains its trusted-parent download path or fails under documented fallback policy.
It never launches a worker with unrestricted ``connect``.

The proxy protocol is an intentional compatibility boundary.
Fetchers cannot use this worker without tested integration when they:

* ignore proxy variables;
* perform local DNS;
* require active FTP; or
* need unsupported transport.

Download integration
--------------------

The capability is for archive and resource downloads, not recipe parsing.
Workers continue to use existing ``Stage`` and fetcher abstractions.
This preserves mirrors, checksums, staging, progress, cancellation, and ``--keep-stage`` behavior.

Recipe evaluation remains a separate no-network worker boundary.
``spack checksum`` uses this capability only for download and remote discovery.
It never gives recipe URL evaluation network permission.
Its checksum policy need not impose the future install whitelist.
All DNS and outbound connections still occur in the proxy.

Validation
----------

Linux supervisor tests prove:

* worker sockets reach only the configured proxy;
* workers observe the expected syscall result; and
* stale notifications, invalid descriptors, failed transfer, failed local
   connections, and other local endpoints never continue the syscall.

Proxy tests cover:

* HTTP forwarding and ``CONNECT``;
* passive FTP and canonical authority policy;
* DNS, redirects, and rebinding-resistant address checks; and
* logging, malformed input, and timeouts.

They prove that denied names are not resolved.
They prove that disallowed addresses are not connected.
They prove that PASV cannot select unauthorized endpoints.

Integration tests cover:

* selected ``Stage`` fetchers;
* uppercase and lowercase proxy variables;
* inherited bypass removal;
* cancellation, timeout, and descriptor cleanup; and
* unavailable-kernel fallback.

They also prove that DNS, direct TCP, UDP, Unix sockets, ``socketpair``, active FTP, and unsupported operations cannot bypass policy.
