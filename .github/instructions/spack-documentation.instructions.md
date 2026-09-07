---
name: Spack Documentation
description: "Use when writing, editing, formatting, validating, or reviewing RST documentation under lib/spack/docs/."
applyTo: "lib/spack/docs/**"
---

# Spack Documentation

Use the `sandbox-documentation` skill for every change under `lib/spack/docs/`.

- Write each prose sentence on one physical source line.
- Never manually hard-wrap a sentence to a fixed column width.
- Prefer simple, short sentences and one concept per paragraph.
- Use paragraphs, vertical lists, tables, headings, directives, and other RST structures instead of dense prose.
- After editing, run `PYTHONPATH=lib/spack .venv/bin/python .github/workflows/bin/format-rst.py RST_FILES...` on every task-owned RST file, even when it already appears formatted.
- Use explicit file paths when unrelated or user-owned RST changes are present.
- Run the validation required by the skill before handing off the change.