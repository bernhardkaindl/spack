..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Planned Work
============

This page records later sandbox work that is not an immediate command milestone.

Shared user and network namespaces
----------------------------------

Evaluate a per-invocation unprivileged user and network namespace shared by the proxy and worker.
This can hide the proxy loopback listener from unrelated host-namespace processes.

.. list-table:: Namespace design questions
   :header-rows: 1
   :widths: 30 70

   * - Topic
     - Assessment
   * - Availability
     - Optional.  Some distributions disable unprivileged user namespaces or
       restrict user/group ID mapping.
   * - Process layout
     - A trusted launcher creates the namespaces and starts both processes
       inside them.  Proxy and worker do not need a parent-child relationship.
   * - Supervisor
     - The seccomp supervisor can remain outside the shared namespace while it
       retains the notification and pidfd handles.
   * - Fallback
     - When unavailable, retain the authenticated loopback proxy design.
       Do not weaken worker socket mediation.
