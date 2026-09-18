..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Planned Work
============

This page records later sandbox work that is not an immediate command milestone.

Shared user and mount namespaces
--------------------------------

Evaluate a per-invocation unprivileged user and mount namespace shared by the proxy and worker, as described in :doc:`namespace-backend`. This can hide the proxy loopback listener from unrelated host-namespace processes and replace host ``bin``, ``include``, and other directories with empty tmpfs mounts, bind-mounting only whitelisted content into the visible tree.

The namespace backend is the preferred Linux confinement backend when available. It complements the existing Landlock and seccomp stack by providing true filesystem hiding rather than only denial. When unavailable, the existing Landlock-only backend is used subject to ``config:sandbox:allow_fallback``.

The Windows sandbox implementation starts the worker as a new executed instance with restricted handles rather than forking the trusted parent. That matches the Linux namespace backend's new-instance model: the trusted parent creates protocol endpoints, then launches a fresh process that applies confinement before importing any recipe code.

.. list-table:: Namespace design questions
   :header-rows: 1
   :widths: 30 70

   * - Topic
     - Assessment
   * - Availability
     - Optional.  Some distributions disable unprivileged user namespaces or
       restrict user/group ID mapping.
   * - Process layout
     - A trusted launcher creates the namespaces and starts the worker as a new
       executed instance inside them.  The worker does not fork from the trusted
       parent.
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
