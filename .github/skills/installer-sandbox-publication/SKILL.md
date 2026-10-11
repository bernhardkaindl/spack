---
name: installer-sandbox-publication
description: "Use when publishing the Spack installer-sandbox series one PR at a time, pushing namespace-sandbox layers, or updating installer-sandbox PR titles, reviewer descriptions, and GitButler stack footers."
---

# Installer Sandbox Publication

## Scope and Naming

This series has **18 review layers**. The user-facing feature is the Spack
installer sandbox: the installer builds packages when it is not installing
precompiled packages. Linux user/mount namespaces are the new backend, not
the feature name. Keep existing `namespace-sandbox/v2/...` branch names.

Title format: `installer-sandbox <position>/18: <phase-specific summary>`.
The aggregate testing PR #53185 is **not** a numbered review layer and must
not appear in the layer footers.

Published layers and saved descriptions are recorded in
[the description assets](./assets/published-prs.json). These are inventory and
historical snapshots, not writing templates. Confirm current GitHub state before
relying on this inventory, and update it when publishing a layer.

## Publish Only the Requested Layer

For the loop around this procedure (which layer is next, fixing a failing layer
at its owner, rewrite tooling, closing local-gate gaps), see the
`installer-sandbox-series` skill.

1. Load the GitButler and GitHub CLI skills. Preserve unrelated work and the
   local stack; do not rename branches, rewrite history, or publish later layers.
2. Inspect the previous PR's `headRefOid` and `statusCheckRollup` with
   `gh pr view <number> --repo spack/spack --json headRefOid,statusCheckRollup`.
   Proceed only when all reported checks are successful at the intended head:
   both completed check runs (`conclusion: SUCCESS`) and legacy status contexts
   (`state: SUCCESS`) count. Pending, failed, skipped, missing, or ambiguous
   required checks do not satisfy the gate. Do not poll or sleep waiting for CI.
3. Confirm the requested layer's tip matches its reviewed/tested revision.
   If it changed, validate that exact layer using the Spack unit-test skill
   before publication. Check executable diff coverage against its immediate
   parent **per production file**, not just the overall percentage. Aim for
   namespace-file coverage above 90%; report unmet gates instead of hiding them.
   [assets/validate-layers.sh](./assets/validate-layers.sh) runs these gates for
   every layer of a map file (`OLDSHA NEWSHA` per line): ruff, vermin
   (`-t=3.6-`), pylint, mypy, license, slotscheck, a focused coverage run gated
   by `MIN_COVERAGE` (default 85.26) per file, and tests. See its header for
   `CHECKS`, `NS_TARGET` and `COV_EXTRA`. Its focused coverage can under-report;
   confirm a failing file with the full suite before adding tests.
4. Check for an existing PR by head branch before creating one. Never duplicate
   a PR or change its base without authorization.
5. Use GitButler for branch pushes with explicit repository selection:
   `GIT_DIR=/home/bkaindl/spack/spack/.git
   GIT_WORK_TREE=/home/bkaindl/spack/spack but push <requested-layer>`.
   Set these variables only for this command, not for the worker or tests.
   This avoids GitButler's bare-repository discovery failure while preserving
   `safe.bareRepository=explicit`; never apply the test-only `all` override
   to publication. Verified on 2026-10-11 by pushing layer 4 to
   `3c3c34ce827a6569b3aa76555f0b3f607044533a`.
   The selected layer includes its ancestors; never push the aggregate or a
   higher layer to publish a lower one. Use `--with-force` only for explicitly
   authorized amended published commits. Check the selected refs with
   `--dry-run` before a rewritten-history push, and verify the resulting
   GitHub head SHA. Review synchronization can fail after a successful push:
   record that warning separately and verify the remote head before retrying.
   Important: the aggregate push on 2026-10-11 updated remote layer branches
   5-18 as ancestors even though `but push --dry-run` listed only the aggregate.
   A dry run is not reliable evidence of single-ref scope. Never push the
   aggregate to advance a numbered layer; pushing it requires explicit user
   authorization and disclosure that descendant-layer remote refs also move.
   This does not create their PRs or satisfy their sequential validation gates.
6. This fork-based series previously encountered GitButler's HTTP 422 invalid
   base: the preceding fork branch does not exist in upstream Spack. The user
   authorized the fallback used for PRs 2 and 3: push the selected layer with
   GitButler, then create a regular PR with
   `gh pr create --repo spack/spack --base develop --head bernhardkaindl:<branch>
   --title <title> --body-file <description>`.
   This is the explicit PR-creation path for the series: use `gh pr create`,
   not `but pr new`, after the selected layer's push succeeds. Preserve the
   local GitButler stack; upstream PRs remain develop-based because their
   preceding fork branches are not upstream base branches. Do not
   unstack/recreate branches or use `gh stack` mutations to work around it.
7. Open regular PRs, not drafts, for this series as requested by the user.
   Confirm the resulting PR's head, base, and draft state. Do not claim its CI
   passed based on local tests or the preceding PR's checks.

## Write for Reviewers

Draft and rewrite PR bodies with the [spack-pr-descriptions](../spack-pr-descriptions/SKILL.md)
skill: prose of about 80-130 words before the footer, no template headings, and
no validation evidence. Do not restate its rules here.

## Refresh All Published Footers

After opening a layer, update the footer on **every published numbered PR**.
Use `This is PR <position>/18 in a stack:`, independent of how many layers are
currently published. Do not add bold markup or `made with GitButler` to this line.
List published layers newest first with their actual series positions; do not
invent PR numbers or list unpublished layers. Mark only the current PR with
the pointing marker. The Unicode arrow is intentional to match GitButler.

```md
<!-- GitButler Footer Boundary Top -->
---
This is PR 1/18 in a stack:
- <kbd>&nbsp;3&nbsp;</kbd> #53190
- <kbd>&nbsp;2&nbsp;</kbd> #53189
- <kbd>&nbsp;1&nbsp;</kbd> #53186 👈
<!-- GitButler Footer Boundary Bottom -->
```

Use the script, not ad hoc edits:

```sh
.github/skills/installer-sandbox-publication/assets/refresh_footers.py  # dry run, reads GitHub only
.github/skills/installer-sandbox-publication/assets/refresh_footers.py --apply --backup-dir /tmp/footer-backup
```

The script takes PR numbers only from the `number` field of
[published-prs.json](./assets/published-prs.json), never from layer position
arithmetic. Before any write it checks every listed PR on GitHub: the number, the
head branch equal to the inventory `branch`, and a title starting with
`installer-sandbox <position>/18:`. If any check fails it edits nothing. It
replaces only the block between the markers, appends the block when it is
absent, and stops on malformed or duplicated markers. It saves each original
body to the backup directory before editing and reads each body back to verify
it. Run the dry run first and check that every PR it lists is a series layer.

Never edit a PR that is not in `published-prs.json` with the same
`namespace-sandbox/v2/` head branch. Other open PRs, such as `installer-ui/*`
and `fix/*` branches, are not part of the series even when their numbers are
close. If the script refuses, fix the inventory after checking GitHub. Do not
bypass the check with a manual `gh pr edit`.

GitButler may regenerate footers during later operations, so perform this
refresh **after** publication or other forge operations. Never change another
project's PRs. Report updated PR links and whether anything was pushed or left
local; do not commit or publish skill changes incidentally.