---
name: spack-change-review
description: 'Make, validate, and review changes in the Spack repository. Use when implementing or reviewing Spack core, command, installer, solver, test, schema, documentation, or sandbox command-hardening changes; checking Ruff formatting, Python compatibility, focused tests, CI parity, generated files, Git history, security boundaries, protocols, or provenance.'
argument-hint: 'Describe the Spack change or review scope'
user-invocable: true
disable-model-invocation: false
---

# Spack Change And Review

Use this workflow to make or review repository changes with checks scaled to the affected behavior.
Prefer repository evidence and executable validation over assumptions about current conventions.

## Start From The Change Boundary

1. Inspect `git status --short` before reading or editing.
2. Establish the review base with `git merge-base HEAD develop` when the branch targets `develop`.
3. Read the narrow owning implementation, its nearest tests, and any applicable repository skill.
4. For implementation tasks, state one local behavior hypothesis and one cheap check that can
  disprove it.
5. Make the smallest grounded edit, then run that check before widening the change. For review-only
  tasks, do not edit; test hypotheses through targeted reads and non-mutating checks.

Keep committed, staged, unstaged, and untracked changes distinct. Preserve user changes even when the
worktree is dirty. Treat untracked editor settings and local configuration overrides, including
`.vscode/` and local files under `etc/spack/`, as user state unless the task explicitly owns them.
Do not stage them incidentally.

Do not use destructive Git operations or rewrite history unless the user explicitly requests it.
Do not create commits or branches unless requested. When a history rewrite is requested, identify the
merge base and intended final commit sequence before changing refs.
When a follow-up belongs to an earlier commit, make it a fixup and squash it into
that parent commit rather than leaving a standalone corrective commit.  Before
rewriting, identify the merge base, target commit, and final sequence.  Create
the fixup with ``git commit --fixup=<target>`` and use a non-interactive
``git rebase --autosquash`` plan to fold it into that target.  Reinspect the
resulting history and complete diff afterward.

## Repository Ownership

- `lib/spack/spack/`: core Python implementation.
- `lib/spack/spack/cmd/`: CLI commands and argument handling.
- `lib/spack/spack/solver/`: concretization and source-planning boundaries.
- `lib/spack/spack/installer/`: build, publication, metadata, and registration behavior.
- `lib/spack/spack/schema/`: configuration and data schemas; update schema tests with semantics.
- `lib/spack/spack/test/`: unit and integration tests; mirror the owning module where practical.
- `lib/spack/docs/`: RST documentation and developer contracts.
- External package repositories, including `spack/spack-packages`: package recipes, not core
  implementation.
- `var/spack/test_repos/`: package-repository fixtures used by core tests.
- `lib/spack/spack/vendor/`: generated vendoring output; inputs and patches live under
  `var/spack/vendoring/`.

Keep changes inside the narrow owning boundary. Follow existing APIs and helpers before adding a new
abstraction. Do not repair unrelated failures or reformat unrelated files.

## Code Clarity

Add or update module, class, method, and function docstrings when introducing or materially changing an abstraction. In code being actively changed, add concise comments for non-obvious decisions, policy branches, ordering constraints, or security boundaries. Comments should explain why the code takes that path rather than narrating syntax that is already clear from the code.

## Python Style And Compatibility

Spack declares Python `>=3.6` in `pyproject.toml`. The Ruff configuration uses
`target-version = "py37"`; this influences Ruff formatting and lint decisions but does **not** prove
Python 3.6 syntax or runtime compatibility.

For changed Python files, run review-only Ruff checks with explicit paths:

```console
ruff format --config pyproject.toml --check --diff FILES...
ruff check --config pyproject.toml --no-fix FILES...
```

Before implementation handoff, format every task-owned changed Python file, then rerun both
review-only commands to prove the resulting files are clean.

The explicit `--no-fix` matters because `pyproject.toml` sets `fix = true`. To apply formatting and
safe lint fixes intentionally:

```console
ruff format --config pyproject.toml FILES...
ruff check --config pyproject.toml --fix --no-unsafe-fixes FILES...
```

The repository wrapper also runs import checks, Ruff, and mypy:

```console
bin/spack style FILES...
bin/spack style --fix FILES...
hatch run style
```

Passing paths keeps Ruff focused. Mypy in the style wrapper checks the full `spack` package. Enable
package-recipe type checking only when relevant:

```console
SPACK_MYPY_CHECK_PACKAGES=1 bin/spack style FILES...
```

Use Vermin for the repository's static Python 3.6 compatibility gate:

```console
vermin --backport importlib \
  --backport argparse \
  --violations \
  --backport typing \
  -t=3.6- \
  -vvv \
  --exclude-regex lib/spack/spack/vendor \
  lib/spack/spack/ bin/ var/spack/test_repos
```

`# novermin` is reserved for intentional, guarded compatibility exceptions. Vermin is a static
syntax/API check, not a Python 3.6 runtime test. A real 3.6 runtime check requires an external
Python 3.6 interpreter or the RHEL 8 UBI `platform-python` CI environment; do not claim local runtime
coverage when that environment is unavailable.

## Tests

Run the narrowest affected test first:

```console
bin/spack unit-test -q lib/spack/spack/test/path/to/test.py
bin/spack unit-test -q lib/spack/spack/test/path/to/test.py::test_name
bin/spack unit-test -q -k 'expression'
```

Use discovery when ownership is unclear:

```console
bin/spack unit-test --list
bin/spack unit-test --list-long
bin/spack unit-test --list-names -k 'expression'
```

After focused checks pass, broaden according to blast radius. Shared contracts, protocols, schemas,
or cross-module behavior require all directly affected suites. Full local entry points are:

```console
bin/spack unit-test
hatch run test
```

The Linux CI shape is approximately:

```console
python -m pytest -x --verbose --dist worksteal -n4
```

Full tests need additional host tools and may vary by platform. Report skips, missing dependencies,
and unavailable environments rather than treating them as passes.

## CI-Oriented Prechecks

Use applicable checks rather than running every precheck for a narrow change:

```console
bin/spack python -m slotscheck \
  --exclude-modules="spack.test|spack.vendor|spack.installer.windows|spack.util.win_acl" \
  lib/spack/spack/
bin/spack style --base HEAD^1
bin/spack license verify
pylint -j "$(nproc)" --disable=all --enable=unspecified-encoding \
  --ignore-paths=lib/spack/spack/vendor lib
```

Always run `git diff --check` before presenting or committing a change.

## Documentation

For sandbox RST, load and follow `.github/skills/sandbox-documentation/SKILL.md`
as the owning workflow.

The definitive documentation checks are:

```console
make -C lib/spack/docs clean html
make -C lib/spack/docs linkcheck
```

Sphinx builds use warnings as errors and may need Git history, Graphviz, Inkscape, network access,
and the package repository. Before `make clean`, inspect ignored docs state and the nested package
checkout for user work; prefer a disposable docs tree when cleanup could delete unrelated state.
If Sphinx is unavailable, say so and run the bounded fallback on task-owned RST files:

```console
PYTHONPATH=lib/spack .venv/bin/python .github/workflows/bin/format-rst.py RST_FILES...
rg '(:doc:|:ref:|\.\. toctree::)' lib/spack/docs
git diff --check
```

The command uses the workspace virtual environment for formatter dependencies and
``PYTHONPATH=lib/spack`` for the source-tree ``spack`` imports. The RST formatter
rewrites files in place. In implementation tasks, use it only on task-owned files and inspect its
diff. In review-only tasks, run it on disposable copies or omit it and report the gap. Never restore
broad paths to undo formatter output in a dirty worktree. The fallback does not replace Sphinx
reference and warning validation.

Do not hand-edit generated documentation under `lib/spack/docs/_build/`,
`.spack/spack-packages`, `command_index.rst`, generated `spack*.rst`, or `spack.lock`.
`lib/spack/docs/_spack_root` is a generated symlink to the checkout; never edit source through that
alias. Edit `command_index.in` or command implementations as appropriate.

Do not hand-edit generated shell completions. Regenerate them with:

```console
bin/spack commands --update-completion
```

## Review The Complete Delta

Review both behavior and repository hygiene:

```console
git status --short
git diff --name-status develop...HEAD
git diff develop...HEAD
git diff --cached
git diff
git diff --check
git log --oneline --decorate develop..HEAD
```

Use `git log -p -- PATH` and `git blame PATH` when the current implementation does not explain a
constraint. Treat the merge-base diff as authoritative after rebases; intermediate commit messages
may describe superseded designs.
The reviewed delta is the union of committed branch changes, staged changes, unstaged changes, and
relevant untracked files; do not infer content coverage from `git status` alone.

In code review, lead with concrete defects and regression risks, ordered by severity and linked to
files and lines. Check malformed input, rollback, ordering, bounds, platform behavior, backward or
current-only compatibility, and missing tests. If no defect is found, state that and name residual
validation gaps.

## Sandbox Command-Hardening Boundary

The sandbox project incrementally hardens existing normal command paths that import package recipes.
Do not add a parallel installer or command implementation. Preserve ordinary command behavior while
introducing small, independently testable seams that can later execute in a confined worker.

The trusted parent owns command-line parsing, policy selection, process launch, response validation,
and terminal presentation. A less-trusted worker may import recipe code only after its confinement
backend is active. Transport must be bounded, byte-oriented, and structured; do not use pickle or
treat raw worker standard output as a terminal response.

Document the module, classes, and policy functions that implement a sandbox boundary with concise docstrings. Add short rationale comments at non-obvious trust transitions, ordered confinement steps, fallback decisions, and irreversible kernel-policy application points. Keep these comments local to the decision they explain and state the security or compatibility reason, not a restatement of the code.

Document each capability in `lib/spack/docs/sandbox/` before implementation, including the trust
boundary, validation, limits, failure behavior, tests, future uses, and toctree integration. Update
both `status.rst` and `roadmap.rst` when a planned milestone lands. Apply confinement only after a
focused contract and direct kernel-boundary tests demonstrate the preceding transport path.

## Completion Report

Summarize the behavior changed, the exact focused and broad checks that passed, skipped or
unavailable checks, and any residual risk. Do not claim Sphinx, Python 3.6 runtime, Landlock, live
network/package progression, or full-suite coverage unless it actually ran in the current
environment.