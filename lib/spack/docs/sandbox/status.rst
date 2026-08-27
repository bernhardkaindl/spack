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

The request carries abstract native specs, test-dependency selection, deprecated-version policy, solve strategy, and frozen local-store and build-cache reuse metadata.
The response carries ordered final concrete native specs, DAG hashes, and bounded warnings.
Request creation, response restoration, and structural validation do not import package recipes.
Virtual-provider and other recipe-dependent satisfaction checks remain worker-owned.

The scalable transport splits JSON into bounded frames and rejects oversized frame declarations before reading their payloads.
It supports requests and responses larger than the existing command worker's four-MiB message limit, bounds every failure-diagnostic field, hides raw worker output, closes inherited descriptors, and reaps failed workers.
It adds no default timeout.
The total response ceiling defaults to one GiB and can be raised with ``config:sandbox:concretizer:max_response_bytes``.

Focused tests cover malformed and stale requests and responses, ordered root association, DAG-hash validation, abstract, concrete, and spliced native-spec round trips, duplicate JSON keys, large payloads, optional timeout and response-resource policies, setup ordering, diagnostics, and legacy worker compatibility.

The launcher-neutral one-shot path now runs the existing ``Solver.solve()`` in a confined forked worker and restores final native specs in the parent.
After inherited descriptors are closed, its setup hook discards stale lock bookkeeping and recreates the store and binary-cache index with fresh lock objects.
An allowlisted error protocol preserves catchable Spack, configuration, spec, unknown-package, and unsatisfiable-spec categories; unexpected internal failures retain the transport failure path.

Trusted parent preflight ensures Clingo is importable and imports only configured or installed
compiler candidate recipes to populate compiler properties.
Local-store specs and install metadata are frozen before every worker solve; build-cache candidates
are refreshed and frozen only when enabled by reuse policy.
The worker applies the existing reuse filters without store access or network refresh.

Landlock allows reads from active repositories, Spack and Python runtime paths, and loaded
configuration.
Writes are limited to parent-selected persistent misc-cache and concretization-cache roots.
Current cache readers parse structured data; cache content remains untrusted and subject to parser and native-spec validation.
Seccomp denies sockets, ``fork``, ``vfork``, executable replacement, and blocked IPC.
Legacy ``clone`` is allowed only with ``CLONE_THREAD``; ``clone3`` returns ``ENOSYS`` so libc uses the inspectable legacy form for Clingo threads.
No solver-specific memory ceiling is imposed.

Focused parity covers versions, variants, dependency DAGs, virtual roots, externals, installed-spec reuse, automatic splicing, compilers, platforms, test dependencies, ordered warnings, cache-enabled and cache-disabled operation, invalid inputs, and inherited solver timeout configuration.
Transport lifecycle coverage includes explicit worker deadlines, parent interruption, truncated frames, child crashes, inherited-descriptor cleanup, and child reaping.
Real kernel-boundary tests prove that selected recipe evaluation begins after confinement and cannot read or write unrelated paths, create TCP or Unix sockets, or execute a program.
They also prove that child-process creation is denied while a normal asynchronous Clingo solve succeeds.
``spack.concretize.concretize_one()`` and unified together solves now select the confined worker automatically when supported.
They use the existing direct solver only when confinement is unavailable and the shared ``config:sandbox:allow_fallback`` policy permits fallback.
This covers explicit ``spack spec`` inputs and normal callers that use those shared operations.
When-possible, separate, and broad environment/install integration remain incomplete.

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
