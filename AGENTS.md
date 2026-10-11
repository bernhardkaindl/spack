# Agent priorities (local, uncommitted)

Keep this file uncommitted.

## Installer-sandbox series: first failing PR first

The 18-layer series `namespace-sandbox/v2/01..18` is published one PR at a time.
Work on the first PR in the series that is not green on GitHub CI, and finish
everything that affects it before anything else:

1. Fix every failure and every known review item for that PR, validate that
   exact layer (`.github/skills/installer-sandbox-publication/assets/validate-layers.sh`,
   including `bin/spack style --base <parent>`), and push it.
2. Only then start work that affects later PRs: coverage gaps in later layers,
   style or cleanups across all layers, Phase 3, and other follow-ups in
   `.github/projects/installer-sandbox-tests.md`.

Do not spend time validating or repairing later layers while the first failing PR
still has open items. When a fix for the first PR rewrites later layers (rebase),
check only that they still apply cleanly; defer their full validation.
Record deferred items in the ledger instead of working on them.

The loop is in `.github/skills/installer-sandbox-series/SKILL.md`; run it on demand
with the `installer-sandbox-series` agent (`.github/agents/`).

## Never idle while CI runs

Until the unit tests of the most recently pushed PR finish green, do not stop and
wait: read the deferred items in the ledger, pick one, and run with it. If those
tests fail instead, fixing them takes over as the first priority. Deferred work must
not change the pushed layer's commit; it goes to later layers or to uncommitted files.

## Decide, or grill the user

Decide on your own whenever a choice is reversible or has a clear best answer: follow
upstream Spack coding and commit-message practice, otherwise best taste, and record
the decision in the ledger. When the work cannot continue without the user, because
it needs a decision (for example how to fix something with several reasonable
designs, or an action outside the agent's authorization), grill the user with the
`structured-questions` skill: one decision per question, with a recommendation.
Do not ask about things you can decide.
