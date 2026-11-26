---
id: DOC-004
title: Hardening checks
summary: Slow automated checks that look for codenav bugs (markers, commands, invariants, runtimes)
type: reference
status: current
updated: 2026-11-26
verified:
covers: [hardening/**, tests/test_fuzz_refactors.py, tests/test_fuzz_support.py, pyproject.toml]
related: [DOC-003]
supersedes:
superseded_by:
---

# Hardening checks

Checks that find bugs without a manual QA session. They are slow, so each sits behind a pytest marker registered in
`pyproject.toml` and deselected by default (`addopts` has `-m "not fuzz and not differential and not crosshair and
not mutation"`), so `uv run pytest -q` stays fast. Passing `-m <marker>` on the command line overrides the default.
When a check finds a product bug it is written up in `hardening/FINDINGS-<workstream>.md` and kept as a strict
`xfail` test, so fixing the bug flips the test.

## Refactor fuzzer (`fuzz`)

**What it checks.** Picks symbols at random (seeded, reproducible) in real projects, applies a refactor through the
codenav MCP server (a fresh stdio server per case), and judges the result with the project's own test suite and
`ty check`, compared with a baseline taken before. Refactors: `rename` (default sites, and `sites=["all"]`),
`change_signature` adding a defaulted parameter, `move` of a top-level function to a new module with
`keep_reexport`, and `edit_symbol(action="delete")` of symbols `unused` labels `unused`. Symbols: functions, methods
(Protocol/ABC members flagged), classes, module constants, dataclass fields.

| invariant | statement |
|---|---|
| I1 | every new test failure or ty error after a default refactor traces to a site the reply listed as optional (u-ids), a held plan, or "needs manual attention"; a count such as "N attribute accesses could not be checked and are not listed" is not "listed". With `sites=["all"]`, failures in files the opted-in sites rewrote are reported separately as over-rename damage, not as I1 |
| I2 | rename X -> Y then Y -> X leaves `git status --porcelain` empty (default sites and `sites=["all"]`) |
| I3 | edits only touch `.py` files under the source/test roots (no dot-directories; other files only when `sites=["all"]` listed them); a held or refused call writes nothing |
| I4 | deleting an `unused`-labelled symbol never adds a ty error or a test failure |
| I5 | replies obey the v2 format: answer line first, `next:`/retry lines parse as calls of existing tools with known keywords and literal values, error replies carry a corrected call or `Example:` |

The ty comparison ignores file and line (refactors shift them) and compares the multiset of (rule, message), with
the new name mapped back to the old one so pre-existing errors that quote a renamed symbol do not count as new.
Tests: failing testcase ids from `--junitxml`; the baseline runs twice and flaky tests are excluded.

**Corpus.** `hardening/corpus.toml` pins name, commit, source/test roots and the test command of each project:
`srxy` (private sandbox clone), `click`, `tomlkit`, optional `python-dotenv`. `uv run python -m hardening.fuzz_refactors
--setup` clones and prepares the OSS ones (own uv venv each). Sandboxes are reset between cases
(`git checkout -- . && git clean -fd`) and left clean.

**How to run.**

```bash
# the report: >= 60 refactors across the corpus, writes results-seed<N>.jsonl + summary-seed<N>.md to --out
uv run python -m hardening.fuzz_refactors --seed 1 --count 80 --out /tmp/cnav-h1-out
# only some projects / operations
uv run python -m hardening.fuzz_refactors --seed 2 --count 20 --projects tomlkit --ops rename,move
# the small fixed-seed pytest entry (tomlkit, 8 cases)
uv run pytest -m fuzz tests/test_fuzz_refactors.py
```

**Runtime.** About 2.5 s per case on tomlkit, about 4 s on click, about 12 s on srxy (server start, the refactor,
`ty check`, the test suite, the reverse rename). The 80-case run takes about 7-10 minutes; the pytest entry about 35 s.
The pure helpers (`hardening/fuzz_checks.py`) are unit-tested in the default suite (`tests/test_fuzz_support.py`).

Every failing case in `results-seed<N>.jsonl` carries the project, commit, symbol, exact call, the replies and the
failing tests, which is the minimal repro for a finding.
