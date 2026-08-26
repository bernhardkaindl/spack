..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

Roadmap
=======

The migration is additive.
Command behavior remains unchanged until its confined replacement is validated.

Subsequent command-hardening milestones
---------------------------------------

``spack install`` worker boundary
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Build on the checksum worker boundary to harden normal installation.
Source and patch staging are implemented as described in :doc:`status` and :doc:`install-worker`.
The next scope is build-phase confinement.
Later scope may include selected package build-time downloads.

The trusted parent retains:

* command parsing;
* policy selection;
* response validation;
* terminal output; and
* package state mutation.

The worker reuses existing ``Stage`` and fetcher abstractions.
It must not introduce a second fetch implementation.

Build-phase confinement
^^^^^^^^^^^^^^^^^^^^^^^

* Grant selected compilers, build tools, dependency prefixes, stage, and install-prefix access without opening their parent directories.
* Preserve existing hooks, phases, logs, metadata archiving, prefix commit, and database behavior.
* Keep build-time network access denied by default.
* Replace the current unlimited installer worker memory profile with the measured Linux admission,
  throttling, and recovery design in the adaptive build memory scheduling plan.

Build-time downloads
^^^^^^^^^^^^^^^^^^^^

Some packages download additional files during build or installation.
Do not grant this capability by default.

An administrator-controlled policy must identify eligible packages and allowed destinations before implementation.
The worker must still use the proxy for every outbound connection.

Keep the local proxy and sanitized HTTP, HTTPS, and FTP proxy environment active whenever build
networking is disabled, including when learning is disabled and no destination group matches.
The denying proxy must reject an attempted download promptly with an isolation-specific diagnostic
instead of allowing DNS failures, connection hangs, or generic hostname-resolution errors.

Report each new canonical destination immediately whether it is allowed or denied.  When learning
is disabled, reporting must never update configuration.  If a package fails after one or more
denied or unlisted attempts, repeat the deduplicated network warnings after that package's failure
output so they remain visible beside the final error.  Tests must cover bounded denial latency,
warning deduplication and placement, unchanged configuration, and cleanup of proxy and seccomp
notification resources after both success and failure.

Checksum parity coverage
~~~~~~~~~~~~~~~~~~~~~~~~

The implemented checksum boundary needs additional compatibility coverage:

* Test recipe-defined ``fetch_options`` supported by urllib.
* Preserve ``--keep-stage`` cleanup and retention behavior.
* Test cancellation, worker timeout, proxy timeout, and descriptor cleanup.
* Extend live passive-FTP coverage beyond ``ftp.alsa-project.org`` and ``ftp.gnu.org`` when representative servers remain available.
* Run real seccomp-notification tests on supported Linux kernels.
  Retain explicit fallback tests for hosts without user notification.

Later command boundaries
~~~~~~~~~~~~~~~~~~~~~~~~

* Harden ``spack spec``, environment concretization, and implicit install concretization through the shared :doc:`concretizer-worker` plan.
* Continue installation and staging hardening through :doc:`install-worker`.
* See :doc:`planned-work` for namespace isolation and later assessments.

Open design assessments
-----------------------

The checksum integration permits arbitrary public HTTP, HTTPS, and FTP authorities.
It does not impose the future install whitelist.

See :doc:`design-decisions` for administrative URL policy, configured-mirror operation, redirect treatment, and configuration precedence.
Neither integration may weaken recipe-import confinement or introduce a second fetch implementation.
