..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Solved Sandboxing Issues
========================

This page records resolved sandbox failures whose symptoms did not identify the denied capability.
Each entry preserves the observed failure, root cause, narrow policy change, remaining boundary, and regression coverage for reviewers.

.. _sandbox-solved-rust-cargo-socketpair:

Rust and Cargo subprocess launch
--------------------------------

Cargo initially reported ``could not exec the linker cc`` with ``Operation not permitted``, even though the generic ``cc`` executable correctly entered Spack's compiler wrapper and selected the C compiler from ``SPACK_CC``.
A native syscall trace showed that the failure occurred before ``execve``: Rust creates an anonymous ``AF_UNIX`` ``SOCK_SEQPACKET`` socket pair and transfers a small message across it to report child-launch errors.
The build network sandbox denied ``socketpair`` and, after pair creation was narrowed to the Unix domain, still denied the connected descriptor operations used to transfer that message.
The corrected seccomp policy allows only ``AF_UNIX`` socket pairs and permits message I/O on descriptors that are already open.
This does not grant addressable Unix or external network access: the worker closes inherited descriptors before confinement, and seccomp continues to deny ordinary ``socket`` creation as well as ``connect``, ``bind``, ``listen``, acceptance, and address-bearing send operations.
A real-kernel regression creates an ``AF_UNIX`` ``SOCK_SEQPACKET`` pair, verifies a message round trip, and separately proves that ordinary Unix socket creation still fails.
Unit tests also verify the exact seccomp comparison that denies every non-``AF_UNIX`` socket-pair domain.
An isolated native Cargo replay subsequently completed the Rust bootstrap manifest build, confirming that ``rustc``, the generic ``cc`` wrapper, the selected compiler, ``collect2``, and ``rust-lld`` can all launch under the resulting policy.

.. _sandbox-solved-empty-aclocal:

Empty host Autoconf macro directory
-----------------------------------

``isa-l`` invoked ``aclocal`` with ``-I /usr/share/aclocal`` and failed with ``Permission denied`` because Landlock correctly denied the host directory.
Returning ``ENOENT`` is not compatible with tools that expect an existing macro search directory.
The build worker now enters a private user and mount namespace before starting worker threads, then bind-mounts an empty stage-owned directory over ``/usr/share/aclocal`` before Landlock is applied.
The build observes an empty directory instead of a ``-EPERM`` failure and cannot use host Autoconf macros.
The mask is skipped when the concrete DAG provides an external ``autoconf`` dependency, because a host Autoconf legitimately uses its own system macro directory.

This Linux-specific workaround demonstrates masking selected host directories as empty without granting them access or returning ``-EPERM``.
It requires unprivileged user and mount namespaces; when they are unavailable, Spack warns and preserves the existing Landlock behavior.
A focused real-kernel test verifies that the bind mount appears empty, and ``isa-l`` completes with the mask enabled.

The preferred solution is the package-level change in `spack/spack-packages#6277 <https://github.com/spack/spack-packages/pull/6277>`_, which avoids this host path and does not require Linux mount namespaces.
