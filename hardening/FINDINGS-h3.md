# FINDINGS-h3: property, state-machine, CrossHair and mutation checks

Found by the checks in `tests/property/` (see `docs/hardening.md` for how to run them). Product code was not
changed. Each finding below has a minimal repro, and a deterministic test marked
`@pytest.mark.xfail(strict=True, reason="FINDINGS-h3 #N")`; when the bug is fixed the test flips to an error
("XPASS(strict)") and the marker is removed.

Where a general property had to be narrowed to keep running, the narrowing is an `assume(...)` or an excluded
generator option next to a comment `FINDINGS-h3 #N`, so a fix lets the property cover the case again.

| # | Area | Severity | Summary | xfail test |
|---|------|----------|---------|------------|
| 1 | `change_signature` call rewrite | medium | Removing the only argument of a call written with a trailing comma leaves `f(,)` | `test_signature_findings.py::test_given_the_only_argument_removed_from_a_call_with_a_trailing_comma_then_the_call_still_parses` |
| 2 | `change_signature` call rewrite | low | Keyword arguments written out of parameter order are re-emitted in parameter order | `test_signature_findings.py::test_given_keyword_arguments_out_of_parameter_order_then_adding_a_parameter_leaves_them_where_they_are` |
| 3 | `change_signature` call rewrite | low | A keyword argument written `b = 2` is re-spelled `b=2` by an unrelated change | `test_signature_findings.py::test_given_a_keyword_argument_written_with_spaces_then_a_change_elsewhere_keeps_its_spelling` |
| 4 | `multi_edit` merge | medium | The same insert in two items, with a different insert from another item between them, is written twice | `test_plan_merge_props.py::test_given_the_same_insert_in_two_items_with_another_insert_between_then_it_is_written_once` |
| 5 | `multi_edit` merge | medium | Edits on different lines of a file with repeated lines are refused as overlapping | `test_plan_merge_props.py::test_given_two_edits_on_different_lines_of_a_file_with_repeated_lines_then_they_merge` |
| 6 | `multi_edit` merge | high | Two edits whose line diffs align differently silently give a wrong merge (an extra line) | `test_plan_merge_props.py::test_given_two_edits_whose_diffs_share_an_incidental_hunk_then_the_merge_is_not_silently_wrong` |
| 7 | argument aliases | low | An alias given `true` and its target given `1` are merged, not reported as a conflict | `test_args_props.py::test_given_an_alias_true_and_its_target_one_then_they_are_a_conflict_not_a_merge` |
| 8 | `move` (`__all__` editing) | low | Dropping the only entry of a parenthesis-less tuple `__all__ = "a",` leaves `__all__ = ` | `test_all_entry_props.py::test_given_a_bare_tuple_all_with_one_entry_then_dropping_that_entry_keeps_the_module_valid` |
| 9 | `move` (`__future__`) | medium | `from __future__ import annotations` is inserted above a shebang or an encoding line | `test_all_entry_props.py::test_given_a_shebang_then_adding_the_future_import_keeps_the_shebang_on_the_first_line`, `...test_given_an_encoding_line_then_adding_the_future_import_keeps_it_within_the_first_two_lines` |
| 10 | `move` (`__all__` editing) | medium | Dropping the first of two entries of a tuple `__all__` leaves `("b")`, a plain string | `test_all_entry_props.py::test_given_a_two_entry_tuple_all_then_dropping_the_first_entry_leaves_a_tuple_not_a_string` |
| 11 | `pysource` indentation helpers | low | A form feed at the start of an indented line makes `indent_block` produce invalid Python | `test_all_entry_props.py::test_given_a_form_feed_in_an_indented_line_then_indenting_the_block_keeps_it_valid_python` |

No violation was found by the edit-state state machine (see "Checked and clean" at the end).

---

## #1 Trailing comma after the only argument: `f(,)`

- **Input**: definition `def f(a, b=1)`, change `remove=["a"]`, call `result = f(1,)`. Same for `result = f(a=1,)` and
  the multi-line `result = f(\n\t1,\n)`.
- **Actual**: `result = f(,)` (and `f(\n\t,\n)`), not valid Python.
- **Expected**: `result = f()`.
- **Invariant**: a rewritten call statement parses. (`call_edit`'s "arguments only removed from the end" branch
  cuts `old_args[0].start..old_args[-1].end` and leaves the trailing comma.)
- **Impact**: the call is a valid input; the edit would write invalid code. `refactor_tools` runs `check_syntax`
  afterwards, so the likely user-visible result is a refusal of a valid change, not a corrupted file; that was not
  verified end to end.

## #2 Keyword arguments reordered by a change that did not ask for it

- **Input**: `def f(a, b=1)`, change `add=[{"name": "n", "value": 9, "keyword_only": True}]`, call
  `result = f(b=2, a=1)`.
- **Actual**: `result = f(a=1, b=2, n=9)`.
- **Expected**: `result = f(b=2, a=1, n=9)` (arguments kept as written, the new one appended).
- **Invariant**: a call is rewritten only where the change requires it. Semantics are preserved for names and
  constants, but not for effectful values: `f(b=g(), a=h())` is refused as a manual call instead of left alone.

## #3 A keyword argument loses its spelling

- **Input**: same definition and change, call `result = f(1, b = 2)`.
- **Actual**: `result = f(1, b=2, n=9)`. **Expected**: `result = f(1, b = 2, n=9)`.
- **Invariant**: text the change does not need to touch is kept byte for byte.

## #4 The same insert twice around another insert

- **Input**: base `a = 1\nb = 2\n`; three plans in one `multi_edit` made from it: items 1 and 3 insert
  `import os\n` before line 2, item 2 inserts `import sys\n` before line 2.
- **Actual**: `a = 1\nimport os\nimport sys\nimport os\nb = 2\n` (`import os` twice).
- **Expected**: `import os` once. `merge_plans` drops a duplicate only when it equals the hunk written *just
  before*; item 2's hunk sits between them.
- **Invariant**: the same edit from several plans is applied once.

## #5 Repeated lines make independent edits "overlap"

- **Input**: base `x = 1\nx = 1\nx = 1\n`; item 1 replaces the first line with `FIRST`; item 2 replaces the last
  with `SECOND`.
- **Actual**: `ToolInputError: item 1 and item 2 both change m.py around line 3; give them separately`.
- **Expected**: `FIRST\nx = 1\nSECOND\n`.
- **Cause**: each plan is diffed against the base with `difflib`, which aligns identical lines arbitrarily, so item
  1's hunk can be reported at the other end of the run of identical lines.
- **Invariant**: edits to different places merge.

## #6 A silently wrong merge

- **Input**: base `"x = 1\n" * 4`; item 1 = `FIRST` + three `x = 1`; item 2 = `x = 1`, `x = 1`, `SECOND`, `x = 1`.
- **Actual**: `FIRST\nx = 1\nx = 1\nSECOND\nx = 1\n` (five lines: `SECOND` was inserted instead of replacing the third
  line).
- **Expected**: `FIRST\nx = 1\nSECOND\nx = 1\n`.
- **Cause**: as #5, but here the two alignments happen not to overlap, so nothing is refused and an extra line is
  written. This is the worse variant: no refusal, wrong file.
- **Invariant**: merging item edits equals applying each intended edit to the base.

## #7 `true` and `1` are not different

- **Input**: `resolve_aliases("t", {"line"}, {"line": 1, "lineno": True})`.
- **Actual**: `({"line": 1}, None)` (the values compare equal in Python: `1 == True`).
- **Expected**: a refusal "give either 'line' or 'lineno', not both".
- **Invariant**: an alias and its target agree only when the values are identical, not merely `==`.

## #8 Dropping the only entry of a bare tuple `__all__`

- **Input**: `import os\n\n__all__ = "a",\n`, `moves._drop_all_entry(text, "a")`.
- **Actual**: `import os\n\n__all__ = \n`. **Expected**: `__all__ = ()` (or the statement removed).
- **Invariant**: the edited module still parses. `move` calls `check_syntax` afterwards, so this shows as a refusal
  of a valid move.

## #9 `__future__` import above the shebang / encoding line

- **Input**: `moves._with_future_annotations("#!/usr/bin/env python\nimport os\n")`, and with
  `# -*- coding: latin-1 -*-\nx = 1\n`.
- **Actual**: `from __future__ import annotations\n#!/usr/bin/env python\nimport os\n`; the encoding variant gives the
  declaration on line 3.
- **Expected**: the shebang stays on line 1; the encoding declaration stays within lines 1-2 (PEP 263), so the
  import goes after them (`imports.add_import` already skips them).
- **Impact**: an executable script stops being executable via its shebang; a latin-1 file with non-ASCII bytes
  stops decoding.

## #10 `("a", "b")` minus `"a"` is `("b")`

- **Input**: `__all__ = ("a", "b")\n`, `moves._drop_all_entry(text, "a")`.
- **Actual**: `__all__ = ("b")\n`, a string, not a tuple. `moves._all_names` then reads no names, and `from m import *`
  fails at import time with a `TypeError`.
- **Expected**: `__all__ = ("b",)\n`.
- **Invariant**: `__all__` keeps its type. This passes `check_syntax`, so it is not caught.
- A bare `__all__ = "a", "b"` behaves the same way (becomes `"b"`).

## #11 Form feed at the start of an indented line

- **Input**: `class A:\n\x0c pass\n` (valid Python), `pysource.indent_block(text, "\t")` under `if 1:`.
- **Actual**: `unindent does not match any outer indentation level`. `convert_indent_unit` is affected too.
- **Expected**: still valid.
- **Impact**: very unlikely in real code; found by the hypothesmith fuzz and excluded from it (`usable()`).

---

## Observations that are not pinned (layout only; the result is valid and binds the same)

- A multi-line call that gets an argument added keeps the closing layout but puts the new argument on the last
  line: `f(\n\t1,\n\t2,\n\t3, n=9,\n)`.
- Reordering `f(1,\n\t2)` gives `f(2,\n1)`: the continuation line loses its indentation when the first argument
  moves.

## Candidates checked and not confirmed

- State machine over `edit_state`/`edit_ops`: see below.
- `rebind_call` evaluation order: no violation beyond #2 in thousands of examples with an execution oracle (old call
  on the old definition vs new call on the new definition; bindings and the order of side effects compared).

## Checked and clean: the edit-state state machine

`tests/property/test_edit_state_machine.py` drives real files through plan/apply/undo/followup/eviction and
simulated server restarts, with external resets and deletes. The known gap (applied edits whose files were reset
outside the tool must not look cleanly applied) did **not** reproduce: an externally reset file is flagged
`[changed since:` in the list and `undo` refuses. Scenarios covered: journal and plan eviction at 50 entries, follow-up
plans, child edits, stale previews, restarts, external resets and deletes.

## Mutation testing

See the "Mutation testing" section of `docs/hardening.md` for the run and the numbers. The survivors worth a test were
turned into tests in `tests/property/test_mutation_killers.py`; the rest are listed at the end of that section.
