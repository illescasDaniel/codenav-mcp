---
id: DOC-004
title: Hardening checks
summary: Property, state-machine, fuzz, CrossHair and mutation checks (h3): invariants, run commands, runtimes
type: how-to
status: current
updated: 2026-11-26
verified:
covers: [tests/property/**, pyproject.toml, hardening/FINDINGS-h3.md]
related: []
supersedes:
superseded_by:
---

# Hardening checks (h3)

Automated checks that find bugs without a manual QA session. They live in `tests/property/`. The fast ones run
in the default `uv run pytest -q` (about 7 s for the whole directory); the slow ones sit behind pytest markers
registered in `pyproject.toml` and deselected by default (`addopts` has
`-m "not fuzz and not differential and not crosshair and not mutation"`). A later `-m` on the command line
replaces that one, so each command below passes its own.

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

## Default (fast) properties

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
- **Runtime**: about 7 s (146 passed, 15 xfailed, with the 13 slow tests deselected).

## Fuzz

The same properties with 1500 random examples each (the state machine with 400 runs of up to 45 steps), without the
fixed seed.

- **Run**: `HYPOTHESIS_PROFILE=fuzz uv run pytest tests/property -m "not crosshair and not mutation and not fuzz" --timeout=0 -q`
- **Runtime**: about 3 to 4 min (189 s and 235 s in two runs; the state machine alone about 2 min 45 s).
- Failing examples are printed with a `Falsifying example`; reproduce with the `@reproduce_failure` line Hypothesis
  prints, then narrow the property with `assume` plus a deterministic xfail test as done for the existing findings.

### Source transforms on generated Python (hypothesmith)

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

## CrossHair (symbolic execution)

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

## Mutation testing (mutmut)

Mutates `signature.py`, `plan_merge.py`, `args.py` and `reply.py` (1636 mutants) and runs the unit tests for them
(`tests/property/`, `tests/test_signature.py`, `tests/test_plan_merge.py`, `tests/test_args.py`) against each
mutant. Configuration is in `[tool.mutmut]` in `pyproject.toml`. Not a gate: the report is read by a person.

- **Run**: `uv run mutmut run --max-children 12`, then `uv run mutmut results` (survivors) and
  `uv run mutmut show <mutant>` (the diff). It writes `mutants/` (ignored by git); delete it with `trash mutants`
  before a re-run after changing the tests or code.
- **Runtime**: about 1 min on a 32-core machine (45 mutants/s with 12 workers, the first run 15/s).
- **Result**: first run 1231 killed / 355 survived / 47 not covered / 3 timeouts; after the added example tests in
  `tests/property/test_mutation_killers.py`: 1431 killed / 196 survived / 8 not covered / 1 timeout. Not-killed per
  module, first run to last: `signature` 206 to 100, `plan_merge` 100 to 36, `args` 88 to 66, `reply` 11 to 3.
- **Survivors left** (grouped in the "Mutation survivors" section of `hardening/FINDINGS-h3.md`): mostly the wording of refusal
  messages and hints, `autojunk=` arguments of `difflib` (only matter above 200 lines, a performance heuristic),
  `strict=` of `zip` calls where the lengths always agree, and equivalent mutants.

## Not run by default: `differential`

The `differential` marker is registered for comparisons against other tools (jedi, rope); h3 adds no such checks.
