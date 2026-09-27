---
name: spack-build-verification
description: "Run quick local Spack package build verifications. Use when installing, reinstalling, or uninstalling a spec to smoke-test repository changes with the current checkout."
argument-hint: "Provide the Spack spec and desired install, reinstall, or cleanup check"
---

# Spack Build Verification

Run package verification commands from the repository root so they use the current
checkout rather than another Spack installation.

## Bash Environment

Source the local setup script in the same shell command before invoking Spack:

```bash
. share/spack/setup-env.sh && spack <command>
```

Do this for each independent terminal invocation unless the persistent terminal has
already sourced the script and remained in the repository root.

## Quick Checks

Use the cheapest command that exercises the changed behavior:

```bash
. share/spack/setup-env.sh && spack spec -Il '<spec>'
. share/spack/setup-env.sh && spack install '<spec>'
```

To force an already-installed package through its build and install path, use:

```bash
. share/spack/setup-env.sh && spack install --overwrite -y '<spec>'
```

For cleanup, always acknowledge the confirmation prompt non-interactively:

```bash
. share/spack/setup-env.sh && spack uninstall -y '<spec>'
```

Add `--dependents` or other uninstall options only when the requested cleanup requires
them. Do not remove unrelated installed specs.

## Verification Rules

1. Preserve the exact requested spec, including versions, variants, compiler, and target.
2. Prefer a normal install when the spec is absent and `--overwrite -y` when an installed
   spec must exercise the build again.
3. Use `-v --keep-stage` only when diagnosing a failure or retaining its complete log is
   useful.
4. Report the concrete spec hash and whether the command installed, reinstalled, reused,
   or removed it.
5. If the command fails, retain the stage and inspect the first causal diagnostic before
   changing code.
