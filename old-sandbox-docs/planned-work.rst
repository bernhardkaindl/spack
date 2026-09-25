..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Planned Work
============

This page records later sandbox work that is not an immediate command milestone.

Shared user and mount namespaces
--------------------------------

Evaluate a per-invocation unprivileged user and mount namespace shared by the proxy and worker, as described in :doc:`namespace-backend`. This can hide the proxy loopback listener from unrelated host-namespace processes and replace host ``bin``, ``include``, and other directories with empty tmpfs mounts, bind-mounting only whitelisted content into the visible tree.

The namespace backend is the preferred Linux confinement backend when available. It complements the existing Landlock and seccomp stack by providing true filesystem hiding rather than only denial. When unavailable, the existing Landlock-only backend is selected as a constrained fallback. The shared ``config:sandbox:allow_fallback`` setting applies only when no sandbox worker is available and trusted direct execution is considered.

The internal Linux backend self-restricts the existing forked install child, leaving the trusted installer supervisor outside the namespace. A future external launcher such as Bubblewrap may use a fresh executed worker. A future Windows worker that confines recipe Python must be created inside AppContainer, but that platform-specific requirement does not determine the Linux process model.

.. list-table:: Namespace design questions
   :header-rows: 1
   :widths: 30 70

   * - Topic
     - Assessment
   * - Availability
     - Optional.  Some distributions disable unprivileged user namespaces or
       restrict user/group ID mapping.
   * - Process layout
     - Before starting child-local threads, the existing forked Linux install
       child completes trusted namespace mount setup and drops namespace
       capabilities. It applies Landlock before build phases. External launcher
       backends may instead execute a fresh worker.
   * - Supervisor
     - The seccomp supervisor can remain outside the shared namespace while it
       retains the notification and pidfd handles.
   * - Mount tree
     - The setup child makes the root mount private, mounts empty tmpfs over
       selected host directories, and bind-mounts only whitelisted programs,
       headers, and paths.  Landlock is applied inside the namespace against the
       visible mount tree.
   * - Fallback
     - When unavailable, retain the authenticated loopback proxy design and the
       existing Landlock-only backend.  Do not weaken worker socket mediation.
