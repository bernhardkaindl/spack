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

SourcePlan version 2 added bounded immutable URL resources.
New plans use version 6, while versions 1 through 5 remain valid for existing provenance records.
Each resource has an identifier, the same fixed-URL source descriptor used for the main source, a relative ``destination``, and a string, implicit, or ordered mapping ``placement``.
The recipe worker may emit at most 32 resources and 32 unique candidate URLs per source descriptor.
Every source and resource requires a SHA-256 checksum; mutable VCS fetchers and fetch options remain rejected.

The trusted parent validates the complete plan without importing recipe code and authorizes every candidate URL before fetching any of them.
It then fetches, verifies, and extracts the main source and resources in one private workspace.
As in normal Spack staging, a single top-level archive directory is stripped before the source is published or a resource placement is applied.
The four GiB download limit, 100,000 archive-entry limit, and 16 GiB expanded-size limit are aggregate limits across the entire plan, so resources cannot multiply staging authority.
Archive traversal, links, special files, malformed relative paths, duplicate resource names, and placement conflicts fail the transaction before the prepared tree is published.

Version 2 supports only an explicit nonempty string ``placement``.
Version 5 adds implicit placement for expanding resources whose archive contains exactly one top-level directory and no sibling entries.
The trusted parent records the validated top-level directory name while stripping that directory during extraction and places the resource under ``destination/<top-level-directory>``, matching normal ``ResourceStage.srcdir`` behavior without exposing a private staging path.
Flat, multi-root, hidden-sibling, and non-expanding resources fail closed when placement is omitted because they have no unambiguous relative placement name.
Version 6 serializes recipe dictionary placement as an ordered list of exact ``source`` and ``destination`` records, preserving recipe insertion order without relying on JSON object-key behavior.
A plan may contain at most 256 mapping records across all resources.
Sources select regular files or directories under the safely extracted resource root; for non-expanding resources, the selectable filename is derived with the same ``default_download_filename`` rule as normal ``Stage`` handling, including sanitized query text.
Empty source paths select the complete resource root, while destinations must be nonempty normalized relative paths.
Source or destination duplicates and ancestor overlaps within a mapping are rejected, and every source plus every final destination is prevalidated before that resource copies any content.
Missing sources, unsupported entries, existing destinations, and conflicts with earlier resources abort the complete unpublished prepared-tree transaction.

Tests cover recipe-worker serialization, malformed descriptors, traversal, duplicate names, string, implicit, and mapped placement, legacy-schema rejection, mapping overlap and query-bearing filename handling, ambiguous-layout rollback, authority prevalidation, aggregate limits, placement conflicts, prepared-tree identity, and consumption by the confined build worker without a parent recipe import.

SourcePlan patches
------------------

SourcePlan version 3 supports ordered repository-local file patches without allowing the trusted parent to resolve package classes or read repository paths.
The confined planning worker reads each selected ``FilePatch``, verifies its concrete-spec SHA-256 identity, and emits its owner, strip level, relative working directory, reverse flag, parsed targets, and canonical base64 payload.
A plan may contain at most 32 patches, each decoded payload is limited to 48 KiB, and all decoded payloads together are limited to 512 KiB.
The per-patch limit keeps canonical base64 within the concretization protocol's existing 64 KiB string bound.

Only UTF-8, LF-terminated unified diffs are accepted.
Validated ``diff --git`` and ``index`` preambles are allowed, but rename, create, delete, binary, mode-only, ed-script, duplicated-target, and other patch formats fail closed.
Patch levels are limited to 0 through 16; working directories and parsed targets must be normalized relative paths.

After source and resource staging, the parent writes validated payloads into the private preparation workspace and launches a fresh patch worker.
That worker applies Landlock before invoking the exact resolved system ``patch`` executable without a shell or interactive prompts.
It can write only the unpublished source tree and private patch state, rechecks every payload checksum and working directory, and requires every parsed target to resolve to an existing regular non-symlink file inside the source tree.
Reject output is captured instead of written to the source tree.
All patches are applied in concrete-spec order before the prepared tree is atomically published; any timeout or failure discards the complete workspace.

The later confined build worker independently compares patch owner, checksum, level, working directory, reverse flag, and order against ``spec.patches`` from the verified repository before executing package phases.
This prevents a syntactically valid but unrelated patch plan from being consumed by a recipe.
The persisted SourcePlan and prepared-stage digests bind both the patch inputs and resulting source tree into install provenance.

SourcePlan version 4 adds immutable URL patches.
The planning worker emits the URL and expanded patch SHA-256; compressed patches also carry a distinct archive SHA-256 and normalized archive extension.
The trusted parent authorizes every patch URL together with source and resource URLs before issuing any request, and redirects remain subject to the same ``SourceFetchPolicy``.
Plain downloads are limited to 48 KiB.
Compressed downloads are limited to four MiB, must expand to exactly one regular file of at most 48 KiB, and are also charged to the plan-wide four GiB download budget.
The trusted parent verifies the archive checksum before extraction and the expanded checksum before unified-diff parsing.

Supported compressed patch formats are tar, gzip-compressed tar, bzip2-compressed tar, xz-compressed tar, their common abbreviated extensions, zip/whl, and single-file gzip, bzip2, and xz.
Legacy ``.Z`` and compressed URLs without a recognized extension fail closed because they would require another trusted decompressor contract or content-based format selection.
Derived targets are passed to the same Landlock patch worker used for repository-local patches.
The build worker additionally compares URL, archive checksum, and extension against the concrete recipe's ``UrlPatch`` before executing phases.

Tests cover malformed and non-unified payloads, Git preambles, target binding, traversal, checksum and option validation, ordered and reverse application, working directories, URL authority, plain and compressed downloads, separate archive and expanded checksums, bounded decompression, transactional rollback, real recipe consumption, and build-worker rejection of patch metadata not bound to the concrete recipe.

Package patch methods
---------------------

Package-defined ``patch()`` methods use the same ``run_patch_method`` helper as normal ``PackageBase.do_patch()``.
The normal path remains responsible for staging, declarative patch application, retry markers, and restaging; the shared helper preserves its custom-method behavior by changing to ``stage.source_path``, invoking the selected multimethod, and treating ``NoSuchMethodError`` as not applicable.

The worker-based installer invokes that helper exactly once after it verifies the concrete spec, repository identity, package hash, SourcePlan, prepared-stage digest, and declarative patch binding, and before it creates the builder or runs any selected phase.
All requested phases execute in the same fresh worker, so a multi-phase install does not repeat the method.
The package receives the same concrete spec and configured build environment used by its build phases.
Its stage facade intentionally exposes only ``path`` and ``source_path``; methods that fetch, expand, restage, or destroy a normal ``Stage`` are unavailable because those operations remain trusted-parent responsibilities.

Recipe code is already under Landlock when the method runs.
It can read host and dependency paths but can write only the prepared source tree, install prefix, and private worker state, and it cannot open TCP connections.
A method failure aborts the worker and the parent-owned prefix transaction; the prepared-stage digest prevents reuse as an unmodified input after any source-tree mutation.
Build-worker protocol version 5 reports whether a matching method completed, and new install provenance version 2 persists that boolean as ``build.patch_method``.
The recipe package hash binds the method implementation; SourcePlan needs no arbitrary-code descriptor or schema revision.
The recipe-free verifier continues to accept version 1 provenance records, which predate this field.

Tests cover successful method execution before install, declarative-before-method ordering, parent recipe-import isolation, conditional multimethod no-op behavior, denied writes outside the Landlock allowlist, structured failure, the persisted execution flag, and legacy provenance validation.

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

The command supports SourcePlan version 6 fixed SHA-256 URL sources, string, implicit, or dictionary-placed URL resources, bounded repository-local or URL unified-diff patches, and package-defined ``patch()`` methods.
It does not support ``.Z`` or extensionless compressed patches, ambiguous implicit resource layouts, mutable VCS sources, custom fetchers, or fetch options.
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
