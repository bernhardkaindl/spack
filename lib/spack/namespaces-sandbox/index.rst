..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Namespace Backend - Capability Probe
====================================

Status summary
--------------

Phase 1 (capability probe), Phase 2 (mount-tree helpers), and Phase 3
(backend selection and installer integration) are complete.

The ``NamespaceSandbox`` class, ``get_sandbox`` selection, and the
installer's mount-tree preparation are wired.  The full build-tool policy,
and policy-driven bind mounts of whitelisted compilers, tools, and headers
remain unimplemented.  Phases 4 and later (policy-driven mount tree,
build-phase confinement, concretizer worker) have not been started.

Motivation: why a namespace backend
------------------------------------

The Linux Landlock security module provides a way to enforce filesystem access
control policies at the kernel level. Landlock's filesystem model is a
deny-by-default policy with explicit allow rules. A denied path produces
``-EPERM``. Some build tools and configure scripts do not tolerate ``Permission
denied`` or ``Operation not permitted`` when accessing a path.

They may abort or behave differently when a file or directory exists but can't
be opened, versus when it does not exist.

Linux mount namespaces solve a different problem than Landlock. Mount
namespaces can replace host directory trees with empty filesystem views, so
denied content is not visible at all instead of producing ``-EPERM``. The
existing Landlock stack remains in force; the namespace backend only adds the
ability to truly hide host filesystem content rather than only denying access
to it.

For that reason the namespace backend is the preferred confinement backend
when available, with Landlock-only as the fallback.

Availability probe
------------------

Namespaces are widely available to unprivileged users, but on some hosts they
could be restricted by the host policy, e.g. to interactive shell sessions.

The implementation starts with a capability probe:

* ``namespace_sandbox_available(libc=None) -> bool`` runs in a disposable child
  process created with ``fork``. The child calls
  ``unshare(CLONE_NEWUSER | CLONE_NEWNS)``, writes the UID and GID mapping for
  the invoking user, and makes the root mount private (``MS_REC | MS_PRIVATE``).
  The child reports success or failure through a pipe and exits, so the parent
  is never affected.
* Later phases use it to select the Landlock-only fallback.

No behavior of existing sandboxes changes. There is no mount tree,
no ``NamespaceSandbox`` backend, and no installer integration yet.

Mount-tree helpers
------------------

The mount-tree layer enters the same private user and mount namespace when
needed and then replaces selected host directories with empty views:

* ``prepare_empty_directory_masking(paths, libc=None) -> bool`` enters the
  private namespace for masking, returning ``True`` once it is active or when
  *paths* is empty, and ``False`` when unprivileged namespaces are denied.
* ``hide_directories_as_empty(paths, stage_path, namespace_ready=False,
  libc=None) -> bool`` covers each existing directory in *paths* with a
  bind-mount of an empty directory created under
  ``stage_path/spack-empty-host-dirs/<index>``. Sandboxed tools then see an
  empty directory (``ENOENT`` for missing entries) instead of the host tree.
  When *namespace_ready* is ``False`` it first enters the private namespace
  via ``prepare_empty_directory_masking``; it returns ``True`` on success and
  ``False`` when the namespace backend is unavailable so callers can keep the
  Landlock-only fallback.

Backend selection and installer integration
-------------------------------------------

Phase 3 wires the helpers into the existing sandbox selection and installer
paths.

What is wired:

* ``NamespaceSandbox(Sandbox)`` — backend combining the private namespace
  with Landlock. ``prepare_mount_tree(hidden_dirs, stage_path)`` enters the
  namespace and masks the listed host directories as empty before any Landlock
  rule is granted; ``allow_read``/``allow_write`` delegate to an internal
  ``LandlockSandbox`` so ``apply`` confines the namespace-restricted tree;
  ``bind_mount(source, target=None)`` is the primitive to re-expose whitelisted
  content.
* ``get_sandbox()`` returns ``NamespaceSandbox`` on Linux when the probe
  succeeds, and ``LandlockSandbox`` otherwise.
* ``spack.installer.build.default_hide_as_empty_dirs(spec)`` — interim,
  policy-free list hiding ``/usr/share/aclocal`` unless the spec has an
  external ``autoconf`` dependency.
* ``_enable_sandbox`` calls ``prepare_mount_tree`` with that list before
  granting Landlock rules, and warns instead of failing when the namespace
  turned out unavailable after selection.

Still incomplete:

* The full build-tool policy (selected compilers, expansion tools, support
  paths) that a complete installer boundary needs.
* Policy-driven bind mounts and build-phase confinement; the concretizer-worker
  evaluation has not started.
