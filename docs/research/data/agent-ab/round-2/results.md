# Small-model pilot, round 2: harder tasks, a hint arm for Luna, task 8 again (2026-11-27)

srxy-ab at 953d27c, same runners and task text as round 1 (`../small-models-20261127-1049/results.md`). New here:

- **Tasks 10-12** (scripts/small-models/extra-tasks.md), 2 reps per arm. Each has a trap for text search: other
  things named `score`, unused-looking functions that are used, other `matches` methods in the same file.
- **Arm `codenav-hint`** (Luna only): the codenav arm plus one line committed into the clone's AGENTS.md and
  CLAUDE.md: "For Python code navigation and refactors (finding callers or references, renaming, changing
  signatures, moving code), prefer the codenav MCP tools over grep, sed and hand edits: they follow the types."
- **Task 8 again**, because round 1 found a codenav gap (Python heredocs in shell scripts). codenav changed during
  this round, so every run records the codenav build it used (summary.md, "codenav build"):
  - 3f2a833: heredoc names offered as optional sites, unlabelled;
  - aa1a584: those sites labelled, e.g. `Python heredoc: keyword of FileSearchResult()`;
  - f25bc84: no optional sites in CLI flags (`matches-all`) or in prose of a class that has its own member of that name.
  - `dirty=N` counts uncommitted files in the codenav checkout. dirty=1 and 2 were docs/research and pyproject's
    mutmut section, not source. dirty=4 (Haiku task 8 rep 5) also had the uncommitted f25bc84 source changes.
  - Plain arms don't load codenav; their build column is only when they ran.

Grading facts and ground truth: scripts/small-models/README.md and ground-truth/.

## In plain words

- **A one-line hint is what makes Luna use codenav.** Without it Luna called codenav in 0 of 12 runs over both
  rounds. With it, 12 of 13 runs called codenav (round 2's 8 runs: 7; the 5 extra runs: 5).
- **Tasks 10 and 12 were too easy to separate the arms.** All 10 runs of task 10 renamed exactly the 44 sites and no
  other `score`; all task 12 runs renamed the right 4 lines and no other `matches`. The only extra edits were two
  docstring lines in Luna hint runs, both offered by codenav as optional sites (below).
- **Task 11 (find unused functions): codenav helped Haiku, not Luna.**
  - Haiku with codenav (it called `unused` both times): 4 of 4 both reps. Haiku plain: 2 of 4, then 4 of 4.
  - Luna got 1 or 2 of 4 in every run except one: the hint run that called `unused` (4 of 4). The other hint run
    didn't call codenav and got 1 of 4.
  - No run named a function that is used.
- **Task 8 after the fix: codenav runs are complete; the plain runs are not reliably.** Complete means the code and
  tests, plus examples/playground.py, docs/python-api.md and the two screenshot .sh scripts.
  - Haiku codenav on the fixed builds: 5 of 6 complete. The miss (3f2a833, rep 2) skipped the unlabelled heredoc
    sites; every run on a labelled build (aa1a584, f25bc84) was complete.
  - Haiku plain: 3 of 4 complete (round 1 rep 1 and round 2 reps 2 and 4; rep 3 missed python-api.md and tui.sh).
    So for Haiku the gain is effort, not completeness: codenav runs used 161k-359k tokens in and 11-15 calls,
    plain runs 368k-519k and 18-21 calls.
  - Luna plain: 0 of 3 complete (each missed 2-4 of the 4 non-code files). Luna hint: 4 of 5 complete (rep 2 missed
    both .sh, on the unlabelled build).
  - All 20 task 8 runs across both rounds kept the JSON "lines" keys (one removed and one added line with the key,
    the same as the reference patch).
- **Watching the runs found two more codenav defects, fixed during the round:** unlabelled heredoc sites were
  skipped (fixed by labels in aa1a584), and false-lead optional sites in prose (fixed in f25bc84, see below).

## Grading

### Task 10: rename Matcher.score to similarity (44 sites in 16 files)

| runner / arm | rep 1 | rep 2 |
|---|---|---|
| Haiku codenav | 44/44 | 44/44 |
| Haiku plain | 44/44 | 44/44 |
| Luna codenav (no codenav calls) | 44/44 | 44/44 |
| Luna plain | 44/44 | 44/44 |
| Luna hint | 44/44 | 44/44 |

No run touched the image-similarity `score`, result scores or `score=` arguments.

### Task 11: unused module-level functions in cli.py (4 in the corrected key)

| runner / arm | rep 1 | rep 2 |
|---|---|---|
| Haiku codenav | 4/4 (`unused`) | 4/4 (`unused`) |
| Haiku plain | 2/4 | 4/4 |
| Luna codenav (no codenav calls) | 1/4 | 1/4 |
| Luna plain | 2/4 | 1/4 |
| Luna hint | 1/4 (no codenav call) | 4/4 (`unused`) |

The first key had 3 functions and missed `execute_search`; it was corrected to 4 and checked with jedi before
grading (ground-truth/task-11.txt says so). No run named a used function. Haiku's codenav runs cost more tokens
(97k and 163k in, against 47k and 70k plain).

### Task 12: rename _Segment.matches to matches_name (4 lines in 2 files)

All 12 runs renamed the 4 lines and left the other `matches` methods alone. Extra edits, both in Luna hint runs and
both offered by codenav as an optional site that Luna applied:

- rep 1 (3f2a833): the docstring at ignore_rules.py:142, which belongs to another class's `matches`. f25bc84 no
  longer offers prose of a class that defines its own member of that name.
- rep 4 (f25bc84): specs/ignore-rules/PLAN.md:59, a planning doc that says `_Segment.matches`. That doc names the
  renamed method, so it is arguably right; it is still offered.

### Task 8 by codenav build (complete = playground.py, python-api.md, gui.sh and tui.sh)

| runner / arm | build | rep | result |
|---|---|---|---|
| Haiku codenav | dd9da03 (round 1) | 1 | missed both .sh (not scanned before the fix) |
| Haiku codenav | 3f2a833 | 2 | missed both .sh (sites offered, unlabelled, skipped) |
| Haiku codenav | 3f2a833 | 3 | complete |
| Haiku codenav | aa1a584 | 4, 5 | complete |
| Haiku codenav | f25bc84 | 6, 7 | complete |
| Haiku plain | - | 1 (round 1), 2, 4 | complete |
| Haiku plain | - | 3 | missed python-api.md and tui.sh |
| Luna codenav, no hint | dd9da03 (round 1) | 1 | missed playground.py, python-api.md and both .sh |
| Luna plain | - | 1 (round 1) | missed python-api.md and both .sh |
| Luna plain | - | 2 | missed playground.py and both .sh |
| Luna plain | - | 3 | missed all 4 |
| Luna hint | 3f2a833 | 1 | complete |
| Luna hint | 3f2a833 | 2 | missed both .sh |
| Luna hint | aa1a584 | 3 | complete |
| Luna hint | f25bc84 | 4, 5 | complete |

## Raw numbers

summary.md (scripts/small-models/summarize.py). Tokens in are counted differently for the two models (round 1's
results.md), so compare arms within a model only. Wall-clock seconds are in the table but not used: up to 3 runs
shared the machine.

## Limits

- 2 reps (task 8: up to 7), 1 repo, hand-graded against hand-built keys; one key (task 11) was wrong at first.
- codenav changed between runs of the same arm. The task 8 table is by build; don't pool builds.
- The tasks were chosen after round 1, knowing where text search misleads. Tasks 10 and 12 still didn't separate
  the arms: srxy's names are distinct enough that careful grep works.
- Haiku wasn't given the hint; it adopts codenav without one.
