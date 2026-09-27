---
name: spack-unit-tests
description: "Run and troubleshoot Spack unit tests and local CI checks. Use for focused pytest checks, the complete unit-test gate, pre-commit validation, xdist parallelism, VS Code terminal contamination, and test dependencies."
---

# Spack Unit Tests

Use the repository's `.venv` and validate the narrowest affected behavior before
running the complete suite.

## Environment

Run tests from the repository root with the source tree first on `PYTHONPATH`.
Prefix commands with `PYTHONSTARTUP=` to prevent the VS Code Python extension
from injecting startup output into subprocesses:

```bash
PYTHONSTARTUP= PYTHONPATH="$PWD/lib/spack" .venv/bin/python -m pytest ...
```

Install missing unit-test dependencies into `.venv`, not the system Python:

```bash
PYTHONSTARTUP= .venv/bin/python -m pip install \
  -r .github/workflows/requirements/unit_tests/requirements.txt
```

For checks launched through `bin/spack python`, set `SPACK_PYTHON` explicitly.
Changing `PATH` alone may still select `/usr/bin/python3`, which cannot import
packages installed only in `.venv`:

```bash
PYTHONSTARTUP= SPACK_PYTHON="$PWD/.venv/bin/python" \
  ./bin/spack python -m <module> ...
```

The VS Code Python extension may export `PYTHONSTARTUP` and
`PYTHON_BASIC_REPL=1`. These inject `Ctrl click to launch VS Code Native REPL`
into subprocess output and break tests such as
`lib/spack/spack/test/cmd/python.py::test_python`. Unset both variables for
CI-like test runs:

```bash
PYTHONSTARTUP= PYTHON_BASIC_REPL= \
  PYTHONPATH="$PWD/lib/spack" \
  .venv/bin/python -m pytest ...
```

Use `env -u PYTHONSTARTUP -u PYTHON_BASIC_REPL` instead when you need to
remove both variables entirely rather than override them with empty values.

## Focused Validation

Run the smallest test or test files that can falsify the current change:

```bash
PYTHONSTARTUP= PYTHON_BASIC_REPL= \
  PYTHONPATH="$PWD/lib/spack" \
  .venv/bin/python -m pytest -q path/to/test.py::test_name
```

For namespace sandbox changes, the recurring focused suite is:

```bash
PYTHONSTARTUP= PYTHON_BASIC_REPL= \
  PYTHONPATH="$PWD/lib/spack" \
  .venv/bin/python -m pytest -q \
  lib/spack/spack/test/sandbox_common.py \
  lib/spack/spack/test/test_sandbox_namespaces.py \
  lib/spack/spack/test/test_namespace_probe_lifecycle.py
```

Add installer and configuration tests when those areas change:

```bash
PYTHONSTARTUP= PYTHON_BASIC_REPL= \
  PYTHONPATH="$PWD/lib/spack" \
  .venv/bin/python -m pytest -q \
  lib/spack/spack/test/installer/build.py \
  lib/spack/spack/test/installer/core.py \
  lib/spack/spack/test/config.py
```

## Complete Unit-Test Gate

The Linux CI workflow runs pytest with xdist and stops at the first failure.
This machine has 16 physical cores and 32 hardware threads; `-n16` completed
the full suite successfully and is the preferred local setting:

```bash
PYTHONSTARTUP= PYTHON_BASIC_REPL= \
  PATH="$PWD/.venv/bin:$PATH" \
  PYTHONPATH="$PWD/lib/spack" \
  .venv/bin/python -m pytest \
  -x --verbose --dist worksteal -n16
```

Use fewer workers if the machine has fewer cores or a failure indicates resource
pressure. Do not restart a healthy long-running suite merely to change worker
count.

## Failure Triage

Every failure report must include a copy-paste reproducer before proposing a
fix. The reproducer must start immediately after `git clone`: include dependency
setup, revision or worktree creation, directory changes, environment variables,
and the test command. Preserve the exact test selector, working directory,
interpreter, `PYTHONPATH`, temporary-directory location, relevant environment
variables, and revision that produced it.

1. Rerun the failing node alone with startup output suppressed:

  ```bash
  PYTHONSTARTUP= PYTHON_BASIC_REPL= \
    PYTHONPATH="$PWD/lib/spack" \
    .venv/bin/python -m pytest -q path/to/test.py::test_name
  ```

2. If it passes in the current tree, do not call the failure stale yet. Compare
  the original and current revision, worktree path, current directory, pytest
  configuration, interpreter, and environment. Print the values used by the
  reproducer:

  ```bash
  printf 'cwd=%s\n' "$PWD"
  git rev-parse HEAD
  git status --short
  PYTHONSTARTUP= .venv/bin/python -m pytest --version
  PYTHONSTARTUP= .venv/bin/python -c \
    'import os, sys; print(sys.executable); print(os.getcwd()); print(os.environ.get("PYTHONPATH"))'
  ```

3. If the failure came from another commit or worktree, reproduce it there
  instead of changing the current checkout. Starting in the freshly cloned
  repository, replace only `FAIL_COMMIT`, `TEST_SELECTOR`, and `PYTHON_ROOT` in
  this complete command. The worktree can be any unused path; a sibling keeps
  it easy to inspect:

  ```bash
  python3 -m venv .venv
  PYTHONSTARTUP= .venv/bin/python -m pip install \
    -r .github/workflows/requirements/unit_tests/requirements.txt \
    -r .github/workflows/requirements/style/requirements.txt
  FAIL_COMMIT=<commit-that-failed>
  TEST_SELECTOR=path/to/test.py::test_name
  PYTHON_ROOT="$PWD/.venv/bin/python"
  REPRO_DIR="$(dirname "$PWD")/spack-issue"
  git worktree add --detach "$REPRO_DIR" "$FAIL_COMMIT"
  cd "$REPRO_DIR"
  PYTHONSTARTUP= PYTHON_BASIC_REPL= \
    PYTHONPATH="$PWD/lib/spack" \
    "$PYTHON_ROOT" -m pytest -q "./$TEST_SELECTOR"
  ```

  Report the full expanded command and its output. Do not report a current-tree
  pass as a fix unless the same revision and environment now pass.

4. For path-sensitive tests, preserve both the worktree and pytest temporary
  directory locations. For example, the style test's original
  `assert __file__ in output` can pass accidentally when pytest uses `/tmp`:
  output such as `../../../home/user/spack-issue/...` contains the absolute
  `/home/user/spack-issue/...` string. A sibling worktree reproduces the defect
  deterministically when `--basetemp` is also a sibling:

  ```bash
  python3 -m venv .venv
  PYTHONSTARTUP= .venv/bin/python -m pip install \
    -r .github/workflows/requirements/unit_tests/requirements.txt \
    -r .github/workflows/requirements/style/requirements.txt
  FAIL_COMMIT=<commit-that-failed>
  REPRO_DIR="$(dirname "$PWD")/spack-issue"
  TEST_TMP="$(dirname "$PWD")/spack-test-tmp"
  PYTHON_ROOT="$PWD/.venv/bin/python"
  git worktree add --detach "$REPRO_DIR" "$FAIL_COMMIT"
  cd "$REPRO_DIR"
  PYTHONSTARTUP= PYTHON_BASIC_REPL= \
    PYTHONPATH="$PWD/lib/spack" \
    "$PYTHON_ROOT" -m pytest -q --basetemp="$TEST_TMP" \
    ./lib/spack/spack/test/cmd/style.py::test_style
  ```

  This emits `../../spack-issue/...`, which does not contain the absolute
  worktree path and exposes the assertion. Reproduce with the original
  temporary-directory placement, not only from the main checkout.

5. If the node passes alone, reproduce ordering or process-state dependencies
  with the smallest neighboring test sequence. Keep tests scoped to the
  behavior they assert, and stub unrelated privileged operations such as
  namespace mutation when testing a later error boundary.

6. Treat output added by `PYTHONSTARTUP` as terminal contamination, not a source
  regression; confirm by rerunning with `PYTHONSTARTUP=` or both VS Code
  variables unset.

7. Apply formatter or test-required changes to the commit that owns the
  behavior, then rerun the exact reproducer and the complete gate. Do not
  accept a known-red intermediate commit in a review series.

## Local CI Checks

Before committing, run the checks that match the changed files. This sequence
matches the repository's precheck workflow; install the style tools once into
`.venv` if they are missing:

```bash
PYTHONSTARTUP= .venv/bin/python -m pip install \
  -r .github/workflows/requirements/style/requirements.txt
PYTHONSTARTUP= PATH="$PWD/.venv/bin:$PATH" \
  PYTHONPATH="$PWD/lib/spack" bin/spack style --base HEAD^1
PYTHONSTARTUP= PATH="$PWD/.venv/bin:$PATH" \
  PYTHONPATH="$PWD/lib/spack" bin/spack license verify
PYTHONSTARTUP= PATH="$PWD/.venv/bin:$PATH" \
  PYTHONPATH="$PWD/lib/spack" \
  pylint -j "$(nproc)" --disable=all --enable=unspecified-encoding \
  --ignore-paths=lib/spack/spack/vendor lib
PYTHONSTARTUP= .github/workflows/bin/format-rst.py \
  $(git ls-files 'lib/spack/docs/*.rst')
PYTHONSTARTUP= PATH="$PWD/.venv/bin:$PATH" \
  PYTHONPATH="$PWD/lib/spack" \
  vermin --backport importlib --backport argparse --violations \
  --backport typing -t=3.6- -vvv \
  --exclude-regex lib/spack/spack/vendor \
  lib/spack/spack/ bin/ var/spack/test_repos
PYTHONSTARTUP= PYTHON_BASIC_REPL= \
  SPACK_PYTHON="$PWD/.venv/bin/python" \
  ./bin/spack python -m slotscheck \
  --exclude-modules="spack.test|spack.vendor|spack.installer.windows|spack.util.win_acl" \
  lib/spack/spack/
```

Run `ruff format` on every changed Python file, then verify formatting and lint:

```bash
PYTHONSTARTUP= .venv/bin/ruff format <changed-python-files>
PYTHONSTARTUP= .venv/bin/ruff format --check <changed-python-files>
PYTHONSTARTUP= .venv/bin/ruff check <changed-python-files>
```

The changed-file repository style gate is:

```bash
PYTHONSTARTUP= PATH="$PWD/.venv/bin:$PATH" PYTHONPATH="$PWD/lib/spack" \
  bin/spack style --base HEAD^1
```

Report pre-existing repository-wide mypy failures separately from regressions in
the changed files; do not silently claim a failing style gate passed.