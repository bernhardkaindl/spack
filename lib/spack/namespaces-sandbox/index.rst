Namespace Backend - Capability Probe
====================================

Motivation: why a namespace backend
------------------------------------

The Linux Landlock security module provides a way to enforce filesystem access
control policies at the kernel level. Landlock's filesystem model is a
deny-by-default policy with explicit allow rules. A denied path produces
``-EPERM``. Some build tools and configure scripts do not tolerate
``Permission denied`` or ``Operation not permitted`` when accessing a path.

They may abort or behave differently when a file or directory exists but
can't be opened, versus when it does not exist.

Linux mount namespaces solve a different problem than Landlock.
Mount namespaces can replace host directory trees with empty filesystem views,
so denied content is not visible at all instead of producing ``-EPERM``.
The existing Landlock stack remains in force; the namespace backend only adds
the ability to truly hide host filesystem content rather than only denying
access to it.

For that reason the namespace backend is the preferred confinement backend
when available, with Landlock-only as the fallback.

Availability probe (Phase 1)
----------------------------

Namespaces are widely available to unprivileged users, but on some hosts,
they could be restricted by the host policy e.g. to interactive shell sessions.

Therefore, this series starts with a capability probe:

* ``namespace_sandbox_available(libc=None) -> bool`` runs in a disposable
  child process created with ``fork``. The child calls
  ``unshare(CLONE_NEWUSER | CLONE_NEWNS)``, writes the UID and GID mapping
  for the invoking user, and makes the root mount private
  (``MS_REC | MS_PRIVATE``). The child reports success or failure through a
  pipe and exits, so the parent is never affected.
* Later phases can use it to select the Landlock-only fallback.

No behavior of existing sandboxes changes in this phase. There is no mount
tree, no ``NamespaceSandbox`` backend, and no installer integration yet;
later phases build on top of ``namespace_sandbox_available()``.

The namespace backend launches the worker as a new executed instance inside
the namespaces, rather than forking the trusted parent. That matches the
Windows sandbox model, where the worker also starts as a new executed
process with restricted handles.
