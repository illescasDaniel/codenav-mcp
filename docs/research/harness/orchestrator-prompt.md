# codenav A/B run for the video

One Claude session runs everything: it gives the same 7 tasks to two subagents, `codenav-agent` (built-in tools + the
12 codenav tools) and `plain-agent` (built-in tools only), resets the repo between runs, checks the results and writes a
report. You watch the codenav calls live in the terminal.

## How to run (desktop app with its terminal)

1. **Terminal** (any directory, fish is fine):

   ```fish
   bash <video-folder>/scripts/watch.sh
   ```

   It waits for calls. `watch.sh all` shows every tool call of both agents instead of codenav only. Ctrl+C stops it.

2. **New Claude session** in `~/Projects/Code/Python/srxy-ab`, model Sonnet or Opus, in a mode that does not
   ask for permission on every command (bypass / full access). Paste the prompt below. Edit `TASKS` / `REPEATS` first
   if you want a shorter run (each task is two agent runs, about 2-6 minutes each).

3. **Results** land in `<video-folder>/results/ab-<date-time>/`:
   `results.md` (the report), and one folder per run with the agent's answer, its diff, check output and its tool log.

If the terminal shows nothing after the first agent starts, the session did not load the project hooks: check that
the session's folder is srxy-ab and that `.claude/settings.local.json` there has the `DEMO_LOG` entry.

## The prompt to paste

````text
You are the orchestrator of an A/B test of the codenav MCP for a YouTube video. Two subagents do the same coding tasks
in this repository: `codenav-agent` (built-in tools + codenav MCP tools) and `plain-agent` (built-in tools only).
You never do the tasks yourself and you never call codenav tools yourself; you only use Bash for bookkeeping, the
Agent tool to start the subagents, and Read/Write for the report. Work sequentially, never two agents at once.

TASKS: 1 2 3 4 5 6 7
REPEATS: 1

## 0. Setup (stop and tell me if any check fails)
- R=<video-folder>/results/ab-$(date +%Y%m%d-%H%M); mkdir -p "$R".
  LIVE=<video-folder>/results/live/events.jsonl (the project hook
  appends every tool call there; the live viewer follows that path). Use these absolute paths in every command.
- Check `pwd` is ~/Projects/Code/Python/srxy-ab and `git rev-parse --short HEAD` is 953d27c.
- KEEP = the pathspec `-- . ':(exclude).claude' ':(exclude).mcp.json' ':(exclude).codex' ':(exclude).cursor'` (the test harness and the
  MCP configs live there; never reset them).
  `git status --short` with KEEP must be empty and `git stash list` must be empty.
- After your first Bash call, `tail -n 1 "$LIVE"` must show a record of that call. If the file is missing, the hooks are
  not loaded: stop and tell me.
- Write "$R/env.txt": date, `claude --version`, srxy-ab HEAD, and codenav:
  `git -C ~/Projects/Code/MCPs/codenav-mcp log -1 --format='%h %s'` plus whether that checkout is dirty.
- Baseline, once: `uv run --no-sync ty check > "$R/baseline-ty.txt" 2>&1; echo "exit $?" >> "$R/baseline-ty.txt"` and
  `uv run --no-sync pytest tests/unit tests/cli -q -n 4 -p no:pytest-qt > "$R/baseline-pytest.txt" 2>&1; echo "exit $?" >> "$R/baseline-pytest.txt"`.

## 1. Runs
For each task N in TASKS and each repeat K in REPEATS: if N+K is even run codenav-agent first, otherwise plain-agent
first (so the order alternates). For each run, with T="$R/task-N-rep-K-<arm>" (arm = codenav or plain):
1. `mkdir -p "$T"`. Check the tree is clean (KEEP status empty). Move the live log aside so the run gets its own log:
   `mv "$LIVE" "$T/before.jsonl" 2>/dev/null; mv "$LIVE.raw" "$T/before.jsonl.raw" 2>/dev/null`.
2. Start the subagent (`codenav-agent` or `plain-agent`) with exactly this text and NOTHING else (no hints about tools,
   no other task, no grading criteria, no earlier results): the COMMON block below, a blank line, then the TASK N block.
   Record the start and end time.
3. Right after it returns: `mv "$LIVE" "$T/events.jsonl"; mv "$LIVE.raw" "$T/events.jsonl.raw"` (ignore a missing file:
   it means zero tool calls). Save the subagent's final answer verbatim to "$T/answer.md".
4. Save its changes: `git add -A KEEP && git diff --cached --binary > "$T/changes.patch"; git diff --cached --stat > "$T/diffstat.txt"; git reset -q`
   (KEEP spelled out as the pathspec). For tasks 3-6 also run the two checks from the COMMON block yourself and save
   them as "$T/ty.txt" and "$T/pytest.txt" with their exit codes appended.
5. Restore: `git checkout KEEP && git clean -fdq -- . ':(exclude).claude'`, then confirm KEEP status is empty. If the
   agent left a stash, save it (`git stash show -p > "$T/stash.patch"`), drop it, and note it.
6. Append one line to "$R/runs.tsv": task, rep, arm, order (first/second), start, end, agent type, the agent's id if
   the Agent result shows one.

## 2. Metrics (after all runs)
- The session transcript path is the `transcript_path` field of any line in a `events.jsonl.raw` file.
- `python3 -I <video-folder>/scripts/collect_metrics.py <transcript>`
  prints one JSON line per finished subagent (tokens, duration, tool counts by tool). Match them to runs.tsv by agent
  id, or by order if ids are missing. Save the raw output as "$R/metrics.jsonl".
- From each run's events.jsonl also count failed tool calls (`status` not ok) and Bash calls that are really search or
  edit fallbacks (grep/rg/sed/awk/python edits).

## 3. Grading (compare the two arms of the same task; read the answers and patches before looking at the arm names
where practical)
Score each run success / partial / fail, with the evidence:
- Task 1: three real call sites of FileSearchPort.execute under src/: search_session.py:117, search_worker.py:223,
  cli.py:817. The sqlite `connection.execute(...)` calls, `execute_search` functions and the implementation
  `def execute` in search_runner.py are not calls of the port and must be left out.
- Task 2: the declaration in domain/models.py, attribute reads AND constructor keywords (`lines=`), in src and tests,
  plus docs if any; confirmed vs possible clearly separated. Patch must be empty.
- Task 3: port, implementation, the three callers and the tests renamed; the unrelated `execute` methods/calls
  untouched; docs/api-reference.md regenerated if a test compares it; ty and tests pass.
- Task 4: keyword-only `case_sensitive: bool` (required, no default) on the port and the implementation; explicit
  `case_sensitive=False` at every existing call including tests; API docs regenerated with
  `uv run --no-sync python scripts/docs/export_public_api.py` if needed; ty and tests pass.
- Task 5: `size_bytes: int | None`; every warning path in application/skipped_file_warnings.py handles None (no
  `None:,` formatting crash, no size suggestion from None); known sizes unchanged; ty and tests pass.
- Task 6: new module srxy/application/tag_labels.py has split_tag_label; `from srxy.application.utils import
  split_tag_label` still works; internal users updated or still working; ty and tests pass.
- Task 7: `parser.add_argument("--max-file-size", type=int, default=DEFAULT_MAX_FILE_SIZE, ...)` in
  adapters/inbound/cli/cli.py:366; dest `max_file_size`; default DEFAULT_MAX_FILE_SIZE = 100 * 1024 * 1024 bytes
  (application/search_defaults.py:10); input is a plain integer byte count (`type=int`), and 0 means unlimited
  (values <= 0 become None in normalize_max_file_size, application/search_runner.py:73, called at :115; the copy at
  cli.py:81 is an unused duplicate, so citing only it is a partial). Patch must be empty.
- Read-only tasks (1, 2, 7) must leave an empty patch.

## 4. Report: "$R/results.md"
- Start with 3-5 plain sentences for the video: who won what, and the honest caveats.
- One table per task: verdict, total tokens, duration, tool calls (codenav / Read+Grep+Glob / Edit+Write / Bash),
  failed calls, search/edit fallbacks in Bash, ty and test exit codes, diffstat.
- A totals table over all tasks per arm, then a short "what codenav did that grep/sed could not, and where it lost".
- Limits: number of runs, one repo, one model, order effects, prompt cache, anything that went wrong in the harness.
- Finally tell me the results path, and leave the repo with KEEP status empty.

COMMON:
Work only in this repository. Use whichever available tools fit the task. For a read-only task, leave the working tree
unchanged. For a coding task, make the smallest complete change, preserve unrelated behavior, and run:
  uv run --no-sync ty check
  uv run --no-sync pytest tests/unit tests/cli -q -n 4 -p no:pytest-qt
Do not commit and do not use git stash. Use trash rather than rm if you need to delete files. Summarize your answer or
changes with file:line references and report any checks run, failures, and uncertainty.

TASK 1:
Which production code under src/ directly calls FileSearchPort.execute? Give each file:line, explain how you identified
it as a call to this port, and leave unrelated execute calls out of the answer. Change nothing.

TASK 2:
Trace the impact of renaming FileSearchResult.lines. Report where the field is declared and how production code
constructs, reads, or otherwise depends on it. Include affected tests and documentation if you find them. Distinguish
confirmed uses from possible references. Change nothing.

TASK 3:
Rename FileSearchPort.execute to run in the port, its implementation, its callers, and relevant tests. Preserve any
unrelated execute methods and update affected generated docs where necessary.

TASK 4:
Add a required keyword-only case_sensitive: bool parameter to FileSearchPort.execute and its implementation. Pass
case_sensitive=False in existing callers, including tests, so existing search behavior is preserved. Keep this change
limited to the signature and its consumers. Regenerate affected API docs using the project's generator.

TASK 5:
Some skipped files have an unknown size. Change SkippedFile.size_bytes from int to int | None and make sure warnings
work when the size is unknown. Preserve the current warning behavior for known sizes.

TASK 6:
Move split_tag_label from srxy.application.utils to a new srxy.application.tag_labels module. Keep the old import
working, and make sure internal consumers and tests still behave the same.

TASK 7:
Where is the --max-file-size CLI option defined? State its parser destination, default and accepted input format, with
file:line references. Change nothing.
````

## Notes

- Fairness: both agents get the same model (Sonnet), the same six built-in tools, the same one-line instructions
  (no "you MUST use codenav") and the same task text. The only difference is the 12 codenav tools.
- codenav runs from the local checkout `~/Projects/Code/MCPs/codenav-mcp` (whatever branch is checked out,
  recorded in `env.txt`), via srxy-ab's `.mcp.json`.
- `collect_metrics.py` reads the session transcript; it is what gives per-agent tokens and durations.
- `old/` has the previous standalone-launcher version (one `claude -p` process per run), kept for reference.
