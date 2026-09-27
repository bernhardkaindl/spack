---
name: spack-commit-messages
description: "Use when writing or rewriting Spack commit messages, especially when preparing GitButler stack commits for review."
---

# Spack Commit Messages

Use this skill when creating or rewriting a commit message for the Spack
repository. Base the message on the change being described, not on the pull
request title or on a generic conventional-commit template.

## Repository Style

Recent merged commits on `develop` consistently use:

- A lowercase component or subsystem, followed by a colon and a concise
  description: `installer: stream selected logs above the TTY overview`.
- An action-oriented subject in the imperative or simple present tense.
- A subject that names the affected code and the important result, without
  implementation trivia or a trailing period.
- A short body when the change is non-trivial. Explain the old behavior or
  motivating problem first, then explain the resulting behavior and important
  compatibility or design consequences.
- Concrete details only when they help a reviewer assess the change: relevant
  edge cases, user-visible behavior, data-format changes, performance
  implications, or why an approach was chosen.
- Focus on one coherent change. Split unrelated fixes instead of making the
  subject cover several topics.

Harmen Stoppels's messages are a particularly useful model: they use precise
component names, compact subjects, and bodies that state the motivating issue
before carefully describing the new invariant or behavior. Longer explanations
are structured with short paragraphs and examples only where an example makes
the contract clearer.

## Subject Checklist

Before finalizing the subject:

1. Identify the narrowest meaningful component, such as `installer:`, `spec.py:`,
   `buildcache:`, `parser:`, or `solver:`.
2. State the change and its result in one line. Prefer `fix`, `keep`, `show`,
   `remove`, `avoid`, `allow`, `preserve`, or `use` when they accurately name
   the action.
3. Keep it brief enough to scan. Do not add a period, issue number, PR number,
   author name, or `WIP` marker.
4. Keep the subject lowercase unless a proper name or required code spelling
   makes capitalization necessary.

Good shapes include:

```text
spec.py: make satisfies and intersects independent of global state
buildcache: keep index records for other formats
installer: preserve partial lines in streamed TTY logs
```

## Body Checklist

Add a body when the subject does not explain why the change is needed or when
the behavior has meaningful consequences. Use this structure:

```text
<What was wrong, surprising, slow, or fragile?>

<What changes, including the new behavior or invariant?>

<Compatibility, format, performance, or migration consequences, if relevant.>
```

Use plain paragraphs rather than a change-log list. Include tests or commands
only when they clarify a non-obvious validation decision. Preserve existing
trailers when rewriting a message; do not invent `Signed-off-by`, co-author, or
PR trailers. The `(#<PR-number>)` suffix added by the merge is not part of a
local commit message and must be omitted.

Be exacting about every sentence in the body. A detail belongs only when it
changes the reviewer-facing contract, explains a constraint or tradeoff, or
makes the behavior testable. Describe a user interaction by naming its trigger
and outcome, rather than using an ambiguous abstraction such as "selection" or
"context". Omit incidental implementation history, including buffer sizes,
normalization steps, rendering mechanics, and intermediate fixes, unless the
detail is itself a supported behavior, a compatibility constraint, or a
meaningful performance guarantee.

Prefer the UI's concrete concepts and actions over overloaded platform terms.
For example, say "above the active-build overview" or "after toggling live log
display" instead of "terminal history" or "full-screen redraw" when those
implementation terms could be mistaken for terminal scrollback or a separate
user-facing mode. Name what remains visible or selectable and where the user
sees it.

Do not qualify a familiar UI term with an internal rendering distinction unless
that distinction is necessary to understand the change and is explained. Prefer
"overview" to an unexplained term such as "mutable overview"; describe the
observable problem directly, such as rows being hidden when the overview exceeds
the terminal height.

Every statement phrased as behavior introduced by the commit must be supported
by that commit's diff. Check the immediate parent to distinguish newly added or
changed behavior from context that was already true. Omit pre-existing behavior
unless it is explicitly identified as context and is necessary to explain the
change. Do not let a nearby unchanged code path become an accidental claim about
what the commit adds.

## Rewrite Workflow

1. Inspect the commit's diff and its immediate parent. Determine the component,
   user-visible behavior, motivation, and any compatibility consequences.
2. Match each behavioral statement to an added or changed line in the diff. If
   it existed in the parent, either label it clearly as necessary context or
   remove it.
3. Read the current message and retain only technical detail that passes the
   reviewer-meaningfulness test above. Remove PR metadata, ambiguous shorthand,
   implementation narration, intermediate fixes, and unrelated claims.
4. Write the subject using the repository style above, then add a focused body
   when needed. Wrap body lines at about 72 characters.
5. For a GitButler stack, use the commit IDs from `but status` and reword one
   commit at a time:

   ```bash
   but status
   but reword <commit-id> -m $'component: concise result\n\nWhy the old behavior was a problem.\n\nWhat the change now guarantees.'
   ```

   `but reword` recreates the selected commit and rebases dependent commits.
   Re-read `but status` if a later command needs fresh IDs, especially after
   working with a SHA-based selector. Do not use raw `git commit`, interactive
   rebase, or amend commands for a GitButler-managed stack.

6. Check the final subjects together for consistent scope, tense, and ordering.
   Do not add PR numbers merely because upstream merge commits contain them.

## Anti-Patterns

- `Fix stuff`, `updates`, or another vague subject without a component.
- Capitalized title-case subjects or imperative slogans unrelated to Spack's
  local style.
- A subject that lists every file changed instead of the behavior changed.
- A body that repeats the subject without explaining motivation or consequences.
- An implementation detail that does not affect the change's contract, such as
  a temporary buffer limit or terminal-rendering step.
- An abstract noun that obscures the interaction being described; name the
  action that triggers the behavior and the observable result instead.
- An overloaded platform term, such as "terminal history," when concrete UI
  terminology describes the behavior without suggesting a different feature.
- An unexplained internal qualifier, such as "mutable overview," when the
  ordinary UI term and the observable effect are sufficient.
- A statement about behavior that was already present in the commit's parent,
  presented as though the commit introduces it.
- Generated bot dependency-update messages used as style references.
- A PR number copied from a merged `develop` commit into a local stack commit.
