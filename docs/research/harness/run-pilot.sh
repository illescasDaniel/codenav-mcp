#!/usr/bin/env bash
# One small-model A/B run in its own throwaway clone of srxy-ab (the real srxy-ab tree is never touched).
#   bash run-pilot.sh RUNNER ARM TASK REP [CAMPAIGN_DIR]
#   RUNNER: haiku (claude -p, claude-haiku-5-5) or luna (codex exec, gpt-6-luna)
#   ARM:    plain (built-in tools only), codenav (built-in tools + the 12 codenav tools) or codenav-hint (codenav, plus
#           a one-line project hint, HINT below, committed into the clone's AGENTS.md and CLAUDE.md)
#   TASK:   1-7 from ../orchestrator-prompt.md, 8-12 from extra-tasks.md (same COMMON block for all)
# Results: CAMPAIGN_DIR/<runner>/task-N-rep-K-<arm>/ (prompt, answer, raw stream, patch, ty/pytest after the run).
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)  # this copy keeps orchestrator-prompt.md and extra-tasks.md next to it
fixture=${SRXY_AB:-$HOME/Projects/Code/Python/srxy-ab}
mcp=${CODENAV_MCP:-$HOME/Projects/Code/MCPs/codenav-mcp}
base=953d27c8fbef9136944398f234997abf72ddee93
runner=${1:-} arm=${2:-} task=${3:-} rep=${4:-}
campaign=${5:-"${XDG_STATE_HOME:-$HOME/.local/state}/srxy-ab/results/small-models-$(date +%Y%m%d)"}
if [[ ! $runner =~ ^(haiku|luna)$ || ! $arm =~ ^(plain|codenav|codenav-hint)$ || ! $task =~ ^[1-9][0-9]?$ || ! $rep =~ ^[1-9][0-9]*$ ]]; then
  echo 'Usage: run-pilot.sh haiku|luna plain|codenav|codenav-hint TASK(1-12) REP [CAMPAIGN_DIR]' >&2
  exit 2
fi
haiku_model=${HAIKU_MODEL:-claude-haiku-5-5}
luna_model=${LUNA_MODEL:-gpt-6-luna}
luna_effort=${LUNA_EFFORT:-medium}
HINT='For Python code navigation and refactors (finding callers or references, renaming, changing signatures, moving code), prefer the codenav MCP tools over grep, sed and hand edits: they follow the types.'

mkdir -p "$campaign/$runner"
run="$(cd "$campaign/$runner" && pwd)/task-$task-rep-$rep-$arm"
if ! mkdir "$run"; then
  echo "Run already exists: $run (use another REP)" >&2
  exit 2
fi

checkout_root=${XDG_STATE_HOME:-"$HOME/.local/state"}/srxy-ab/checkouts
mkdir -p "$checkout_root"
checkout=$(mktemp -d "$checkout_root/$runner-task-$task-rep-$rep-$arm.XXXXXX")
git clone --quiet --shared "$fixture" "$checkout"
git -C "$checkout" checkout --quiet --detach "$base"
ln -s "$fixture/.venv" "$checkout/.venv"
echo '/.venv' >> "$checkout/.git/info/exclude"
printf '%s\n' "$checkout" > "$run/checkout-path.txt"
diffbase=$base
if [[ $arm == codenav-hint ]]; then
  # Committed, so the agent sees a clean tree and the hint is not part of its patch.
  for f in AGENTS.md CLAUDE.md; do printf '\n## Tools\n\n%s\n' "$HINT" >> "$checkout/$f"; done
  git -C "$checkout" -c user.name=pilot -c user.email=pilot@localhost commit --quiet -am 'pilot: codenav hint'
  diffbase=$(git -C "$checkout" rev-parse HEAD)
fi

# Same text the Sonnet A/B gives its subagents: the COMMON block, a blank line, the TASK block.
python3 -I - "$here/orchestrator-prompt.md" "$task" "$here/extra-tasks.md" > "$run/prompt.txt" <<'PY'
import re, sys
from pathlib import Path
doc = Path(sys.argv[1]).read_text()
body = doc.split("COMMON:\n", 1)[1].split("\n````", 1)[0]
common = body.split("\n\nTASK 1:\n", 1)[0].strip()
tasks = dict(re.findall(r"TASK (\d+):\n(.*?)(?=\n\nTASK \d+:|\Z)", body, re.S))
tasks.update(re.findall(r"TASK (\d+):\n(.*?)(?=\n\nTASK \d+:|\Z)", Path(sys.argv[3]).read_text().strip(), re.S))
print(common + "\n\n" + tasks[sys.argv[2]].strip())
PY

{
  echo "runner=$runner arm=$arm task=$task rep=$rep"
  echo "fixture=$base diffbase=$diffbase"
  echo "codenav=$(git -C "$mcp" log -1 --format='%h %s') dirty=$(git -C "$mcp" status --porcelain | wc -l)"
  if [[ $runner == haiku ]]; then echo "model=$haiku_model $(claude --version)"; else echo "model=$luna_model effort=$luna_effort $(codex --version)"; fi
  echo "started=$(date -Is)"
} > "$run/metadata.txt"

cd "$checkout"
export PYTHONPATH="$PWD/src"   # the shared .venv has srxy installed from srxy-ab; make imports use this clone
export DEMO_LOG="$run/events.jsonl"
set +e
if [[ $runner == haiku ]]; then
  if [[ $arm == codenav* ]]; then
    printf '{"mcpServers":{"codenav":{"type":"stdio","command":"uv","args":["run","--no-sync","--project","%s","codenav-mcp"]}}}\n' "$mcp" > "$run/mcp.json"
  else
    printf '{"mcpServers":{}}\n' > "$run/mcp.json"
  fi
  claude -p --verbose --output-format stream-json --no-session-persistence \
    --model "$haiku_model" --setting-sources project --strict-mcp-config --mcp-config "$run/mcp.json" \
    --permission-mode bypassPermissions --tools 'Read,Grep,Glob,Edit,Write,Bash' \
    < "$run/prompt.txt" > "$run/stream.jsonl" 2> "$run/stderr.log"
  agent_exit=$?
  python3 -I -c 'import json,sys
last=None
for line in open(sys.argv[1]):
    try: e=json.loads(line)
    except ValueError: continue
    if e.get("type")=="result": last=e
print((last or {}).get("result",""))' "$run/stream.jsonl" > "$run/answer.md"
else
  extra=()
  if [[ $arm == codenav* ]]; then
    extra=(-c 'mcp_servers.codenav.command="uv"'
           -c "mcp_servers.codenav.args=[\"run\",\"--no-sync\",\"--project\",\"$mcp\",\"codenav-mcp\"]"
           -c 'mcp_servers.codenav.startup_timeout_sec=120'
           -c 'mcp_servers.codenav.tool_timeout_sec=600')
  fi
  codex exec --json --ephemeral --ignore-user-config --skip-git-repo-check \
    -m "$luna_model" -c "model_reasoning_effort=\"$luna_effort\"" \
    --dangerously-bypass-approvals-and-sandbox -C "$checkout" -o "$run/answer.md" "${extra[@]}" - \
    < "$run/prompt.txt" > "$run/stream.jsonl" 2> "$run/stderr.log"
  agent_exit=$?
fi
set -e
echo "agent_exit=$agent_exit finished=$(date -Is)" >> "$run/metadata.txt"

# Snapshot the agent's work, then run the same checks ourselves.
git status --short > "$run/status.txt"
git add -N -- .
git diff --binary "$diffbase" > "$run/changes.patch"
git diff --stat "$diffbase" > "$run/diffstat.txt"
set +e
uv run --no-sync ty check > "$run/ty.txt" 2>&1; echo "exit $?" >> "$run/ty.txt"
uv run --no-sync pytest tests/unit tests/cli -q -n 4 -p no:pytest-qt > "$run/pytest.txt" 2>&1; echo "exit $?" >> "$run/pytest.txt"
set -e
echo "$run"
