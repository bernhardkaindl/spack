---
name: installer-sandbox-supervision
description: "Use when the hourly Agents-window supervisor checks, repairs, or resumes installer-sandbox-ci.service and its five-minute timer, including recoverable backups of blocking local edits."
---

# Installer Sandbox Supervision

Perform one bounded recovery checkpoint, then exit. The native hourly automation
is a watchdog for the five-minute systemd worker, not a second CI publisher.
Read the current repository publication, unit-test, minimal-diff-review skills,
the worker agent instructions, pause/completion markers, and recent journal.
Use the GitButler and gh skills for their respective CLI operations.

## Inspect Before Acting

- Query service/timer ActiveState, SubState, MainPID, Result, last activation,
  and timer next activation. An inactive oneshot between checkpoints is normal.
- If the service is running, do not restart it or modify its files or workspace.
  Report progress and exit. Never infer a hang solely from elapsed time.
- A completion marker means stop: do not restart or clear it.
- Respect an explicit user stop/pause recorded in the journal or marker.
  An accidentally stopped timer without such a record may be restarted.
- Read the exact pause reason and evidence. Missing auth, unknown ownership,
  semantic conflicts, unavailable logs, or failed validation are not permission
  to delete the marker. Resolve and verify the blocker first.

## Recover Without Losing Work

The user permits automatic recovery of identified blocking staged/unstaged edits
without repeated approval, provided they are backed up under
`/home/bkaindl/spack/backup/installer-sandbox-ci/<UTC-timestamp>-<unique-id>/`.
This permission is not blanket permission to discard unrelated or active work.

Before editing, parking, or removing any blocking changes, acquire the worker's
existing lock at `${XDG_STATE_HOME:-$HOME/.local/state}/installer-sandbox-ci/worker.lock`
with nonblocking flock and hold it through recovery. If busy, exit. Stop the
timer while recovering; do not stop an active worker. Recheck service state and
changes after acquiring the lock. Never truncate/remove the lock file.

Create a private backup directory (0700), then save binary-capable staged and
unstaged Git diffs scoped to the exact affected paths, plus the GitButler diff,
branch/commit SHAs, and byte-for-byte copies of affected files (including
untracked files). Preserve modes and symlinks. Save pre-edit scheduler files too
if repairing them. Verify backups exist and match the original bytes before
changing anything. Empty diffs do not back up untracked files. Keep backups local;
never commit secrets, send backups elsewhere, or delete these backups.

Prefer a dedicated `copilot/installer-ci-recovery-<unique-id>` local GitButler
checkpoint containing only identified blocking non-secret changes. Keep
Co-authored-by trailers on checkpoint commits. If safe parking is unavailable,
verified diff files plus byte-for-byte copies are the fallback. Use selected
GitButler file/hunk operations, never broad discard, git stash, reset --hard,
or wholesale checkout. A checkpoint alone may leave changes applied: verify
whether it actually removes the blocker before claiming recovery.

Do not rewrite shared history, touch unrelated work, or discard ambiguous mixed
hunks. Do not move an applied branch holding another agent's active work.
If ownership or safe isolation cannot be established, preserve everything and
report the blocker instead of guessing. Never weaken tests or Git safety policy.

## Validate And Resume

Repair only the proven scheduler/environment/owned-edit blocker. Validate shell
syntax and systemd units when changed, and use relevant repository checks for
code changes. The publication skill's explicit GIT_DIR/GIT_WORK_TREE push recipe
preserves safe.bareRepository=explicit; the isolated test-only all override is
not authorized for publication. The supervisor does not push PRs or open layers.

Append an actual UTC timestamp, observed state, actions, backup directory,
checkpoint branch/SHA if any, validation evidence, and next gate to the local
uncommitted installer-sandbox-ci-journal.md. After verified recovery, remove only
the resolved pause marker; daemon-reload if units changed. Release the worker
lock, start the timer and one immediate worker checkpoint, and verify service
launch or normal successful completion. On failure leave a precise pause reason
and restore scheduling only if safe. Do not loop or repeatedly retry a blocker.

Return a concise Agents-window status: healthy/pending, recovered, complete,
or blocked; include evidence and backup paths. Report tool/progress summaries,
not private internal reasoning.
