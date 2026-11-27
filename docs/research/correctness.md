---
id: DOC-006
title: "Research: correctness of codenav's refactors on real projects"
summary: "Measured results of the refactor fuzzer (187 random refactors on srxy, click, tomlkit) and the jedi/rope differential (references 40/40, renames 38/40), what failed, and what was fixed"
type: reference
status: current
updated: 2026-11-27
verified:
covers: [hardening/**, src/codenav_mcp/**]
related: [DOC-004, DOC-005, DOC-007]
supersedes:
superseded_by:
---

# Research: correctness of codenav's refactors on real projects

The evidence that codenav's writes don't silently break code and that its reads agree with independent tools. How the
checks work and how to run them is in [Hardening checks](../hardening.md) (DOC-004). This page holds the numbers and
links to the raw results.

## Finding C1: random refactors on 3 real projects found no silent breakage

- **What:** the refactor fuzzer (`hardening/fuzz_refactors.py`) picks random symbols and runs one refactor on each:
  rename, rename with every optional site, change_signature, move or delete.
- **Projects:** srxy at 953d27c, click and tomlkit.
- **Oracle:** each project's own test suite and `ty check`, compared with a baseline.
- **Invariants:** I1 no silent breakage, I2 round trip, I3 containment, I4 delete safety, I5 reply format.

| run | codenav | cases | I1 fail | I2 fail | I3 fail | I4 fail | I5 fail | data |
|---|---|---|---|---|---|---|---|---|
| seed 1 | b08229a | 80 | 1* | 0 | 0 | 0 | 0 | [summary](data/correctness/summary-seed1.md), [cases](data/correctness/results-seed1.jsonl) |
| seed 2 | b08229a | 60 | 0 | 0 | 2* | 0 | 0 | [summary](data/correctness/summary-seed2.md), [cases](data/correctness/results-seed2.jsonl) |
| seed 3 | 1c7d26f | 15 | 0 | 0 | 0 | 0 | 0 | [summary](data/correctness/summary-seed3.md), [cases](data/correctness/results-seed3.jsonl) |
| seed 4 | 3f2a833 | 16 | 0 | 0 | 0 | 0 | 0 | [summary](data/correctness/summary-seed4.md), [cases](data/correctness/results-seed4.jsonl) |
| seed 5 | f25bc84 | 16 | 0 | 0 | 0 | 0 | 0 | [summary](data/correctness/summary-seed5.md), [cases](data/correctness/results-seed5.jsonl) |

\* All three failures were the fuzzer's own false positives, not codenav bugs:

- **Seed 1, `srxy-1-13`:** a test compares a generated API doc. codenav's reply had said to regenerate that doc.
  I1 now counts a failure as reported when the failing test reads a file the reply named for regeneration.
- **Seed 2, `click-2-12` and `click-2-16`:** correct renames in click's `examples/`. I3 now allows the project's
  `.py` files outside dot-folders and build folders.

Seed 4 lists one case apart, `tomlkit-4-3`, under "over-rename damage": an opted-in change, not a silent one. codenav
refused to move `loads` out of `tomlkit/api.py` because the move creates an import cycle. The fuzzer then retried
with `allow_import_cycle=true`, the reply warned about the circular import, and 11 test modules failed to import.
That is the documented behaviour of the opt-in ([Tool reference](../tools.md#moves)).

Each case runs the project's tests, which takes 3-17 s per case. Per-operation timings are in each summary.

## Finding C2: references and renames agree with jedi and rope

`hardening/diff_references.py` and `hardening/diff_rename.py` were run on srxy at 953d27c: 40 seeded-random symbols,
seed 0, codenav at b08229a.

- **References against jedi:** 40 of 40 symbols agree, with 139 shared locations and 0 found by only one tool.
  [data](data/correctness/diff-refs-s0.json)
- **Renames against rope:** 38 of 40 symbols agree. The 2 that differ are 2 lines codenav edits and rope doesn't.
  Both were checked by hand, and both are correct uses rope misses. 0 lines are rope-only.
  [data](data/correctness/diff-rename-s0.json)

## Finding C3: rope also misses real call sites

While building the ground truth for the agent tasks ([DOC-007](agent-ab.md)), rope's rename missed:

- 3 calls of `Matcher.score`: `composite.py:34`, and `search_files.py:382` and `:383`, all called through a
  `CompositeMatcher`-typed variable;
- 2 calls of `_Segment.matches`: `ignore_rules.py:148` and `:173`.

So rope's output alone isn't a reliable ground truth. The task truths add these lines, each checked by hand; the files
are in [data/agent-ab/ground-truth](data/agent-ab/ground-truth/).

## Bugs these checks found and fixed

- **`convert_indent_unit` (fixed in dd9da03):** the hypothesmith property found it. A snippet whose blocks weren't a
  whole number of indent units could change structure when re-indented. Such snippets are now kept as written.
- **Review fixes (f3e5ccf and 1c7d26f):** these came from a manual review, not from the fuzzer:
  - change_signature names the test fakes it can't link;
  - impact sorts docstrings as comments;
  - search reads CLI-style names such as `--max-file-size`;
  - move accepts `to=`.

## Limits

- One run per seed. The fuzzer picks symbols at random, so rare constructs may never be drawn.
- I1 sees only what the projects' tests cover. In srxy, `scripts/` and `examples/` aren't type-checked by ty, so only
  I1 guards them there.
- The differential compares positions, not the full text of each edit.
