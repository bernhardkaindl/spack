#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$repo"
state="${XDG_STATE_HOME:-$HOME/.local/state}/installer-sandbox-ci"
mkdir -p "$state"
chmod 700 "$state"
exec 9>"$state/worker.lock"
flock -n 9 || exit 0
if [[ -e .github/agents/installer-sandbox-ci.paused || -e .github/agents/installer-sandbox-ci.done ]]; then
    printf 'CI shepherd paused or complete; inspect .github/agents/installer-sandbox-ci.* markers.\n'
    exit 0
fi
pause_on_error() {
    result=$?
    timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    printf '%s\nLauncher failed (exit %s); inspect the service journal before resuming.\n' \
        "$timestamp" "$result" > .github/agents/installer-sandbox-ci.paused
    printf '\n## %s - Status\n\nLauncher failed (exit %s); paused to prevent repeated failed invocations.\nInspect the service journal before resuming.\n' \
        "$timestamp" "$result" >> installer-sandbox-ci-journal.md
    exit "$result"
}
trap pause_on_error ERR
unset PYTHONSTARTUP PYTHON_BASIC_REPL
printf 'Starting installer sandbox CI checkpoint: model=gpt-6.1-sol effort=low\n'
copilot -C "$repo" --agent installer-sandbox-ci \
    --model gpt-6.1-sol --reasoning-effort low \
    --stream on --log-level debug --log-dir "$state" \
    --disable-builtin-mcps --allow-all \
    --add-dir "$HOME/.agents/skills" --add-dir "$HOME/.copilot/skills" \
    --deny-tool 'shell(git push)' --deny-tool 'shell(git reset)' \
    --deny-tool 'shell(gh pr merge)' \
    -p 'Perform one installer-sandbox CI checkpoint under your agent instructions. First read the current repository publication, unit-test, and minimal-diff-review skills listed there. Discover current PR heads/checks; do not repeat the already pushed layer-4 repair unless current logs require another fix. Use the publication skill explicit GIT_DIR/GIT_WORK_TREE GitButler push recipe without changing the Git safety policy, and gh pr create for new series PRs. If pending, exit. If all published layers are green, validate and publish only the next layer. Do not wait or poll; the scheduler will invoke you again. Preserve concurrent work and leave the investigation note uncommitted.'