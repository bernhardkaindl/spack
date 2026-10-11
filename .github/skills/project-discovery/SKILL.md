---
name: project-discovery
description: "Use when starting repository work: discover relevant .github/projects ledgers from lightweight metadata, follow their checkpoints, and report GitButler stack updates at turn end."
---

# Project Discovery

Project ledgers in `.github/projects/*.md` describe active, intermittent work
that may span several agents and GitButler pull-request stacks. Use this skill
at the start of an implementation, debugging, review, or planning task.

## Discovery

1. List `.github/projects/*.md`.
2. Read only the YAML frontmatter of each project file first. Frontmatter must
   identify the project with `name`, `description`, `status`, and optional
   `paths` globs.
3. Match the user's request, target files, symbols, or failing behavior against
   the project description and paths.
4. Read the full ledger only when a project is relevant. Follow its delivery
   policy, open checkpoints, validation gates, and evidence requirements.
5. If no project matches, continue normally. Do not load every project body or
   create a project ledger merely because a task has more than one step.

For a project file without frontmatter, read only its title and first short
paragraph to determine whether it is relevant. When editing such a project,
add the standard frontmatter unless compatibility with an established project
format requires otherwise.

## Ledger Maintenance

When completing a ledger item:

1. Run its stated validation gate before marking it complete.
2. Record the exact commands, results, GitButler commit reference, and pull
   request reference in the item's evidence block.
3. Keep tests and required documentation with the implementation they verify.
4. Do not mark items complete based only on code inspection or an intended
   change.

## GitButler Delivery

Read the relevant project's delivery policy before making version-control
changes. Where applicable, keep related work in a dedicated GitButler stack.
Commit each completed, coherent checkpoint with GitButler. When work is
sufficiently independent for review, create a separate branch and draft pull
request layer in that stack rather than combining it with an unrelated
checkpoint. A stack is also called a series in user discussion. Do not rewrite
published or shared history without explicit user authorization.

Pushing remote branches and opening or updating pull requests remain the user's
decision. Prepare the branch and report the exact proposed GitButler action, but
do not push or create a review unless the user explicitly asks. GitHub stacked
pull requests cannot span repositories: if the first pull request is not in the
target repository, GitHub cannot create a dependent pull request there. Record
that blocker in the project ledger and follow the user's chosen workaround, such
as incremental independent pull requests with the repository's `develop` branch
as their base.

## End-of-Turn Report

At the end of every turn that changed files, ran validation, or changed project
state, state concisely:

- Work completed and validation results.
- Relevant project ledger items updated or still open.
- GitButler stacks or PR layers updated, including each commit's change and
  destination branch.
- Any blocker that prevented a planned commit or draft PR, including cross-repo
   stacked-PR limitations and the user's selected fallback.
- Whether remote push or pull-request creation was deliberately left to the
   user.