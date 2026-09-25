..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Overview
========

Package recipes are Python modules maintained in separate repositories.
They run as executable code in the user's context.
Development requires users to be able to modify them.
Sandboxing prevents a recipe import from modifying the rest of the system.

Scope
-----

The project incrementally hardens existing normal command paths.
It does not introduce parallel command implementations.

The trusted parent owns:

* command-line parsing and policy selection;
* worker launch and response validation; and
* terminal presentation and user interaction.

A less-trusted worker imports recipe code only after confinement is active.
It communicates through a bounded, structured transport.

.. _sandbox-fallback-policy:

Shared fallback policy
----------------------

Sandboxed commands select their worker automatically when its required confinement capabilities are available.
``config:sandbox:allow_fallback`` controls whether an unavailable worker may use the command's existing trusted direct path.
It defaults to ``false``.

With fallback disabled, capability probing fails before recipe or package work and identifies the unavailable worker capability.
With fallback enabled, the command invokes its existing launcher-neutral operation directly.
It never launches an unconstrained worker.

Linux confinement
-----------------

Before launching a worker, the parent selects:

* configured package-repository roots;
* their Python import roots;
* the Spack Python source tree; and
* the concrete-spec-selected compilers, build tools, headers, and dependency
  prefixes.

When available, the parent prefers a Linux user and mount namespace backend
described in :doc:`namespace-backend`.  The current narrow integration masks
``/usr/share/aclocal`` when an external Autoconf does not require it. Phase 4
will hide host ``bin``, ``include``, and other directories and bind-mount only
the whitelisted programs, headers, and paths the worker needs. The completed
namespace policy is the default filesystem confinement and makes unavailable
paths naturally return ``ENOENT``. Landlock inside the namespace is reserved
for opt-in testing of permission-denied behavior. The current narrow
integration still applies it until the policy-driven mount tree is complete.

When unprivileged user and mount namespaces are unavailable, the parent uses
the existing Landlock-only backend.  Landlock grants read and execute access
only to the selected roots and denies writes.  Seccomp denies socket
operations, process creation and execution, and IPC.  The worker sets
``PR_SET_NO_NEW_PRIVS``, enforces memory rlimits, and closes inherited
descriptors before confinement.

The internal namespace backend self-restricts the existing forked Linux install
child; the trusted installer supervisor remains in the host namespaces.
External launchers such as Bubblewrap may use a fresh executed worker as an
alternate backend. Windows AppContainer has its own process-creation
requirements and does not determine the Linux launch model. See
:doc:`namespace-backend` for the platform-specific process boundaries and the
Linux mount-tree design.
