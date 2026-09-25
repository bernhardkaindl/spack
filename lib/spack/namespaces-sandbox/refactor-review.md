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

- [x] Phase 1 increment, capability probe and fallback: a boolean disposable-child probe
  exists, with lifecycle regression tests. Reason-specific reports remain open.
- [x] Phase 2 increment, mount-tree setup: the narrow namespace-entry and bind-mount
  primitives exist. The planned tmpfs policy tree, preserved sources, namespace
  handle, and explicit cleanup API remain open.
- [x] Phase 3 increment, install-worker integration: the existing forked Linux
  install child enters the namespace before starting its logging thread and
  applies Landlock before build phases. The supervisor remains unaffected.
- [ ] Phase 4, policy-driven mount tree.
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
- [ ] Add reason-specific capability results after the boolean probe lifecycle is
  stable.

### Mount-tree state and failure semantics

- [x] Keep namespace readiness false until namespace entry and all requested
  masks succeed.
- [x] Keep setup and mount errors fatal after setup starts; falling back after a
  partial process mutation is unsafe.
- [x] Keep namespace re-entry idempotent within a process.
- [x] Enter the namespace even when the selected hidden-directory list is empty.
  Fixed in `_enable_sandbox()` and covered for external `autoconf`.
- [ ] Decide and document retry behavior after a failed mask. The process may
  already be in a namespace and may contain a partial mount tree.
- [ ] Preserve selected sources before hiding parent directories; create and
  validate targets; account for merged-`/usr` aliases and mount ordering.

### Landlock composition and installer errors

- [x] Keep Landlock grants and application layered on the namespace view.
- [x] Normalize lazy `LandlockSandbox` initialization failures. Namespace
  selection can succeed while the first `allow_read` or `allow_write` raises a
  raw `OSError`; the composite backend now raises `SandboxError`, the installer
  wraps grants and application, and backend selection preflights Landlock.
- [ ] Implement the documented shared fallback policy. A warning in the current
  hook is not the final `config:sandbox:allow_fallback` trust decision.

### Next selected work

- [ ] Define and implement a structured namespace capability and fallback
  decision. It must distinguish namespace-to-Landlock fallback from an
  unconstrained-worker fallback, report the failed probe operation, permit
  fallback only before process mutation, and keep partial namespace or mount
  setup failures fatal. Add focused selection and diagnostic tests before
  starting the Phase 4 mount policy.

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
- Fork, pipe, read, and wait lifecycle paths have direct regression coverage.
- Empty mount trees now establish real readiness rather than reporting probe-only
  availability.
- Mount failures propagate instead of silently accepting a partial view.
- Recursive bind flags are used for directory sources and ordinary bind flags
  for files.
- The integration test checks actual `ENOENT` visibility and parent isolation
  without masking system directories.
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
