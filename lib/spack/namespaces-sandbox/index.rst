..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Namespace Backend - Implementation Status
=========================================

Review boundary
---------------

The capability probe, basic mount-tree helpers, backend selection, and the
existing installer hook are implemented and tested. This is a prerequisite for,
not the implementation of, the fresh-executed install-worker boundary described
in ``lib/spack/docs/sandbox/namespace-backend.rst``.

This increment makes namespace readiness truthful, contains probe failures, and
checks mount visibility in a disposable process. It deliberately keeps the
installer's narrow ``/usr/share/aclocal`` mask. Host ``/bin``, ``/usr/bin``, and
``/usr/include`` are not masked: doing so before preserving selected sources
would hide the very programs and headers needed to populate the replacement
view. The unfinished compiler/tool/header policy is not part of this boundary.

Status by design phase
----------------------

The phases below match ``lib/spack/docs/sandbox/namespace-backend.rst`` and the
grouping in ``lib/spack/spack/test/test_sandbox_namespaces.py``.

* **Phase 1 -- partial:** the boolean capability probe, cached preflight, and
  Landlock fallback selection are implemented. Reason-specific capability
  reports and the final shared worker policy remain open.
* **Phase 2 -- partial:** namespace entry, mapping, propagation containment,
  empty-directory masking, and bind-mount primitives are implemented. The
  policy-driven tmpfs tree, preserved mount sources, namespace handle, and
  explicit cleanup interface remain open.
* **Phase 3 -- experimental:** the current build worker enters the namespace
  before starting its logging thread, and the installer applies the narrow
  mask before Landlock. This is not the planned fresh-executed worker boundary.
* **Phases 4 through 6 -- not started:** policy-driven mounts, full build-phase
  confinement, and concretizer-worker evaluation remain future work.

The case-by-case findings, acceptance criteria, and worklog are tracked in
``lib/spack/namespaces-sandbox/refactor-review.md``.

Phase 1: capability probe and fallback
--------------------------------------

``namespace_sandbox_available(libc=None)`` runs in a disposable forked child.
The child creates a user and mount namespace, maps the invoking UID and GID to
the same numeric values, denies setgroups, and makes the root mount private
with ``MS_REC | MS_PRIVATE``. This probe does not execute recipe code and does
not establish the planned worker launch model.

* Missing fork support, libc-loading failures, and operating-system errors in
  the probe report unavailability. Parent-side operational failures are not
  cached, so the worker can retry once at its safe pre-thread setup point.
* Both pipe descriptors are closed if fork fails. The parent closes its read
  descriptor and reaps the child even if reading the probe result fails.
* The child always exits with ``os._exit``; unexpected setup exceptions cannot
  unwind into the parent's caller or duplicate the test runner.
* The probe checks namespace creation, mappings, and propagation containment.
  It does not yet diagnose individual failure reasons or test every mount type.
* Completed default-libc probe results are cached. Installer construction
  performs the preflight before build workers start; forked workers inherit
  that result. If preflight failed operationally, the worker retries before
  starting ``Tee`` and then freezes its local fallback result so later backend
  selection cannot probe after threads exist.

Phase 2: mount-tree setup
-------------------------

``prepare_empty_directory_masking(paths, libc=None)`` returns ``True`` only
when the calling process has entered the private namespace, including for an
empty path list. Repeated preparation in that process reuses the active
namespace rather than probing a nested namespace. A failed availability probe
returns ``False``. Errors during actual setup propagate; they must not be
interpreted as a harmless fallback after partially changing process state.

``hide_directories_as_empty(paths, stage_path, namespace_ready=False,
libc=None)`` masks existing directories with empty stage-owned directories at
``stage_path/spack-empty-host-dirs/<index>``. An empty path list is a no-op.
The helper returns ``False`` if namespaces are unavailable; a mount failure
raises ``OSError`` rather than accepting a partially prepared view. This is
still the narrow bind-mount implementation, not the planned tmpfs policy tree.

``NamespaceSandbox.prepare_mount_tree`` explicitly enters the namespace before
masking and marks it ready only after successful preparation, even when there
are no directories to hide. ``bind_mount(source, target=None)`` uses recursive
binds for directories and ordinary binds for files. Sources and targets must
already exist and remain reachable; this primitive does not preserve hidden
sources or create replacement targets. Mounts disappear when the last process
holding the namespace exits; ``cleanup`` does not actively unmount them.

Phase 3: install-worker integration
-----------------------------------

``NamespaceSandbox`` delegates read/write grants and application to Landlock.
Namespace masking does not replace Landlock confinement. Backend selection
prefers namespaces on Linux when the probe succeeds, otherwise Landlock.

The installer prepares the mount tree before granting Landlock permissions.
Its default mask hides ``/usr/share/aclocal`` unless an external ``autoconf``
dependency needs the host macros. An empty mask still enters the namespace.
The worker performs this entry before starting its ``Tee`` logging thread. If
preparation reports namespace unavailability, the existing hook warns and
still applies Landlock. Setup or mount exceptions abort rather than continuing
with a partial mount tree. Pre-thread setup failures close the state stream,
write their traceback to the build log, and exit with ``BUILD_ERROR``. Landlock
initialization errors are normalized as ``SandboxError`` and then as installer
errors.
The shared ``config:sandbox:allow_fallback`` worker policy in the design is not
yet implemented by this hook.

Verification
------------

Unit tests cover UID/GID mapping, re-entry, empty-tree readiness, denied
availability, fatal mount errors, bind flags, Landlock delegation, and installer
ordering on both the namespace and fallback paths. Tests also observe namespace
preparation before ``Tee`` construction and verify controlled reporting of
pre-thread setup failures. Unit namespace setup mocks both libc and mapping-file
writes; it never writes the test runner's ``/proc`` mappings. Probe lifecycle
regressions exercise descriptor cleanup, retry and cache behavior, and isolate
the unexpected-exception case in a separate interpreter.

A Linux integration test creates an actual private namespace and masks a
temporary host directory. It verifies an empty listing and ``ENOENT`` inside
the child, then checks that the parent's file remains visible and unchanged.
It skips when unprivileged namespaces are unavailable. No system directories
are masked by the test.

Run the focused checks with::

    python3 -m pytest lib/spack/spack/test/test_namespace_probe_lifecycle.py \
        lib/spack/spack/test/test_sandbox_namespaces.py \
        lib/spack/spack/test/sandbox.py lib/spack/spack/test/sandbox_common.py

Phase 4: policy-driven mount tree
---------------------------------

Preserve selected mount sources before hiding parents; handle merged-/usr
aliases, target creation, and mount ordering without exposing a backup host
tree to untrusted code. Derive compiler, tool, interpreter, runtime, and header
selections from the current spec APIs and a shared Landlock/mount policy, not a
fixed tool list.

Phase 5: build-phase confinement
--------------------------------

Launch a fresh executed worker with bounded protocol descriptors and apply
confinement before importing recipe code. The current installer hook alone is
not that trust boundary. Implement and test the shared fallback policy and
richer capability reports as part of this boundary.

Phase 6: concretizer worker
---------------------------

Evaluate concretizer workers only after full build-phase confinement is
reliable. Consider seccomp notifications and mediated network access later;
seccomp remains exploratory work in ``lib.sandbox/spack/spack/``, not part of
this implementation.
