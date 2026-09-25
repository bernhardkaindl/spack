..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Namespace Backend Plan
======================

This page plans a Linux user and mount namespace based sandbox backend that
provides stronger isolation than Landlock alone and serves as the preferred
confinement backend when available for the install worker and build-phase
confinement.

The existing Landlock stack is additive to this backend, not a replacement.
Landlock continues to deny writes and restricts read and execute access to
selected roots. The namespace backend adds the ability to truly hide host
filesystem content rather than only denying access to it.

Seccomp is not part of the current develop branch's sandbox implementation.
It exists in an earlier exploratory sandbox branch
(``lib.sandbox/spack/spack/``) but has not been merged into
``lib/spack/spack/``. Seccomp is a valuable later-stage addition because it
provides features that Landlock and namespaces alone cannot: seccomp user
notifications and the ability to selectively intercept and mediate network
syscalls, enabling an enforced proxy that performs hostname-based HTTP/HTTPS
filtering through a CONNECT proxy. These capabilities would sit as an
additional inner layer inside the Landlock and namespace confinement, not as
a replacement for either. Seccomp should be treated as a late-stage
enhancement to pursue only after the install-worker sandbox — with the
namespace backend, Landlock, and the mount-tree helpers — is fully working and
reliable.

Motivation
----------

Landlock's filesystem model is a deny-by-default policy with explicit allow
rules. A denied path produces ``-EPERM``. Some build tools and configure
scripts do not tolerate ``Permission denied`` or ``Operation not permitted``
when accessing a path that should not exist in their environment at all. They
may abort, fall back to a host tool, or behave differently when a directory is
accessible but empty versus when it is inaccessible.

The existing private namespace usage in the install worker is a narrow
workaround: it bind-mounts an empty stage-owned directory over
``/usr/share/aclocal`` to suppress host Autoconf macros. That proves the
mechanism works, but it does not generalize the approach to the broader
sandbox problem.

Mount namespaces solve a different problem than Landlock:

.. list-table:: Isolation model comparison
   :header-rows: 1
   :widths: 25 37 38

   * - Property
     - Landlock
     - User and mount namespaces
   * - Write denial
     - Yes, explicit rules deny writes to selected paths.
     - Yes, inherited from the Landlock policy applied inside the namespace.
   * - Read and execute restriction
     - Yes, explicit allow rules grant access to selected roots.
     - Yes, inherited from the Landlock policy applied inside the namespace.
   * - File visibility
     - Denied paths produce ``-EPERM``. Directory entries remain visible.
     - Mount points can replace directory trees with empty tmpfs or other
       mounts, so denied content is not visible at all.
   * - Configure-script behavior
     - Tools that stat a directory and expect ``ENOENT`` for a missing
       include or ``bin`` directory see the real host directory and may fail
       with ``-EPERM`` when they try to read it.
     - Tools see an empty directory or a directory containing only
       bind-mounted content. ``ENOENT`` is natural for missing headers and
       programs.
   * - Host tool suppression
     - Individual paths must be granted or denied explicitly. The parent
       directory of a granted executable remains accessible.
     - Mounting an empty tmpfs over ``/bin``, ``/usr/bin``, or
       ``/usr/include`` hides the entire host tree. Only explicitly
       bind-mounted programs and headers are visible.
   * - Implementation complexity
     - Landlock rules are declarative and validated by the kernel.
     - Mount namespaces require careful mount ordering, propagation
       containment, and cleanup. User namespaces require UID and GID
       mapping setup.

The namespace backend is therefore preferred when available because it can
provide both Landlock's deny capabilities and true filesystem hiding.

Availability
------------

Unprivileged user and mount namespaces are optional on some distributions.
Some distributions disable them entirely, restrict ``uid_map`` and
``gid_map`` writes, or require additional sysctl settings.

The capability probe must test:

* ``unshare(CLONE_NEWUSER)`` success;
* ``unshare(CLONE_NEWNS)`` success from an unprivileged process;
* ``/proc/self/setgroups`` write ability when the kernel requires it before
  writing ``uid_map``;
* ``uid_map`` and ``gid_map`` write ability for the calling user's numeric
  UID and GID; and
* mount propagation containment ability, including ``MS_PRIVATE`` on the
  root mount and the directory bind mount required by the current masking
  backend; and
* dropping the user-namespace capability sets required after trusted mount
  setup.

The Phase 4 probe must additionally test tmpfs and every other mount operation
introduced by the policy-driven mount tree before selecting that backend.

The probe returns a structured capability result with availability, the failed
operation, and its reason. Completed child results are cached and inherited by
forked workers. Parent-side operational failures remain retryable until the
install child freezes its pre-thread result. This preserves diagnostics such as
``write /proc/self/uid_map: Operation not permitted`` without probing after a
worker thread exists.

When the probe reports unavailability, backend selection explicitly chooses
the existing Landlock-only sandbox. This is a constrained fallback, so it is
distinct from the shared ``config:sandbox:allow_fallback`` policy for trusted
direct execution when no sandbox worker is available. The command never
launches an unconstrained worker.

Fallback is permitted only from this side-effect-free capability decision.
Once the install child starts namespace entry or mount-tree preparation, a
failure is fatal for that child: it neither retries setup nor falls back from a
possibly partially mutated process.

Process model
-------------

The internal Linux namespace backend reuses the existing forked install child.
The trusted installer supervisor creates that child through the existing
``multiprocessing.Process`` launch path. Before starting its logging thread,
the child freezes namespace availability, calls
``unshare(CLONE_NEWUSER | CLONE_NEWNS)``, configures UID and GID mappings,
makes the root mount private, prepares the narrow mount tree, and drops all
capabilities in the new user namespace. The supervisor remains in the host
namespaces and is not restricted.

This closes the :term:`mount-authority window`: no recipe-controlled Python
runs while the child holds the capability needed to create mounts. The child
then starts logging and performs recipe-controlled staging, patching, and
builder setup. Landlock grants and application still happen immediately before
the existing package build-phase loop. Landlock's deny-by-default policy also
does not grant the ``/proc`` UID/GID mapping writes required to turn a later
nested user namespace into renewed mount authority.
This is the same self-restriction pattern used by the Landlock-only backend:
neither ``unshare()`` nor ``landlock_restrict_self()`` requires an intervening
``exec``.

On Landlock ABI 8 and newer, ``LANDLOCK_RESTRICT_SELF_TSYNC`` applies the
Landlock domain to the child-local logging thread as well. On older ABIs that
trusted thread is outside the Landlock domain, although it remains inside the
mount namespace; it must not execute package recipe or build-tool code.

The forked child inherits loaded modules, Python objects, environment state,
and open descriptors from the supervisor. The current boundary therefore
protects build phases; it is not confinement-before-recipe-import and does not
claim that inherited descriptors have been minimized. A future fresh-executed
worker may provide that additional hardening, but it is not required to use
the internal namespace backend.

External Linux launchers such as Bubblewrap have a different interface: they
construct confinement while starting another command. Supporting one naturally
adds an alternate fresh-exec launcher. That optional backend must preserve the
same installer state, logging, and failure contracts, but it does not replace
the internal fork-and-self-restrict path.

Mount tree design
-----------------

The namespace backend prepares a private mount tree before the worker
applies Landlock. The mount tree hides host content and presents only the
content the worker legitimately needs.

Base mounts
~~~~~~~~~~~

The install child performs these mounts in order:

1. Make the root mount private with ``MS_PRIVATE`` so mounts do not propagate
   back to the host.
2. Mount an empty tmpfs over ``/bin``.
3. Mount an empty tmpfs over ``/usr/bin``.
4. Mount an empty tmpfs over ``/usr/include``.
5. Mount an empty tmpfs over other host directories that should not be
   visible to the worker, such as ``/usr/share`` subtrees that are not
   explicitly granted.

The exact set of hidden directories is derived from the host policy and the
concrete spec. It is not a fixed list. Directories that contain only content
the worker never needs are hidden entirely. Directories that contain content
the worker needs are hidden and then partially populated through bind mounts.

Bind mounts for whitelisted content
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

After hiding the host directories, the setup child bind-mounts only the
content the worker needs into the visible tree:

* Selected compiler drivers and their support programs are bind-mounted from
  their real host paths into the visible ``bin`` or equivalent tree.
* Selected build tools such as ``tar``, ``patch``, ``git``, and compression
  tools are bind-mounted individually.
* Selected header trees are bind-mounted from their real host paths into the
  visible ``/usr/include`` tree. This includes the exact glibc headers, Linux
  UAPI headers, the selected libstdc++ header tree, and compiler-internal
  headers.
* The Spack stage, prefix, and temporary directories are bind-mounted or
  accessed directly inside the namespace.
* The package repository roots, Spack source tree, and Python runtime paths
  are bind-mounted or remained accessible through the mount tree.

The bind mounts use ``MS_REC`` where needed to expose directory trees, but
each bind mount targets a specific resolved path. The worker never sees the
host ``/usr/bin`` or ``/usr/include`` parent; it sees only the bind-mounted
content the trusted parent selected.

This is the key improvement over Landlock alone: a build tool that searches
``/usr/include`` for a header sees either an empty directory or the
bind-mounted headers, never the full host include tree. A tool that searches
``/bin`` for a utility sees only the whitelisted programs, not the host
utilities.

Mount cleanup
~~~~~~~~~~~~~

The namespace and all of its mounts disappear when the install child exits.
Future code that performs explicit cleanup before process exit must handle:

* Unmounting the tmpfs hides in reverse order.
* Unmounting bind mounts.
* Destroying the user namespace.

If the worker crashes or is killed, the parent reclaims the namespace and
its mounts. The host mount tree is unaffected because the root mount was made
private.

UID and GID mapping
-------------------

The user namespace maps the invoking user's numeric UID and GID to the same
values inside the namespace. This prevents programs from mistaking the
unprivileged worker for UID 0 and ensures that filesystem ownership checks
behave as expected for files the worker legitimately accesses.

The mapping is:

* ``uid_map``: ``<numeric-uid> <numeric-uid> 1`` for the invoking user, plus any
  additional mapping needed for files owned by other UIDs that the worker
  must read.
* ``gid_map``: ``<numeric-gid> <numeric-gid> 1`` for the invoking user's primary group,
  plus any additional mapping needed for files owned by other GIDs.

The worker runs as the mapped UID inside the namespace. It does not gain
privileged capabilities. The namespace is unprivileged.

Relationship to Landlock (and future seccomp)
----------------------------------------------

The namespace backend does not replace Landlock. It complements it:

* Landlock is applied inside the namespace after the mount tree is prepared.
  It denies writes to paths that should not be writable and restricts read
  and execute access to the selected roots. Inside the namespace, Landlock
  operates on the visible mount tree, so its deny rules apply to the
  bind-mounted content as well.

* Seccomp is not part of the current develop branch's sandbox implementation.
  It exists in an earlier exploratory sandbox branch
  (``lib.sandbox/spack/spack/``) but has not been merged into
  ``lib/spack/spack/``. Seccomp is a valuable later-stage addition because it
  provides features that Landlock and namespaces alone cannot: seccomp user
  notifications and the ability to selectively intercept and mediate network
  syscalls, enabling an enforced proxy that performs hostname-based HTTP/HTTPS
  filtering through a CONNECT proxy. When added, seccomp would sit as an
  additional inner layer inside the Landlock and namespace confinement, not as
  a replacement for either. It should be pursued only after the install-worker
  sandbox — with the namespace backend, Landlock, and the mount-tree helpers —
  is fully working and reliable.

* The combination of the namespace backend and Landlock already provides
  defense in depth: the mount tree limits what the worker can see, and Landlock
  denies writes and restricts access to what it can see. Seccomp would add
  further syscall-level mediation on top of that foundation.

Differences from the current narrow namespace usage
----------------------------------------------------

The current install worker uses a private user and mount namespace only to
bind-mount an empty directory over ``/usr/share/aclocal``. That is a single
mount in a namespace that otherwise shares the host mount tree.

The namespace backend generalizes this to a full private mount tree:

* Multiple tmpfs hides over host ``bin`` and ``include`` directories.
* Multiple bind mounts for whitelisted programs and headers.
* Full mount propagation containment.
* UID and GID mapping for the user namespace.
* Application to both staging and build workers, not only the build-phase
  ``aclocal`` workaround.

The current workaround is a proof of concept for the mechanism. The namespace
backend is the general facility that the workaround was pointing at.

Windows equivalent
------------------

Windows does not have mount namespaces or Landlock, and AppContainer cannot
self-restrict an already-running process. The current Windows backend therefore
applies AppContainer when it creates external build-tool processes. Direct
Python file I/O performed by package recipes in the installer process is not
confined by that backend.

A future Windows boundary that also confines recipe Python must create the
installer worker inside an AppContainer, with explicit handles and process
state. That fresh-process requirement is specific to AppContainer. It does not
require the internal Linux namespace backend to replace its existing forked
install child with a fresh-executed worker.

Implementation phases
---------------------

Phase 1: Capability probe and fallback
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Add a capability probe for unprivileged user and mount namespaces to the
shared sandbox capability detection. The probe reports whether the namespace
backend is available and, if not, why.

The structured probe result feeds an explicit backend decision. When the
namespace backend is available, the install worker prefers it. When it is
unavailable, the worker uses the existing Landlock-only backend and retains the
failed operation and reason for diagnostics. The decision identifies Landlock
as constrained and rejects an unconstrained backend. It is made before any
process mutation; namespace-entry and mount failures remain fatal.

Phase 2: Mount tree setup
~~~~~~~~~~~~~~~~~~~~~~~~~

Implement the mount tree setup in a dedicated module. The module:

* Creates the user and mount namespaces.
* Configures UID and GID mapping.
* Makes the root mount private.
* Mounts tmpfs hides over selected host directories.
* Bind-mounts whitelisted programs and headers.
* Cleans up the mount tree when the worker exits.

The module is independent of the worker protocol and confinement policy. It
exposes process-local namespace-entry, masking, and bind-mount primitives used
by the existing install child.

Phase 3: Integration with the install worker
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Integrate the namespace backend into the install worker's launch path. When the
namespace backend is available, the existing Linux install child performs the
trusted mount setup and drops namespace capabilities before starting
child-local threads. It later applies Landlock immediately before the build
phases.

This phase does not require a second worker protocol or ``exec``. An external
sandboxing tool may be added later as an alternate launcher, and a future
fresh-exec boundary may harden inherited-state and recipe-import isolation.

The existing Landlock policies remain valid inside the namespace.
They operate on the visible mount tree, so their rules apply to the
bind-mounted content. Seccomp policies from the earlier exploratory branch
may be added later as a further inner layer; see `Relationship to Landlock
(and future seccomp)`_.

Phase 4: Policy-driven mount tree
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Make the set of hidden directories and bind-mounted content driven by policy
rather than a fixed list. The policy derives from:

* The concrete spec's selected compilers and build tools.
* The host's available header trees and compiler installations.
* The package-repository roots and Spack source tree.
* The stage, prefix, and temporary directories.

The policy is the same source as the existing Landlock allow rules, but it
now drives both Landlock grants and mount-tree setup. A path that appears in
the Landlock allow set may also appear as a bind mount in the namespace
backend. A path that is not in the allow set is hidden by a tmpfs mount if
it is in a directory that the namespace backend hides.

Phase 5: Build-phase confinement
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Extend the namespace backend to the build-phase confinement described in
:ref:`install-worker-build-phase`. The build worker uses the same namespace
backend with build-specific policy for compilers, build tools, dependency
prefixes, and the install prefix.

Phase 6: Concretizer worker
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Evaluate whether the concretizer worker benefits from the namespace backend.
The concretizer worker's threat model is recipe evaluation during solving,
not build tool execution. The namespace backend may provide additional
isolation for the concretizer worker, but the primary confinement for that
worker is Landlock, which is already implemented in the develop branch.
Seccomp is a later-stage enhancement that could add syscall filtering and
user notifications; see `Relationship to Landlock (and future seccomp)`_.

Decision gates
--------------

* [x] The namespace backend is preferred when available; Landlock-only is the
  fallback, not the default.
* [ ] The mount tree is derived from policy, not a fixed list.
* [x] The internal Linux backend confines only the existing forked install
  child; the trusted installer supervisor remains outside the namespace.
* [ ] External Linux sandbox tools use a separate launcher without changing
  the internal backend's process model.
* [ ] A future Windows worker that confines recipe Python is created directly
  inside AppContainer; this platform-specific requirement does not constrain
  Linux launch design.
* [ ] Landlock remains in force inside the namespace; seccomp is a later-stage
  addition not yet in the develop branch.
* [x] The capability probe reports specific reasons when the namespace backend
  is unavailable.
* [x] The command never launches an unconstrained worker when the namespace
  backend is unavailable.

References
----------

* :doc:`overview` describes the shared trust boundary and the current Linux
  confinement stack.
* :doc:`install-worker` describes the install worker plan, including the
  current narrow namespace usage for ``/usr/share/aclocal``.
* :doc:`planned-work` records the earlier shared namespace assessment.
* :doc:`solved-issues` records the empty ``aclocal`` directory and GCC header
  masking cases that motivated the namespace approach.
