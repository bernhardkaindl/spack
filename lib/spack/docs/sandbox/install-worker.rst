..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Install Worker Plan
===================

This is the implementation plan for a confined worker option of the existing installer.
It does not propose another install command, scheduler, UI, source plan, provenance model, or fetch implementation.

The trusted command parent keeps command parsing, concretization, install scheduling, locks, build-cache policy, database writes, terminal UI, and final presentation.
The worker uses existing staging and build operations under confinement.
Source downloads use the existing network supervisor and proxy; the worker never receives direct network access.

Status and Scope
----------------

The capability-selection, bounded native-spec request, ``spack stage`` worker, and existing-installer staging integration are implemented.
Build-phase confinement remains a separate milestone.

Checklist notation is intentionally text-based for now:

* [x] design or planning work is complete;
* [ ] implementation or validation work remains.

Planning status:

* [x] Anchor the worker in the existing ``spack stage`` and installer paths.
* [x] Preserve the installer scheduler and terminal UI.
* [x] Reuse the network supervisor, proxy, ``Stage``, and package methods.
* [x] Capture applicable compiler, host-tool, path-policy, and test lessons from the abandoned branch.
* [x] Resolve the initial worker, fallback, patching, URL, import, and host-tool decisions.
* [x] Implement the shared sandbox-worker fallback policy and squash it into the existing info/checksum worker commit.
* [x] Establish the install-worker capability and bounded native-spec request contract.
* [x] Implement and validate the stage worker.
* [x] Integrate worker staging into the existing installer.
* [ ] Implement and validate build-phase confinement.

The first command integration is the staging worker for ``spack stage``.
``PackageBase.do_stage()`` already performs stage creation, fetch, checksum, cache-local handling, and archive expansion.
``PackageBase.do_patch()`` calls ``do_stage()`` before applying patches.
The worker must call these methods, rather than reimplement fetch, patch, mirror, checksum, or stage behavior.

The second milestone makes that capability available to the source-staging portion of the existing ``PackageInstaller`` child.
It retains ``BuildGraph`` scheduling, the jobserver, build child lifecycle, state pipe, database actions, cancellation, and ``TerminalUI``.
The user interface is a worker option for the existing installer, not a new command.

Build-phase confinement is a later, separate milestone.
Compiler selection and build-tool access are recorded now, but must not delay staging integration.

Trust Boundary
--------------

The trusted parent owns:

* existing command argument parsing and option/configuration resolution;
* selection of the concrete spec, repository, stage root, and worker mode;
* worker and proxy launch, cancellation, timeout, and cleanup;
* validated state forwarding to the existing installer UI; and
* database, prefix commit, binary-cache installation, and terminal output.

The confined worker owns only the selected existing staging operation.
It may write its selected stage, source cache, and exact shared stage-lock file.
It reads package repositories, runtime support, selected local source or mirror paths, and individually resolved archive-expansion tools.
HTTP, HTTPS, and passive-FTP requests must traverse the invocation proxy described in :doc:`network-supervisor`.
The worker uses the existing ``Stage`` fetcher, configured mirrors, checksums, cache-local behavior, and proxy environment.

Recipe-controlled URLs, patches, build input, and package code remain untrusted.
They must not select host filesystem access, direct sockets, response structure, or parent actions.

Architecture Constraints
------------------------

* Do not add ``spack install-worker`` or ``spack stage-worker`` commands.
* Do not add ``SourcePlan``, provenance transport, a parallel source model, or a parallel fetcher.
* Do not move scheduling or UI logic out of ``PackageInstaller``.
* Do not replace ``PackageBase.do_stage()``, ``PackageBase.do_patch()``, or the existing ``Stage`` fetch path.
* Do not allow a worker with unrestricted ``connect``.
  Worker use is automatic when supported.
  When support is unavailable, the trusted direct path is allowed only when ``config:sandbox:allow_fallback`` is ``true``; otherwise the command fails with capability diagnostics.
* Put new worker protocol, launch, policy, and capability code in dedicated install-worker modules with dedicated tests.
  Existing command and installer files receive only small integration adapters.

Proposed Dedicated Ownership
----------------------------

Names are provisional and establish ownership, not a frozen API:

* [x] ``spack.install_worker``: versioned bounded request and automatic capability-selection contracts.
* [x] ``spack.install_worker.policy``: automatic worker/fallback capability selection.
* [x] ``spack.install_worker.stage``: worker entry point invoking selected package staging methods.
* [x] ``spack.sandbox.restrict_stage_worker``: stage filesystem, process, IPC, and resource policy.
* [ ] ``spack.install_worker.policy``: later build capabilities, including selected compiler discovery.
* [x] ``spack.test.install_worker.test_contract``: request and capability-selection contract tests.
* [x] ``spack.test.install_worker.test_stage``: lifecycle, stage, and no-direct-network tests.
* [x] Existing installer and command suites: scheduler, state, UI, cache-policy, and source-install integration tests.

Worker policy does not belong in ``installer/build.py`` or ``installer/core.py``.
Those files should receive a minimal launcher call at the existing staging boundary.
``cmd/stage.py`` similarly selects the launcher around its existing ``_stage(pkg)`` operation.
Command test files contain only option and compatibility cases that cannot be covered by dedicated worker tests.

Vertical Delivery Plan
----------------------

1. Establish the install-worker capability contract
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Worker use is automatic for ``spack stage`` and the existing ``spack install`` when the required confinement and proxy capabilities are available.
There is no separate worker command or opt-in worker flag.

During implementation on the current development host, do not bypass a failed capability check to continue end-to-end work.
Stop and report the failing call and diagnostics so the worker is tested as an actual confined worker.

The parent constructs a small concrete-spec request.
It must not serialize an alternate source plan.
The worker restores only normal Spack state needed to obtain that selected package and invokes normal methods.
Responses are bounded structured status/error data; package output continues through existing paths.

Acceptance checks:

* [x] supported hosts select the worker automatically without a new command or flag;
* [x] the install-worker contract uses the shared fallback decision without changing its precedence;
* [x] ``allow_fallback: false`` includes the failed install-worker capability probe;
* [x] malformed, oversized, stale, or mismatched requests fail before package work.

2. Stage worker: fetch and expand
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Wrap the existing ``_stage(pkg)`` path from ``spack stage`` in the dedicated launcher.
Inside the worker, retain ``pkg.stage.keep = True`` and the stage context, then call ``pkg.do_stage()``.
Preserve the current command semantics: ``spack stage`` calls ``do_stage()`` and does not patch.
Installation calls ``do_patch()`` unless patching is disabled.

Grant stage-root write access, parent-selected package inputs, and proxy configuration.
Use ``run_json_worker_with_network`` or a narrow evolution of that mechanism.
Do not recreate proxy or fetch logic.

Normal staging may invoke only individually resolved ``tar``, ``unzip``, ``patch``, compression
fallback tools, and subordinate helpers such as GNU tar's ``gunzip`` and its shell interpreter.
Their loader/library paths are readable.  For a tool selected from the concrete DAG, only that
tool's link/run dependency closure is added; an unselected executable remains inaccessible.
Stage and build workers currently impose no installer memory ceiling because package builds can
legitimately require most of a large host.  They retain limits inherited from the invoking process or
service.  Future adaptive admission and throttling have a dedicated planning page in the sandbox
documentation.

Preserve:

* ``--path``, ``--exclude``, ``--skip-installed``, environment traversal, and ``--no-checksum``;
* mirrors, existing fetch options, checksums, cache-local handling, expansion, and stage retention; and
* cancellation, timeout, errors, and cleanup.

Acceptance checks:

* [x] archive fetch and expansion succeed through the proxy;
* [x] mirror and checksum failures match direct staging;
* [x] direct network, DNS, UDP, Unix sockets, and out-of-stage writes fail;
* [x] worker setup failures include bounded diagnostics naming the failed setup operation;
* [x] ``--path`` does not widen access to sibling directories; and
* [x] focused ``spack stage`` tests pass in worker and permitted fallback modes.

3. Reuse staging from the existing installer
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

At ``installer.build._install()``, replace only the ``pkg.do_patch()`` or ``pkg.do_stage()`` call with the launcher when worker mode is selected.
Keep ``BuildRequest``, ``start_build()``, process groups, tee/log channels, ``send_state()``, ``PackageInstaller._schedule_builds()``, and ``TerminalUI``.

The installer already owns the package stage context and lock when it invokes the worker.
Its worker request therefore selects ``do_patch()`` or ``do_stage()`` according to ``skip_patch`` but does not reacquire that lock.
The standalone ``spack stage`` path continues to acquire and retain its own stage.

The parent emits the existing ``staging`` state.
Validated worker status maps to the existing state stream; it does not create another progress display.
After staging, the normal installer child continues with normal hooks, builder phases, metadata, and prefix lifecycle.

Binary-cache installation and rewiring remain unchanged because they do not use source staging.

Acceptance checks:

* [x] multi-package installs retain dependency ordering, jobserver limits, locks, failure propagation, cancellation, and database behavior;
* [x] the current terminal overview shows normal staging and build states;
* [x] source installs use proxy-supervised staging; and
* [x] binary-cache, cache-only, and source-only paths retain current behavior.

4. Build-phase confinement and tool policy
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

After staging is integrated, apply dedicated build policy immediately before existing builder phases.
It receives the concrete spec and prepared stage and prefix; it does not change compiler selection or create a new build path.

Read access includes non-external dependency prefixes, the selected package directory, Spack runtime and sbang resources, selected loader files, and selected compiler tools.
Write access is limited to the stage, exact selected prefix, necessary temporary space, and explicit trusted configuration.
The parent derives capabilities from the concrete spec and trusted configuration.

Discover compiler drivers dynamically from concrete ``c``, ``cxx``, and ``fortran`` virtual edges.
Query each selected driver for subordinate programs and plugin files.
Do not grant compiler directories wholesale.
The Linux system-tool baseline is derived from real package builds and allows tools only as
individual resolved paths.  Its fixed host reads are ``/lib``, ``/lib64``, ``/usr/lib``,
``/usr/lib64``, dynamic-loader configuration, ``/proc/cpuinfo``, distribution and MIME metadata,
``/bin/sh``, and ``/usr/include`` when a selected compiler resolves below ``/usr``.
Every added tool or path requires a focused test demonstrating why it is needed.

An optional alpha learning mode may propose package-specific executable grants.
It is disabled by default and must name an already loaded configuration file as its policy target.
The trusted installer parent, never package code, updates that file through Spack's structured configuration API.
Learned entries use package-name selectors and individually resolved executable paths; they never grant an executable's parent directory.

Learning mode also routes build-phase TCP through an invocation-scoped proxy owned by the trusted
installer parent.  It permits public HTTP, HTTPS, and FTP destinations for discovery, warns
immediately for each new canonical destination, and prints a deduplicated package summary after the
build attempt.  The parent records each destination in a reusable ``network-allow-<host>`` group and
adds the package-name selector.  Outside learning mode, only matching network groups may use the
proxy; builds never receive direct socket access unless the explicit legacy ``allow_network`` option
is enabled.

Landlock does not report denied paths.
Learning therefore combines three signals before granting an executable:

* a seccomp-notification trace of an attempted ``execve`` or ``execveat`` outside the active Landlock read policy;
* validation by the trusted parent that the exact path exists and is executable outside confinement; and
* a matching ``Permission denied`` diagnostic in the failed build log.

Build-log text alone is untrusted and may only produce a non-actionable hint.
When all signals agree, learning records the executable in a generated ``learned-<package>`` whitelist and reschedules only that package through the existing installer scheduler.
Retries continue while each failed attempt identifies a new executable.
A repeated denial, an invalid path, no matching traced attempt, or a policy-write failure stops learning and preserves the normal build failure.

Acceptance checks:

* [ ] selected C, C++, and Fortran compilers work with reported front ends, assembler, linker, archive tools, and plugins;
* [ ] bare reported names resolve through both wrapper PATH and host default PATH;
* [ ] core utilities work only at their approved resolved paths;
* [ ] unselected compilers, arbitrary host binaries, sibling prefixes, and the install-prefix parent remain inaccessible; and
* [ ] logs, hooks, metadata archiving, and prefix commit retain current behavior.
* [ ] learning is disabled by default, validates traced executable paths in the trusted parent, and never grants from log text alone;
* [ ] learned package-name whitelists are written only to the configured loaded scope; and
* [ ] learning retries preserve locks, dependency ordering, failure propagation, and database behavior and stop on repeated denials.
* [ ] learning reports and persists canonical build download destinations through the trusted
  proxy, and learned groups remain enforceable after learning is disabled;

Concretization Dependency
-------------------------

Sandboxing ``spack spec`` recipe evaluation and, where practical, concretization remains an independent roadmap item.
It may be implemented before the staging worker, but it does not block this plan: the installer consumes a concrete spec regardless of where concretization ran.

The staging worker must import the selected package recipe after confinement is active.
It receives only the parent-selected concrete spec, repository state, and other minimal normal Spack state needed to invoke existing package methods.
It must not depend on a new concretization or source-plan protocol.

Build workers may also import dependency recipes lazily after confinement.
This is required when a concrete spec was restored from the concretizer-worker protocol and does
not carry inherited Python package objects.
The build policy grants read-only access to every parent-selected active repository root; inactive
repositories and unrelated host paths remain inaccessible.

* [ ] Harden recipe evaluation used by ``spack spec`` and environment concretization as its own project.
* [x] Prove that a concrete spec produced directly or by the concretizer worker enters the same installer worker path.
* [ ] Import the selected staging recipe only after worker confinement is active.

Captured Prior-Branch Material
------------------------------

The abandoned ``progress-v3+sandbox-fixes-v2`` branch is a policy and test reference, not an implementation template.
Its useful material was in ``lib/spack/spack/installer/sandbox.py`` and ``lib/spack/spack/test/sandbox.py``.
Retained concepts must move to dedicated install-worker modules and tests.

Retain these concepts:

* ``COMPILER_LANGUAGES = ("c", "cxx", "fortran")``;
* collect configured compiler paths from concrete dependency-edge virtuals and ``extra_attributes["compilers"]``;
* query selected drivers with ``-print-prog-name=`` for compiler front ends, ``collect2``, linker/binutils helpers, and required core utilities;
* query compiler files with ``-print-file-name=``;
* resolve a bare reported program through both wrapper PATH and ``os.defpath``; and
* grant individual driver, tool, file, and narrowly documented runtime paths, never ``/usr/bin`` or another whole tool directory.

The implemented Linux baseline, validated from real package build failures, is:

.. code-block:: text

     compiler_programs = (
       "cc1",
       "cc1plus",
       "f951",
       "collect2",
       "lto1",
       "lto-wrapper",
       "cpp",
       "as",
       "ld",
       "ar",
       "ranlib",
       "strip",
     )
     core_build_programs = (
       "chmod",
       "cp",
       "install",
       "ln",
       "mkdir",
       "mv",
       "rm",
       "cat",
       "cut",
       "ls",
       "touch",
       "wc",
       "basename",
       "dirname",
       "env",
       "expr",
       "pwd",
       "sort",
       "tr",
       "uname",
       "find",
       "git",
       "grep",
       "ldd",
       "which",
       "xargs",
       "awk",
       "bash",
       "perl",
       "sed",
     )
   compiler_files = ("liblto_plugin.so",)

Build capability provenance
~~~~~~~~~~~~~~~~~~~~~~~~~~~

The baseline remains grouped in Python for the initial implementation.
Keep this evidence when moving capabilities into package-scoped YAML whitelists so reviewers can
trace every grant to an observed package phase.

.. list-table:: Observed build sandbox capabilities
   :header-rows: 1
   :widths: 24 38 38

   * - Package or subsystem
     - Observed phase
     - Required capability
   * - compiler drivers
     - compile and link
     - ``cc1``, ``cc1plus``, ``f951``, ``collect2``, ``lto1``, ``lto-wrapper``,
       ``cpp``, ``as``, ``ld``, ``ar``, ``nm``, ``ranlib``, ``strip``,
       ``liblto_plugin.so``, and ``/usr/include``
   * - ``gcc-runtime``
     - install
     - the exact C, C++, and Fortran drivers declared by its external compiler dependency,
       plus their individually resolved support programs and files
   * - ``pkgconf`` and ``berkeley-db``
     - Autoconf and libtool configure/link
     - ``file``, ``diff``, ``rmdir``, ``true``, ``nm``, ``sort``, ``uniq``, and the
       ``file`` magic databases
   * - ``diffutils``
     - configure and build
     - ``echo``, ``cmp``, ``uniq``, ``date``, and ``xargs``
   * - ``ncurses``
     - configure and generated-source build
     - ``mawk``, ``sleep``, ``tbl``, ``paste``, ``head``, and ``/etc/passwd``
   * - ``font-util``
     - configure and build
     - ``id`` and ``gzip``
   * - ``perl``
     - configure
     - ``split``, ``realpath``, ``egrep``, ``tail``, ``arch``, ``comm``, ``/etc/hosts``,
       and locale data
   * - stage archive expansion
     - fetch and expand
     - ``tar``, ``unzip``, ``gzip``, ``gunzip``, ``bunzip2``, ``xz``, ``7z``, ``patch``,
       and ``sh``
   * - ``libxml2``
     - expand a cached ``.tar.xz`` source archive
     - ``tar``, ``xz``, each selected tool's own prefix and link/run dependency closure, and read
       access to the configured source-cache target behind the staged archive symlink
   * - non-archive source resources
     - install after staging
     - read-only access to the configured source cache for retained symlink targets, observed
       with ``ca-certificates-mozilla``

Retain and relocate tests proving: dependency prefixes are readable; the stage and exact prefix are writable; the prefix parent is not writable; configured symlinks resolve correctly; and sbang, Spack runtime, package directories, loader inputs, selected drivers, selected support paths, and compiler plugins are readable.
Also retain filesystem-only, network-only, and no-op policy tests.

Do not retain the branch's installer sandbox module as the integration point or use it to add source-plan/provenance architecture or a separate download path.

Decision Gates
--------------

These decisions are resolved for the initial implementation:

* [x] Automatically use workers when supported; do not add a worker command or opt-in flag.
* [x] Add shared ``config:sandbox:allow_fallback``, defaulting to ``false``.
  When enabled, unavailable worker capabilities fall back to the existing trusted path.
* [x] Preserve ``spack stage`` fetch-and-expand semantics; installer staging continues to patch unless disabled.
* [x] Initially use the checksum worker's public-authority proxy policy to preserve normal staging compatibility.
  A restrictive install allowlist remains a separate future policy milestone.
* [x] Import package recipes inside confinement using minimal parent-selected concrete-spec and repository state.
* [x] Start build-tool policy with the prior Linux baseline, validating individual resolved paths.
  Add platform-specific policies only with focused tests.

Stop and ask before changing any resolved decision or introducing a conflicting constraint.

Validation Matrix
-----------------

Each slice adds focused dedicated worker tests before integration tests.
Cover request validation, proxy-only networking, proxy failure cleanup, stage write scope, fetch/expand/patch compatibility, compiler path discovery, and installer scheduler/UI continuity.
Reuse command tests for direct-mode parity and add only worker-specific command cases.

Run the new ``spack.test.install_worker`` tests first, then relevant stage, install, and installer tests.
On capable Linux, use real-kernel tests for direct network denial and proxy mediation.
Retain explicit fallback tests elsewhere.
Build this page through the root Sphinx doctree before implementation lands.
