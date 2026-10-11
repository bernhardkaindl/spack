---
name: spack-pr-descriptions
description: "Use when writing or rewriting a Spack pull request description, especially reviewer-facing PR text for stacked installer-sandbox layers, and when deciding whether an unattended run may update a PR body."
---

# Spack PR Descriptions

Use this skill when drafting or rewriting a pull request body for the Spack
repository, including each layer of the installer-sandbox series. Write from the
layer's diff against its immediate parent, not from the commit message or the
title. Commit-message rules live in the `spack-commit-messages` skill, and the
publication loop in `installer-sandbox-publication`.

## Workflow

1. Read `git --no-pager diff <parent> <tip>` for the layer and list the behavior
   it adds or changes. Ignore context that the parent already had.
2. Draft the prose as described under "Write for Reviewers".
3. Check every behavioral sentence against an added or changed line.
4. Count the words before the footer and remove repeated transitions.
5. Keep the footer block between the GitButler markers exactly as it is. Only
   the prose above it changes; the publication skill owns the footer.
6. Save the draft in the session `files/` folder, never in the repository.

## Interactive and Unattended Runs

Interactive means a user can answer in this session. Show the current and
proposed body (or the changed paragraphs) with a one-line reason for each
change, then ask for a decision with the structured-questions skill. Apply only
the text the user chooses, using `gh pr edit --repo spack/spack --body-file`.

Unattended means no user can answer, such as timer-driven runs. Update the body
directly with `gh pr edit` for every PR the run publishes or refreshes, keep the
footer intact, and record the change in the ledger.

Exception: an unattended run never edits PR #53186 (layer 1). Its body changes
only when the user explicitly asks for it, in either mode.

## Write for Reviewers

Write a short technical note, not a report with a repeated template. Default to
one or two paragraphs and roughly **80-130 words before the footer**, about half
the original PR 4 description. A small fix may need only two sentences. Use more
space only for a concrete compatibility issue or behavior that needs explaining.
Do not pad a short description to reach the word count.

Do not use the mechanical `Purpose` / `Changes` / `Scope` headings. Start with
the actual failure, missing behavior, or change; do not repeat the title. Then
explain the main mechanism and its effect. Mention the Linux namespace backend
where it matters, not in every paragraph. Avoid repeated "This phase..." and
"The next phase..." transitions; add a scope sentence only when a reviewer could
otherwise mistake this layer for a completed feature.

Draft from the layer's diff, not a list of every changed function. Group related
changes into one explanation. Name a path, command, or concrete input/output
when that makes the behavior clearer. Prefer a brief example to another abstract
sentence. Do not invent demonstrations, measurements, or personal experience.
Do not copy contributors' prose or claim human authorship.

Bullets are optional, for **two to four short items** such as distinct affected
operations. Keep them to phrases, usually under ten words; write explanations
as prose rather than long sentences disguised as bullets. A small code block
can show a command or before/after case. Skip routine test inventories, generic
benefits, comprehensive implementation catalogs, and words such as "establishes
the vocabulary" when the concrete behavior says more.

For PR 1, include a short opening motivation for the series without requiring
a heading: source builds run upstream logic with the user's access, and host
tools can become undeclared inputs. Describe limiting unrelated filesystem
access and making inputs explicit as goals, not guarantees of complete isolation
or reproducibility. Keep this introduction within the length budget where possible.

### Upstream Reference Points

A sample of 25 recently updated merged Spack PRs was inspected on 2026-10-10.
The examples below are from personal contributor accounts; account identity
does not prove whether drafting assistance was used. The sample also includes
long explanations, so brevity is a default for this series, not an upstream rule.

- [#53099](https://github.com/spack/spack/pull/53099): explains the false-success
   case in two sentences, including the observable zero-package audit result.
- [#53124](https://github.com/spack/spack/pull/53124): shows the unwanted diagnostic,
   states the fix, and gives the relevant regression reference.
- [#53140](https://github.com/spack/spack/pull/53140): short opening, compact list
   of affected operations, then one implementation note.
- [#53092](https://github.com/spack/spack/pull/53092): explains parser behavior
   through syntax examples and a restriction, not a generic change inventory.
- [#53105](https://github.com/spack/spack/pull/53105): explains the import-time
   `STORE` binding and the bootstrap/build-cache contexts where it matters.

To refresh the sample, use `gh pr list --repo spack/spack --state merged
--search 'sort:updated-desc' --limit 25 --json number,title,author,mergedAt,body,url`.
Read varied fixes and features from multiple contributors, ignoring bot accounts,
HTML template comments, and generated stack footers. Borrow the choices of
detail and layout, not the wording; do not infer a mandatory template from one PR.

### Examples

Possible PR 4 rewrite (two paragraphs, no headings; not a live PR edit):

```md
Hiding host directories also hides files needed by the selected toolchain.
`gcc`, for example, needs helper programs and headers beyond its driver
executable; staging tools need interpreters and runtime libraries too.

Select those inputs from the chosen compilers and tool packages rather than
exposing their entire host directories. Compiler queries locate helpers and
support files, and the Linux header policy selects the GCC/libstdc++ include
trees. Keep alias paths separate from canonical sources: a `cc` symlink must
still work after its parent directory is masked. Replacement directory mounts
and generated symlinks let the mount plan restore that view. Installer policy
activation comes later.
```

Another shape, when a behavior example is sufficient:

```md
Preserve selected files before masking their parent directories. Otherwise
hiding `/usr/bin` also makes a selected `/usr/bin/gcc` unavailable before it
can be restored into the sandbox view.

The mount plan records the source and access mode first, then applies masks
and restores the selected paths. Read-only binds restrict the sandbox view
without changing the host file's permissions.
```

These illustrate different layouts, not paragraphs to reuse across layers.
Before publishing, remove a sentence that only repeats another, check each
behavioral claim against the diff, and count prose words excluding the footer.
If the draft still reads like a release-note checklist, regroup it around the
one problem the reviewer needs to understand.

Do not include:

- A leading ordinal/CI-testing sentence or justification for a regular PR.
- Local test counts, coverage percentages, tool results, or review verdicts.
- A prose `Stack:` chain, fork-base workaround, or merge-order instructions.
- Aggregate CI-testing context, Codecov caveats, or generic security-audit text.

Keep validation evidence in the local investigation note or a relevant project
ledger, not these descriptions. The investigation note must remain uncommitted.
Use the footer below for published-layer links rather than duplicating them
in prose. When refreshing an existing description, preserve useful unrelated
reviewer content; remove the obsolete process paragraphs requested above.
