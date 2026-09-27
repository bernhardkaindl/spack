---
name: minimal-diff-review
description: "Use before committing, amending, or preparing code for review to minimize diff churn, preserve existing prose and layout, and keep only task-required changes."
---

# Minimal Diff Review

Use this workflow after implementation and validation, before committing or amending changes.

## Procedure

1. Compare the work against the correct base. For stacked changes, inspect each commit or branch
   against its immediate parent, not only the cumulative stack against the target branch.
2. Identify the behavior, tests, and documentation required by the task. Remove changes that do
   not support those requirements.
3. Preserve existing comments, docstrings, test names, terminology, control-flow shape, layout,
   and whitespace unless the task directly invalidates them.
4. Tolerate existing comments that are terse, slightly imprecise, or use imperfect terminology
   when correcting them would be off-topic. Do not turn an unrelated wording correction, such as
   changing "rounding" to "truncation," into review noise.
5. Avoid whitespace-only edits, structural rewrites, renamed locals, and reordered code when the
   existing form can express the required behavior with a smaller change.
6. When a test assertion must change, add one concise one-line comment explaining why only when
   the new expectation would otherwise surprise a reviewer. Place it next to the changed
   assertion. Do not add comments that merely restate the assertion or implementation.
7. Check each stacked layer for changes introduced in one layer and reverted or rewritten in a
   later layer. Move the final intended form into the earliest layer that owns the behavior.
8. Run the narrowest executable validation that covers the minimized changes, followed by the
   repository's required formatting and lint checks.

## Review Questions

- Would reverting this line break the requested behavior, its focused test, or required docs?
- Is this edit needed in this layer, or is it cleanup unrelated to the layer's purpose?
- Can the existing expression or control-flow structure carry the behavior with fewer changed
  lines?
- Does a changed assertion need a reason, or is its meaning already obvious from the test?