..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

``spack info`` Sandbox Boundary
===============================

``spack info`` evaluates package recipes in a confined child process.
The parent accepts only the worker response; it never forwards child standard output.

Human-readable output
---------------------

Normal ``spack info`` runs the existing renderer in the child and returns its text as one JSON string.
The parent validates the response type and writes it.

Bounded worker launcher
-----------------------

The POSIX launcher uses dedicated pipes for one JSON request and response.

.. list-table:: Worker transport limits
   :header-rows: 1
   :widths: 30 70

   * - Property
     - Contract
   * - Encoding
     - UTF-8 JSON with an eight-byte length prefix.
   * - Response limit
     - 4 MiB.
   * - Failure diagnostics
     - Validated exception, traceback, and up to 1 MiB of captured standard
       output and error.
   * - Deadline
     - 120 seconds.

Malformed, duplicate-key, oversized, and failed responses are rejected.
Child standard streams are redirected to ``/dev/null``.

Fallback behavior
-----------------

The shared :ref:`sandbox-fallback-policy` applies.
Recipe-import confinement is required by default.

.. list-table:: Fallback settings
   :header-rows: 1
   :widths: 35 65

   * - Setting
     - Behavior
   * - ``allow_fallback``
     - Uses the existing in-process renderer when recipe-import confinement is
       unavailable.  It does not create an unconstrained worker.

On non-Linux platforms, the command fails unless trusted direct fallback is enabled.

Recipe-import confinement policy
--------------------------------

Before recipe import, the parent captures trusted repository roots, Python import roots, and the Spack source tree needed for lazy imports.
Landlock grants read and execute access only to these roots and denies writes.
The filesystem rules require Linux Landlock, but no particular Landlock network ABI.

Before import, the worker:

* sets ``RLIMIT_AS`` to the default 1 GiB ceiling;
* closes inherited descriptors except its JSON pipes;
* sets ``PR_SET_NO_NEW_PRIVS``; and
* installs a fail-closed ``EPERM`` seccomp filter when network blocking is used.

The filter blocks TCP, UDP, ICMP, Unix-domain sockets, process creation and execution, and SysV/POSIX IPC.
