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

.. _sandbox-solved-gcc-16-headers:

GCC 16 headers selected by older Clang
--------------------------------------

Clang selects the newest compatible host GCC installation when it needs the GNU C++ standard library.
An older Clang can therefore select GCC 16 headers even when an older GCC installation and header tree are available, and some builds fail because that Clang cannot parse the newer headers.

Landlock grants are additive below an allowed directory, so the build policy cannot grant ``/usr/include`` and then deny only ``/usr/include/c++/16``.
The build policy no longer grants ``/usr/include``.
It loads a versioned immutable policy from ``share/spack/sandbox/linux-header-policy.yaml`` and grants only named glibc files and subdirectories, named Linux UAPI subdirectories, target-specific equivalents, and one selected libstdc++ header tree.
The runtime policy does not query ``dpkg``, RPM, APK, or another distribution package database.
Missing policy paths are ignored, which lets one relative-path inventory cover glibc-based Linux distributions with different header layouts.

The selected compiler reports its GCC installation through ``-print-libgcc-file-name``.
GCC receives the matching libstdc++ version.
Other C++ compilers receive the newest installed libstdc++ version no newer than the policy's compatibility ceiling, currently GCC 15.
The worker masks competing host GCC installation candidates so Clang's GCC detector selects the same version that Landlock permits.
Both target-first and version-first multiarch libstdc++ layouts are supported.
It does not change compiler selection recorded in the concrete spec.

This compatibility policy uses the private mount namespace prepared before worker threads start.
If unprivileged user and mount namespaces are unavailable, Spack emits the existing masking warning.
Landlock still denies non-allowlisted header paths, but Clang may select a masked-out GCC installation and fail instead of falling back.
Other namespace and mount errors still fail before recipe-controlled build phases run.

Focused policy tests prove that the ``/usr/include`` parent is absent, required libc and Linux roots are present, only the selected libstdc++ version is granted, GCC 16 receives its own headers, and competing GCC installations are selected for masking.
The existing real-kernel masking test proves that every selected directory appears empty inside the private namespace.
