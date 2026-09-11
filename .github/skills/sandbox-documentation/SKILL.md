---
name: sandbox-documentation
description: 'Write, format, validate, maintain, or review Spack RST documentation under lib/spack/docs/. Use for every documentation change, including sandbox command hardening, feature status, roadmaps, architecture, worker protocols, confinement policy, and recipe-import trust boundaries.'
argument-hint: 'Describe the Spack documentation change'
user-invocable: true
disable-model-invocation: false
---

# Spack And Sandbox Documentation

Use this skill for every change under `lib/spack/docs/`.
Apply the sandbox-specific sections when changing the incremental hardening of normal Spack command paths that import package recipes.

This is a living instruction: update it when verified project practices change,
but keep those updates isolated from implementation changes. After user approval,
squash an instruction update into the commit that originally added this skill.

## Sandbox Information Architecture

Keep sandbox documentation under `lib/spack/docs/sandbox/` and link every maintained page from `index.rst`.

The tree must provide these ownership boundaries:

- `index.rst`: concise landing page and toctree; do not duplicate page bodies.
- `overview.rst`: scope, trust boundary, and the relationship to normal command paths.
- `status.rst`: implemented behavior verified by code and focused tests.
- `roadmap.rst`: incomplete command migrations, ordered next steps, and unresolved design decisions.
- `info-command.rst`: the `spack info` worker boundary, output, validation, and confinement contract.

Create additional focused pages when a topic needs sustained detail. Prefer a dedicated contract page over adding unrelated sections to an existing page.

## Workflow

1. Inspect `git status` and the current docs diff. Preserve user edits and in-progress feature documentation.
2. Write each prose sentence on one physical source line. Do not manually hard-wrap a sentence.
3. Use simple, short sentences. Use paragraphs, vertical lists, tables, headings, directives, and other RST structures to keep the source scannable.
4. Run the RST formatter on every task-owned RST file after editing. Let it normalize sentence layout before reviewing or validating the result.
5. For sandbox documentation, identify the single owning page for each statement. Keep current facts in status, future work in roadmap, mechanisms in an owning contract page, and scope in overview.
6. Update documentation before implementing a new capability. State the intended trust boundary, compatibility behavior, bounds, failure mode, and tests.
7. Keep status factual and testable. Do not call a feature complete unless implementation and focused validation exist.
8. Keep roadmap items actionable and remove or rewrite items when their work lands.
9. Preserve RST labels or add replacement labels when moving externally referenced content. Update all `:doc:` and `:ref:` links.
10. Add every new sandbox page to `sandbox/index.rst`; link that index from the root `lib/spack/docs/index.rst` toctree.

## Source Layout Is Formatter-Owned

The repository formatter keeps each prose sentence on one physical source line.
Do not hand-wrap sentences to a fixed column width, even when a source line is long.
The formatter will undo that wrapping.

Avoid long lines by revising the prose, not by inserting line breaks inside a sentence.
Prefer simple, short sentences and one concept per paragraph.
When content has several parts, use a vertical list, table, definition list, directive, or separate paragraphs.

After changing RST, run the formatter on the explicit task-owned files:

```console
PYTHONPATH=lib/spack .venv/bin/python .github/workflows/bin/format-rst.py RST_FILES...
```

Do not use a broad wildcard when unrelated or user-owned RST changes are present.
Review the formatter diff before continuing.

## Writing Rules

- Keep documentation concise: state each fact once, prefer short paragraphs and lists, and omit implementation-history narration.
- Format source for direct review as well as rendered output.
- Prefer short sentences and vertical RST structures over dense paragraphs or long sentences.
- Never manually wrap a prose sentence across source lines.
- Use one concept per paragraph and descriptive section headings.
- Distinguish trusted command-parent behavior from less-trusted recipe-evaluation-worker behavior explicitly.
- Treat recipe-controlled text as untrusted data. Do not describe raw worker standard output as a parent or terminal response path; describe only the bounded JSON response.
- Explain normal-command compatibility and intentional divergence without implying that `spack install` is already confined.
- Put command syntax and configuration precedence on the relevant command contract page, not in overview.
- Put unresolved alternatives and assessments in roadmap, not status.
- Avoid generated HTML under `lib/spack/docs/_build/`.
- When documenting implementation guidance, require concise module, class, method, and function docstrings, plus local rationale comments for non-obvious trust boundaries, fallback policy, confinement ordering, and kernel-policy transitions.

## Validation

The root `lib/spack/docs/index.rst` owns the Sphinx doctree. Build it through the documentation Makefile so its `-W --keep-going` options turn warnings into failures. In this checkout, use the workspace virtual environment because it supplies the configured Sphinx extensions:

```console
make -C lib/spack/docs SPHINXBUILD="$PWD/.venv/bin/sphinx-build" html
```

This writes generated output below `lib/spack/docs/_build/html/`; do not edit or commit that output. Do not run `make clean` in a dirty documentation tree without first checking that its generated paths contain no user state.

After edits:

1. Search for references to moved or deleted page names and fix source RST references.
2. Verify every RST page under `sandbox/` appears in its index toctree and that the root `index.rst` links the sandbox index.
3. Check heading adornments, labels, `:doc:` targets, and `:ref:` targets.
4. Run the formatter on every task-owned RST file, even when the source already appears formatted.
5. Run the root HTML build above when available. If Sphinx is unavailable, report that and run text/reference checks and `git diff --check`.
6. Review the diff for duplicated facts, orphan pages, accidental generated output, and unrelated edits.