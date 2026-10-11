---
name: multi-project-management
description: "Use when coordinating intermittent project ledgers, GitButler stacks or series, open work inventory, or isolated parallel validation in separate Spack worktrees and VS Code windows."
---

# Multi-Project Management

Use this skill only when the current task has no logical next step, the user
asks for coordination across active projects, or a reviewable series needs an
inventory. It complements `project-discovery`; it does not replace focused
implementation work.

## Terms

- A **stack** is a linear GitButler branch and pull-request dependency chain.
- A **series** is an alias for a stack, especially when users discuss its
  review sequence rather than GitButler mechanics.

## Work Inventory

Keep the main agent focused. When an inventory would add unrelated context, use
a read-only subagent at the end of the turn to inspect:

1. Active `.github/projects` frontmatter and ledger checkpoints.
2. `but status` for applied stacks and uncommitted work.
3. Open pull requests for relevant series branches.
4. Missing or blocked review layers, validation gates, and the next independent
   unit-test or build-verification task.

Return only actionable findings. Do not create worktrees, switch branches,
push, open pull requests, or mutate ledger state during inventory.

For the installer UI series, distinguish published layers from local-only
layers. If early layers have pull requests but later layers do not, report the
gap and suggest independent validation of the next unreviewed layer. Do not
infer that an absent pull request is an error: report its known base or
cross-repository constraint.

## Isolated Parallel Validation

Offer an isolated validation checkout only when it can proceed independently of
the active task. Ask the user before creating it. Prefer a separate VS Code
window and an isolated Spack configuration so tests and package operations do
not share the current checkout's configuration or stage directories.

The current checkout implements isolation as `spack isolate --path`, not a
global `spack --isolate` option. It updates configuration files in the target
Spack worktree, so run it only after creating the dedicated worktree.

Use these commands as a user-facing template. Replace both refs with the
specific series layer and clean package-repository revision to validate:

```bash
TARGET="$HOME/spack/pr/installer-ui"
SPACK_SOURCE="$HOME/spack/spack"
PACKAGES_SOURCE="$HOME/spack/pr/1/spack-packages"
SPACK_REF="installer-ui/v1/failed-build-rows"
PACKAGES_REF="<clean-package-repository-ref>"

mkdir -p "$TARGET"
git -C "$SPACK_SOURCE" worktree add --detach "$TARGET/spack" "$SPACK_REF"
git -C "$PACKAGES_SOURCE" worktree add --detach \
  "$TARGET/spack-packages" "$PACKAGES_REF"
cd "$TARGET/spack"
./bin/spack isolate --path "$TARGET/isolate"
./bin/spack repo add "$TARGET/spack-packages/repos/spack_repo"
code --new-window "$TARGET/spack"
```

Before suggesting this template, verify that both source repositories exist and
select a clean package-repository ref. A dirty source worktree can host a new
worktree, but its uncommitted changes are not copied; never present it as the
revision under test.

In the new VS Code window, start an independent chat or agent with the explicit
target branch, package repository path, and validation command. The VS Code
Agents view may coordinate this well when each agent is assigned an isolated
workspace; do not allow parallel agents to mutate the same worktree.

## End-of-Turn Suggestion

When the current task has no next logical implementation step, offer the user a
short next-action suggestion based on the inventory. State the target series
layer, the proposed isolated test, and that it will run in a separate VS Code
window. Do not start the worktree setup, test, push, or pull-request operation
without user approval.