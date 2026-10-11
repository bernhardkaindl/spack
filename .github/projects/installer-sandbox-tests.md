---
name: installer-sandbox-tests
description: "Migrate namespace/installer sandbox tests out of sandbox_common.py into Linux-specific and installer-sandbox test files with one platform-support flag, across the 18-layer namespace-sandbox series."
status: active
paths:
  - lib/spack/spack/test/sandbox_common.py
  - lib/spack/spack/test/test_sandbox_namespaces.py
  - lib/spack/spack/test/test_namespace_probe_lifecycle.py
  - lib/spack/spack/test/linux/**
  - lib/spack/spack/test/installer/sandbox.py
  - lib/spack/spack/test/sandbox_base.py
  - lib/spack/spack/sandbox_base.py
---

# Installer-sandbox test migration

Approved plan (2026-10-11). The installer sandbox is Linux-only, but
`sandbox_common.py` holds 34 installer-sandbox tests and only one generic
`Sandbox` test, and CI repairs started bending Linux path literals to run on
Windows/macOS. Tests move to files named after what they test, and one flag,
`spack.sandbox_base.installer_sandbox_supported()`, skips them where the
installer sandbox is unsupported.

## Delivery policy

- Series: `namespace-sandbox/v2/01-backend-lifecycle` .. `18-bootstrap-ci-opt-out`
  (plus the empty `namespace-sandbox/v2-review-shaped` top branch).
- Layers 1-4 are published (#53186, #53189, #53190, #53191). Rewriting them
  needs the user's explicit push authorization; prepare locally only.
- Keep `installer-sandbox-ci` paused while history is rewritten.
- Backup refs: `refs/backup/test-migration-20261011/namespace-sandbox/v2/*`.
- The superseded uncommitted `sandbox_common.py` edits from the CI agent are
  saved in the session folder as `sandbox_common-uncommitted.patch` (worktree)
  and `sandbox_common-staged.patch` (older staged snapshot).
- Scratch worktree checkouts must use `git -c core.hooksPath=/dev/null`;
  otherwise GitButler's shared post-checkout hook tears down its hooks
  (restore with `but setup` from the main checkout).

## Target layout

| Path | Contents | Skip condition |
| --- | --- | --- |
| `test/linux/installer_sandbox.py` | installer-sandbox tests (from `sandbox_common.py`, plus installer tests from the namespace backend file in phase 2) | `not installer_sandbox_supported()` |
| `test/linux/sandbox_namespaces.py` | namespace backend tests | `sys.platform != "linux"` |
| `test/linux/sandbox_namespace_probe.py` | namespace probe lifecycle tests | `sys.platform != "linux"` |
| `test/sandbox_base.py` | generic `Sandbox` interface tests (phase 2) | none |
| `test/installer/sandbox.py` | platform-neutral installer-sandbox tests (phase 3, deferred) | `not installer_sandbox_supported()` |

`test/sandbox/` cannot be used: it would shadow the upstream `test/sandbox.py`.

## Checkpoints

- [x] Phase 0: classification, backups, GitButler spike.
  Evidence: rewriting refs while applied loses stack names; unapplying the
  stack, rewriting raw commit objects (tree/parent only) and re-applying keeps
  the stack, branch names and new paths.
- [x] Phase 1: path rewrite of all layers + flag + module-level skips.
  Evidence (2026-10-11): session scripts `rewrite_series.py` + `phase1.py`
  rewrote all 18 commits (old->new in `phase1.map`; tip `f29028ddf4`).
  Layer 1 adds `test/linux/__init__.py` and `installer_sandbox_supported()`
  in `sandbox_base.py`; each moved file gets a module `pytestmark`; the
  developer guide path follows the rename. `get_sandbox()` intentionally
  keeps its upstream `platform.system()` check (switching it broke tests
  that mock `spack.sandbox.platform`). `validate.sh` per layer: identical
  collected test IDs old vs new, ruff check/format clean, all moved tests
  pass (57..218). Simulated `sys.platform = "darwin"`: 218 skipped. mypy
  clean for `sandbox_base.py`. Workspace focused run: 325 passed. Stack
  re-applied via unapply -> update-ref -> apply; change IDs preserved.
  Not pushed: layers 1-4 now diverge from #53186/#53189/#53190/#53191.
- [x] Phase 2: per-layer cleanup (redundant per-test platform skips, Windows
  path workarounds, split installer tests out of the backend file, generic test
  to `test/sandbox_base.py`).
  Evidence (2026-10-11): `phase2.py` through `rewrite_series.py` on all 18
  layers (map `phase2.map`; tip `f3febcfcfe`). Removed 6 `sys.platform`
  `pytest.skip` blocks (installer), up to 11 `skipif(sys.platform != "linux")`
  markers (backend), 6 `skipif(not hasattr(os, "fork"))` markers (probe);
  restored layer 4's original `/usr/...` literals in the two compiler
  support tests (CI had rewritten them under `tmp_path` for Windows); dropped
  the `win32` sbang branch. `FakeLibc` and `namespace_setup` moved to
  `test/linux/conftest.py` (precedent: `spack.test.installer.conftest`).
  Fixed the stale `test_namespace_probe_lifecycle.py` comment. Unused imports
  pruned and files formatted with ruff. `validate.sh` per layer: identical
  union of collected test IDs, ruff clean, all pass (57..218). Tip: 218 pass
  serially; simulated darwin: 1 passed (`sandbox_base.py`), 217 skipped; mypy
  clean for new files. Workspace focused run: 325 passed. Kept the layer-9
  and layer-16 `tmp_path / "usr"` trees: they build real fake filesystems.
- [x] Phase 2b: import cleanup in `test/linux/installer_sandbox.py`.
  `ns` is the only alias for `spack.sandbox_namespaces` (plain import
  dropped); `build` is the only module-level alias for `spack.installer.build`.
  Function-local imports of already-bound modules and names were removed;
  local `from spack.installer.build import X` became `build.X`, local
  `from spack.sandbox_namespaces import X` became `ns.X`, and local
  `types.SimpleNamespace` uses the module import. Imports inside embedded
  child-process code strings are untouched. Applied per layer with
  `cleanup_imports.py` (AST-based, then `ruff format`); map `cleanup.map`,
  tip `09a2d1743e`. `validate.sh`: identical test IDs, ruff clean, all pass.
  Workspace run: 218 passed. mypy clean on the file.
- [x] Phase 2c: scoped fork warning and Python 3.6 floor.
  CPython 3.12+ warns (`DeprecationWarning`) when `os.fork()` runs while another
  thread is alive; the probe forks, and xdist workers keep a thread alive.
  `os.fork` is not scheduled for removal. Added a commented
  `filterwarnings` marker to `pytestmark` in `sandbox_namespaces.py`,
  `sandbox_namespace_probe.py`, and `installer_sandbox.py` only.
  Also replaced `subprocess.run(text=True)` (3.7+) in an embedded child script
  of `installer_sandbox.py` with `universal_newlines=True` (3.6). Maps:
  `scope.map`, then `scope2.map`; tip `383bc50f51`. `validate.sh`: identical
  test IDs, ruff clean, all pass in 18 layers. Workspace `-n 8`: 218 passed,
  no warnings. `vermin -t=3.6-` on all changed files: minimum 3.6.
- [x] Phase 2d: layer validation script with the CI gates, now in the repo as
  `.github/skills/installer-sandbox-publication/assets/validate-layers.sh`
  (+ `cov_gate.py`; pointers added to the publication skill and the CI agent).
  Per layer: ruff, vermin (CI flags + grep for
  `text=`/`capture_output=` in added lines, since vermin cannot see embedded
  child scripts), pylint `unspecified-encoding`, mypy, `license verify`,
  slotscheck, focused coverage (`cov_gate.py`: diff-cover against the parent
  layer, hard gate 85.26% per file and 89% overall, soft 90% for namespace files),
  and tests. Select with `CHECKS=`; override with `MIN_COVERAGE`. First run
  over `scope2.map`: everything passes except coverage at L6
  (`installer/core.py` 73.33%, 4 missed lines) and L12 (`installer/build.py`
  82.35%, 3 missed lines). The focused run can under-report; confirm with the
  full suite before adding tests.
- [x] Phase 2e: pre-publication review of layer 1. Reworded layer 1's commit
  message (it claimed "platform-agnostic wiring tests"; the tests are
  Linux-only and skipped elsewhere). `installer_sandbox.py` used both
  `build._enable_sandbox(...)` and a by-name import of `_enable_sandbox`; now
  only `build._enable_sandbox` (`enable_sandbox_alias.py`, map `enable.map`).
  Nothing else in layer 1 needed changes: ruff, vermin, pylint, mypy, license,
  slotscheck pass; diff coverage 90.00% overall, `sandbox_namespaces.py`
  89.55% (soft 90% target, hard gate 85.26% met).
  Layer 1 tip `d14c88b90c` (all layers rewritten, tree unchanged except
  `installer_sandbox.py`; backup of the previous tips: `scope2.map`). Pushed
  with `--with-force` to `bk/namespace-sandbox/v2/01-backend-lifecycle`
  (`6d0ad86908` -> `d14c88b90c`) on the user's authorization; PR #53186 head
  verified. GitButler's review synchronization warned for #53185, #53189,
  #53190 and #53191 (stack footers not updated; layers 2-4 on the remote still
  hold the old commits). Layers 2+ are not pushed.
  Second push (same day): `spack style --base HEAD^1` failed in CI with
  `redundant import: spack.error` in `sandbox_namespaces.py` (ruff does not
  report it). Added the `style` check to `validate-layers.sh`; removed the unused
  `import spack.*` lines with `redundant_imports.py` (map `redundant.map`).
  Layer 1 tip `495a735f7a`, pushed (`d14c88b90c` -> `495a735f7a`); PR #53186
  head verified. Layer 1 passed all checks incl. `style` and coverage (90.00%).
  Deferred (see `AGENTS.md`: first failing PR first): full all-layer rerun of
  `validate-layers.sh` with `style` on the rewritten layers 2-18 (the run showed
  redundant imports at L1-8 and L12, which `redundant.map` should fix, but is
  unverified on 2-18).
- [x] Phase 2f: PR 2 review and series tooling. 2026-10-11 ~14:00: PR #53186 head
  `495a735f7a` has no failing checks (prechecks, style, import-check, DCO pass;
  unit tests incl. Windows/macOS still running). Layer 2 (`980ba80a14`, map
  `l12.map`) needed no further changes and passes all checks with layer 1:
  ruff, style, vermin, pylint, mypy, license, slotscheck, 74 tests, diff coverage
  91.00%. Not pushed: the publication gate requires PR 1 green first (and remote
  #53189 still holds the old commits). New: skill `installer-sandbox-series`
  (loop + `assets/rewrite_series.py`, `assets/apply_map.py`) and the agent
  `.github/agents/installer-sandbox-series.agent.md`.
- [x] Coverage gaps (2026-10-11, while PR #53186 CI ran; only its Windows unit
  tests were still running, no failures). L6 `installer/core.py`: added
  `test_launch_failure_finalizes_build_lifecycle` (launcher raising finalizes the
  lifecycle as `BUILD_ERROR`), amended into L6 (`d4be702dca`). L12
  `installer/build.py`: the 3 "missed" lines were 2 real gaps and 1 line covered
  only by the wider suite (`test/install.py`); added
  `test_active_namespace_policy_network_failure_is_reported`, amended into L12
  (`707c7762ac`), diff coverage of `build.py` 94.1% with the focused set. Layers 7-18
  rebased cleanly; tips now L13 `d6bc3b21e0`, L18 `9040e25281`. Layers 1-4 are unchanged.
  `style` on all 18 layers (CHECKS=style, tips map) passes, which closes the deferred
  all-layer `style` rerun. Full validation of L6/L12 still happens when they are first.
- [ ] Phase 2h: layer-2 macOS flake fixed upstream and restacked. Root cause: a
  develop race in the shared binary index cache. Every xdist worker uses the
  `misc_cache` from `test/data/config/config.yaml`. `regenerate_spec_cache` deduped
  associations by index hash alone, so a second mirror with identical content lost
  its specs. `_remove_stale_cache_entries` deletes index files of mirrors that this
  process does not configure, and `_fetch_mirror_index` trusted a cache entry whose
  file was gone. Upstream draft PR #53192 (`823afbe432`, reproducers in the body).
  Layer 2 fix commit `48a40a35e7` (cherry-pick of `823afbe432`, with the failing CI
  run `38139332696` / job `114469964934` in its message). Layers 3-18 rebased as
  objects (`/tmp/l2restack/restack.map`, same subjects, change-id headers rebuilt)
  and the series refs moved with `apply_map.py`. Layer-2 gates at `48a40a35e7`
  pass: ruff, style, vermin, pylint, mypy, license, slotscheck, coverage
  (`binary_distribution.py` 89.00%), tests 159 passed under `-n 8`. The `ids` check
  is skipped on purpose because the fix adds tests. Pushed
  `980ba80a14 -> 48a40a35e7` (fast-forward) to `bk/namespace-sandbox/v2/02-mount-plan-validation`;
  PR #53189 head verified. CI run `38154431982` pending. Review sync warned for
  #53189-#53191 and #53185 (recorded, not retried). GitButler's sync had replaced
  the series footers with its own "part N of 5" block (which lists #53185, an older
  PR outside the series). Refreshed the footer block of #53186/#53189/#53190/#53191
  to `This is PR N/18 in a stack:` per the publication skill, with only that block
  changed (verified by read-back). Refresh again after every later push, because
  GitButler may overwrite it.
  Open: (1) layer 2 CI must be green before layers 3-18 are published;
  (2) pushed #53190 (`bd7a14e665`) and #53191 (`c12e453bfd`) still hold the pre-Phase-2
  commits, so their publication needs `--with-force` (standing authorization in the
  series agent; the local layer-3 changes are the Phase 2 cleanup recorded above, eight
  files including the test-layout move).
- [x] Phase 2g: PR #53186 green (35/35 at `495a735f7a`). Layer 2 `980ba80a14`
  validated again (ruff, style, vermin, pylint, mypy, license, slotscheck, 74 tests,
  91.00%) and pushed with `--with-force` (`a11857d24d` -> `980ba80a14`); PR #53189
  head verified, base `develop`, not draft. GitButler's review synchronization
  warned for #53189-#53191 and #53185 (recorded, not retried). Refreshed the footer
  of #53186/#53189/#53190/#53191 to `This is PR N/18 in a stack:`. Layers 3-4 on the
  remote still hold the old commits. Next: wait for #53189 CI, then layer 3
  (`d822b8c412`).
- [x] Phase 2i: layer 3 republished. PR #53189 (layer 2) green; layer 3 `a480d05b3d` (on
  `48a40a35e7`) passed ruff, style, vermin, pylint, mypy, license, slotscheck, coverage
  (91.00%, per-file gate met), tests 88 passed; ids check skipped (layer adds tests).
  Pushed with `--with-force` `bd7a14e665 -> a480d05b3d`; PR #53190 head verified, base
  `develop`, not draft. Review sync failed for #53189-#53191 and #53185 (recorded, not
  retried). GitButler replaced the footer; restored to `This is PR 3/18 in a stack:`.
  Next: layer 4 (#53191) after CI on #53190; #53191 still holds pre-Phase-2 commits.
- [x] Phase 2j: layer 4 and layer 5 published. Layer 4 `unit-tests / windows` failed
  (POSIX literals); fixed with `os.path.realpath`, pushed `7bfc9a6abd`. Codecov patch
  84.46% (target 85.26%) led to build.py branch tests and a replacement-mount test;
  layer 4 validated at 99.00% and pushed. Gates: per-file 85.26% (hard), total floor
  89% (hard; lowered from 90 at the user's request so layer 2 at 89.00% passes), soft
  90% for namespace files. The series agent and skill gained a GitLab exception: a
  pending `ci/gitlab-ci` alone does not block the next layer.
  Layer 4 `ci/gitlab-ci` later became ERROR ("Pipeline failed", GitLab pipeline 1865607
  on `7bfc9a6abd`; the job log needs GitLab auth). Codecov patch is absent from its
  rollup. Layer 4 is still the first failing layer; the cause is unknown.
  The user chose to publish PR 5 anyway. Layer 5 `6eca412f86` (on `7bfc9a6abd`):
  validated cov 100.00% of 116 lines, 150 tests, all gates passed. Force-pushed
  `81b107d178 -> 6eca412f86`; the old remote head was a stale pre-rewrite history with
  no open PR. PR #53194 is open (base `develop`, not draft); footers restored on
  #53186-#53194. Step 9 next-layer preparation for layer 6 follows.
  Layer 4 (#53191) GitLab ERROR: pipeline 1865607 -> child 1865608, job
  `hep-generate` (25236191) failed in `spack ci generate` with HTTP 502 fetching
  `gitlab.spack.io/spack/resource-requests`. Infrastructure, not PR code; the series
  continues under the GitLab flakiness rule. Layer 6 (#53195, `49296c6825`) published
  after validation (L6 cov 90.00%, `installer/build.py` 89.43% soft). Footers restored
  on #53186-#53195. Next: re-check #53191 `ci/gitlab-ci` and PR 5/6 CI; layer 7 after.

- [ ] Phase 3 (deferred): promote platform-neutral installer tests to
  `test/installer/sandbox.py`. Precondition to re-add it to the todo list: a v3
  re-spin of the installer-sandbox series (for example, adding Windows AppContainers
  as an installer-sandbox backend on Windows, or a macOS sandbox variant). Until
  then it is not active work and must not displace the current series layers.
  Decided by the user: stay deferred until that re-spin.
- [x] Follow-up: updated `.github/skills/spack-unit-tests/SKILL.md` focused-suite
  paths to `test/sandbox_base.py`, `test/linux/installer_sandbox.py`,
  `test/linux/sandbox_namespaces.py`, `test/linux/sandbox_namespace_probe.py`.
  The focused run of those four files passes (219 tests).

## Series skill changes (this session)

- New skill `.github/skills/spack-pr-descriptions/SKILL.md` holds the PR-body
  writing rules, moved verbatim from `installer-sandbox-publication`, which now
  links to it. It also defines interactive vs unattended edits and the PR #53186
  exception.
- `spack-commit-messages` now requires a concise subject (72 chars or less), a
  short body (40-90 words), and the user's `Signed-off-by` as the last line, with
  `Co-authored-by: Copilot` above it on new agent-made commits. The old
  "do not invent Signed-off-by" rule is removed.
- Series skill step 9 (prepare the next layer while the pushed layer's CI runs)
  and an "Interactive and unattended runs" section were added. The series agent
  and the CI agent link to them.
- PR #53189 (layer 2) body: the one-time exception was applied after the user
  chose the drafted prose. The footer block is unchanged. Draft kept at
  `files/pr2-body.md`, previous body at `files/pr2-body-current.md`.
- Layer 2 CI, commit 980ba80a14: `unit-tests / macos (macos-15-intel, 3.14)`
  failed in `binary_distribution.py::test_buildcache_cmd_smoke_test` (`corge` and
  `symly` not in the store after `buildcache install`). It does not run sandbox
  code, and layer 1 passed the same macOS job. Root cause and fix: Phase 2h.
  Develop itself reproduces this race: 9 failures in 300 clean develop runs
  (4 confirmed `MatchError` of the same symptom); 0 in 120 runs with the fix.
  Unexplained and not fixed by this change: run-28 (`test_url_buildcache_entry_v2_exists`,
  one in about 28 runs before the in-update retry; not reproduced in 300 clean develop
  runs or in 120 runs with the fix), the prune test (1 in 300 on develop), and
  `test_install_v2_layout[unsigned]` (probable, same family). Fallback if run-28
  reappears in CI: isolate the `misc_cache` per test.

## Tests in migration

Source of truth for per-test features and introducing layer:
session file `classify.txt`. Summary at series tip:

| Source | Tests | Destination (phase 1) | Phase 2 moves |
| --- | --- | --- | --- |
| `sandbox_common.py` | 35 | `linux/installer_sandbox.py` | `test_allow_read_reports_both_the_requested_and_resolved_path` -> `sandbox_base.py` (done) |
| `test_sandbox_namespaces.py` | 86 | `linux/sandbox_namespaces.py` | 18 installer tests -> `linux/installer_sandbox.py` (done) |
| `test_namespace_probe_lifecycle.py` | 16 | `linux/sandbox_namespace_probe.py` | none |

Installer tests moved in phase 2 from the backend file (by introducing layer):
`test_installer_normalizes_landlock_initialization_error`,
`test_worker_prepares_namespace_before_tee`,
`test_pre_thread_namespace_failure_is_reported`,
`test_installer_mounts_before_landlock`,
`test_default_mask_does_not_hide_tools` (layer 1);
`test_namespace_policy_validation_precedes_namespace_mutation`,
`test_namespace_policy_validation_is_unconditional` (layer 6);
`test_active_namespace_policy_skips_landlock` (layer 7);
`test_private_device_setup_failure_stops_before_tee`,
`test_namespace_worker_root_validation`,
`test_namespace_worker_environment_rejects_existing_paths`,
`test_namespace_worker_environment_unchanged_for_fallback`,
`test_live_private_devices_and_concurrent_shared_memory`,
`test_live_namespace_worker_environment` (layer 8);
`test_pre_thread_setup_prepares_namespace_without_dropping_authority`,
`test_pre_thread_setup_keeps_network_namespace_when_allowed`,
`test_staging_rejects_active_network_namespace`,
`test_namespace_authority_drops_after_recipe_setup` (layer 12).
- 2026-10-12: Layer 7 published as #53196 (`namespace-sandbox/v2/07-production-activation`,
  tip `e63f4b9d42` on layer 6 `49296c6825`; validate-layers output in `/tmp/l7-validate.log`,
  all gates passed, L7 cov 92.00% of 130 lines). The remote branch held the pre-rewrite
  `5dc4ff7cf5` (no PR), so the push used `--with-force`. Footers refreshed on #53186, #53189,
  #53190, #53191, #53194, #53195, #53196. PR 4 (#53191) `ci/gitlab-ci` failure accepted by the user.
  Mistake during the footer refresh: a wrong PR-number map also edited #53187, #53188 and #53192
  (other stacks). Their bodies were restored from GitHub body-edit history (`userContentEdits`),
  verified to match the prose before the restore. Next: layer 8 (`221389824e`, validated in `/tmp/l8-validate.log`).

### 2026-10-12 - Footer guard added

- Added `.github/skills/installer-sandbox-publication/assets/refresh_footers.py`. It takes PR numbers only from `published-prs.json`, checks each live PR's number, head branch, and `installer-sandbox N/18:` title before any write, backs up original bodies, and reads each body back. Dry run by default.
- The series and CI agents, the publication skill, and the series skill now require this script for footer edits and forbid `gh pr edit` on any PR outside the inventory (for example #53187, #53188, #53192).
- Dry run on all seven series layers: every footer already correct, nothing written.
- Layer 7 (#53196) status at this point: head `e63f4b9d42`, unit tests, bootstrap jobs and `ci/gitlab-ci` still pending. Not green yet.

- Added to the series agent: when a PR's CI fails from a GitLab infrastructure error, newer runs for other PRs have passed, and no `@spackbot run pipeline` comment was recently posted, post `@spackbot run pipeline` on that PR to re-run its failed jobs. Not triggered yet: #53196 has no failure.
