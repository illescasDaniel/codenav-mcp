> Copy of the harness used for [DOC-007](../agent-ab.md), kept with the data so the runs can be repeated. The
> originals live next to the recordings (`<video-folder>/scripts/`). `run-pilot.sh` expects a srxy checkout with
> its `.venv` at `$SRXY_AB` and this repository at `$CODENAV_MCP`; `orchestrator-prompt.md` holds tasks 1-7 (the
> Sonnet A/B), `extra-tasks.md` tasks 8-12. `export-runs.sh` copies a campaign into `../data/agent-ab/`.

# Small-model A/B (Haiku, GPT Luna)

Each run is a separate process in its own throwaway clone of srxy-ab
(`~/.local/state/srxy-ab/checkouts/`), so it never touches srxy-ab itself, and can run while you record the
Sonnet A/B.

```fish
cd <video-folder>
set C results/small-models-(date +%Y%m%d-%H%M)
bash scripts/small-models/run-campaign.sh haiku "1 3 4 5 8 9" "1 2" $C   # one runner at a time; start luna in a 2nd shell if you like
python3 -I scripts/small-models/summarize.py $C > $C/summary.md
```

- `run-pilot.sh RUNNER ARM TASK REP [DIR]` runs a single run. It saves:
  - prompt.txt, answer.md and stream.jsonl (the raw agent stream);
  - changes.patch and diffstat.txt;
  - ty.txt and pytest.txt, checked after the run.
- Runners:
  - `haiku` runs `claude -p` with `claude-haiku-5-5` (set `HAIKU_MODEL` to change it).
  - `luna` runs `codex exec` with `gpt-6-luna` (set `LUNA_MODEL` and `LUNA_EFFORT`, default medium). It uses
    `--ignore-user-config`, so the codenav server is added only to the codenav arm, with `-c mcp_servers.codenav...`.
- Haiku runs also write the hook log to `events.jsonl` in the run folder. Codex calls only appear in stream.jsonl.
- Tasks: 1-7 are the Sonnet A/B tasks. 8 and 9 are in extra-tasks.md and are the ones where grep can mislead.
- Every task uses the same COMMON block, and neither arm is told to use codenav.
- Delete the clones afterwards: `trash ~/.local/state/srxy-ab/checkouts/*`. The results folder has everything needed.

## Grading facts for tasks 8 and 9

- **Task 8:** after the run, the 9 `"lines"` / `'lines'` strings in src/ and tests/ must still be there. A complete
  run also updates:
  - examples/playground.py:123;
  - docs/python-api.md:32;
  - the `lines=` keyword in the Python heredocs of scripts/docs/export_gui_screenshot.sh and export_tui_screenshot.sh.
  - `size.lines` at cli.py:95 is a terminal size, not this field, so it must stay.
- **Task 9:** only the copy at adapters/inbound/cli/cli.py:81 is deleted (it has no references). The copy at
  application/search_runner.py:73 is called at :115.

## Grading facts for tasks 10-12

The answer keys are in [data/agent-ab/ground-truth/](../data/agent-ab/ground-truth/) (one line per `file:line` the old side of the patch must touch);
`python3 -I grade.py CAMPAIGN TASK` compares each run's patch with them (tasks 10 and 12).

- **Task 10** (`Matcher.score` to `similarity`): 44 lines. rope's rename gives 41 (`task-10-rope-raw.txt`, from
  `rope_rename_truth.py`); it misses 3 calls through a `CompositeMatcher`-typed variable: composite.py:34 and
  search_files.py:382/383. Must stay: about 49 `.score` reads of result fields, 146 `score=` keywords, the
  ImageSimilarity `score` method (image_similarity.py:52, its port at ports/outbound/content.py:96 and its caller at
  search_files.py:197) and the QML role `b"score"` in gui/models.py.
- **Task 11** (unused module-level functions in cli.py): 4 functions, graded by hand from answer.md:
  normalize_max_file_size (:81), render_results (:598), sync_options_to_args (:646), execute_search (:749).
  The first version of the key had only 3; execute_search was added after runs named it and a grep confirmed it.
  Naming a live function (render_progress, run_preflight, run_plain, resolve_show_progress, `_args_want_*`) is a
  false positive.
- **Task 12** (`_Segment.matches` to `matches_name`): 4 lines, ignore_rules.py:59, 148, 173 and
  tests/unit/test_ignore_rules_fast_paths.py:40. `IgnorePattern.matches` (and its docstring at :142) must stay.
