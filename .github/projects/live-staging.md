# Live Staging Progress Ledger

This ledger tracks work to make source staging observable in the interactive installer UI. Agents
working on an item must update its checkbox, add the commit and PR reference when applicable, and
record the command and result for each completed validation gate. Do not mark an item complete
based only on code inspection.

## GitButler Delivery Policy

- [ ] Create this project as a dedicated GitButler stack layered above the existing
  `installer-ui/v1/failed-build-rows` live install-progress stack.
- [ ] Keep published `installer-ui/v1/*` branches unchanged unless the user explicitly authorizes
  history rewriting.
- [ ] Commit this ledger as the dedicated base branch and draft PR of the new stack.
- [ ] When a checkpoint is complete, commit its implementation, tests, and required documentation
  using GitButler on a dedicated branch above the prior project branch.
- [ ] Create a draft GitButler pull request for every completed checkpoint branch, so each plan
  step is a reviewable PR layer in this project stack.
- [ ] Update the completed checkpoint evidence with its GitButler commit and PR references.
- [ ] Before creating a checkpoint commit, run its validation gate and record the exact commands
  and results in its evidence block.
- [ ] Use GitButler for all branch, stack, commit, pull-request, and history operations.

## Goal

When source staging is in progress, show a stable staging state in the live installer overview.
`spack install --verbose` starts with the selected build's stage output visible. Interactive
controls may change display visibility while the stage process continues to collect progress.

## Design Boundaries

- [ ] Preserve ordinary non-verbose install output and existing staging behavior unless an item
  explicitly changes it.
- [ ] Keep Spack-owned staging states structured and stable, independent of external tool wording.
- [ ] Treat Git transfer text as a best-effort enhancement, not a protocol or a condition for
  staging success.
- [ ] Do not conflate `--verbose` with `--debug`.
- [ ] Preserve raw stage output in the build log even when the UI condenses it into structured
  progress.
- [ ] Keep Windows and non-GNU-tar portability requirements explicit before adding
  external-tool-specific flags.

## Interaction Contract

- [ ] `spack install --verbose` uses the existing installer verbose flag to start following the
  selected build's output.
- [ ] `v` toggles following for the selected build without restarting or reconfiguring an in-flight
  staging command.
- [ ] `q` stops the interactive live view according to the existing UI's quit semantics; document
  whether it also disables log forwarding or merely hides rendering.
- [ ] Progress collection remains enabled while output is hidden, so returning to the overview
  shows the current stage state rather than a stale one.
- [ ] Update the TTY key-help text and focused UI tests if the effective key semantics change.

## Checkpoint 1: Establish the Data Path

- [ ] Trace source staging from `PackageInstaller._start()` through `BuildRequest`,
  `worker_function`, `PackageBase.do_stage()`, and `Stage`.
- [ ] Decide where structured staging events cross the worker-parent boundary: extend the existing
  state pipe rather than parsing terminal output in the parent when possible.
- [ ] Carry the existing installer `verbose` value in `BuildRequest` so fork and spawn workers
  make the same visibility decision.
- [ ] Confirm global `tty` state is not the only transport for this behavior.
- [ ] Add a focused worker/core test proving the parent receives a staging event before worker
  exit.

### Gate 1

- [ ] Focused installer core and worker tests pass.
- [ ] `ruff format --check` and `ruff check` pass for changed Python files.
- [ ] Evidence recorded below.

Evidence:

```text
Pending.
```

## Checkpoint 2: Stable Stage States

- [ ] Define a small structured event vocabulary, beginning with states such as `fetching source`,
  `expanding archive`, `cloning source`, `checking out source`, and `updating submodules`.
- [ ] Emit state transitions from Spack-owned fetch and expansion boundaries.
- [ ] Render the current staging state in the existing per-build progress row without corrupting
  build-progress data from CMake, Bazel, or binary caches.
- [ ] Specify precedence when source staging, build output, and binary-cache progress would
  otherwise update the same row.
- [ ] Add UI tests for state transitions, row rendering, and reset on stage completion or failure.

### Gate 2

- [ ] Focused installer UI and staging tests pass.
- [ ] Existing CMake/Bazel and binary-cache progress tests remain green.
- [ ] `ruff format --check` and `ruff check` pass for changed Python files.
- [ ] Evidence recorded below.

Evidence:

```text
Pending.
```

## Checkpoint 3: Git Progress Enhancement

- [ ] Add an explicit Git progress mode controlled by the existing installer verbose value; do not
  enable debug output.
- [ ] In progress mode, pass `--progress`, omit `--quiet`, and preserve stderr through the worker
  tee for clone and fetch operations.
- [ ] Apply equivalent progress behavior to recursive submodule updates.
- [ ] Use a C locale only if textual parsing requires it, and scope that environment change to the
  Git invocation.
- [ ] Split output records on both newline and carriage return.
- [ ] Recognize a conservative subset of Git progress messages with an operation and percentage;
  fall back safely to the stable `cloning source` or `updating submodules` state for all unknown
  messages.
- [ ] Retain raw Git output in logs and never fail staging because parsing does not recognize a Git
  version, transport, server message, or locale.

### Gate 3

- [ ] Unit tests verify Git progress arguments, normal quiet behavior, and recursive submodule
  behavior.
- [ ] UI tests prove carriage-return records update before a newline arrives.
- [ ] A local verbose Git-backed stage demonstrates visible progress or a documented fallback state
  before completion.
- [ ] `ruff format --check` and `ruff check` pass for changed Python files.
- [ ] Evidence recorded below.

Evidence:

```text
Pending.
```

## Checkpoint 4: Archive Expansion Progress

- [ ] Inventory archive expansion paths and identify whether Spack uses Python archive APIs or
  external tools for each supported archive type.
- [ ] Prefer Spack-owned member or byte progress where the archive API exposes it.
- [ ] Evaluate GNU tar `--checkpoint` only as an optional capability; do not make it a universal
  requirement.
- [ ] Ensure external tar output is not parsed as a stable protocol.
- [ ] Add tests for a representative tarball extraction and an unsupported or capability-disabled
  external tar path.

### Gate 4

- [ ] Archive extraction tests pass on the supported test platform.
- [ ] The portability decision is documented in code or developer documentation.
- [ ] `ruff format --check` and `ruff check` pass for changed Python files.
- [ ] Evidence recorded below.

Evidence:

```text
Pending.
```

## Checkpoint 5: End-to-End Review

- [ ] Verify `spack install --verbose` starts with live selected-build output.
- [ ] Verify `v` hides and restores output without losing the current staging state.
- [ ] Verify `q` follows the documented interactive quit behavior.
- [ ] Verify a normal install remains quiet while its overview still shows the stable staging state.
- [ ] Exercise a Git package with submodules, such as `py-torch`, or record a reproducible smaller
  fixture that covers clone plus recursive submodules.
- [ ] Exercise a tarball-backed package.
- [ ] Run all focused tests for touched installer, stage, fetch-strategy, and Git utility modules.
- [ ] Run format and lint checks for every changed Python file.
- [ ] Review the final diff for changes outside this ledger's scope.

### Gate 5

- [ ] All prior gates have recorded passing evidence.
- [ ] The change is split into reviewable GitButler stack layers without rewriting published stack
  branches unless explicitly authorized.
- [ ] Developer documentation describes the user-visible behavior, trust boundary for external-tool
  text parsing, limitations, and test coverage.
- [ ] Evidence recorded below.

Evidence:

```text
Pending.
```

## Proposed Stack Shape

- [ ] Base PR: this ledger and delivery contract.
- [ ] Checkpoint 1 PR: worker-to-UI data-path work.
- [ ] Checkpoint 2 PR: structured staging events and overview state rendering.
- [ ] Checkpoint 3 PR: verbose Git/submodule progress collection and best-effort parser.
- [ ] Checkpoint 4 PR: archive expansion progress, subject to portability review.
- [ ] Checkpoint 5 PR: interaction and documentation follow-up, only if not naturally owned by an
  earlier checkpoint.