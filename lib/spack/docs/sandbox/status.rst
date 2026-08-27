..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Current Status
==============

``spack info`` is the first hardened normal command.

Implemented concretizer-worker contract foundation
---------------------------------------------------

The concretizer worker is not integrated into a solve path and does not yet apply confinement.
Its versioned launcher-neutral contract and scalable process transport are implemented.

The request carries abstract native specs, test-dependency selection, deprecated-version policy, and solve strategy.
The response carries ordered final concrete native specs, DAG hashes, and bounded warnings.
Request creation, response restoration, and structural validation do not import package recipes.
Virtual-provider and other recipe-dependent satisfaction checks remain worker-owned.

The scalable transport splits JSON into bounded frames and rejects oversized frame declarations before reading their payloads.
It supports requests and responses larger than the existing command worker's four-MiB message limit, bounds every failure-diagnostic field, hides raw worker output, closes inherited descriptors, and reaps failed workers.
It adds no default timeout or total response-size policy; callers may supply explicit resource limits.

Focused tests cover malformed and stale requests and responses, ordered root association, DAG-hash validation, abstract, concrete, and spliced native-spec round trips, duplicate JSON keys, large payloads, optional timeout and response-resource policies, setup ordering, diagnostics, and legacy worker compatibility.

Implemented ``spack info`` boundaries
-------------------------------------

The child renders normal output and returns it through a bounded JSON pipe.
The command interface is unchanged.

The child:

* redirects standard streams and closes inherited descriptors;
* applies rlimits and Landlock before recipe import;
* reads only parent-selected repositories, import roots, and Spack source; and
* denies writes.

Landlock's network option uses libseccomp to deny socket operations, process creation and execution, and IPC.
``PR_SET_NO_NEW_PRIVS`` is always set.
Seccomp isolates TCP, UDP, and ICMP without Landlock ABI 4 network controls.

A 120-second deadline kills the child process group after timeout or protocol failure.

Focused tests cover output compatibility and worker failures.

Implemented ``spack stage`` and installer staging boundary
-----------------------------------------------------------

Supported hosts stage source in a confined worker through the existing ``Stage`` and fetcher abstractions.
The worker uses the invocation proxy for network access and may write only its selected stage, fetch cache, and any exact stage-lock file it acquires.
It can execute only individually selected archive-expansion tools.

The existing installer reuses this worker at its source-staging boundary.
It retains scheduling, jobserver limits, state and log channels, terminal UI, hooks, builder phases, database actions, and binary-cache behavior.
The installer child owns the stage context and lock, so its worker applies the requested patch behavior without reacquiring the lock.
Installer stage and build workers currently impose no Spack memory ceiling, while retaining core-dump
suppression and any limits inherited from the invoking process or service.

Focused tests cover proxy-mediated fetch and expansion, patching under the parent-held stage lock, direct network and filesystem denial, source installation, cache-only behavior, scheduler ordering, cancellation, state handling, and database updates.

Implemented ``spack checksum`` boundary
----------------------------------------

On systems supporting seccomp user notification and pidfds, discovery runs in a confined worker for explicit versions, ``--preferred``, ``--latest``, omitted versions, and remote fallback.

The boundary provides:

* one local proxy destination for every worker TCP socket;
* public HTTP, HTTPS, and FTP source access for checksum;
* bounded response validation and parent-side interactive selection;
* a separate ``Stage`` fetch worker returning version/SHA256 pairs;
* process and IPC denial, read-only recipe files, and stage-root write access;
   and
* a random invocation credential checked before request parsing or DNS.

Unsupported systems retain existing compatibility paths only when ``config:sandbox:allow_fallback`` is enabled.
Failures after worker selection fail closed.

Implemented network-proxy foundation
------------------------------------

The invocation-scoped proxy provides:

* absolute-form HTTP forwarding;
* HTTPS ``CONNECT`` tunneling without TLS termination;
* an HTTP-to-passive-FTP gateway;
* canonical scheme, IDNA-hostname, and port policy before DNS;
* proxy-side DNS and outbound connections;
* hop-by-hop header stripping and trusted ``Host`` replacement; and
* passive-FTP data-peer pinning against PASV bounce behavior.

Focused tests cover policy normalization, HTTP forwarding, denied requests, ``CONNECT`` relay, passive FTP, forged PASV addresses, authentication, and worker proxy-environment injection.

Seccomp user notifications mediate worker ``socket`` and ``connect`` calls.
The supervisor validates thread-group identity through the worker pidfd.
It creates only IPv4 TCP stream sockets and redirects them to the local proxy.
It denies bypass operations after listener transfer.
Resolved addresses must be globally routable.

Live checksum smoke tests completed through the passive-FTP proxy against:

* ``ftp.alsa-project.org``; and
* ``ftp.gnu.org``.
