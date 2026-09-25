# Namespace sandbox refactor review

This file tracks the review of the staged namespace-sandbox evolution. It is a
working ledger: findings are recorded before repair, completion boxes require
focused test evidence, and deferred items remain explicit.

## Review boundary

The staged increment improves the capability probe, mount-tree helpers,
`NamespaceSandbox`, installer wiring, tests, and this project's status page. It
implements the narrow Linux install-child integration, but not the
policy-driven mount tree in `lib/spack/docs/sandbox/namespace-backend.rst`.

## Phase status

- [x] Phase 1 increment, capability probe and fallback: the disposable-child
  probe returns availability, failed operation, and reason. Backend selection
  distinguishes namespaces, constrained Landlock, and forbidden unconstrained
  fallback, with lifecycle and diagnostic regression tests.
- [x] Phase 2 increment, mount-tree setup: the narrow namespace-entry and bind-mount
  primitives exist. The planned tmpfs policy tree, preserved sources, namespace
  handle, and explicit cleanup API remain open.
- [x] Phase 3 increment, install-worker integration: before starting its
  logging thread, the existing forked Linux install child completes trusted
  namespace mount setup and drops mount authority. It applies Landlock before
  build phases. The supervisor remains unaffected.
- [ ] Phase 4, policy-driven mount tree (immutable plan validation and
  non-writable mask sources are complete; policy-derived mounts remain open).
- [ ] Phase 5, complete build-phase policy validation and hardening.
- [ ] Phase 6, concretizer-worker evaluation.

## Case-by-case review

### Probe lifecycle and exception handling

- [x] Keep the child-wide `finally: os._exit(0)` so unexpected child exceptions
  cannot unwind into the caller or duplicate the test runner.
- [x] Keep descriptor cleanup on fork failure.
- [x] Keep parent-side descriptor cleanup and child reaping when reads fail.
- [x] Keep libc-load and unsupported-platform failures as unavailability.
- [x] Avoid invoking the fork-based probe after any worker thread has started.
  Installer preflight caches the default-libc result, and the build worker
  enters the namespace before starting `Tee`. Later selection reuses the cache.
  A fresh-exec mode remains optional hardening or an external-backend concern.
- [x] Add reason-specific capability results after the boolean probe lifecycle
  is stable. Child setup and parent probe failures retain the failed operation
  and reason across the pipe and process-local cache.

### Mount-tree state and failure semantics

- [x] Keep namespace readiness false until namespace entry and all requested
  masks succeed.
- [x] Keep setup and mount errors fatal after setup starts; falling back after a
  partial process mutation is unsafe.
- [x] Keep namespace re-entry idempotent within a process.
- [x] Enter the namespace even when the selected hidden-directory list is empty.
  Fixed in `_enable_sandbox()` and covered for external `autoconf`.
- [x] Do not retry after failed namespace entry or masking. The process may
  already contain partial namespace or mount state, so the worker fails and a
  fresh worker is the only valid retry boundary.
- [x] Validate the current narrow mask targets before namespace mutation. The
  immutable plan canonicalizes existing directory targets, rejects invalid,
  duplicate, and ancestor/descendant targets, and assigns deterministic
  stage-owned source paths.
- [x] Preserve explicitly selected sources before hiding parent directories.
  The immutable plan canonicalizes source paths and merged-`/usr` target
  aliases, validates source/target types and target containment below a hidden
  directory, stages sources before masks, and restores them by increasing
  target depth. Recursive binds are limited to preserved directory trees.
- [x] Use mask sources that remain non-writable after confinement. Mask sources
  are populated in a private tmpfs and remounted read-only before mask binds
  are exposed. Landlock write grants to the stage cannot populate the source.

### Landlock composition and installer errors

- [x] Keep Landlock grants and application layered on the namespace view.
- [x] Normalize lazy `LandlockSandbox` initialization failures. Namespace
  selection can succeed while the first `allow_read` or `allow_write` raises a
  raw `OSError`; the composite backend now raises `SandboxError`, the installer
  wraps grants and application, and backend selection preflights Landlock.
- [ ] Implement the documented shared fallback policy. A warning in the current
  hook is not the final `config:sandbox:allow_fallback` trust decision for
  trusted direct execution when no constrained worker is available. It does not
  gate namespace-to-Landlock fallback because Landlock remains constrained.

### Next selected work

- [x] Define and implement a structured namespace capability and fallback
  decision. It must distinguish namespace-to-Landlock fallback from an
  unconstrained-worker fallback, report the failed probe operation, permit
  fallback only before process mutation, and keep partial namespace or mount
  setup failures fatal. Add focused selection and diagnostic tests before
  starting the Phase 4 mount policy.

- [x] Close the [mount-authority window](../docs/sandbox/glossary.rst#sandbox-term-mount-authority-window)
  between pre-thread namespace entry and Landlock application. This was the
  interval in which recipe-controlled Python could create a bind mount beneath
  a path that Landlock later grants recursively. The disposable probe now
  verifies capability dropping; the trusted pre-thread path prepares the narrow
  mask, drops user-namespace capabilities, and hands the same sandbox instance
  to later Landlock setup. A regression asserts
  ``prepare -> drop -> Tee -> recipe setup -> Landlock`` and rejects a later
  ``bind_mount`` call. This protects the current narrow mount set; the
  immutable Phase 4 mount plan remains required before adding policy mounts.

- [x] Define and validate an immutable Phase 4 mount plan before performing any
  mounts. The current increment covers the narrow empty-directory masks and
  explicit preserved sources: resolve and sort targets, validate target and
  stage types, canonicalize merged-``/usr`` aliases, reject duplicate or
  overlapping targets, validate source containment, and produce deterministic
  preservation and restoration order. Plan-level regressions prove invalid
  plans fail before namespace entry.

- [x] Use non-writable empty mask sources. The mask-source root is a private
  tmpfs remounted read-only after planned endpoints are created. The capability
  probe checks tmpfs creation and remounting, and a real-kernel regression
  grants stage write access before attempting source and target modifications.

### Tests and documentation structure

- [x] Keep the live test in a disposable interpreter and verify both child
  masking and unchanged parent visibility.
- [x] Keep probe lifecycle tests separate from mocked namespace setup tests.
- [x] Restore Phase 1 through Phase 6 headings in
  `test_sandbox_namespaces.py`, preserving the stronger staged tests.
- [x] Restore the same phase map in `index.rst`, distinguishing completed
  increments from incomplete broader phases and not-started work.
- [x] Scope Phase 3 completion to the internal Linux install-child integration;
  do not imply confinement-before-recipe-import or inherited-state minimization.
- [x] Re-evaluate Phase 3 with platform-specific process requirements and mark
  the narrow Linux install-child integration complete.
- [x] Run formatting, focused tests, staged-diff checks, and documentation
  validation after the final edits.

## Accepted staged improvements

- Probe children cannot escape into the caller on unexpected setup exceptions.
- Structured capability results retain exact child and parent failure
  operations and reasons across the probe pipe and cache.
- Backend decisions identify Landlock-only fallback as constrained and reject
  an unconstrained fallback before constructing a sandbox.
- The disposable probe now exercises the directory bind mount required by the
  current masking backend, so that failure selects Landlock before mutating the
  install worker.
- The trusted pre-thread path completes the narrow mount setup and drops all
  user-namespace capabilities before `Tee` or recipe-controlled Python runs.
  Later namespace bind mounts are rejected; Landlock then confines build phases.
- The narrow masking path now builds an immutable mount plan before namespace
  entry. It canonicalizes and sorts targets, validates directory types, rejects
  duplicates and overlapping targets, and assigns deterministic empty-source
  paths. Invalid plans cannot partially mutate the worker.
- Explicit preserved sources are now planned before masks and restored into the
  hidden tree afterward. Source and target types are validated, merged-``/usr``
  aliases resolve to canonical targets, preserved directory trees use recursive
  binds, and restoration order is deterministic by target depth.
- Fork, pipe, read, and wait lifecycle paths have direct regression coverage.
- Empty mount trees now establish real readiness rather than reporting probe-only
  availability.
- Mount failures propagate instead of silently accepting a partial view.
- Recursive bind flags are used for directory sources and ordinary bind flags
  for files.
- The integration test checks actual `ENOENT` visibility and parent isolation
  without masking system directories.
- Mask sources live on a read-only tmpfs, so writable stage grants cannot
  populate the empty view through either the source or target alias.
- The namespace filesystem policy is intended to be the default confinement;
  Landlock is retained in the current narrow integration only as a transitional
  constraint and should become opt-in for permission-denied behavior tests.
- The status page is candid about the narrow `/usr/share/aclocal` policy and the
  optional inherited-state and pre-import hardening that Phase 3 does not claim.

## Worklog

- 2026-09-25: Inventoried all six staged files and compared the staged status
  page and tests with the committed phase grouping.
- 2026-09-25: Accepted the probe lifecycle, readiness, fatal-error, recursive
  bind, and live-isolation improvements after case-by-case review.
- 2026-09-25: Found that an external `autoconf` produced an empty mask and caused
  `_enable_sandbox()` to skip namespace entry. Removed the nonempty-list guard
  and added both empty and nonempty installer test cases.
- 2026-09-25: Ran the focused namespace and shared sandbox suite: 30 passed.
- 2026-09-25: Confirmed that the build worker starts `Tee` before sandbox
  selection, so its fallback availability probe currently forks a threaded
  process. Marked this as a Phase 3 blocker.
- 2026-09-25: Confirmed that lazy Landlock construction can raise outside the
  installer's current `SandboxError` normalization boundary.
- 2026-09-25: Cached default-libc capability results, moved worker namespace
  entry before `Tee`, and added cache and pre-thread preparation regressions.
- 2026-09-25: Preflighted Landlock during composite backend selection and
  normalized lazy construction errors through the installer boundary.
- 2026-09-25: Restored the six-phase map in tests and status documentation while
  retaining the staged lifecycle, failure, bind, and live-integration coverage.
- 2026-09-25: Kept operational parent probe failures retryable, froze fallback
  at the worker's pre-thread setup point, and tested preparation-before-`Tee`
  ordering against actual `Tee` construction.
- 2026-09-25: Added controlled pre-thread setup failure handling: close the
  state stream, write the traceback to the build log, and exit `BUILD_ERROR`.
- 2026-09-25: Reran the focused namespace and shared sandbox suite: 38 passed.
- 2026-09-25: Ran `ruff format` and `ruff format --check` on all six changed
  Python files, `ruff check`, standalone RST parsing, and complete-patch
  whitespace validation; all passed.
- 2026-09-25: Re-reviewed the process boundary after the implementation commit.
  Confirmed that Linux namespace entry and Landlock self-restriction work in
  the existing forked install child and do not restrict the supervisor. Decoupled
  Linux from Windows AppContainer and optional external-launcher requirements,
  marked the narrow Phase 3 increment complete, and selected structured
  capability/fallback policy as the next slice.
- 2026-09-25: Reran Ruff and the focused namespace/shared sandbox suite (38
  passed). A fresh full Sphinx build reports no warnings from the changed
  sandbox pages; its warning gate remains blocked by 15 unrelated repository
  autodoc, toctree, and reference warnings.
- 2026-09-25: Replaced the boolean-only probe result with a structured
  capability result, transported operation-specific child failures through the
  probe pipe, retained parent failure diagnostics, and preserved retry/freeze
  cache semantics.
- 2026-09-25: Added an explicit namespace backend decision: prefer namespaces,
  classify Landlock-only as constrained fallback, and reject unconstrained
  fallback. Confirmed that actual namespace and mount failures remain fatal and
  selected preflight validation of an immutable Phase 4 mount plan as the next
  work item.
- 2026-09-25: An independent review found that the capability probe omitted the
  bind mount required by current masking. Added that operation to the disposable
  probe and a focused failure-transport regression.
- 2026-09-25: The same review identified a pre-existing mount-authority window:
  recipe-controlled setup runs after namespace entry but before Landlock. Also
  recorded that stage-owned mask sources are writable and corrected broader
  docs that described the unimplemented Phase 4 ``bin``/``include`` policy as
  current behavior. Closing the authority window is the next selected work.
- 2026-09-25: Closed the mount-authority window by performing the narrow trusted
  mount setup before `Tee`, dropping all user-namespace capabilities, and
  handing the prepared sandbox to later Landlock setup. The capability probe
  now tests the drop, lifecycle tests assert ordering and reject later bind
  mounts, and the public sandbox glossary defines the term. Selected immutable
  Phase 4 mount-plan validation as the next work item.
- 2026-09-25: Ran `ruff format`, `ruff format --check`, and `ruff check` on all
  four changed Python files; the focused namespace and shared sandbox suite
  passed 45 tests. A fresh full Sphinx build reports no warnings from changed
  pages; its warning gate remains blocked by the same 15 unrelated repository
  autodoc, toctree, and reference warnings.
- 2026-09-25: Implemented the immutable mount-plan increment for the current
  narrow mask. Plans canonicalize and deterministically order existing directory
  targets, validate stage and target types, reject duplicate and overlapping
  targets before namespace entry, and create stage-owned sources only during
  application. Added plan-level regressions and selected preservation of
  whitelisted sources, merged-``/usr`` aliases, and mount ordering as the next
  work item.
- 2026-09-25: Implemented preserved-source planning for the Phase 4 mount plan.
  Explicit source/target pairs are canonicalized and type-checked before
  namespace entry, staged before hidden parent mounts, and restored in
  target-depth order; merged-``/usr`` aliases are resolved to canonical targets.
  Added policy-level ordering and pre-entry failure regressions, and selected
  non-writable empty mask sources as the next work item.
- 2026-09-26: Replaced writable stage-owned mask directories with a private
  tmpfs source root. Planned endpoints are created before the root is remounted
  read-only, then mask binds and preserved-source restoration proceed from that
  immutable view. Extended the disposable capability probe to cover tmpfs and
  read-only remount operations, and added a real-kernel regression that grants
  stage writes before attempting source and target modification. The current
  namespace backend still layers Landlock until policy-derived allowlist mounts
  are complete; selected that policy and the explicit Landlock diagnostic mode
  as the next work.
