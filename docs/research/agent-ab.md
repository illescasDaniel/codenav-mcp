---
id: DOC-007
title: "Research: coding agents with and without codenav (A/B pilots)"
summary: "Haiku 5.5, GPT-6 Luna and Sonnet on srxy with and without codenav: adoption (Luna 0/12 without a hint, 12/13 with one), correctness by task and codenav build, token effort, false leads, and the codenav defects the runs exposed"
type: reference
status: current
updated: 2026-11-27
verified:
covers: [docs/research/harness/**]
related: [DOC-005, DOC-006]
supersedes:
superseded_by:
---

# Research: coding agents with and without codenav (A/B pilots)

What happens when a coding agent gets codenav's tools next to its built-in ones: does it use them, does its work get
more correct, and what does it cost? Each run gives one model one task on its own clone of srxy, a Python search tool
of about 1,200 tests. The answers and patches are graded by hand against fixed keys. These are pilots: small n, one
repository. Read the numbers as evidence of direction, not as effect sizes.

## Setup

- **Repository:** srxy-ab, a copy of srxy at 953d27c. Before every run: ty clean; 1170 tests passed, 7 skipped.
- **Arms:**
  - `plain`: the runner's built-in tools only.
  - `codenav`: the built-in tools plus the 12 codenav tools. The agent isn't told to use them.
  - `codenav-hint` (Luna only): the codenav arm plus one line in the clone's AGENTS.md and CLAUDE.md: "For Python
    code navigation and refactors (finding callers or references, renaming, changing signatures, moving code),
    prefer the codenav MCP tools over grep, sed and hand edits: they follow the types."
- **Runners:**
  - Haiku: `claude -p --model claude-haiku-5-5`, with Read, Grep, Glob, Edit, Write and Bash.
  - Luna: `codex exec -m gpt-6-luna`, medium reasoning effort, with shell and apply_patch.
  - Sonnet (the first A/B only): Claude Code subagents with codenav v1's tools.
- **Tasks:** the same COMMON block plus one TASK block for every arm. Tasks 1-7 are in
  [orchestrator-prompt.md](harness/orchestrator-prompt.md), tasks 8-12 in [extra-tasks.md](harness/extra-tasks.md).
  Tasks 8-12 each have a trap for text search: JSON keys with the same name as a field (8), a second copy of a
  function (9), other things named `score` (10), functions that look unused (11), other `matches` methods (12).
- **After each run:** the patch, `ty check`, the test suite and the agent's answer are saved. The harness is in
  [harness/](harness/README.md): `run-pilot.sh` runs one cell, `summarize.py` builds the table, `grade.py` and the
  [ground truth](data/agent-ab/ground-truth/) grade it, and `export-runs.sh` copies the runs here without local paths.
- **Ground truth:** built from rope's rename and jedi's references, then corrected by hand where they miss call sites
  ([C3 in DOC-006](correctness.md#finding-c3-rope-also-misses-real-call-sites)).

| campaign | date | codenav | runs | data |
|---|---|---|---|---|
| Sonnet A/B | 2026-11-26 | v1 tools | 2 (3 tasks each) | [sonnet-v1](data/agent-ab/sonnet-v1/results.md) |
| Small models, round 1 | 2026-11-27 | dd9da03 | 24 + 1 Terra control | [round-1](data/agent-ab/round-1/results.md), [table](data/agent-ab/round-1/summary.md) |
| Small models, round 2 | 2026-11-27 | 3f2a833, aa1a584, f25bc84 | 48 | [round-2](data/agent-ab/round-2/results.md), [table](data/agent-ab/round-2/summary.md) |

Every run folder has the prompt, the agent's answer (`answer.txt`), `changes.patch`, `diffstat.txt`, `ty.txt`, the
test summary and `metadata.txt` with the codenav build used.

## Finding A1: whether an agent uses codenav depends on the model, and one line of project hint fixes it

| runner / arm | runs that called codenav |
|---|---|
| Haiku, codenav | 5 of 6 (round 1), 12 of 12 (round 2) |
| Luna, codenav | 0 of 6 (round 1), 0 of 6 (round 2) |
| Luna, codenav-hint | 7 of 8 (round 2), 5 of 5 (extra runs on later builds) |
| GPT-6.1 Terra, codenav (control) | 1 of 1 |

- Luna had the tools loaded: a separate check had it call `outline` successfully, and Terra under the same
  `codex exec` setup used `rename` and `edits`. Luna's 0 of 12 is the model's choice, not a missing tool.
- Haiku is the smaller model and adopted codenav unprompted. So adoption isn't a matter of size.
- The hint is one sentence. The run that didn't follow it (task 11 rep 1) also got that task wrong (1 of 4).
- In the first Sonnet A/B the codenav arm was told it must use codenav; 3 of its 6 calls failed on wrong argument
  names. v2 renames guessed argument names to the real ones ([Arguments and errors](../tools.md#arguments-and-errors)).

## Finding A2: on a 35-file rename, codenav made Luna complete and made Haiku cheaper

Task 8 renames `FileSearchResult.lines` to `line_matches` in 35 files of code and tests, while the JSON keys named
`"lines"` must stay. The tests cover the code, so every run ended with ty clean and the tests passing. The difference is
in 4 files the tests don't cover: `examples/playground.py`, `docs/python-api.md`, and two shell scripts with Python
heredocs that build `FileSearchResult(lines=...)`. codenav offers these as optional rename sites. "Complete" means
all 4 were renamed. codenav changed during the runs, so this table is by build; don't pool builds.

| runner / arm | codenav build | complete | missed |
|---|---|---|---|
| Haiku, codenav | dd9da03 | 0 of 1 | both .sh (not scanned yet) |
| Haiku, codenav | 3f2a833 | 1 of 2 | both .sh (offered unlabelled, skipped) |
| Haiku, codenav | aa1a584 | 2 of 2 | |
| Haiku, codenav | f25bc84 | 2 of 2 | |
| Haiku, plain | - | 3 of 4 | python-api.md and tui.sh (1 run) |
| Luna, codenav (no calls) | dd9da03 | 0 of 1 | playground, python-api.md, both .sh |
| Luna, plain | - | 0 of 3 | 2 to 4 of the 4 files in each run |
| Luna, codenav-hint | 3f2a833 | 1 of 2 | both .sh (1 run) |
| Luna, codenav-hint | aa1a584 | 1 of 1 | |
| Luna, codenav-hint | f25bc84 | 2 of 2 | |

- **Luna:** with the hint and codenav, 4 of 5 complete and 3 of 3 on the labelled builds; without codenav, 0 of 4.
- **Haiku:** plain Haiku was usually complete too, because it grepped every file. The gain was effort: codenav runs
  used 161k-359k input tokens and 11-15 tool calls, plain runs 368k-519k and 18-21 calls.
- **The wire format held:** all 20 task 8 runs kept the JSON `"lines"` keys.
- **Luna's token use:** hint runs 319k-458k input tokens, plain runs 472k-935k. Token counts are computed differently
  for the two runners (summary.md notes how), so compare arms within a model only.

## Finding A3: on the other tasks the arms were equally correct

- **Round 1, tasks 1, 3, 4, 5 and 9** (callers, renames, adding a parameter, a type change, deleting the unused copy
  of a function): all 20 runs correct; ty clean and the tests passing after every run.
- **Task 10** (rename `Matcher.score`, 44 sites, leave other `score`s): all 10 runs exact.
- **Task 12** (rename `_Segment.matches`, 4 lines, leave the other `matches`): all 12 runs renamed the right 4 lines.
  Two Luna hint runs also applied an optional site they shouldn't have (finding A5).
- **Sonnet A/B** (3 tasks, one run each, v1 tools): both arms correct; the plain arm used fewer tokens (22k against
  41k total).
- srxy's names are distinct and its tests are thorough, so a careful agent with grep gets these right. A repository with
  more overloaded names or weaker tests may separate the arms; that's untested.

## Finding A4: finding unused code, the `unused` tool helped when it was called

Task 11 asks which module-level functions in `cli.py` nothing uses. The key has 4.

| runner / arm | rep 1 | rep 2 |
|---|---|---|
| Haiku, codenav | 4/4, called `unused` | 4/4, called `unused` |
| Haiku, plain | 2/4 | 4/4 |
| Luna, codenav (no calls) | 1/4 | 1/4 |
| Luna, plain | 2/4 | 1/4 |
| Luna, codenav-hint | 1/4, no codenav call | 4/4, called `unused` |

The 3 runs that called `unused` found all 4; 6 of the other 7 runs found at most 2. No run named a function that
is used. Haiku's codenav runs used more tokens here (97k and 163k against 47k and 70k).

## Finding A5: watching small models found three codenav defects, all fixed

Each defect was found by reading the runs, fixed, then re-run on the same task:

1. **Python in shell scripts wasn't scanned** (round 1, task 8). Fixed in 3f2a833: names in quoted Python heredocs of
   `.sh` and `.bash` files are offered as optional rename sites.
2. **The new heredoc sites were unlabelled,** so Haiku (3f2a833 rep 2) and Luna (rep 2) skipped them as shell text.
   Fixed in aa1a584: each site says what it is, for example `Python heredoc: keyword of FileSearchResult()`. Every run
   on a labelled build applied them.
3. **False-lead optional sites.** In task 12, codenav offered the docstring of another class's `matches`, and a Luna
   hint run applied it (rep 1, ignore_rules.py:142). The same preview also offered the CLI flag text
   `matches-all`. Fixed in f25bc84: no sites inside CLI-style hyphenated words, and no sites in the comments or
   docstrings of a class that defines its own member of that name. The f25bc84 hint runs of task 12 didn't repeat
   the error. One applied `specs/ignore-rules/PLAN.md:59`, a planning doc that names `_Segment.matches`; that site
   is still offered and is arguably correct.

The task 11 answer key had a defect too: the first version listed 3 functions and missed `execute_search`. It was
corrected and checked with jedi before grading ([task-11.txt](data/agent-ab/ground-truth/task-11.txt)).

## Limits

- **Small n:** 1 rep in round 1, 2 in round 2 (task 8 up to 7). One repository, one machine, one day.
- **Selection:** tasks 8-12 were written knowing where text search misleads; round 2 was designed after round 1.
- **Moving target:** codenav was fixed between runs of the same arm. Results are reported per build; the
  improvement after a fix is shown by 2-4 runs.
- **Hand grading** against hand-built keys; one key was wrong at first.
- **Effort settings:** Luna ran at medium effort only, Haiku at the CLI default. Haiku wasn't given the hint.
- **No timing:** up to 3 runs shared the machine, so wall-clock times are recorded but not compared.
