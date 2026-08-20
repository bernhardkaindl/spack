..
   Copyright Spack Project Developers. See COPYRIGHT file for details.

   SPDX-License-Identifier: (Apache-2.0 OR MIT)

.. meta::
   :description lang=en:
      Developer documentation for recipe-free provenance verification in the experimental sandboxed installer.

.. _sandboxed-installer-provenance:

Sandboxed Installer Provenance
==============================

The experimental worker-based installer executes less-trusted package recipe code in a fresh process confined with Linux Landlock.
The trusted parent retains source staging, prefix transactions, post-install actions, metadata publication, and database registration.
This path is additive and is not yet used by the normal ``spack install`` workflow.

An install produced across that boundary needs a durable account of what the parent validated.
The parent therefore writes ``.spack/sandbox_provenance.json`` before creating the install manifest.
The manifest covers the provenance file along with the rest of the installed metadata.

What the record binds
---------------------

The versioned provenance record contains only bounded, recipe-free data:

* the concrete spec DAG hash and package hash;
* the validated SourcePlan and its SHA-256 digest;
* ordered repository identities;
* the worker protocol version and applied sandbox restrictions;
* the ordered build phases and prepared-stage identities before and after the build;
* the build-log size and digest, without its parent-only host path;
* the install-tree identity returned by the worker;
* the ordered allowlisted parent actions; and
* the install-tree identity after those actions.

The two install-tree identities intentionally can differ.
For example, an allowlisted parent action can normalize ELF RPATHs after the confined worker exits.
Recording both values preserves the boundary between worker output and trusted parent mutation.

Why verification is needed
--------------------------

Writing JSON is not sufficient evidence by itself.
A later consumer must be able to reject a truncated, oversized, malformed, internally inconsistent, or unrelated record without importing the package recipe that produced the installation.
Verification provides that reusable boundary.

``spack.installer.install_metadata`` separates verification into three operations:

``read_install_provenance(prefix)``
   Opens the metadata directory and provenance file through bounded, no-follow descriptors, rejects duplicate JSON keys, and limits the encoded record to one MiB.

``validate_install_provenance(spec, provenance)``
   Enforces exact versioned fields and bounds, validates hashes and install-tree identities, revalidates the SourcePlan, checks canonical parent actions, and binds the record to the expected concrete spec.

``verify_install_provenance(spec, prefix)``
   Checks the installed files against Spack's install manifest and then applies semantic provenance validation.
   It does not resolve ``spec.package`` or import repository recipes.

The same install-tree identity validator is used for live worker responses and persisted records.
This prevents the accepted representation from drifting between installation and later verification.

Security boundary and limitations
---------------------------------

Manifest verification establishes consistency between the current prefix and the locally stored manifest.
It detects changes to the provenance file when the manifest is unchanged.
The manifest is not signed, so it is not an authenticity proof against an actor that can rewrite both the prefix and its manifest.

The provenance record also does not retain source archives, build logs, repository snapshots, or host-only paths.
Their digests identify the artifacts, but a consumer needs separately retained content to compare those identities later.
Verification likewise does not replay a build or claim that two builds are reproducible.

Future uses
-----------

The stable, recipe-free verifier is intended as a foundation for later capabilities:

* signed attestations that sign a canonical provenance digest with a configured identity;
* build-cache metadata that allows consumers to validate how an artifact crossed the sandbox boundary before installing it;
* policy checks over repositories, sandbox ABI, source identities, phases, and parent actions;
* audit and incident-response tooling that can inspect installs without loading their recipes;
* comparison of independently retained source, repository, log, and install-tree artifacts; and
* provenance-aware promotion of artifacts between trust domains.

A signed attestation must define canonical bytes, signer identity, key distribution, revocation, and verification policy.
It should reference the provenance digest rather than add unbounded or environment-specific data to ``sandbox_provenance.json``.
Signing the current local manifest alone would not establish all of these semantics.

SourcePlan resources
--------------------

SourcePlan version 2 adds bounded immutable URL resources while retaining version 1 validation for existing provenance records.
New plans use version 2 even when they contain no resources.
Each resource has an identifier, the same fixed-URL source descriptor used for the main source, and relative ``destination`` and ``placement`` paths.
The recipe worker may emit at most 32 resources and 32 unique candidate URLs per source descriptor.
Every source and resource requires a SHA-256 checksum; mutable VCS fetchers and fetch options remain rejected.

The trusted parent validates the complete plan without importing recipe code and authorizes every candidate URL before fetching any of them.
It then fetches, verifies, and extracts the main source and resources in one private workspace.
As in normal Spack staging, a single top-level archive directory is stripped before the source is published or a resource placement is applied.
The four GiB download limit, 100,000 archive-entry limit, and 16 GiB expanded-size limit are aggregate limits across the entire plan, so resources cannot multiply staging authority.
Archive traversal, links, special files, malformed relative paths, duplicate resource names, and placement conflicts fail the transaction before the prepared tree is published.

Version 2 deliberately supports only an explicit nonempty string ``placement``.
Implicit placement depends on archive-layout inference, while dictionary placement projects multiple paths and introduces merge-order and conflict semantics.
Both forms remain rejected until those semantics have a dedicated bounded validator and transactional tests.
Patches also remain rejected because patch selection, working directories, strip levels, path confinement, application order, and tool execution require a separate protocol revision.

Tests cover recipe-worker serialization, malformed descriptors, traversal, duplicate names, unsupported placement, authority prevalidation, aggregate limits, transactional rollback, placement conflicts, prepared-tree identity, and consumption by the confined build worker without a parent recipe import.

Experimental command
--------------------

``spack sandbox-install`` is the explicit developer entry point for the complete worker-based workflow.
It composes sandboxed concretization, sandboxed SourcePlan creation, trusted source preparation, confined build phases, allowlisted parent actions, metadata publication, and database registration.
It does not alter or intercept ``spack install``.
Parent prefix transactions apply only to Spack-managed installations; external prefixes such as ``/usr`` are never pivoted or mutated.

The command requires at least one ordered ``--repository`` path.
Network and local source authority is denied by default and must be granted with repeatable ``--source-origin`` and ``--file-root`` arguments.
Repository snapshots remain enabled by default; ``--no-repository-snapshots`` explicitly selects the weaker live-repository mode.
Build phases and parent actions cross typed interfaces through repeatable ``--phase`` and ``--post-action`` arguments.
Failure at any step aborts the workflow; by default a failed registered install restores the prior prefix and leaves the database unchanged, while ``--keep-failed-prefix`` retains the failed replacement for debugging.

For example:

.. code-block:: console

   $ spack sandbox-install zlib@1.3.1 \
      --repository /path/to/packages \
      --source-origin https://zlib.net \
      --phase install

The command supports SourcePlan version 2 fixed SHA-256 URL sources and simply placed URL resources.
It does not support patches, implicit or dictionary resource placement, mutable VCS sources, custom fetchers, or fetch options.
It is Linux-only because the worker requires Landlock filesystem and TCP restrictions.
It is intended for development and trust-boundary evaluation, not as a compatibility replacement for normal installation.
Command tests must verify authority parsing, ordered argument transport, temporary-stage lifetime, and registration inputs, while the worker integration suite remains responsible for real confinement, rollback, and provenance behavior.

Policy configuration
--------------------

``config:sandbox_installer`` provides optional defaults only for ``spack sandbox-install`` and is ignored by normal ``spack install``.
The strict schema supports ``repositories``, ``source_origins``, ``file_roots``, ``repository_snapshots``, ``phases``, ``post_actions``, and ``timeout``.
Repository and source-authority defaults are empty, preserving fail-closed behavior.

.. code-block:: yaml

    config:
      sandbox_installer:
         repositories:
         - /path/to/packages
         source_origins:
         - https://zlib.net
         file_roots: []
         repository_snapshots: true
         phases: [install]
         post_actions: [drop_redundant_rpaths, set_permissions]
         timeout: 120

For each policy field, one or more command-line arguments replace the configured value rather than extending it.
This precedence prevents an invocation that names a narrower repository, origin, phase, or action list from silently inheriting additional configured authority.
The command still requires at least one repository after policy resolution.
Malformed origins, nonpositive timeouts, unknown fields, duplicate or malformed phase lists, and unknown or noncanonical parent actions fail before recipe execution.
A syntactically valid phase that the selected recipe does not define is rejected inside the confined build worker.

Extending the worker-based installer
------------------------------------

Each new worker-installer capability must update developer documentation in the same change and remain linked from the documentation tree.
Document its protocol fields, trusted and untrusted responsibilities, bounds, failure behavior, tests, and compatibility implications.
Changes to the persisted record require a schema-version decision and tests for both valid records and malformed or tampered inputs.
