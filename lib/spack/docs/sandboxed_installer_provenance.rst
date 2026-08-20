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

Extending the worker-based installer
------------------------------------

Each new worker-installer capability must update developer documentation in the same change and remain linked from the documentation tree.
Document its protocol fields, trusted and untrusted responsibilities, bounds, failure behavior, tests, and compatibility implications.
Changes to the persisted record require a schema-version decision and tests for both valid records and malformed or tampered inputs.
