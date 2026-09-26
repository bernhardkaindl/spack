# Namespace sandbox refactor review

This file tracks the review of the staged namespace-sandbox evolution. It is a
working ledger: findings are recorded before repair, completion boxes require
focused test evidence, and deferred items remain explicit.

## Review boundary

The staged increment improves the capability probe, mount-tree helpers,
`NamespaceSandbox`, installer wiring, tests, and this project's status page. It
implements the narrow Linux install-child integration and immutable policy
model, but not the policy-derived mount tree in
`lib/spack/docs/sandbox/namespace-backend.rst`.

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
- [ ] Phase 4, policy-driven mount tree (immutable plan and policy validation,
  non-writable mask sources, and access enforcement are complete;
  installer-policy activation and the complete derived tree remain open).
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
- [x] Require every preserved mount to declare read-only or read-write access.
  Read-only file and directory aliases use
  `mount_setattr(MOUNT_ATTR_RDONLY)`; directory attributes are recursive.
  Invalid modes fail before namespace entry, and writable mounts remain
  explicitly writable.

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

- [x] Enforce access modes for preserved allowlist mounts without Landlock.
  Requests explicitly select read-only or read-write access. The capability
  probe checks recursive read-only mount attributes, and real-kernel
  regressions cover files, directory trees, both preserved aliases, and the
  positive writable case.

- [x] Define and validate one immutable namespace filesystem policy before
  mutation. It must classify hidden roots, read-only mounts, read-write mounts,
  and namespace-local generated paths; reject duplicate, overlapping, and
  access-conflicting entries; and be constructible from trusted installer spec
  and configuration inputs. Keep the runtime on the current narrow mask until
  the policy is complete enough to replace default Landlock safely.

- [x] Select the hidden roots and trusted compiler, tool, header, repository,
  and Spack-source grants needed to compile the installer-created policy into a
  complete mount tree. Prove representative policies compile without silently
  dropping paths, provide scratch space outside all hidden roots, but do not
  activate the broader tree until those inputs are complete. A dormant helper
  now requires explicit hidden roots and canonical host selections, includes
  concrete dependency prefixes, active repositories, required ``sbang`` paths,
  and partitioned Spack source trees, derives parent masks, verifies ancestor
  coverage after deduplication, and rejects overlap with caller-provided
  external scratch. Synthetic-host and fail-closed input tests exercise the
  boundary; the worker still uses the narrow mask.

- [x] Resolve the selected compiler helpers and stage-tool closure without
  ambient compiler fallback. Absolute `-print-prog-name` and
  `-print-file-name` answers are kept with their searched spellings, bare
  answers and Spack binutils wrappers are rejected, aliases remain available
  for later generated symlinks, and `cpp`, `file`, Git, script helper chains,
  and Spack-built tool dependency prefixes are covered by focused tests. The
  selectors remain dormant and the worker still uses the narrow mask.

- [x] Add explicit passthrough and replacement policy entries plus generated
  alias symlinks. Use the A3 spelling/source records to replace derived parent
  masks without restoring broad `/usr`, `/tmp`, or `/var/tmp` trees. The
  dormant policy now keeps alias paths lexical, canonicalizes their sources,
  validates replacement sources at hidden roots, and mounts replacements before
  nested restorations. The live worker remains unchanged.

- [x] Allocate durable, supervisor-cleaned mount-plan scratch outside all
  hidden and replacement roots. The lease now requires a canonical
  non-symlink base, creates unique mode-0700 endpoint directories outside the
  policy and lifecycle roots, and rejects allocation collisions. Cleanup is
  supervisor-only, refuses live attached workers and stale-path replacement,
  and is independent of stage and prefix lifecycle semantics. Concurrent,
  setup-failure, symlinked-base, collision, and abnormal-exit cases are
  covered before policy activation.

- [x] Prove a recursively read-only namespace view with explicit writable
  mounts. Selected-tree plans now set ``MOUNT_ATTR_RDONLY`` recursively on
  inherited mounts and clear it only on explicit writable bind views. The
  capability probe, plan-level tests, installer assertion, and disposable
  real namespace test cover recursive setup, ``EROFS`` passthrough denial, and
  writable restoration without changing the live worker.

- [ ] Preserve host-visible stage and prefix lifecycles. Use a stable
  host-backed per-build stage parent for child-owned cleanup, and move prefix
  pivot, rollback, and failed-prefix cleanup to the supervisor without
  changing the path seen by build tools.

- [ ] Materialize the complete trusted input selection in pre-thread installer
  setup. Select the complete hidden host/device roots; resolve the concrete
  build's external compiler executables and canonical aliases, support tools,
  exact header and runtime trees, and Python runtime; replace the broad system
  temporary-directory grant with a scoped worker directory; allocate durable
  mount-plan scratch outside the derived hidden roots; and validate the
  resulting policy with representative real compiler builds before activation.
  Planning, per-commit selection, and evidence are tracked in
  [phase-4-input-selection.md](phase-4-input-selection.md).

### Tests and documentation structure

- [x] Keep the live test in a disposable interpreter and verify both child
  masking and unchanged parent visibility.
- [x] Keep probe lifecycle tests separate from mocked namespace setup tests.
- [x] Restore Phase 1 through Phase 6 headings in
  `test_sandbox_namespaces.py`, preserving the stronger staged tests.
- [x] Restore the same phase map in `index.rst`, distinguishing completed
  increments from incomplete broader phases and not-started work.
- [x] Integrate the sandbox developer-documentation index into the top-level
  Sphinx/Read the Docs tree.
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
- Preserved allowlist mounts carry explicit access modes. Read-only aliases are
  recursively enforced by the kernel and return `EROFS` without Landlock;
  explicitly writable aliases retain writes to their selected source.
- The immutable filesystem policy canonicalizes four categories and rejects
  duplicate, nested, hidden-root, generated-path, and cross-access conflicts.
  Generated paths are created in the private mask tmpfs before it becomes
  read-only. Hand-constructed policies are revalidated before compilation.
- The policy model now distinguishes caller-provided replacement roots and
  generated symlinks from ordinary passthrough mounts. Replacement sources are
  validated as directories at explicit hidden roots and mounted before nested
  restorations; generated alias paths remain lexical while their targets are
  canonicalized and created in the private mask source.
- Trusted installer construction classifies the existing dependency, prefix,
  stage, temporary, device, `sbang`, and configured path grants. Missing paths
  are omitted like current sandbox grants and redundant same-access descendants
  are collapsed. Policy compilation rejects classified paths that do not yet
  have a containing hidden root; the live worker does not use this broad policy.
- Hidden roots must exist, lexical overlap checks consider every prior ancestor,
  and mount-plan scratch space and its reserved source subtrees may not overlap
  hidden roots. Preserved sources are rechecked and never recreated after
  validation. Descriptor-based anchoring against concurrent same-type source
  substitution remains future hardening at this trusted pre-thread boundary.
- Selected-tree policy compilation now treats explicit hidden roots and exact
  host compiler, tool, header, runtime, and scoped temporary paths as required
  trusted inputs.
  Non-external compiler and tool packages remain covered by concrete dependency
  prefixes; whole external prefixes such as ``/usr`` are not restored because
  that would defeat selective ``/usr/bin`` and ``/usr/include`` masking.
- Active repository roots, required ``sbang`` paths, and partitioned Spack
  source trees are explicit read-only grants. Required paths fail if absent or
  non-canonical. Same-access descendants are omitted only after coverage by an
  ancestor mount is proved, and top-level paths fail rather than forcing a
  hidden filesystem root. Explicit and derived parent masks compile
  deterministically without changing the live worker. Unrelated host trees
  remain visible until the complete hidden-root/device policy is selected.
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
- 2026-09-26: Added explicit read-only/read-write access to preserved mount
  requests and enforced read-only file and recursive-directory aliases with
  `mount_setattr`. Extended capability diagnostics for unsupported read-only
  mount attributes. Disposable real-kernel tests prove `EROFS` through both
  preserved aliases without Landlock and prove explicit writable mounts still
  propagate writes. Selected immutable namespace filesystem-policy
  construction and pre-mutation conflict validation as the next work item.
- 2026-09-26: Added an immutable namespace filesystem policy with canonical
  hidden-root, read-only, read-write, and generated-path categories. Policy
  construction rejects duplicate, nested, cross-access, and invalid generated
  entries; hand-built policies and compilability are checked before namespace
  entry. Added trusted installer construction from existing sandbox grants and
  generated-path planning without wiring the broad policy into the worker. The
  namespace/shared sandbox suite passes 80 tests. Selected complete hidden-root
  and compiler/tool/header/repository grant derivation as the next work item.
- 2026-09-26: Ran `ruff format --check`, `ruff check`, complete-patch whitespace
  validation, and the 80-test namespace/shared sandbox suite; all passed. A
  fresh full Sphinx build reports no warning from the changed policy pages and
  confirms the sandbox index is now in the top-level doctree. The warning gate
  remains blocked by 14 unrelated repository autodoc, orphan, and reference
  warnings.
- 2026-09-26: Added the dormant selected-tree namespace policy compiler. It
  requires explicit masks and canonical compiler, tool, header, runtime, and
  scoped temporary selections; includes concrete dependency prefixes, active
  repository roots, required ``sbang`` paths, and partitioned Spack source
  trees; derives non-overlapping parent masks; and verifies every requested path
  remains represented after ancestor collapse. Review added reserved-scratch
  overlap rejection and fail-closed missing, symlink, and top-level path cases,
  and narrowed readiness claims because unrelated host trees remain visible.
  Kept the worker on the narrow ``/usr/share/aclocal`` mask and selected the
  complete host/device and production-input policy plus real-build validation
  as the next work item.
- 2026-09-26: Independent re-review found no remaining high- or medium-severity
  issues after the scratch, canonical-path, explicit-mask, repository, and
  documentation repairs. Ruff formatting and lint passed for all four changed
  Python files, and the namespace/shared sandbox suite passed 86 tests. A fresh
  Sphinx build reported no warning from the changed pages; its warning gate
  remains blocked by the same 14 unrelated autodoc, toctree, and reference
  warnings.
- 2026-09-26: Moved the input-selection item into its own ledger,
  `phase-4-input-selection.md`, with sub-phases, excluded Landlock-era
  mechanisms, reusable earlier tests, and a one-commit-per-step sequence.
  It records that the worker enters the namespace before staging, that
  derived parent masks would hide `/usr/lib` and `/tmp`, and that stage and
  prefix mount points conflict with `PrefixPivoter`. Selected policy-data
  loading (A1) as the next commit.
- 2026-09-26: Completed the policy-data loading increment (A1). The shipped
  sandbox and Linux header policies now load lazily with version, structure,
  alias, namespace-section, and safe-relative-path validation; the Landlock
  command stub remains excluded and the live worker is unchanged. The Phase 4
  ledger now selects concrete compiler and system-header selection (A2).
- 2026-09-26: Completed A2 in the Phase 4 ledger. Added dormant compiler
  language-edge and supported-compiler-node selection plus policy-driven
  system-header and GCC-installation selection, with repository-aware compiler
  discovery and focused synthetic tests. The live worker remains on the narrow
  mask; A3 helper, alias, and stage-tool resolution is now selected.
- 2026-09-26: Completed A3 in the Phase 4 ledger. Added dormant compiler
  helper, alias, executable-support, Git, stage-tool, helper-chain, and
  Spack-tool-runtime selectors. Absolute compiler answers are canonicalized
  without ambient fallback; searched spellings remain distinct, Spack
  binutils wrappers are excluded, and focused tests cover the complete A3
  boundary. The live worker remains on the narrow mask; B1 explicit
  passthrough, replacement, and generated-symlink policy entries are now
  selected.
- 2026-09-26: Completed B1 in the Phase 4 ledger. Replaced derived parent
  masks with explicit hidden-root inputs for the selected-tree compiler,
  added replacement-root and generated-symlink policy entries, and covered
  their deterministic mount-plan application. Alias spellings survive
  `realpath` canonicalization, nested restorations follow replacements, and
  the live worker remains on the narrow mask. Selected durable mount-plan
  scratch and collision cleanup as B2.
