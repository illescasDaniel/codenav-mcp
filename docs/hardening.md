---
id: DOC-004
title: Hardening checks
summary: Slow automated checks that look for bugs (differential runs against jedi and rope), how to run each and what they assert
type: reference
status: current
updated: 2026-11-26
verified:
covers: [hardening/**, tests/test_hardening_*.py, pyproject.toml]
related: [DOC-003]
supersedes:
superseded_by:
---

# Hardening checks

Checks that look for bugs instead of waiting for a QA session to find them. The slow ones sit behind a pytest marker
(`fuzz`, `differential`, `crosshair`, `mutation`) registered in `pyproject.toml` and deselected by default, so
`uv run pytest -q` stays fast. Select one with `-m <marker>`, which replaces the default deselection. A check that
finds a product bug writes a minimal repro to `hardening/FINDINGS-<id>.md` and keeps a failing test marked
`xfail(strict=True, reason="FINDINGS-<id> #N")`: fixing the bug flips the test, and the marker is then removed.

## Differential: references against jedi, rename against rope

**Invariant.** For a sampled function, method or class, codenav and an independent tool agree on where the symbol
is used:

- `hardening/diff_references.py` compares the (path, line) set of `lookup(name=..., show=["references"], limit=1000)`
  with jedi's `get_references` at the definition.
- `hardening/diff_rename.py` compares the (path, line) set that `rename(apply=false)` edits with rope's
  `Rename(...).get_changes`. Two modes: codenav's certain sites against rope's default rename, and codenav with
  `sites=["all"]` against rope with `docs=True` and every unsure occurrence accepted. The second mode is noisy by
  construction (rope rewrites every comment and string that spells a common name), so read its
  `codenav_lines_also_in_rope_rate`, not the overlap rate. When codenav's preview diff is cut at 150 lines only the
  per-file edited-line counts are compared (`basis` is `file counts`).

Both scripts only read the project (rope runs without a `.ropeproject` folder, codenav's edit state goes to a
temporary directory, renames are previews). A symbol codenav refuses (ambiguous name, not found, excluded file) is an
error result with an `error_kind`, never counted as an empty result. Sampling is seeded; dunder methods and names
defined twice in a file (property setters) are skipped, and codenav is asked for `Class.method` plus the file path.

```bash
uv run python hardening/diff_references.py PROJECT_DIR --symbols 40 --seed 0 [--output FILE]
uv run python hardening/diff_rename.py PROJECT_DIR --symbols 40 --seed 0 [--output FILE]
uv run pytest -m differential          # known-answer runs of both scripts on a tiny project, about 5 s
```

Each prints one block per symbol and a `SUMMARY` line, and writes JSON (default `hardening/results/diff_*.json`)
with per symbol `only_in_codenav`, `only_in_jedi` / `only_in_rope`, `shared` and the timing. Runtime on a 335-file
project: 40 symbols take about 8 s for references and about 35 s for rename (rope rescans the project per rename);
400 reference symbols about 45 s. The parser and sampler unit tests in `tests/test_hardening_differential.py` run in
the default suite.
