..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

``spack checksum`` Worker Boundary
===================================

``spack checksum`` imports recipes to discover source URLs and evaluate ``url_for_version``.
It can search remote versions, prompt for selection, download archives, and modify ``package.py``.
On supported Linux systems, remote discovery and archive fetching run in separate confined workers behind the local network supervisor.

Trust boundary
--------------

The trusted parent owns:

* command-line parsing and network policy;
* worker response validation;
* interactive selection and terminal output; and
* recipe editing and editor launch.

The discovery worker imports the requested recipe after confinement is active.
It receives a bounded request and returns validated JSON only.
It performs sequential URL discovery through the local proxy.

The fetch worker receives selected version/URL pairs and fetch options.
It returns version/SHA256 pairs only.

The parent must not treat child standard output as a response.
It accepts only the generic worker launcher's bounded JSON response and its bounded failure diagnostics.

Shared fallback policy
----------------------

The shared :ref:`sandbox-fallback-policy` applies to both recipe-import and network-worker capability failures.
With fallback disabled, unavailable network supervision fails before recipe evaluation or downloads.
With fallback enabled, checksum retains its existing direct recipe and download paths.

Protocol
--------

.. list-table:: Worker protocol
   :header-rows: 1
   :widths: 30 70

   * - Message
     - Contents
   * - Discovery request
     - Package name, explicit versions, ``--latest``, and ``--preferred``.
       No writable recipe path or edit instruction.
   * - Discovery response
     - Bounded version-keyed URLs, known versions, URL-change markers, fetch
       options, and package name.
   * - Fetch request
     - Parent-selected URLs, package name, fetch options, and ``--keep-stage``.
   * - Fetch response
     - Version/SHA256 pairs.

The parent validates schema, bounds, package identity, version strings, and URL strings before use.
The worker preserves computed-URL precedence before returning its final set.

Remote version discovery
------------------------

Remote discovery cannot run in the no-network recipe worker.
``find_valid_url_for_version`` tests recipe-derived URLs with network requests.
``fetch_remote_versions`` evaluates ``url_for_version`` while ranking spidered links.
Direct sockets would let recipe code contact arbitrary endpoints.

A download-capable worker instead uses the :doc:`network-supervisor` boundary.
Seccomp user notifications intercept each worker ``connect``.
The trusted supervisor connects that socket only to its local proxy.
The proxy performs DNS and owns every outbound connection.
The original syscall never continues.
This is a download capability, not recipe-worker network permission.

Checksum permits syntactically valid HTTP, HTTPS, and FTP recipe sources.
The proxy rejects resolved loopback, private, link-local, metadata, and other non-global addresses.
This is not the future administrative install whitelist.
Installer integration may add URL-prefix or mirror policy without changing the worker-to-proxy boundary.

Discovery and fetching are sequential in this initial integration.
The worker forces the urllib fetch path because process execution is denied.

Interactive selection
---------------------

In interactive mode, the parent passes the validated candidate set and known versions to the existing ``interactive_version_filter``.
User prompts and decisions never run in the worker.
The parent sends only the selected version/URL pairs to the fetch phase.
Empty selection preserves the current successful early exit.

Fetching and recipe updates
---------------------------

The fetch worker reuses ``get_checksums_for_versions`` and ``Stage(URLFetchStrategy).fetch()`` with concurrency one.

Its confinement grants:

* read access to repository and Spack source roots; and
* write access only to the selected stage root.

Seccomp denies process and IPC creation.
User notifications mediate socket creation and connection.
Bypass operations are denied after listener transfer.

The parent renders version directives and compares ``--verify`` output against validated recipe versions.
For ``--add-to-package`` it resolves the package file, applies ``add_versions_to_pkg``, and launches the editor.
A worker never receives recipe write permission.

Checksum downloads reach ``Stage(URLFetchStrategy).fetch()`` through ``get_checksums_for_versions``.
Package staging reaches the same ``Stage.fetch()`` primitive through ``pkg.do_stage()``.
Future network workers must reuse this path.

Compatibility and limits
------------------------

The command probes network-supervision support in a disposable child.
It never installs irreversible seccomp policy in the trusted parent.
When unavailable, checksum retains its existing direct recipe-worker and in-process paths only when shared fallback is enabled.
Failures after a supported path is selected fail closed.

Focused tests must prove that recipe output cannot alter parent paths, prompts, editor execution, or recipe writes.
They must also reject malformed, duplicate-key, oversized, and failed responses before fetch or mutation.
URL fallback tests must preserve computed-URL precedence over spidered URLs.

HTTPS remains an authority-only tunnel.
The proxy cannot enforce paths without terminating TLS.
Checksum is intentionally broader than future installer policy.
See :doc:`design-decisions`.
