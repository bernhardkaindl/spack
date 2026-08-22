---
name: sandbox-documentation
description: 'Maintain and review RST documentation for the incremental hardening of normal Spack commands that import package recipes. Use when adding, changing, splitting, reorganizing, or auditing sandbox command-hardening documentation, feature status, roadmap items, architecture, structured worker protocols, confinement policy, or recipe-import trust boundaries.'
argument-hint: 'Describe the sandbox documentation change'
user-invocable: true
disable-model-invocation: false
---

# Sandbox Documentation

Use this skill as the documentation-manager workflow for the incremental hardening of normal Spack command paths that import package recipes.

This is a living instruction: update it when verified project practices change,
but keep those updates isolated from implementation changes. After user approval,
squash an instruction update into the commit that originally added this skill.

## Information Architecture

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
2. Identify the single owning page for each statement. Keep current facts in status, future work in roadmap, mechanisms in an owning contract page, and scope in overview.
3. Update documentation before implementing a new capability. State the intended trust boundary, compatibility behavior, bounds, failure mode, and tests.
4. Keep status factual and testable. Do not call a feature complete unless implementation and focused validation exist.
5. Keep roadmap items actionable and remove or rewrite items when their work lands.
6. Preserve RST labels or add replacement labels when moving externally referenced content. Update all `:doc:` and `:ref:` links.
7. Add every new page to `sandbox/index.rst`; link that index from the root `lib/spack/docs/index.rst` toctree.

## Writing Rules

- Keep documentation concise: state each fact once, prefer short paragraphs and lists, and omit implementation-history narration.
- Format source for direct review as well as rendered output.  Prefer short
	sentences, vertical lists, tables, headings, and other Sphinx-supported
	structures over dense paragraphs or long source lines.  Expand prose
	vertically when it makes ownership, alternatives, or acceptance criteria
	easier to scan without rendering.
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
4. Run the root HTML build above when available. If Sphinx is unavailable, report that and run the repository formatter on task-owned RST files with `PYTHONPATH=lib/spack .venv/bin/python .github/workflows/bin/format-rst.py RST_FILES...`, plus text/reference checks and `git diff --check`.
5. Review the diff for duplicated facts, orphan pages, accidental generated output, and unrelated edits.