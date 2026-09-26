..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Namespace Backend Plan
======================

This page plans a Linux user and mount namespace based sandbox backend that
serves as the preferred confinement backend when available for the install
worker and build-phase confinement. Its default filesystem policy hides host
trees behind empty mounts and bind-mounts only allowlisted content, so normal
negative lookups return ``ENOENT`` instead of Landlock's ``EPERM``.

Landlock inside the namespace is an optional diagnostic mode for testing how
installations behave under permission-denied errors. It is not part of the
target default namespace policy. The current narrow implementation still
layers Landlock while the policy-driven mount tree is incomplete; removing it
before every required read, write, and execute path has a namespace mapping
would weaken confinement.

Seccomp is not part of the current develop branch's sandbox implementation.
It exists in an earlier exploratory sandbox branch
(``lib.sandbox/spack/spack/``) but has not been merged into
``lib/spack/spack/``. Seccomp is a valuable later-stage addition because it
provides features that Landlock and namespaces alone cannot: seccomp user
notifications and the ability to selectively intercept and mediate network
syscalls, enabling an enforced proxy that performs hostname-based HTTP/HTTPS
filtering through a CONNECT proxy. These capabilities would sit as an
additional inner layer inside namespace confinement, not as a replacement for
the mount policy. Seccomp should be treated as a late-stage enhancement to
pursue only after the install-worker namespace and mount-tree helpers are fully
working and reliable.

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
     - Writable content is exposed only through planned writable mounts;
       hidden and read-only trees are not writable.
   * - Read and execute restriction
     - Yes, explicit allow rules grant access to selected roots.
     - Host trees are hidden and only allowlisted programs, libraries,
       headers, and data are bind-mounted into the visible tree.
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

The namespace backend is therefore preferred when available because its
filesystem view can express absence naturally instead of relying on
permission-denied errors.

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

The probe tests tmpfs creation, read-only remounting, directory bind mounts,
and capability dropping. Every additional mount operation introduced by the
policy-driven tree must also be probed before selecting that backend.

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
builder setup. The current transitional implementation applies Landlock before
the build-phase loop because the mount policy is incomplete. The target
namespace-only default must separately prevent writes to ``/proc`` UID/GID
mapping files and exposure of tools that could create another namespace.

When opt-in Landlock diagnostic mode is enabled, ABI 8 and newer apply the
Landlock domain to the child-local logging thread with
``LANDLOCK_RESTRICT_SELF_TSYNC``. On older ABIs that trusted thread remains
outside the Landlock domain and must not execute package recipe or build-tool
code.

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

Mount-plan validation
~~~~~~~~~~~~~~~~~~~~~

The current narrow masking path first builds an immutable mount plan before
entering the namespace. It canonicalizes existing directory targets, rejects
non-directory targets, duplicate targets, and ancestor/descendant conflicts,
then sorts the targets and assigns deterministic stage-owned empty-directory
sources. A missing target remains a no-op for the narrow mask. An existing
stage path must be a directory; a not-yet-created stage path is created only
when the validated plan is applied.

The plan also accepts explicitly selected source-to-target requests for the
policy-driven tree. Every request declares read-only or read-write access.
Source paths and targets are canonicalized, source and target file types are
checked, and a target must be below a hidden directory. The plan creates
stage-owned preservation endpoints before hiding parents, then restores those
endpoints into the hidden tree in increasing target-depth order. This handles
merged-``/usr`` aliases by planning against their resolved targets and uses
recursive bind mounts only for preserved directory trees.

Read-only requests use ``mount_setattr(MOUNT_ATTR_RDONLY)`` on both the
preserved and restored aliases. Directory requests use ``AT_RECURSIVE`` so
nested mounts cannot retain write access. Read-write requests do not receive
that attribute. This access distinction is enforced by the mount namespace and
does not depend on Landlock.

This is the planning and validation boundary for Phase 4, not the complete
policy-driven mount tree. Immutable empty sources and mount access modes are
implemented; selecting the compiler, tool, header, runtime, and writable path
set remains open.

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
each bind mount targets a specific resolved path. Tool, header, runtime, and
dependency mounts are read-only. Stage, prefix, and selected temporary mounts
are read-write. The worker never sees the host ``/usr/bin`` or
``/usr/include`` parent; it sees only the bind-mounted content the trusted
parent selected.

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

The completed namespace backend replaces Landlock as the default filesystem
policy for namespaced workers:

* Empty mounts hide non-allowlisted host content. Read-only and writable bind
  mounts expose only the content and mutation points required by the build.
  Missing content therefore produces ``ENOENT`` instead of ``EPERM``.
* Landlock may be enabled explicitly inside the namespace to test installation
  behavior under permission-denied errors or to evaluate defense in depth. It
  is not enabled by default once the mount policy is complete.

* Seccomp is not part of the current develop branch's sandbox implementation.
  It exists in an earlier exploratory sandbox branch
  (``lib.sandbox/spack/spack/``) but has not been merged into
  ``lib/spack/spack/``. Seccomp is a valuable later-stage addition because it
  provides features that Landlock and namespaces alone cannot: seccomp user
  notifications and the ability to selectively intercept and mediate network
  syscalls, enabling an enforced proxy that performs hostname-based HTTP/HTTPS
  filtering through a CONNECT proxy. When added, seccomp would sit as an
  additional inner layer inside namespace confinement, not as a replacement
  for the mount policy. It should be pursued only after the install-worker
  namespace and mount-tree helpers are fully working and reliable.

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
child-local threads. The current narrow integration later applies Landlock
immediately before the build phases as a transitional constraint until the
complete namespace filesystem policy is implemented.

This phase does not require a second worker protocol or ``exec``. An external
sandboxing tool may be added later as an alternate launcher, and a future
fresh-exec boundary may harden inherited-state and recipe-import isolation.

Landlock remains available as an opt-in diagnostic mode inside the namespace.
Seccomp policies from the earlier exploratory branch may be added later as a
further inner layer; see `Relationship to Landlock (and future seccomp)`_.

Phase 4: Policy-driven mount tree
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The immutable mount-plan validation, preserved-source ordering, immutable mask
source, read-only/read-write enforcement, and filesystem-policy model are
complete. ``NamespaceFilesystemPolicy`` classifies canonical hidden roots,
read-only passthrough mounts, read-write mounts, replacement mounts, and
namespace-local generated files, directories, or symlinks in immutable tuples.
Its builder rejects duplicate, nested, and cross-access targets before
namespace entry. Generated entries must be below a hidden root. Replacement
sources must be existing directories mounted at explicit hidden roots;
preserved mounts may be below a hidden root because they are the explicit
content restored after that root is hidden. Requested hidden roots must exist
and be directories.

The installer can construct this policy from the paths represented by its
current Landlock grants: non-external dependency prefixes, the install prefix,
stage and temporary directories, ``/dev/null``, Spack and upstream ``sbang``,
and user ``allow_read`` or ``allow_write`` entries. Missing paths retain the
existing grant behavior and are omitted. Redundant same-access descendants are
collapsed before policy validation; read-only/read-write conflicts remain
errors. This construction is trusted installer work and does not mutate the
filesystem.

Policy construction and mount-plan compilation are deliberately separate. A
complete policy may classify a trusted path before a suitable hidden root has
been selected. Compilation refuses such an uncontained path rather than
silently omitting it. For contained paths, compilation uses the existing
preservation/restoration plan and creates generated endpoints in the private
tmpfs before remounting it read-only. ``NamespaceSandbox`` validates and
compiles the entire policy before ``unshare``. The mount-plan staging directory
must be outside every hidden root so applying a mask cannot shadow its own
sources. Preserved sources are rechecked for existence and type immediately
before mounting and are never recreated if they disappear. File-descriptor
anchoring against a concurrent same-type source substitution remains future
hardening; policy construction and application therefore remain trusted,
single-threaded installer work.

The dormant selected-tree compiler makes the next boundary explicit. Trusted
setup must provide nonempty selections of hidden host roots, exact host
compiler executables, build tools, header trees, runtime paths, and scoped
temporary directories. Non-external compiler and tool packages already appear
as concrete dependency prefixes and remain read-only. Active
package-repository roots come from the repository search path. Spack's
``bin``, ``lib``, ``share/spack``, and ``etc/spack`` trees are separate
read-only grants so the store and writable state below the Spack prefix are not
accidentally exposed. Spack and upstream ``sbang`` paths are required when
selected rather than silently omitted.

The policy data for this selection is loaded lazily from the versioned,
read-only files ``share/spack/sandbox/sandbox.yaml`` and
``share/spack/sandbox/linux-header-policy.yaml``. The first file contains the
runtime and compiler vocabulary plus the namespace hidden roots, replacement
roots, device nodes, and stage programs. It deliberately has no Landlock
``commands`` or ``df`` stub. The second file contains only safe relative
entries below its absolute system include root for the glibc, Linux UAPI, and
libstdc++ header policy. Loaders validate versions, list and alias structure,
namespace path types, and header path traversal before any later selection
work consumes the data. Loading is currently dormant: this commit does not
activate the policy or change the live worker.

A2 adds dormant selection helpers on top of these files. They select compiler
languages represented by concrete DAG edges and supported compiler nodes,
deduplicate repeated selections, and use the header policy to enumerate exact
glibc, Linux UAPI, GCC-internal, and libstdc++ trees. Non-GCC C++ selection is
capped at the configured safe libstdc++ major, while GCC selection follows its
reported installation. The helper also identifies other GCC installations for
later masking. These results are evidence for later policy compilation only;
the worker does not call the helpers.

A3 adds dormant subordinate-input selection. Compiler ``-print-prog-name`` and
``-print-file-name`` answers are accepted only when absolute; bare answers are
not resolved through ambient ``PATH``, and ``libexec/spack`` binutils wrappers
are not selected in preference to real binutils. The selectors preserve each
searched spelling separately from its canonical source, including compiler
driver aliases for later generated symlinks. They also select ``cpp``'s
``cc1``, ``file`` magic data, Git's configured ``--exec-path`` directory, the
fetch/expansion tool set and script-helper closure, and link/run dependency
prefixes for Spack-built tools. A3 records these inputs but does not create
symlinks or activate the policy; B1 consumes these records as explicit
passthrough, replacement, and generated-symlink entries.

B1 completes the dormant policy-entry boundary. Explicit hidden roots are
provided by trusted setup rather than derived from every mount target, so a
selected path outside those roots remains visible passthrough and does not
silently hide a parent such as ``/usr/lib`` or ``/tmp``. A caller-provided
replacement directory can be mounted at a hidden root, with nested selected
paths restored afterward. A3 compiler spelling/source records become generated
symlink entries: the alias path remains lexical, while its target is the
canonical compiler source. The planner creates those symlinks in the private
mask source before it is made read-only. These entries are validated and
compiled but remain dormant; scoped replacement-source allocation and policy
activation are later work.

B2 adds a supervisor-owned ``NamespaceMountPlanScratch`` lease for the
planner's durable endpoint tree. Allocation requires a canonical, real
directory base, creates a unique mode-0700 directory outside every hidden,
replacement, stage, prefix, and writable-policy root, and rejects allocation
collisions. The supervisor may clean the lease after setup failure or after an
attached worker is no longer alive; cleanup refuses live workers and refuses
paths replaced by a symlink or another inode. This scratch is ordinary
host-backed setup state, separate from the host-visible stage and prefix.

B3 completes the dormant access model for selected-tree plans. Such a plan
marks the inherited mount tree read-only recursively with
``MOUNT_ATTR_RDONLY`` before applying its bind restores. Read-write policy
mounts explicitly clear that attribute on their bind views, while read-only
mounts retain it. The capability probe and disposable namespace tests verify
that recursive inherited mounts can be made read-only, passthrough writes
return ``EROFS``, and selected writable mounts remain writable. The live
worker still uses the narrow mask and transitional Landlock.

C1 preserves the host-visible lifecycle of build stages and install prefixes
when the selected-tree policy is eventually activated. Trusted supervisor
setup gives each build a unique host-backed parent below the configured stage
root and places the removable stage directory below that parent. The child
can therefore restage and write ``spack-src`` and ``config.log`` without
making the stage itself a mount point. The supervisor removes successful
stages after the child is reaped, retains failed stages for inspection, and
discards unused stage parents for binary-cache retries.

The supervisor also performs the prefix pivot before launch and creates the
empty target at the exact path used by build tools. Prefix rollback, garbage
removal, ``keep-prefix``, and ``BinaryCacheMiss`` handling happen only after
the child namespace is gone, while the per-prefix write lock remains held
through the transaction. The live worker still uses the narrow mask and
transitional Landlock; this lifecycle boundary prepares it for the complete
policy without activating that policy.

C2 adds a dormant trusted-input boundary for the eventual selected-tree
policy. It canonicalizes explicit hidden and replacement roots, combines the
versioned runtime candidates with the host dynamic-linker search paths, keeps
only real character devices from the device list, and selects read-only
Spack, repository, dependency, configuration, and cache paths. Stage, prefix,
log, jobserver, fetch-cache, and scoped-worker paths are explicit writable
inputs and fail closed when missing or non-canonical. Optional host candidates
are omitted when unavailable. Selection does not mutate the worker or activate
the broader policy; pre-thread validation is the next boundary.

C3 makes validation a configuration-free boundary. Whenever selected paths
and mount-plan scratch are supplied, the immutable selected policy and mount
plan are always compiled before capability freezing, sandbox acquisition, or
namespace mutation. Invalid paths therefore abort worker setup rather than
falling back to an unconstrained worker. The dormant live worker still supplies
neither input, so it remains on the narrow mask until disposable real compiler
and source-build evidence is recorded.

The resulting policy must compile with allocated mount-plan scratch outside
every hidden root, and hidden roots may not overlap the planner's reserved
source subtrees. Representative synthetic-host tests cover explicit masks,
replacement roots, generated aliases, and selected compiler, tool, header,
runtime, dependency, repository, Spack-source, stage, prefix, device, and
temporary paths, including an intentionally ancestor-covered header subtree.
They also prove concurrent scratch allocation, symlinked bases, allocation
collisions, disappeared sources, stale-path replacement, and uncovered
selected paths fail closed.

The worker does not activate the installer-derived policy yet. It continues to
use only the narrow ``/usr/share/aclocal`` mask and transitional Landlock. The
selected-tree compiler is not called by the worker because trusted setup does
not yet materialize the complete hidden host and device policy, exact host
compiler, tool, header, and runtime set, canonical aliases, or a scoped
replacement for the broad system temporary directory. Only paths below
selected hidden roots are absent; unrelated host
trees remain visible, so this helper is not yet a namespace-only replacement
for Landlock. The complete policy derives from:

* The concrete spec's selected compilers and build tools.
* The host's available header trees and compiler installations.
* The package-repository roots and Spack source tree.
* The stage, prefix, and temporary directories.

A path that is not allowlisted is absent below a hidden root. Once every
required path is materialized, represented, compilable, and validated against
real builds, the namespaced worker can activate this tree and stop applying
Landlock by default.

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
* [x] The current narrow mask is represented by an immutable, validated,
  deterministic mount plan before namespace mutation.
* [ ] The complete mount tree is derived from policy, not a fixed list.
* [x] The internal Linux backend confines only the existing forked install
  child; the trusted installer supervisor remains outside the namespace.
* [ ] External Linux sandbox tools use a separate launcher without changing
  the internal backend's process model.
* [ ] A future Windows worker that confines recipe Python is created directly
  inside AppContainer; this platform-specific requirement does not constrain
  Linux launch design.
* [ ] Namespace mounts provide the complete default filesystem policy;
  Landlock is opt-in for permission-denied behavior testing.
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
