# Namespace Sandbox Validation Investigation

This is a local, intentionally uncommitted investigation note.

## Current Overwrite Finding And Repair

The overwrite cause is now established; the unresolved statements below are
historical. PR 53185 still publishes `f9f2bd8aa962ebd527fcc18e27c72c927edc2ddc`,
not the current local sandbox tip. Its latest hosted CI run (38073995380) has
failed unit-test jobs, not an all-green unit-test result. RHEL and macOS logs
explicitly report `test_install_overwrite[new]` with the same directory-hash
mismatch. Other platform failures also remain in that published run.

Every sandbox layer from 3 through 18 was tested independently in a fresh Git
clone, without concurrent installer-UI branches:

- Layers 3 through 7 passed the exact overwrite selector.
- Layer 8 first failed at `9c62df727d76dd616a3d77be7155dec44cea9ef2`.
- Every later layer, 9 through 18, also failed before repair.
- After repair, every layer from 3 through 18 passed that same selector.

Layer 8 added `pkg.stage.path` unconditionally to `SPACK_MANAGED_DIRS` in
`set_wrapper_variables`. The new installer creates a fresh private stage parent
for each build. That per-installation path was published in the build environment,
which is dumped to `spack-build-env.txt` and retained in the installed prefix.
The overwrite test hashes the prefix, including this recorded environment:
otherwise identical installations therefore had different hashes. Ordinary
stages were already covered by the configured stage root, so the additional
entry was unnecessary there. This is a sandbox-layer regression, not an
interaction with another GitButler stack.

The producer now adds an individual stage path only when it is outside the
configured stage root. Stages on a different Windows drive are also retained,
without invoking `commonpath` across drives. This preserves layer 8's intended
compiler-wrapper ordering when namespace worker temporary storage differs from
the supervisor stage root, without recording redundant random paths for ordinary
builds. The existing wrapper-variable test now asserts both ordinary-stage
omission and relocated-stage inclusion. The overwrite assertion was not weakened.

The fix and test were amended into layer 8, now
`1753cf5ca90525e6cd2890f294127bd44f6c3bcf`. The repaired sandbox tip is
`411d65febf9998c58761da0051c66b73086fe6bd`. No branches were pushed during this
investigation; the combined testing PR still needs publication of the repair.

### Reproducer

Starting from a fresh clone with the failing revision available:

```bash
git clone https://github.com/bernhardkaindl/spack.git spack-overwrite
cd spack-overwrite
python3 -m venv .venv
.venv/bin/python -m pip install \
  -r .github/workflows/requirements/unit_tests/requirements.txt
PYTHON_ROOT="$PWD/.venv/bin/python"
git worktree add --detach ../spack-overwrite-failing \
  f9f2bd8aa962ebd527fcc18e27c72c927edc2ddc
cd ../spack-overwrite-failing
env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL \
  PYTHONPATH="$PWD/lib/spack" "$PYTHON_ROOT" -m pytest -q \
  'lib/spack/spack/test/cmd/install.py::test_install_overwrite[new]'
```

The exact local runs used `/home/bkaindl/spack/spack/.venv/bin/python` (Python
3.12.3), fresh shared Git clones under `/tmp/spack-overwrite-series.O8u0ZZ`,
and pytest's default `/tmp/pytest-of-bkaindl` temporary root. Before repair,
layer 8 returned one failure with a prefix-hash mismatch. Repaired checkouts
and per-layer logs are under `/tmp/spack-overwrite-fixed.4VH1XW`.

### Aggregate Verification And Coverage

At the repaired sandbox-only tip, the complete Linux suite with xdist and
coverage passed: 7,280 passed, 197 skipped, 3 xfailed. Aggregate executable
diff-line coverage against `5ca6d8e0d031cf3dd447beaa94560aee1ecb721f` is
**91.91%** (1,694 changed executable lines, 137 missed), above the requested
85.26% gate. This includes the recently added layer-1, layer-3, and layer-4
validation tests plus the ordinary-stage regression assertion.

Per-file cumulative coverage is 100% for build_environment and bootstrap config,
92.47% for installer/build, 91.56% for sandbox_namespaces, 92.86% for sandbox,
and 100% for sandbox_base. Installer/core is 80.56%; the aggregate threshold is
met, but this measurement does not claim every file exceeds that threshold.
Exact percentages are available in `diff-aggregate.json` in the repaired
layer-18 checkout, alongside `coverage.xml` and `full-coverage.log`.

Aggregate import, Ruff format/check, and mypy passed. The two touched Python
files also passed Python 3.6 compatibility analysis and encoding-only pylint;
editor diagnostics reported no errors. Hosted Codecov's reported 83.29% uses
its own aggregation and cannot be certified by a local Linux line-coverage
result. A new push and hosted run are required to confirm that external gate.

The shared combined workspace also passes all six overwrite parametrizations
and all 45 build-environment tests after the layer-8 amendment.

## Remaining Native-Platform Follow-Up

The macOS failure in `test_probe_removes_bind_mount_directories` is a test
portability regression introduced by layer 8's descriptor-link probe. The test
uses fake libc mount operations but left `/proc/self/fd` reads real. On macOS,
the generated link is dangling; the hosted error explicitly names
`probe/shm/fd/13` with ENOENT. The cleanup test now maps descriptor-alias reads
to its own probe file. It continues to assert successful probe reporting and
complete removal of the probe root. Real Linux capability probing is unchanged.
This test repair was amended into layer 8.

The Windows `test_install_with_source[new]` failure points to a likely path
length regression introduced by layer 6's private-stage allocation. That layer
used the entire package-stage name as the temporary parent prefix, then repeated
the same name in the child directory. Using the root and package name from the
hosted fixture, `spack-src/configure` has a 268-character path, exceeding the
traditional Windows MAX_PATH limit. A fixed `.spack-stage-` parent prefix reduces
that path to 202 characters while keeping the identifying package child name.
The repair was amended into layer 6, with a lifecycle test enforcing a bounded
parent component even for long package names.

The Windows console exposes only the parent InstallError, and the run uploaded
no child build-log artifact. Therefore path growth is verified, but attribution
of that specific Windows child failure remains a hypothesis pending native CI.
No native macOS or Windows execution was possible on this Linux host. The fixes
do not claim to reproduce or certify all native platform behavior locally.

The repaired aggregate head is `47ac73ca2d5882fde7398b4df1b99db92c6a2cc3`.
Its isolated checkout under `/tmp/spack-platform-repair.YqhlkW/checkout` passed
137 focused probe/lifecycle/install tests, aggregate import/Ruff/mypy checks,
and the full Linux suite: 7,281 passed, 197 skipped, 3 xfailed. Executable
aggregate diff coverage remains 91.91%, above 85.26%. This note remains
uncommitted. The sandbox stack was pushed with GitButler, and PR 53185 now
publishes `47ac73ca2d5882fde7398b4df1b99db92c6a2cc3`; new CI is queued.
GitButler reported PR-metadata synchronization failures after the successful
push, but GitHub's testing-PR head was verified correct. No unrelated stack
was pushed. Native CI confirmation is still pending.

## What The Earlier Warning Meant

The combined testing PR is https://github.com/spack/spack/pull/53185.
Its repaired head was `5b76cddc6607eb066cb480907be957fa5b9e94e2`.
The first independent review layer is https://github.com/spack/spack/pull/53186,
at `805e0f4eef0e5b3bc916bfb6e78d9973f618d879`.

The full combined-head Linux test run was not green. The overwrite test
`lib/spack/spack/test/cmd/install.py::test_install_overwrite[new]` failed
with an installation-directory hash mismatch. It had also failed before
the latest stage-cleanup repair. Its cause and first affected layer have not
yet been established. The exact overwrite selector now passes independently
on both layer 1 (`805e0f4eef`) and layer 2 (`9430499515`): one pass each.
The combined-head regression does not reproduce on these first two layers.

That full run used a `git archive` snapshot, which has no `.git` metadata.
Tests that inspect Git history, removed packages, or reproduction revisions
also failed. An archive is suitable for focused sandbox tests, but those
Git-dependent failures are not a valid assessment of hosted CI. A full gate
needs a real Git checkout. The reporter failure from that run still needs
separate triage and must not automatically be attributed to missing Git.

## Confirmed Cross-Platform Fixes

Spawned workers on Windows and macOS reconstructed a default package stage
instead of using the supervisor's private stage parent. The supervisor then
removed its private directory, leaving the worker's stage outside it.
The installer-policy layer now rebinds all worker stages before installation.
Both reported install tests failed at teardown on the unpatched snapshot
under `spawn`, and passed after the repair under both `spawn` and `forkserver`.
Those checks ran on Linux; native platform confirmation comes from CI.

The mount-plan layer now expects `os.strerror(errno.EOPNOTSUPP)` instead of
Linux-only error wording. The first layer skips live namespace tests when the
host rejects namespace creation, preserving the failed operation and reason.
Actual masking failures on namespace-capable hosts still fail.

## Import-Check Follow-Up

The new stage-path regression test references `spack.util.timer` indirectly
through the imported installer module. Spack's import checker requires an
explicit import of that module. Fixed with an explicit `spack.util.timer`
import and a direct `spack.util.timer` monkeypatch target. An import alone
would leave Ruff's unused-import check failing.

The fix was amended into layer 6, `namespace-sandbox/v2/06-installer-policy`,
not into the first two layers. The updated combined head is
`f9f2bd8aa962ebd527fcc18e27c72c927edc2ddc`.
Its clean archive passes import, ruff-format, ruff-check, and mypy checks
for the repaired file; mypy checked 659 source files successfully.
The two stage-path regression cases also pass.

The merged workspace's style command independently reported a mypy error in
the concurrent installer-UI stack (`installer/ui.py`, optional string
assignment). That is absent from the isolated namespace head and was not
changed as part of this repair.

## Publication Gate

At the initial check on 2026-10-10, PR 1 had 27 successful checks, no failures,
and four pending checks: Windows, one macOS job, documentation, and GitLab CI.
Linux unit jobs and style had succeeded. Layer 2 must not be pushed or opened
until PR 1's checks and layer 2's required validation are green.

The 18 local branches each own one commit. The combined testing branch retains
the cumulative head. GitButler created PR 1 successfully but reported a failure
updating the combined PR's stack metadata. Both PR heads and `develop` bases
were verified correct; this warning is not a failed code push or test result.
GitButler's first-PR footer lists the two published PRs, not all 18 local layers.
Native GitHub cross-repository stacking is unavailable; publication follows
the installer-UI convention of incremental fork PRs against `develop`.

## Next Evidence

## Completed Follow-Up

The import repair was absorbed into layer 6 and pushed. GitHub confirms the
combined PR head is `f9f2bd8aa962ebd527fcc18e27c72c927edc2ddc`, still based
on `develop`. PR 1's commit was not changed.

Layer 2 was validated in a real isolated Git clone, not an archive:

- Revision: `9430499515ff48bbb6b5e95ff4989df7b7c130c2`.
- Focused sandbox/installer/configuration suite: 235 passed, 1 skipped.
- Executable diff-line coverage against its immediate parent: 91.97%.
- `bin/spack style --base HEAD^1`: import, Ruff format/check, and mypy passed.
- Full Linux gate: 7,121 passed, 197 skipped, 3 xfailed; no failures or errors.
- The exact overwrite selector passed on both layers 1 and 2.

The clone has Git metadata, but no local `develop` branch; some style-command
unit tests explicitly skipped for that reason. Other skips reflect unavailable
tools/platforms. This is a green local Linux run, not certification of all CI
platforms or complete coverage of every skipped test.

At the final PR 1 status snapshot, 28 checks had succeeded and none had failed.
Windows, GitLab CI, and Read the Docs remained pending. Both macOS checks had
succeeded. Layer 2's PR was therefore not opened.

### Push Scope Mistake

`but push namespace-sandbox/v2-review-shaped` pushes the selected branch and
all its ancestors. After splitting the series, pushing the combined repair
also created remote branches for layers 2 through 18. This crossed the user's
condition not to push layer 2 before all gates were green. No additional PRs
were opened. Remote branches were not deleted or rewritten to hide this action.
Layer 2's local checks subsequently passed, but PR 1's pending hosted checks
still block opening its successor PR.

### Reproduce Layer 2 Validation

From this workspace, use a new destination for the clone:

```bash
REPO=/home/bkaindl/spack/spack
PYTHON="$REPO/.venv/bin/python"
git clone --shared --single-branch \
  --branch namespace-sandbox/v2/02-mount-plan-validation \
  "$REPO" /tmp/spack-layer-2-review
cd /tmp/spack-layer-2-review
env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL \
  PYTHONPATH="$PWD/lib/spack" "$PYTHON" -m pytest -q \
  'lib/spack/spack/test/cmd/install.py::test_install_overwrite[new]'
env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL \
  SPACK_PYTHON="$PYTHON" PATH="$REPO/.venv/bin:$PATH" \
  bin/spack style --base HEAD^1
env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL \
  PATH="$REPO/.venv/bin:$PATH" PYTHONPATH="$PWD/lib/spack" \
  "$PYTHON" -m pytest -x --verbose --dist worksteal -n16
```

Evidence from this run is retained under
`/tmp/spack-namespace-followup.opvTrg/layer-2-checkout/`: `focused.log`,
`coverage.xml`, `style.log`, and `full-gate.log`.

The combined-head overwrite regression remains unresolved; it is a later-layer
investigation, not a reproduced blocker for either of the first two layers.
The combined-PR metadata-sync warning also remains; its code head and base
are correct. This note remains intentionally uncommitted.

## PR 4 Publication After the Platform Fix

Regular PR https://github.com/spack/spack/pull/53191 publishes layer 4 at
`ee9ab441a49c9a975c46c8cd16cfd8828320d11d`, titled
`installer-sandbox 4/18: select compiler and build-tool inputs`. Its reviewer
description explains compiler/helper selection, runtime/header inputs, and
canonical sources versus aliases, without local validation statistics.

The current layer differs from the earlier validated layer-4 tree only by the
six inherited PR 3 Linux-only test guards. Its exact isolated checkout under
`/tmp/spack-pr4-publication.MoMaAa/checkout` passed 297 focused tests (1 skipped)
and the full Linux suite with 7,183 passed, 197 skipped, and 3 xfailed. Import,
Ruff format/check, and mypy passed. Incremental executable diff coverage remains
94.55% for the namespace file, 88.21% for installer/build.py, and 89.44% overall.

The user explicitly permitted publication when PR 3 still had no failures,
rather than requiring all checks to finish. Immediately before publication,
the repaired PR 3 head had 31 reported checks, zero failures, and three pending
checks. These results do not claim that PR 3 had finished CI or that PR 4's
hosted CI passed. Only layer 4 and its ancestors were pushed; no later layer or
aggregate head was pushed. This investigation note remains uncommitted.

## PR 3 Windows and macOS Policy Test Scope

Hosted run `38083139160` failed on Windows while loading Linux absolute paths
from `sandbox.yaml`, and on macOS while testing root hiding after its temporary
directory resolved from `/var` to `/private/var`. These six policy tests use
the Linux filesystem layout; Windows and macOS do not support this backend.
They now skip immediately on non-Linux platforms. Shared sandbox wiring and
mocked backend tests remain enabled. Production path validation was not relaxed.

The guards were amended into layer 3 at
`bd7a14e6654d54836b74fc0d718c0a40bb739ad2`. PR 1 and PR 2 revisions are unchanged.
Only layer 3 was pushed, updating PR #53190. GitButler reported a review-metadata
sync warning after the successful push; GitHub head read-back confirmed all
three intended revisions, and their requested PR x/18 footers were restored.
Validation of the exact layer in `/tmp/spack-pr3-platform-fix.yAcpFI/checkout`:

- Calling each guarded test with Windows/macOS platform values confirms all
  six skip before accessing policy data or test arguments.
- Focused suite: 272 passed, 1 skipped.
- Full Linux suite: 7,158 passed, 197 skipped, 3 xfailed.
- Namespace-file incremental diff coverage: unchanged at 91.56%.
- Overall incremental executable diff coverage: unchanged at 91.43%.
- Import, Ruff format/check, mypy, encoding pylint, and Python 3.6 analysis pass.

Native Windows/macOS execution is not available locally; hosted CI must confirm
those platforms. To reproduce the original macOS failure in a fresh clone:

```sh
git clone https://github.com/bernhardkaindl/spack.git spack-pr3-repro
cd spack-pr3-repro
python3 -m venv .venv
.venv/bin/python -m pip install -r .github/workflows/requirements/unit_tests/requirements.txt
git worktree add --detach ../spack-pr3-original cf695de72be59e82e662fea416555ed27967da70
REPRO_PYTHON="$PWD/.venv/bin/python"
cd ../spack-pr3-original
env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL PYTHONPATH="$PWD/lib/spack" "$REPRO_PYTHON" -m pytest -q lib/spack/spack/test/sandbox_common.py::test_complete_namespace_policy_rejects_missing_selected_path
```

The original Windows failure is reproducible with this PowerShell equivalent:

```powershell
git clone https://github.com/bernhardkaindl/spack.git spack-pr3-repro
Set-Location spack-pr3-repro
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r .github/workflows/requirements/unit_tests/requirements.txt
git worktree add --detach ../spack-pr3-original cf695de72be59e82e662fea416555ed27967da70
$reproPython = "$PWD\.venv\Scripts\python.exe"
Set-Location ../spack-pr3-original
$env:PYTHONSTARTUP = ""
$env:PYTHON_BASIC_REPL = ""
$env:PYTHONPATH = "$PWD\lib\spack"
& $reproPython -m pytest -q lib/spack/spack/test/sandbox_common.py::test_namespace_policy_data_is_loaded_from_yaml
```

These native reproducers are documented from the hosted failures, not claimed
as locally executed. This investigation note remains uncommitted.

## PR 1 Coverage Follow-Up

The first layer was amended with tests for sandbox-construction errors,
unavailable mount-tree warnings, authority-drop rejection, worker setup errors
when its log cannot be written, and upstream sbang read access. No production
behavior was changed. Tests exercise mocked failures without privileged mounts.

The exact revised first-layer checkout passed:

- Focused tests: 222 passed, 1 skipped.
- Local executable diff-line coverage: 90.36% (previously 88.07%).
- Installer build-file diff coverage: 90.14% (previously 77.46%).
- Nine additional changed production lines covered; 38 of 394 remain missed.
- All four tools in `bin/spack style --base HEAD^1` passed.
- Full Linux suite: 7,108 passed, 197 skipped, 3 xfailed.

Only `namespace-sandbox/v2/01-backend-lifecycle` was pushed, updating PR #53186
to `6d0ad869083311465f94be775019f2210c109213`. It still contains one commit.
The other remote layers and combined testing PR were not pushed in this step;
their locally rewritten descendants need separate authorized publication.
Hosted CI and Codecov restarted. Local diff-cover does not reproduce Codecov's
multi-platform aggregation, so the final hosted patch percentage is pending.
The existing combined-PR metadata-sync warning recurred after the successful
single-branch push. Evidence is under `/tmp/spack-pr1-final.CXuaWa/checkout/`.

## Revised Layers 2 And 3 Readiness

Both layers were tested in isolated Git clones after the PR 1 coverage update.
No additional production or test changes were required. The threshold was
checked separately for each changed production file, on both the incremental
layer diff and the cumulative diff against the series base. Tests are omitted
from coverage by the repository's configuration. These are executable line
coverage results, not a prediction of Codecov's hosted aggregation.

| Check | Layer 2 | Layer 3 |
| --- | --- | --- |
| Revision | `a11857d24d39d2f7b1862e5a47e67b4bfce5f644` | `87a66eac936d192d91d65efc84742a8121cec98b` |
| Focused tests | 240 passed, 1 skipped | 266 passed, 1 skipped |
| Full Linux suite | 7,126 passed, 197 skipped, 3 xfailed | 7,152 passed, 197 skipped, 3 xfailed |
| Incremental executable diff coverage | 91.97% | 89.52% |
| Cumulative executable diff coverage | 90.52% | 90.46% |
| Import, Ruff format/check, mypy | Passed | Passed |
| Changed-file pylint encoding check | Passed | Passed |
| License, Python 3.6 compatibility, slots | Passed | Passed |

Incremental per-file coverage:

- Layer 2: `sandbox_namespaces.py` 91.97%.
- Layer 3: `sandbox_namespaces.py` 87.66%; `installer/build.py` 91.30%.

Cumulative per-file coverage, matching the fork-based PR's scope:

- Layer 2: installer build 90.14%, sandbox 92.86%, sandbox base 100%,
  namespace backend 90.00%.
- Layer 3: installer build 90.95%, sandbox 92.86%, sandbox base 100%,
  namespace backend 89.80%.

Every reported production-file diff exceeds 85.26%. Evidence is retained in
`/tmp/spack-series-next.xbaiyA/layer-2/` and `layer-3/`: `focused.log`,
`full-gate.log`, `coverage.xml`, `coverage.json`, `diff-incremental.json`,
and `diff-cumulative.json`. The clones have Git history; style-command unit
tests requiring a local `develop` branch still skip, as in earlier checks.

At the final hosted status check for PR 1's `6d0ad86908` head, 28 checks had
succeeded and none had failed. Windows unit tests, macOS Intel unit tests,
and GitLab CI remained pending. The user's condition to publish PR 2 only
after PR 1 is entirely green therefore was not met. Neither PR 2 nor PR 3
was pushed or opened in this follow-up. Previously created remote layers
still contain their older revisions. This note remains uncommitted.

## Layer 3 Coverage Margin Improvement

Layer 3 was amended locally with six validation cases: a file used as a hidden
root, a missing mount source, a generated path outside hidden roots, an invalid
generated-path directory flag, a mount target duplicating its hidden root, and
a forged read-write entry carrying read-only access. Tests check rejection and
the reported operation; input-validation cases also assert errno. These checks
exercise the policy validation boundary before mount mutation. No production
behavior was changed, and neither layer 1 nor layer 2 was modified.

The revised layer-3 commit is `cf695de72be59e82e662fea416555ed27967da70`.
Results from its exact isolated checkout supersede the earlier layer-3 numbers:

- Namespace-file incremental diff coverage: 91.56%, up from 87.66%.
- Installer-file incremental diff coverage: unchanged at 91.30%.
- Overall incremental diff coverage: 91.43%, up from 89.52%.
- Overall cumulative diff coverage: 91.20%, up from 90.46%.
- Six additional changed production lines covered.
- Focused suite: 272 passed, 1 skipped.
- Full Linux suite: 7,158 passed, 197 skipped, 3 xfailed.
- Import, Ruff format/check, mypy, and changed-test encoding pylint passed.

Evidence is retained under `/tmp/spack-policy-margin.AKoE2z/checkout/` in
`focused.log`, `full-gate.log`, `coverage.xml`, `diff-incremental.json`, and
`diff-cumulative.json`. No remote branches were pushed in this follow-up.
The publication gate remains unchanged; hosted Codecov aggregation still
needs confirmation when this layer is published. This note remains uncommitted.

## PR 2 Publication and Layer 4 Review

PR 1's current head has all 35 hosted checks successful. The quick code review
of layer 2 found no blocking issue in mount-plan validation, preservation,
restoration ordering, or read-only mount enforcement. Its branch was pushed at
`a11857d24d39d2f7b1862e5a47e67b4bfce5f644`.

GitButler then failed to create the upstream PR with HTTP 422 (invalid base):
its predecessor branch exists only in the fork. With explicit user permission,
the GitHub CLI created regular PR https://github.com/spack/spack/pull/53189
against upstream `develop`. Its description explains the dependency on PR 1
and the temporarily cumulative diff. The local GitButler stack was preserved.

Layer 3's quick review found no blocking issue in canonicalization, access
categories, or stage/scratch separation from hidden roots. Its previously
verified namespace-file incremental coverage remains 91.56%.

Layer 4's quick review found no blocking issue in selected compiler/tool
inputs, canonical-source versus alias handling, or replacement/generated-path
validation. Five useful rejection cases were added: missing and file-valued
replacement sources, relative symlink targets, aliases outside hidden roots,
and forged replacement entries with read-only access. These tests were amended
into layer 4; no production behavior changed. The revised commit is
`3809060550a82fbafe44f7e8f38a23e4d6c650bd`.

Results from the exact isolated layer-4 checkout:

- Namespace-file incremental executable diff coverage: 94.55%, up from 85.45%.
- Installer-file incremental executable diff coverage: 88.21%.
- Overall incremental executable diff coverage: 89.44%, up from 87.68%.
- Namespace-file cumulative executable diff coverage: 91.20%.
- Overall cumulative executable diff coverage: 90.73%.
- Focused suite: 297 passed, 1 skipped.
- Full Linux suite: 7,183 passed, 197 skipped, 3 xfailed.
- Import, Ruff format/check, mypy, encoding-only pylint, license, Python 3.6
  compatibility analysis, and slots checks passed.

Evidence is under `/tmp/spack-layer4-final.Ap59BF/checkout/`, including
`focused.log`, `full-gate.log`, `coverage.xml`, `diff-incremental.json`, and
`diff-cumulative.json`. Local executable line coverage is not a guarantee of
hosted Codecov's aggregate result. Capability-dependent live tests still require
supporting kernels; these quick reviews are not complete security audits.

Only layer 2 was published in this follow-up. The updated layers 3 and 4 and
the aggregate head were not pushed. The later aggregate overwrite failure
described above remains unresolved. This investigation note remains uncommitted.

## CI Checkpoint 2026-10-11: Paused for Overlapping Work

GitHub discovery found four numbered PRs, with no later numbered layer:
PR 53186 at `6d0ad869083311465f94be775019f2210c109213`, PR 53189 at
`a11857d24d39d2f7b1862e5a47e67b4bfce5f644`, and PR 53190 at
`bd7a14e6654d54836b74fc0d718c0a40bb739ad2` each have 35 successful
reported checks. PR 53191 remains at
`ee9ab441a49c9a975c46c8cd16cfd8828320d11d`; Windows and the aggregate
`all` check failed, and coverage was skipped.

Fetched current failed logs with
`gh run view 38088303241 --repo spack/spack --job 114319495628 --log-failed`.
The log confirms that `test_linux_header_policy_rejects_relative_compiler_roots`
fails while loading the shipped Linux policy: Windows rejects
`system_include_root` before the intended `compiler_roots` assertion.
The log is retained at `/tmp/installer-sandbox-windows-53191.log`.

GitButler identifies owning layer 4 as commit `srn` at the published SHA,
but the shared workspace already has unowned edits in both sandbox test files.
In particular, hunk `nr:d` removes the Linux platform guard from the adjacent
Linux-header unsafe-path test, directly overlapping this platform-boundary
repair area. Numerous other sandbox tests are also removed in dirty hunks.
Multiple applied sandbox histories share these files. Ownership and intended
coordination are uncertain; no dirty test edits were adopted, amended, or
discarded. Per the stop condition, created the paused marker rather than
rewriting the owning commit and its descendants across concurrent work.

No isolated reproduction, validation, repair attempt, push, or publication
was performed in this checkpoint. Resolve ownership of the existing sandbox
test edits before resuming. This note remains uncommitted.

## CI Checkpoint 2026-10-11T00:18:13Z: Validation Permission Blocker

Rechecked GitHub: only numbered layers 1-4 are published. PR #53191 still
has head `ee9ab441a49c9a975c46c8cd16cfd8828320d11d`, and current failed
Windows job 114319495628 confirms the same premature `system_include_root`
failure. Current dirty hunks do not change the failing test. The earlier
ownership blocker is not assumed to apply: a one-line replacement of its
shipped-policy load with `_minimal_header_policy(tmp_path)` would preserve the
`compiler_roots` assertion and use a platform-native absolute fixture.

Created an exact shared Git clone at
`/home/bkaindl/.copilot/session-state/ff4eb426-0def-4250-8ffa-55757796c9ca/files/layer4`;
verified its HEAD matches the PR. Prepared a local pytest plugin to simulate
drive-qualified Windows absolute-path validation only during policy loading,
while treating the Linux test temporary root as the native temporary path.
This is a controlled boundary simulation, not native Windows validation.

Attempted reproduction from that clone:

```bash
env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL \
  PYTHONPATH="$PWD/lib/spack:/home/bkaindl/.copilot/session-state/ff4eb426-0def-4250-8ffa-55757796c9ca/files" \
  /home/bkaindl/spack/spack/.venv/bin/python -m pytest -q -p windows_boundary \
  lib/spack/spack/test/sandbox_common.py::test_linux_header_policy_rejects_relative_compiler_roots
```

Execution was denied because no interactive approval was available. No test
result was produced; no repair attempt or validation pass is claimed.
Created the paused marker. Resume after authorized execution permissions allow
the isolated reproduction and required gates. No code edits, amendments,
pushes, or new PRs; concurrent changes preserved and this note uncommitted.

## CI Checkpoint 2026-10-11T00:39:45Z: Repair Validated, Push Blocked

PR #53191 still publishes `ee9ab441a49c9a975c46c8cd16cfd8828320d11d`.
Current failed Windows logs reproduce the premature `system_include_root`
error. All 35 reported checks/contexts on each of layers 1-3 are successful;
discovery found no numbered layer beyond 4.

Reused the exact Git clone documented above, with that HEAD and no source
changes other than the one-line fixture repair. A session-local simulation
plugin uses `pytest_runtest_call(tryfirst=True)` to intercept policy loading:
drive-qualified Windows absolute paths are required, while pytest's native
temporary root is accepted. The original fixture fails at `system_include_root`;
`_minimal_header_policy(tmp_path)` passes with the existing `compiler_roots`
assertion unchanged. This is not native Windows or macOS validation.

Expanded reproduction, after creating the clone as documented above:

```bash
cd /home/bkaindl/.copilot/session-state/ff4eb426-0def-4250-8ffa-55757796c9ca/files/layer4
env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL \
  PYTHONPATH="$PWD/lib/spack:/home/bkaindl/.copilot/session-state/c78c6253-3f5c-4e83-8e21-be264dbaa482/files" \
  /home/bkaindl/spack/spack/.venv/bin/python -m pytest -q -p windows_boundary \
  lib/spack/spack/test/sandbox_common.py::test_linux_header_policy_rejects_relative_compiler_roots
```

Original: one failure. Repaired: one pass. Native focused suite: 132 passed.
Ruff format and format-check, Ruff lint, repository import checks and mypy
(659 files), encoding-only pylint, and Python 3.6 compatibility analysis passed.
Full Linux gate: 7183 passed, 197 skipped, 3 xfailed in 86.26 seconds.
The full command was `.venv/bin/python -m pytest -x -q --dist worksteal -n16`,
using the absolute repository `.venv` interpreter, source-first PYTHONPATH,
`.venv/bin` prepended to PATH, and both startup variables unset. Pytest temporary
root was `/tmp/pytest-of-bkaindl`. Its environment preserved inherited
GIT_CONFIG entries 0-2 and appended entry 3:
`GIT_CONFIG_COUNT=4`, `GIT_CONFIG_KEY_3=safe.bareRepository`,
`GIT_CONFIG_VALUE_3=all`. This override applied only to isolated test processes.
The full runner and log are in this session's `files/full_gate.py` and
`files/full-gate.log`. Parent Git still reports `safe.bareRepository=explicit`.

Amended only session hunk `nr:22` into owning commit `srn`, producing layer 4
`3c3c34ce827a6569b3aa76555f0b3f607044533a`. Comparing that exact SHA against
the isolated tested working tree with `git diff --exit-code <SHA>` passed;
the old/new commit diff contains exactly one insertion and one deletion in
`sandbox_common.py`. Unrelated dirty hunks remain untouched.

`but push namespace-sandbox/v2/04-compiler-build-tools` failed with exit 128:
GitButler's Git subprocess rejects `/home/bkaindl/spack/spack/.git` as a bare
repository under the host's `safe.bareRepository=explicit` policy. No
test-only override was applied to GitButler or publication. Readback confirms
the remote PR SHA is unchanged, Windows/all still fail, and coverage is skipped.
Created the pause marker. Resume requires an authorized GitButler publication
environment compatible with host safety policy; then push the already validated
layer 4, verify its new remote SHA, and exit for hosted CI. Do not publish layer 5.
This note remains uncommitted.

## 2026-10-11T01:15:22Z - Layer 4 Windows regex repair

Current #53191 head is 3c3c34ce827a6569b3aa76555f0b3f607044533a.
Current run 38099640005 Windows job 114352856203 fails on
`match=str(policy_path)` with `incomplete escape \U`; this is not the prior
system_include_root validation-order failure. Layers 1-3 are fully green;
both layer-4 legacy statuses succeed, Windows/all fail and coverage is skipped.
Current failed logs are in this session's `files/layer4-windows-failed.log`.

Copy-paste reproducer from the local repository (existing repository .venv
already supplies requirements; no dependency installation was needed):

```bash
git clone --quiet --no-hardlinks --single-branch \
  --branch namespace-sandbox/v2/04-compiler-build-tools \
  /home/bkaindl/spack/spack \
  /home/bkaindl/.copilot/session-state/b3bdd9a3-8d41-4658-b2e9-f173f928bf2e/files/layer4-repro
cd /home/bkaindl/.copilot/session-state/b3bdd9a3-8d41-4658-b2e9-f173f928bf2e/files/layer4-repro
git rev-parse HEAD
env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL PYTHONPATH="$PWD/lib/spack" \
  /home/bkaindl/spack/spack/.venv/bin/python -m pytest -q \
  --basetemp='/home/bkaindl/.copilot/session-state/b3bdd9a3-8d41-4658-b2e9-f173f928bf2e/files/C:\Users\runneradmin' \
  lib/spack/spack/test/sandbox_common.py::test_linux_header_policy_rejects_relative_compiler_roots
```

Before: 1 failed with incomplete escape \U at position 82. After the selected
test-boundary fix: 1 passed. Linux backslash-path simulation is not native
Windows validation. Literal containment retains the diagnostic path requirement
and the existing compiler_roots assertion, without interpreting paths as regex.

Validation runner and complete output are in session files
`validate-layer4.py` and `layer4-validation.log`. Interpreter is repository
`.venv/bin/python` (Python 3.12), cwd is the exact clone above, PYTHONPATH is
that clone's lib/spack, PATH starts with repository .venv/bin, SPACK_PYTHON
is the repository .venv interpreter, and both startup/REPL variables are unset.
Only pytest processes append inherited GIT_CONFIG_COUNT=3 with
GIT_CONFIG_KEY_3=safe.bareRepository and GIT_CONFIG_VALUE_3=all, yielding
GIT_CONFIG_COUNT=4 and preserving entries 0-2. Ordinary Git, GitButler and
publication do not receive that override.

Ruff format/check/lint, `bin/spack style --base HEAD` (imports and mypy,
659 sources), changed-file encoding pylint and vermin Python 3.6 checks passed.
Focused sandbox suite: 132 passed. Full `pytest -x -q --dist worksteal -n16`:
7183 passed, 197 skipped, 3 xfailed in 99.39 seconds. Style tests requiring a
develop branch skipped in the single-branch clone; native Windows gaps remain.
The reviewed repair contains exactly two insertions and one deletion in the
relative compiler-root test. No production/import/slots/docs change is involved.
One successful local repair attempt for this regex failure; no hosted success
is claimed before pushing and checking the new SHA.

Amended only `nr:4d` into `srn`; layer 4 is now
`b3bee85034cd37645a09a39448b75ea0ed5a2eba`. Old/new diff is exactly the
reviewed 2 insertions and 1 deletion; `git show <new>:<test-file> | cmp -`
against the validated clone file passed. No other file changed in this layer.
Explicit-repository GitButler force-push dry run lists only layer 4.

2026-10-11T01:16:13Z: `GIT_DIR=/home/bkaindl/spack/spack/.git
GIT_WORK_TREE=/home/bkaindl/spack/spack but push
namespace-sandbox/v2/04-compiler-build-tools --with-force` succeeded (exit 0).
Reported ref update is layer 4 only. GitHub #53191 confirms exact head
b3bee85034cd37645a09a39448b75ea0ed5a2eba and base develop. Fresh checks are
queued/in progress, docs pending, DCO successful. Review synchronization
separately warned about failed updates for #53189, #53190, #53191 and #53185;
no retry, footer rewrite or PR creation was attempted. Exit for hosted CI;
native Windows and all published-head green gates still block layer 5.
Host Git safety policy was not changed. Evidence remains uncommitted.

## 2026-10-11T01:44:32Z - Layer 4 native compiler-query fixture

Current Windows job 114357682517 in run 38101263702 at
b3bee85034cd37645a09a39448b75ea0ed5a2eba failed query result3:
Python 3.14 Windows rejects drive-less /usr/bin/cc1 as absolute. This is
distinct from both earlier header-policy repairs. All three ancestors report
35 successful checks/contexts; only four numbered PRs were discovered.

Reproducer (repository .venv already contains test/style dependencies):

```bash
git clone --quiet --no-hardlinks --single-branch \
  --branch namespace-sandbox/v2/04-compiler-build-tools \
  /home/bkaindl/spack/spack \
  /home/bkaindl/.copilot/session-state/df1c7192-a534-4a16-9c42-77eaca2bd964/files/layer4
cd /home/bkaindl/.copilot/session-state/df1c7192-a534-4a16-9c42-77eaca2bd964/files/layer4
env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL \
  PYTHONPATH="$PWD/lib/spack:/home/bkaindl/.copilot/session-state/df1c7192-a534-4a16-9c42-77eaca2bd964/files" \
  /home/bkaindl/spack/spack/.venv/bin/python -m pytest -q \
  -p windows_query_simulation \
  lib/spack/spack/test/sandbox_common.py::test_compiler_query_failure_and_absolute_result
```

Simulation plugin is in the session files directory above. It enforces
drive-qualified Windows absolute paths only during this test and supplies
PureWindowsPath("C:/fixture") for tmp_path after the repair. Before: 1 failed,
3 passed. After: 4 passed. This is controlled Linux simulation, not native
Windows execution. Native tmp_path replaces only the absolute compiler-query
output; subprocess failure, exception, relative-path rejection, and whitespace
stripping remain asserted. One successful repair attempt for this failure.

`files/validate.py` records commands and exact environment; output is in
`files/validation.log` and `files/full-retry.log`. Interpreter is repository
.venv Python 3.12.3, cwd the exact layer clone, PYTHONPATH its lib/spack
(plus plugin directory for simulation only), PATH begins repository .venv/bin,
SPACK_PYTHON is repository .venv/bin/python, and both startup variables are
unset. Test subprocesses preserve inherited Git entries 0-2 and append entry 3:
GIT_CONFIG_COUNT=4, GIT_CONFIG_KEY_3=safe.bareRepository,
GIT_CONFIG_VALUE_3=all. Ordinary Git and publication retain the host policy.
Temporary root is /tmp/pytest-of-bkaindl.

Ruff format/check/lint, style imports/mypy (659 sources), changed-file encoding
pylint and vermin Python 3.6 checks passed; focused suite 132 passed.
Initial full gate hit unrelated test_generate_index_missing (6780 passed);
the exact failing node passed alone, then unchanged full suite passed:
7183 passed, 197 skipped, 3 xfailed in 94.15 seconds. No unrelated fix or
assertion weakening. Single-branch clone skips develop-dependent style tests.

Amended only session hunk nr:0 into srn. New layer SHA is
2a03e29ba0343d1ea03c33d69e46518678d780c6. Old/new layer diff changes only
the reviewed fixture (7 insertions, 3 deletions); git show/cmp confirms its
test file exactly matches the validated isolated clone. Other dirty hunks,
including concurrently changed unrelated names, were preserved.
Explicit GIT_DIR/GIT_WORK_TREE GitButler dry run selected layer 4; scoped
--with-force push succeeded and reported only its ref update. GitHub #53191
readback confirms that exact SHA and develop base, DCO success, two in-progress
checks and pending docs. Review synchronization warned separately; no retry.
No layer 5 PR, aggregate push, footer edit or safety-policy change. Exit now
for hosted CI at the new SHA; investigation and journal remain uncommitted.

## Aggregate Windows default configuration repair, 2026-10-11T01:50:41Z

PR #53185 Windows job 114355908057 failed at ebdb3201efac39e64058712e82dc7c05ad0cf0ee:
test_default_sandbox_config indexed None because default_config selects only
etc/spack/defaults/windows on Windows, omitting shared base sandbox settings.
Direct job logs were available through gh api even while the run continued.

Exact reproducer used a shared clone of the aggregate branch:

```bash
git clone --shared --branch namespace-sandbox/v2-review-shaped \
  /home/bkaindl/spack/spack \
  /home/bkaindl/.copilot/session-state/e0b76d44-67e6-49a0-96eb-7f222b544a73/files/windows-config-repro
cd /home/bkaindl/.copilot/session-state/e0b76d44-67e6-49a0-96eb-7f222b544a73/files/windows-config-repro
env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL \
  PYTHONPATH="$PWD/lib/spack:/home/bkaindl/.copilot/session-state/e0b76d44-67e6-49a0-96eb-7f222b544a73/files" \
  /home/bkaindl/spack/spack/.venv/bin/python -m pytest -q -p windows_defaults \
  lib/spack/spack/test/config.py::test_default_sandbox_config
```

The local windows_defaults plugin's tryfirst pytest_fixture_setup supplies
use_configuration(etc/spack/defaults/windows) for default_config, exactly as
the Windows fixture does, without globally pretending Linux is Windows.
Original result: the same NoneType TypeError. This is a scope simulation,
not native Windows execution.

Repair checks shared base settings and base plus Windows overrides explicitly,
parameterized on every host. No production settings changed or assertions
weakened. Config suite: 129 passed, 1 skipped. Original repaired aggregate full
gate: 7282 passed, 197 skipped, 3 xfailed. Import, Ruff format/lint and mypy
(659 files) passed. User requested absorption into the owning commit.

Waited for the active layer-4 worker to finish, temporarily stopped the timer,
then amended only config.py hunk nwk:1 into layer-12 commit ool. Owning layer
now 313663be6616aac57340233337e5ef6a1d3cf434. No new fixup commit; unrelated
dirty sandbox_common.py and customization files untouched.

Final aggregate c08dcfc4c8a77fcddd82f557662f68a1bfe3063f also includes the worker's
separate layer-4 Windows fixes. Its first full gate had an install-overwrite
hash assertion failure; that node passed alone with config/sandbox tests
(192 passed, 1 skipped). Repeated exact full gate passed:
7282 passed, 197 skipped, 3 xfailed in 103.20s. Final changed-file style passed.
Both full gates ran -x -q --dist worksteal -n16 with source-first PYTHONPATH,
repository .venv/bin on PATH, startup variables unset, and inherited Git config
entries preserved with test-process-only safe.bareRepository=all appended.

Held worker lock, verified the final local SHA, and pushed aggregate with
explicit GIT_DIR/GIT_WORK_TREE GitButler --with-force. GitHub confirms
c08dcfc4c8a77fcddd82f557662f68a1bfe3063f and fresh queued/in-progress CI.
Push also restacked existing remote refs 5-18; no numbered PRs were opened.
Review synchronization warned separately after successful push. Timer restored
active; hosted Windows success remains pending. This note remains uncommitted.

## Layer-4 compiler selection Windows boundary, 2026-10-11T02:15:56Z

#53191 at 2a03e29ba0343d1ea03c33d69e46518678d780c6 failed Windows job
114362353360 in run 38102837099. The historical header/compiler-query repairs
passed. The next test, test_selected_compilers_uses_language_edges_and_deduplicates,
loaded Linux sandbox.yaml and rejected hidden_roots before selection assertions.
The sibling supported-compiler-node test has the same boundary.

Exact reproduction (repository .venv dependencies were already available):

```bash
git clone --quiet --shared --branch namespace-sandbox/v2/04-compiler-build-tools \
  /home/bkaindl/spack/spack \
  /home/bkaindl/.copilot/session-state/ee01a1a9-8329-4e6f-aed0-5586b2cb3043/files/layer4
cd /home/bkaindl/.copilot/session-state/ee01a1a9-8329-4e6f-aed0-5586b2cb3043/files/layer4
env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL \
  PYTHONPATH="$PWD/lib/spack:/home/bkaindl/.copilot/session-state/ee01a1a9-8329-4e6f-aed0-5586b2cb3043/files" \
  /home/bkaindl/spack/spack/.venv/bin/python -m pytest -q \
  -p windows_policy_simulation lib/spack/spack/test/sandbox_common.py \
  -k test_selected_compilers
```

Plugin in session files wraps only policy loading with Windows drive-qualified
absolute-path semantics. Before: 2 failed with the hosted hidden_roots exception.
After: 2 passed. This is Linux simulation, not native Windows validation.
Supply compiler_languages explicitly to both selection tests, keeping all
language-edge, deduplication and supported-node assertions. No production change.
One successful repair attempt for this newly observed failure.

Session files/validate.py and validation.log record exact commands and output.
Interpreter repository .venv Python 3.12; cwd exact layer clone; source-first
PYTHONPATH (plugin path added only for simulation); PATH begins repository
.venv/bin; SPACK_PYTHON repository .venv/bin/python; both startup variables unset.
Pytest temp root /tmp/pytest-of-bkaindl. Test commands preserve inherited Git
entries 0-2 (safe.bareRepository=explicit, credential.interactive=never,
core.fsmonitor=false), append key 3 safe.bareRepository=all, and set count=4.
The initial focused reproducer needed no Git override. Publication and ordinary
operations retain the host safety policy.

Ruff format left file unchanged; format-check, lint, imports, mypy (659 sources),
encoding and Python 3.6 gates pass. Focused suite: 132 passed. Full exact-layer
Linux gate: 7183 passed, 197 skipped, 3 xfailed in 90.04 seconds. Single-branch
clone skips develop-dependent style tests. Native Windows remains hosted.

Minimal review identifies only two changed calls. Amended selected nr:23 and
nr:9 into srn; unrelated dirty hunks preserved. Exact amended layer is
09ac494329c3b03e1577f536b6d9d97c1c8692f6; old/new diff is only 2 insertions
and 2 deletions; git show/cmp verifies test file matches validated clone.
Explicit GIT_DIR/GIT_WORK_TREE GitButler dry run selected layer 4; scoped
--with-force push succeeded and reported only that ref update. Review sync
warning recorded separately; do not retry successful push. GitHub confirms
new head and develop base, DCO success, two in-progress checks and pending docs.
Exit for hosted CI; no layer 5 publication, aggregate push or safety change.
Investigation and journal remain uncommitted.

## Layer-4 compiler support fixture, 2026-10-11T02:40:44Z

#53191 head 09ac494329c3b03e1577f536b6d9d97c1c8692f6 fails Windows
job 114367430263 in run 38104581744 at
test_compiler_support_paths_ignores_bare_names_and_spack_binutils.
Its rooted POSIX fixture paths are not absolute on hosted Windows Python 3.14,
so the compiler-query boundary rejects every result. This is distinct from the
already repaired header, query and compiler-language fixtures.

Exact reproducer (repository .venv dependencies already available):

```bash
git clone --quiet --shared --branch namespace-sandbox/v2/04-compiler-build-tools \
  /home/bkaindl/spack/spack \
  /home/bkaindl/.copilot/session-state/d8c0a8e7-b87e-497b-a927-d86a088457db/files/layer4
cd /home/bkaindl/.copilot/session-state/d8c0a8e7-b87e-497b-a927-d86a088457db/files/layer4
env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL \
  PYTHONPATH="$PWD/lib/spack:/home/bkaindl/.copilot/session-state/d8c0a8e7-b87e-497b-a927-d86a088457db/files" \
  /home/bkaindl/spack/spack/.venv/bin/python -m pytest -q \
  -p windows_support_simulation \
  lib/spack/spack/test/sandbox_common.py::test_compiler_support_paths_ignores_bare_names_and_spack_binutils
```

Clone HEAD verified as the failing SHA. Session plugin scopes a build.os proxy
to this node: only native tmp_path paths represent drive-qualified absolute
Windows paths; rooted POSIX and bare names are rejected. Before: identical
empty-list assertion failure. After: 1 passed. Linux simulation, not native
Windows verification. Repair supplies native tmp_path compiler/helper/plugin
responses and canonical expected sources, preserving all selection, ordering,
bare-name and Spack-wrapper assertions. Production logic unchanged.
One successful repair attempt for this distinct failure.

Session files/validate.py and validation.log record all commands and output.
Validation cwd exact clone; repository .venv Python 3.12; source-first
PYTHONPATH; repository .venv/bin first on PATH; SPACK_PYTHON repository
.venv/bin/python; both startup variables unset; pytest temporary root
/tmp/pytest-of-bkaindl. Only test subprocesses append safe.bareRepository=all
at index 3, GIT_CONFIG_COUNT=4, preserving inherited indices 0-2
(safe.bareRepository=explicit, credential.interactive=never,
core.fsmonitor=false). Initial focused simulation needs no Git override.
Host/global configuration, GitButler and publication retain explicit policy.

Ruff format unchanged; format-check/lint, imports, mypy (659 sources),
encoding and Python 3.6 checks pass. Focused: 132 passed. Full Linux:
7183 passed, 197 skipped, 3 xfailed in 85.87s. Develop-dependent style tests
skip in this single-branch clone. Native Windows remains the next hosted gate.
Minimal review selects only nr:a8, nr:b and nr:6 into owning srn; unrelated
dirty hunks in the same file remain excluded. Do not publish layer 5.

At 2026-10-11T02:41:25Z selected amendment produced
b39947adbd45d0edf1b4e15b98810f441c76247c. git show/cmp confirms its test
file exactly matches the validated isolated clone; old/new layer diff is only
15 insertions and 9 deletions in this fixture. Workspace Ruff format/check/lint
also pass without formatting other hunks. Scoped GitButler dry run and actual
force push selected layer 4; output reports only that ref update.
Command-scoped GIT_DIR=/home/bkaindl/spack/spack/.git and
GIT_WORK_TREE=/home/bkaindl/spack/spack preserve publication safety policy.
Review sync warned after successful push; no retry. GitHub head readback
confirms the new SHA and develop base, DCO success, changes/triage in progress,
and docs pending. Exit for hosted CI; native Windows remains unverified.
No layer-5 publication or aggregate push. Notes remain uncommitted.

## 2026-10-11T03:19:28Z - Current Windows cc1 fixture repair

#53191 head b39947adbd45d0edf1b4e15b98810f441c76247c fails Windows
job 114371581028, run 38105957194, in
test_executable_support_paths_select_file_magic_and_cpp_cc1.
Python's Windows absolute-path check rejects the fixture's drive-less
/usr/lib/cc1 response. Production selection is correct; supply native
tmp_path helper output and canonical expected sources instead. Preserve both
file-magic and cc1 selection assertions. One successful repair attempt.

Exact reproducer from the repository root:

```sh
git clone --no-hardlinks --single-branch --branch namespace-sandbox/v2/04-compiler-build-tools /home/bkaindl/spack/spack /home/bkaindl/.copilot/session-state/8a6243c0-eecf-4465-8516-534c9f7f7deb/files/layer4
cd /home/bkaindl/.copilot/session-state/8a6243c0-eecf-4465-8516-534c9f7f7deb/files/layer4
env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL PYTHONPATH="$PWD/lib/spack:$(dirname "$PWD")" /home/bkaindl/spack/spack/.venv/bin/python -m pytest -q -p windows_helper_simulation lib/spack/spack/test/sandbox_common.py::test_executable_support_paths_select_file_magic_and_cpp_cc1
```

Clone HEAD confirmed exact failing SHA above. Existing repository .venv
dependencies suffice. Session files/windows_helper_simulation.py replaces
only build.os with a proxy implementing drive-qualified Windows isabs,
while accepting native /tmp fixture paths; no global path-module mutation.
Before: exact empty-list failure. After: 1 passed. Linux boundary simulation,
not native Windows evidence. Actual Windows remains the hosted gate.

Session files/validate.py and validation.log record commands/environment:
repository .venv Python 3.12, source-first PYTHONPATH, .venv/bin first on
PATH, SPACK_PYTHON explicit, startup variables unset, /tmp pytest temporary
root. Test subprocesses alone append safe.bareRepository=all at index 3,
GIT_CONFIG_COUNT=4, preserving inherited indices 0-2:
safe.bareRepository=explicit, credential.interactive=never,
core.fsmonitor=false. Ordinary operations and publication retain host policy.

Ruff format unchanged; format-check, lint, imports, mypy (659 sources),
encoding and Python 3.6 pass. Focused sandbox suite: 132 passed.
Initial full run: 7112 passed then unrelated buildcache signed-migration
listing assertion failed. Exact node rerun: 1 passed; full identical-environment
rerun: 7183 passed, 197 skipped, 3 xfailed in 87.11s. Logs:
files/buildcache-rerun.log and files/full-rerun.log. Develop-dependent style
tests skip in the single-branch clone. No unrelated production changes.
Minimal diff reviewed against layer 4: only this fixture's native helper and
canonical expectations; concurrent hunks elsewhere in the file excluded.

At 2026-10-11T03:20:07Z selected nr:11 amendment into srn produced
c12e453bfd554dc3f511b1d18c76fa08fbdf8512; git show/cmp verified the
exact amended test file matches the validated clone. Workspace Ruff format,
format-check and lint also pass without modifying unrelated hunks.
Explicit command-scoped GIT_DIR=/home/bkaindl/spack/spack/.git and
GIT_WORK_TREE=/home/bkaindl/spack/spack GitButler dry-run/force push report
only layer 4. Push succeeded with a separate review-sync warning; no retry.
GitHub #53191 confirms this SHA, develop base, DCO success and pending
changes/pr/docs checks. Return for hosted CI; no layer 5 or aggregate push.