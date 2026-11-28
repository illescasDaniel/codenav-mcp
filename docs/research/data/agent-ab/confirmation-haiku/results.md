# Haiku confirmation campaign, 2026-11-27

Bounded confirmation of the Haiku A/B on the current, unchanged CodeNav build. Tasks 8, 11 and 12, repeats 1 and 2,
arms `plain` (built-in tools only) and `codenav` (built-in tools plus the 12 codenav MCP tools, no project hint).
12 runs total, all run to completion. No historical builds are pooled with these numbers.

## Build (exact)

- codenav-mcp: `f8b6c8936b05b11d1736cdae184e1a4970677506` (branch `v2`), working tree clean before and after
  (`build-before.txt`, `build-after.txt`).
- Import path verified: `codenav_mcp` resolves to the repository's `src/` (editable install in the repo `.venv`).
- Server smoke test over stdio: 12 tools (`change_signature, check, edit_symbol, edits, fix, impact, lookup, move,
  outline, rename, search, unused`).
- Fixture: srxy-ab, runs detached at base `953d27c8fbef9136944398f234997abf72ddee93` (fixture HEAD `e0e75f8` is not
  used by the runs).
- Model: `claude-haiku-5-5` via `claude -p`, Claude Code 2.1.332, uv 0.13.9, logged in with claude.ai.
- Baseline at 953d27c: `ty check` exit 0; pytest `tests/unit tests/cli` 1170 passed, 7 skipped; 9 wire-key
  `"lines"`/`'lines'` matches in src and tests; `size.lines` at cli.py:95.

## Per-run verdicts

| task | rep | arm | verdict | detail |
|---|---|---|---|---|
| 8 | 1 | codenav | PASS | 9 wire keys kept, `size.lines` kept, field renamed, all 4 ancillary sites updated (playground.py:123, python-api.md:32, both screenshot scripts, 4 `lines=` each) |
| 8 | 1 | plain | FAIL (incomplete) | wire keys and `size.lines` kept, field renamed in src/tests; **0 of 4 ancillary sites updated** (playground.py:123 and python-api.md:32 still `result.lines`; both scripts still `lines=[` x4). ty and pytest pass. |
| 8 | 2 | codenav | PASS | same as rep 1 codenav |
| 8 | 2 | plain | PASS | 9 wire keys kept, `size.lines` kept, all 4 ancillary sites updated |
| 11 | 1 | codenav | PASS | 4/4 of the key (normalize_max_file_size, render_results, sync_options_to_args, execute_search), 0 false positives |
| 11 | 1 | plain | partial | 2/4 (normalize_max_file_size, render_results); missed sync_options_to_args and execute_search; 0 false positives |
| 11 | 2 | codenav | PASS | 4/4, 0 false positives |
| 11 | 2 | plain | FAIL | 1/4 (render_results only); missed normalize_max_file_size, sync_options_to_args, execute_search; 0 false positives |
| 12 | 1 | codenav | PASS | 4/4 (`grade.py`): ignore_rules.py:59, :148, :173 and the test at :40; 0 extra; `IgnorePattern.matches` and its docstring untouched |
| 12 | 1 | plain | PASS | 4/4, 0 extra |
| 12 | 2 | codenav | PASS | 4/4, 0 extra |
| 12 | 2 | plain | PASS | 4/4, 0 extra |

Summary by arm:
- codenav: task 8 2 of 2 PASS; task 11 2 of 2 PASS; task 12 2 of 2 PASS (6 of 6 PASS).
- plain: task 8 1 PASS and 1 FAIL; task 11 0 PASS, 1 partial and 1 FAIL; task 12 2 of 2 PASS.

With 2 repeats per task, these counts are too few to rank the arms.

## Tool adoption (codenav arm)

| task | rep | codenav calls | codenav tools used |
|---|---|---|---|
| 8 | 1 | 3 | lookup, rename, edits |
| 8 | 2 | 4 | lookup, lookup, rename, edits (1 failed: `lookup` with unknown section `definition`) |
| 11 | 1 | 2 | outline, unused |
| 11 | 2 | 2 | outline, unused |
| 12 | 1 | 3 | lookup, rename, edits |
| 12 | 2 | 3 | lookup, rename, edits |

The plain arm has no codenav calls by construction. Haiku used codenav in every codenav run; the task-8 rep-1
plain run was the one that missed the ancillary sites.

## Within-model tokens and time (Haiku only)

Token counts are from `summarize.py`. "In" includes cache creation and cache read tokens, which dominate. Compare
only the numbers inside this table.

| task | rep | arm | s | tokens in | tokens out | tool calls |
|---|---|---|---|---|---|---|
| 8 | 1 | codenav | 93 | 334,568 | 4,615 | 15 |
| 8 | 1 | plain | 112 | 377,711 | 7,969 | 14 |
| 8 | 2 | codenav | 65 | 271,378 | 4,832 | 14 |
| 8 | 2 | plain | 57 | 289,304 | 8,184 | 18 |
| 11 | 1 | codenav | 30 | 106,575 | 5,405 | 6 |
| 11 | 1 | plain | 24 | 70,473 | 3,167 | 4 |
| 11 | 2 | codenav | 27 | 126,138 | 4,909 | 5 |
| 11 | 2 | plain | 14 | 49,327 | 2,496 | 3 |
| 12 | 1 | codenav | 22 | 75,857 | 1,479 | 5 |
| 12 | 1 | plain | 30 | 128,429 | 3,816 | 13 |
| 12 | 2 | codenav | 22 | 59,965 | 1,763 | 5 |
| 12 | 2 | plain | 32 | 158,074 | 3,944 | 13 |

Task 8 and task 12 used fewer input tokens in the codenav arm in these runs; task 11 used more (codenav ran
`unused` and `outline` before grepping). These are two repeats per cell, so they describe these runs only.

## Check results

- `ty check` exit 0 and pytest 1170 passed, 7 skipped, in all 12 runs (`ty.txt`, `pytest.txt`), including the
  task-11 runs that changed nothing.
- No run had `is_error=true` in its final result, and `agent_exit` was 0 for all 12 runs.
- Failed tool calls (from `summarize.py`): task-8 rep-2 codenav 1 (codenav `lookup` with an unknown section name);
  task-12 rep-2 plain 2 (Edit string not found). All other runs 0.
- `haiku-errors.log` is empty.

## Limitations

- 2 repeats per cell, 3 tasks, Haiku only. The result is a confirmation of the individual runs, not a
  statistically strong comparison.
- Task 8 and task 11 are graded by hand (task 8: wire keys, `size.lines`, the four ancillary sites and the
  `.lines` leftovers; task 11: answer text against the four-function key). Task 12 uses `grade.py` and a manual
  check of optional sites; none were touched by any run.
- Grading of task 11 is on the answer text only. Changes were not expected, and `changes.patch` is empty in all four.
- Token counts include cache reads; they are not comparable across campaigns.
- The `codenav-hint` arm was not run (not in this campaign).
- The 12 run clones and the baseline clone were moved to trash after grading and export.
- A smoke test (one trivial Haiku call) was run before the campaign from a scratch folder under
  `~/.local/state/srxy-ab/smoke/`; it is not part of the campaign.

## Harness errors

None in the campaign: `run-campaign.sh` completed both repeats (`campaign-rep1.log`, `campaign-rep2.log`), no run
failed, and `haiku-errors.log` is empty. Before the campaign I briefly wrote a scratch file to `/tmp` during the
server smoke test and moved it to trash; it was not used by any run.

## Files

- Raw runs: `haiku/task-N-rep-K-ARM/` (stream.jsonl, events.jsonl, answer.md, changes.patch, ty.txt, pytest.txt, metadata.txt).
- `summary.md` (from `summarize.py`), `build-before.txt`, `build-after.txt`, campaign logs.
- Export: `docs/research/data/agent-ab/confirmation-haiku/` in the codenav-mcp repository.
