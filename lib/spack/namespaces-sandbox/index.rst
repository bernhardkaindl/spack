..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Namespace Backend - Implementation Status
=========================================

Review boundary
---------------

The capability probe, basic mount-tree helpers, backend selection, and the
existing Linux install-child integration are implemented and tested. The
internal Linux backend self-restricts that existing forked child; a fresh
``exec`` is optional later hardening or part of an external launcher, not a
namespace requirement.

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

* **Phase 1 increment -- complete:** the disposable capability probe reports
  the failed operation and reason, caches completed preflight results, and
  explicitly selects either namespaces or constrained Landlock fallback. The
  broader shared trusted-direct fallback policy remains separate.
* **Phase 2 -- partial:** namespace entry, mapping, propagation containment,
  empty-directory masking, and bind-mount primitives are implemented. The
  policy-driven tmpfs tree, preserved mount sources, namespace handle, and
  explicit cleanup interface remain open.
* **Phase 3 -- complete for the narrow integration:** before starting its
  logging thread, the current build worker performs trusted namespace mount
  setup and drops mount authority. The installer applies Landlock before build
  phases. The broader mount policy is Phase 4.
* **Phase 4 -- policy model complete, derived tree open:** the current narrow
  mask is represented by an immutable, validated, deterministic mount plan
  before namespace mutation. Its empty sources are private tmpfs mounts
  remounted read-only before exposure, so stage write grants cannot populate
  them. Explicit preserved sources declare read-only or read-write access, are
  staged before hiding parents, and are restored through canonical
  merged-``/usr`` aliases in safe order. Read-only file and recursive directory
  mounts are kernel-enforced without Landlock. An immutable filesystem policy
  now classifies hidden roots, read-only and writable mounts, and generated
  namespace-local paths; rejects duplicate, overlapping, and access-conflicting
  entries; and can be built from current trusted installer grants. Compilation
  rejects classified paths outside its hidden roots and a scratch directory
  below a hidden root. Preserved sources are rechecked and never recreated if
  they disappear. The installer-derived policy is not activated:
  policy-derived compiler, tool, header, runtime, and writable trees remain
  future work.
* **Phases 5 and 6 -- not started:** full build-phase policy validation and
  concretizer-worker evaluation remain future work.

The case-by-case findings, acceptance criteria, and worklog are tracked in
``lib/spack/namespaces-sandbox/refactor-review.md``.

Phase 1: capability probe and fallback
--------------------------------------

``namespace_sandbox_capability(libc=None)`` runs setup in a disposable forked
child and returns availability, the failed operation, and its reason.
``namespace_sandbox_available`` remains the boolean compatibility wrapper. The
child creates a user and mount namespace, maps the invoking UID and GID to the
same numeric values, denies setgroups, and makes the root mount private with
``MS_REC | MS_PRIVATE``. This probe does not execute recipe code.

* Missing fork support, libc-loading failures, and operating-system errors in
  the probe report unavailability. Parent-side operational failures are not
  cached, so the worker can retry once at its safe pre-thread setup point.
* Both pipe descriptors are closed if fork fails. The parent closes its read
  descriptor and reaps the child even if reading the probe result fails.
* The child always exits with ``os._exit``; unexpected setup exceptions cannot
  unwind into the parent's caller or duplicate the test runner.
* The probe checks namespace creation, mappings, propagation containment,
  tmpfs creation and read-only remounting, the current directory bind mount,
  recursive read-only mount attributes, and capability dropping. Child and
  parent failures retain operation-specific diagnostics. Every additional
  mount operation introduced by the Phase 4 policy must extend the probe.
* Completed default-libc probe results are cached. Installer construction
  performs the preflight before build workers start; forked workers inherit
  that result. If preflight failed operationally, the worker retries before
  starting ``Tee`` and then freezes its local fallback result so later backend
  selection cannot probe after threads exist.
* ``namespace_sandbox_decision`` selects the namespace backend when available
  and otherwise labels Landlock-only as a constrained fallback. It rejects an
  unconstrained backend; the shared trusted-direct fallback policy is separate.

Phase 2: mount-tree setup
-------------------------

``prepare_empty_directory_masking(paths, libc=None)`` returns ``True`` only
when the calling process has entered the private namespace, including for an
empty path list. Repeated preparation in that process reuses the active
namespace rather than probing a nested namespace. A failed availability probe
returns ``False``. Errors during actual setup propagate; they must not be
interpreted as a harmless fallback after partially changing process state.
The worker does not retry after such a failure because namespace or mount state
may already be partially changed; controlled worker failure is the recovery.

``hide_directories_as_empty(paths, stage_path, namespace_ready=False,
libc=None)`` masks existing directories with immutable empty directories from
a private tmpfs at ``stage_path/spack-empty-host-dirs``. An empty path list is
a no-op. The helper returns ``False`` if namespaces are unavailable; a mount
failure raises ``OSError`` rather than accepting a partially prepared view.
This remains the narrow mask, not the complete policy-derived tree.

``NamespaceSandbox.prepare_mount_tree`` explicitly enters the namespace before
masking and marks it ready only after successful preparation, even when there
are no directories to hide. ``bind_mount(source, target=None)`` uses recursive
binds for directories and ordinary binds for files. Sources and targets must
already exist and remain reachable; this primitive does not preserve hidden
sources or create replacement targets. Mounts disappear when the last process
holding the namespace exits; ``cleanup`` does not actively unmount them.

Phase 3: install-worker integration
-----------------------------------

The current transitional ``NamespaceSandbox`` delegates read/write grants and
application to Landlock after namespace masking. The intended completed
namespace policy will replace that default filesystem role with hidden trees
and explicit read-only or writable bind mounts; Landlock will remain available
as an opt-in mode for testing ``EPERM`` behavior. Backend selection prefers
namespaces on Linux when the probe succeeds, otherwise it records the failed
operation and selects constrained Landlock.

Before starting its ``Tee`` logging thread, the installer prepares the mount
tree and drops all user-namespace capabilities. Its default mask hides
``/usr/share/aclocal`` unless an external ``autoconf`` dependency needs the
host macros. An empty mask still enters the namespace. The resulting sandbox
instance is handed to later setup solely for Landlock grants and application;
it rejects later bind mounts. If preparation reports namespace unavailability,
the existing hook warns and still applies Landlock. Setup or mount exceptions
abort rather than continuing with a partial mount tree. Pre-thread setup
failures close the state stream, write their traceback to the build log, and
exit with ``BUILD_ERROR``. Landlock initialization errors are normalized as
``SandboxError`` and then as installer errors.
The shared ``config:sandbox:allow_fallback`` policy for trusted direct execution
when no constrained worker is available is not implemented by this hook.

Verification
------------

Unit tests cover UID/GID mapping, re-entry, empty-tree readiness, denied
availability, fatal mount errors, immutable mount-plan validation, bind flags,
capability dropping, Landlock delegation, and installer ordering on both the
namespace and fallback paths.
Tests observe trusted prepare-and-drop before ``Tee`` construction, reject a
later bind mount, and verify controlled reporting of pre-thread setup failures.
Unit namespace setup mocks both libc and mapping-file writes; it never writes
the test runner's ``/proc`` mappings. Probe lifecycle regressions exercise
descriptor cleanup, retry and cache behavior, and isolate the
unexpected-exception case in a separate interpreter.

Linux integration tests create actual private namespaces and temporary mount
trees. They verify empty-mask visibility and parent isolation, ``EROFS`` for
read-only file and recursive-directory aliases without Landlock, and successful
writes through an explicit read-write alias. They skip when unprivileged
namespaces are unavailable. No system directories are masked by the tests.

Run the focused checks with::

    python3 -m pytest lib/spack/spack/test/test_namespace_probe_lifecycle.py \
        lib/spack/spack/test/test_sandbox_namespaces.py \
        lib/spack/spack/test/sandbox.py lib/spack/spack/test/sandbox_common.py

Phase 4: policy-driven mount tree
---------------------------------

Define one immutable namespace filesystem policy before namespace mutation. It
must classify hidden roots, read-only mounts, read-write mounts, and generated
namespace-local paths; reject duplicate, overlapping, and access-conflicting
entries; and derive its inputs from trusted spec and configuration state. Only
after that boundary is tested should the installer wire policy-derived
compiler, tool, interpreter, runtime, header, stage, prefix, and temporary
mounts into the plan.

Phase 5: build-phase confinement
--------------------------------

Extend the current build-phase boundary with the complete policy-driven mount
tree and validate real package builds. A future fresh-exec mode may minimize
inherited descriptors and confine before recipe import, but it is separate
hardening rather than a prerequisite for the internal Linux backend. External
sandbox tools may provide alternate launchers.

Phase 6: concretizer worker
---------------------------

Evaluate concretizer workers only after full build-phase confinement is
reliable. Consider seccomp notifications and mediated network access later;
seccomp remains exploratory work in ``lib.sandbox/spack/spack/``, not part of
this implementation.
