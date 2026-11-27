---
id: DOC-004
title: Hardening checks
summary: Automated checks that look for codenav bugs (properties, state machine, refactor fuzzer, differential, CrossHair, mutation): invariants, commands, runtimes
type: how-to
status: current
updated: 2026-11-27
verified:
covers: [hardening/**, tests/property/**, tests/test_fuzz_refactors.py, tests/test_fuzz_support.py, pyproject.toml]
related: [DOC-003]
supersedes:
superseded_by:
---

# Hardening checks

Checks that find bugs without a manual QA session. Fast properties run by default; slow checks each sit behind a pytest marker registered in
`pyproject.toml` and deselected by default (`addopts` has `-m "not fuzz and not differential and not crosshair and
not mutation"`), so `uv run pytest -q` stays fast. Passing `-m <marker>` on the command line overrides the default.
When a check finds a product bug it is written up in `hardening/FINDINGS-<workstream>.md` and kept as a strict
`xfail` test, so fixing the bug flips the test.

## Property tests, state machine, CrossHair and mutation (h3)

They live in `tests/property/`. The fast ones run in the default `uv run pytest -q` (about 7 s for the whole
directory); the slow ones need their marker, and each command below passes its own `-m`.

Product bugs the checks found are in `hardening/FINDINGS-h3.md`. Each is pinned by a test marked
`@pytest.mark.xfail(strict=True, reason="FINDINGS-h3 #N")`: fixing the bug turns the test into an error
(XPASS strict), and the marker is then removed. State shared by all commands: use a throwaway
`CODENAV_MCP_STATE_DIR` so a run never touches your real edit state.

```bash
export CODENAV_MCP_STATE_DIR=$(mktemp -d)   # bash; in fish: set -x CODENAV_MCP_STATE_DIR (mktemp -d)
```

Hypothesis profiles are in `tests/property/conftest.py`: `default` (40 examples, derandomized, so a normal run
never flakes) and `fuzz` (1500 examples, random seed; select it with `HYPOTHESIS_PROFILE=fuzz`). Long runs need
`--timeout=0` (the suite's default 60 s test timeout would stop them).

### Default (fast) properties

- **Invariants**
  - `signature.py` call rewriting, checked by executing the old call on the old definition and the rewritten call
    on the rewritten definition: every parameter binds to the same value and effectful arguments run in the same
    order; the rewritten statement parses; text outside the parentheses is kept; add/remove and reorder/restore
    round trips bind as at the start.
  - `plan_merge.py` / `multi_edit`: merging plans made from one base equals applying every intended edit (or a
    refusal naming an overlap); merge order does not matter except for inserts at one point; a plan merged with
    itself applies once; optional sites are carried across the plan's own edits or refused, never moved to the wrong
    line; a follow-up points at the line it names.
  - `args.py` aliases: no argument is silently dropped; an alias and its target agree or the call is refused;
    refusals name an unknown argument.
  - `reply.py`: pagination partitions the list; every `next:` call parses back to the same arguments plus the offset.
  - `moves.py` `__all__` editing on many layouts (list, tuple, bare tuple, single/multi-line, comments, trailing
    commas): the module still parses and exactly the named entry is added or dropped.
  - `edit_state` / `edit_ops` state machine over real files (`test_edit_state_machine.py`): plans and journal entries
    stay consistent through apply, undo, follow-ups, eviction at 50 entries, server restarts, and files reset or
    deleted outside the tool (those must not look cleanly applied).
- **Run**: `uv run pytest tests/property -q`
- **Runtime**: about 7 s (164 passed, 15 xfailed, with the 13 slow tests deselected).

### Fuzz

The same properties with 1500 random examples each (the state machine with 400 runs of up to 45 steps), without the
fixed seed.

- **Run**: `HYPOTHESIS_PROFILE=fuzz uv run pytest tests/property -m "not crosshair and not mutation and not fuzz" --timeout=0 -q`
- **Runtime**: about 3 to 4 min (189 s and 235 s in two runs; the state machine alone about 2 min 45 s).
- Failing examples are printed with a `Falsifying example`; reproduce with the `@reproduce_failure` line Hypothesis
  prints, then narrow the property with `assume` plus a deterministic xfail test as done for the existing findings.

#### Source transforms on generated Python (hypothesmith)

`tests/property/test_source_transforms_fuzz.py` generates modules (hypothesmith `from_node` / `from_grammar`, built
inside the tests, never at import time) and checks that:

- `pysource.convert_indent_unit` and `indent_block` keep the syntax tree (docstring text aside);
- `SourceMap` offsets stay inside the text and cut out each node's source;
- `imports.add_import` / `add_from_import` / `remove_names` keep the module parsing, add (or drop) exactly the
  named bindings, are idempotent and leave every non-import statement alone, with and without a docstring,
  shebang or encoding line;
- `moves._with_future_annotations` keeps the module valid and adds exactly the one line;
- `tidy.sort_imports` (real `ruff`) keeps every non-import line.

- **Run**: `HYPOTHESIS_PROFILE=fuzz uv run pytest tests/property/test_source_transforms_fuzz.py -m fuzz --timeout=0 -q -W ignore::SyntaxWarning`
- **Runtime**: about 11 minutes (150 examples per test; hypothesmith is slow at building code, `tidy` calls ruff per example).
- The generators produce code that compiles with warnings (such as "int object is not callable"); `-W ignore::SyntaxWarning` hides them.

### CrossHair (symbolic execution)

`tests/property/test_crosshair.py` runs the core pure properties with Hypothesis' CrossHair backend
(`settings(backend="crosshair")`): `reply.paginate` (slice, `more` count, pages partition the list),
`signature.apply_offset_edits` (length arithmetic, or a refusal exactly when two spans overlap),
`args.resolve_aliases`, and the key call-rewriting property.

- **What it can explore**: functions over plain ints, lists, short strings and tuples, where the solver picks
  values to hit each branch. It finds a seeded off-by-one in a pagination count in about a second.
- **What it cannot**: anything that parses text or touches files (`ast`, `tokenize`, `exec`, the language server)
  concretizes the symbolic values, so the call-rewriting test is guided sampling rather than proof (20 examples).
- **Run**: `uv run pytest -m crosshair tests/property/test_crosshair.py --timeout=0 -q`
- **Runtime**: about 28 s (5 passed in 27.5 s; call rewriting is most of it).

### Mutation testing (mutmut)

Mutates `signature.py`, `plan_merge.py`, `args.py` and `reply.py` (1636 mutants) and runs the unit tests for them
(`tests/property/`, `tests/test_signature.py`, `tests/test_plan_merge.py`, `tests/test_args.py`) against each
mutant. Configuration is in `[tool.mutmut]` in `pyproject.toml`. Not a gate: the report is read by a person.

- **Run**: `uv run mutmut run --max-children 12`, then `uv run mutmut results` (survivors) and
  `uv run mutmut show <mutant>` (the diff). It writes `mutants/` (ignored by git); delete it with `trash mutants`
  before a re-run after changing the tests or code.
- **Runtime**: about 1 min on a 32-core machine (45 mutants/s with 12 workers, the first run 15/s).
- **Result**: first run 1231 killed / 355 survived / 47 not covered / 3 timeouts; after the added example tests in
  `tests/property/test_mutation_killers.py`: 1437 killed / 190 survived / 8 not covered / 1 timeout. Not-killed per
  module, first run to last: `signature` 206 to 94, `plan_merge` 100 to 36, `args` 88 to 66, `reply` 11 to 3.
- **Survivors left** (grouped in the "Mutation survivors" section of `hardening/FINDINGS-h3.md`): mostly the wording of refusal
  messages and hints, `autojunk=` arguments of `difflib` (only matter above 200 lines, a performance heuristic),
  `strict=` of `zip` calls where the lengths always agree, and equivalent mutants.

## Refactor fuzzer (`fuzz`)

**What it checks.** Picks symbols at random (seeded, reproducible) in real projects, applies a refactor through the
codenav MCP server (a fresh stdio server per case), and judges the result with the project's own test suite and
`ty check`, compared with a baseline taken before. Refactors: `rename` (default sites, and `sites=["all"]`),
`change_signature` adding a defaulted parameter, `move` of a top-level function to a new module with
`keep_reexport`, and `edit_symbol(action="delete")` of symbols `unused` labels `unused`. Symbols: functions, methods
(Protocol/ABC members flagged), classes, module constants, dataclass fields.

| invariant | statement |
|---|---|
| I1 | every new test failure or ty error after a default refactor traces to a site the reply listed as optional (u-ids), a held plan, "needs manual attention", or a "Regenerate <file> with its generator" note for a file the failing test reads; a count such as "N attribute accesses could not be checked and are not listed" is not "listed". With `sites=["all"]`, failures in files the opted-in sites rewrote are reported separately as over-rename damage, not as I1 |
| I2 | rename X -> Y then Y -> X leaves `git status --porcelain` empty (default sites and `sites=["all"]`) |
| I3 | edits only touch the project's `.py` files (`examples/` and `scripts/` count: ty resolves references there in click, and where the ty config excludes them, as in srxy, only I1 guards those edits; never dot-directories, `venv`, `build`, `dist`, `site-packages`; other files only when `sites=["all"]` listed them); a held or refused call writes nothing |
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
