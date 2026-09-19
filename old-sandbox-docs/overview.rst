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
* their Python import roots; and
* the Spack Python source tree.

Landlock grants read and execute access only to these roots and denies writes.
Seccomp denies socket operations, process creation and execution, and IPC.
The worker sets ``PR_SET_NO_NEW_PRIVS``, enforces memory rlimits, and closes inherited descriptors before confinement.
