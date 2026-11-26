# FINDINGS-h1: refactor fuzzer

Found by `hardening/fuzz_refactors.py` (seed 1, 80 refactors on srxy, click, tomlkit; see docs/hardening.md).
Result of the run: I1 60 pass / 4 fail, I2 39/0, I3 80/0, I4 3/0, I5 76/4 (77 and 41 and 16 not applicable); the 8
failing cases map to #1 (srxy-1-20, srxy-1-27), #2 (click-1-13), #3 (srxy-1-16, srxy-1-18, click-1-5), #4 (click-1-7),
#7 (srxy-1-13). Two further failures of the first runs were checker false positives and were fixed in the checker
(tomlkit-1-9: the failing test reaches a *listed* site in src through its traceback; click-1-19: a move the fuzzer forced
with `allow_import_cycle=true`). Every
finding has a minimal synthetic repro in `tests/test_h1_findings.py`, marked `xfail(strict=True, reason="FINDINGS-h1 #N")`
(fixing the bug flips the test). Project commits: srxy `953d27c`, click `08b803c`, tomlkit `d2aa305`; codenav-mcp v2 @ `5ead15e`.
Severity: high = a refactor leaves the project broken and the reply does not say where; medium = wrong or misleading
result with a workaround; low = reply wording.

## #1 (high, I1): string attribute names are neither rewritten nor listed as optional sites

`hasattr(obj, "name")`, `getattr(obj, "name")`, `monkeypatch.setattr(module, "name", ...)` name the renamed symbol as a
string. `rename` does not rewrite them and does not give them a `u<N>` id: the reply only says "`helper` also appears in
3 strings (not listed: plain words, other symbols), e.g. ..." so `sites=["u1"]` / `sites=["all"]` cannot opt them in.
The project breaks at runtime and the agent has no handle to fix it through codenav.

- Repro (test `test_f1_string_attribute_names_are_rewritten_or_listed`): `src/pkg/core.py: def helper()`,
  `src/pkg/use.py: hasattr(core, "helper") / getattr(core, "helper")()`,
  `tests/test_use.py: monkeypatch.setattr(core, "helper", lambda: 2)`;
  `rename(name="helper", new_name="assist", file_path="src/pkg/core.py")`.
- Actual: applied, 1 edit, the three string sites untouched and not in the optional list. Expected: rewritten (exact
  patch-target style) or listed as `u` sites.
- Seen on the corpus: srxy-1-27 `rename _is_windows` (`monkeypatch.setattr(install_mod, "_is_windows", ...)`, 9 new
  test failures, even with `sites=["all"]`), srxy-1-20 `rename Matcher.score_with_breakdown`
  (`hasattr(matcher, "score_with_breakdown")` in src/srxy/application/use_cases/search_objects.py:22 -> new failure in
  tests.unit.test_dsl).

## #2 (high, I1): the type gate reports "0 new errors" while a test subclass now overrides incompatibly

`change_signature(add=[{"name": ..., "default": "None"}])` on a base-class method applied, with "Type check: 0 new
error(s)", although `ty check` reports a new `invalid-method-override` in a test file that subclasses the class through
the package re-export (`import pkg; class Sub(pkg.Base): def run(self, x)`). The override was neither updated nor listed.

- Repro (test `test_f2_gate_checks_files_that_subclass_through_a_package_reexport`):
  `src/pkg/__init__.py: from .base import Base as Base`, `src/pkg/base.py: class Base: def run(self, x: int)`,
  `tests/test_base.py: import pkg; class Sub(pkg.Base): def run(self, x: int)` (nested in a test function);
  `change_signature(name="Base.run", file_path="src/pkg/base.py", add=[{"name": "extra", "default": "None"}])`.
- Actual: `Applied as edit ...`, "0 new error(s)"; `ty check` -> `tests/test_base.py:6:7 error[invalid-method-override]`.
  Expected: the override updated/listed, or the plan held. (With `from pkg.base import Base` the gate does catch it and
  holds the plan, so the dependents set misses subclasses reached through an `import pkg` attribute access.)
- Seen on the corpus: click-1-13 `change_signature Command.parse_args` -> `tests/test_commands.py:157`
  (`class OptParseCommand(click.Command)`).

## #3 (medium, I5): `edit_symbol(action="delete")` on a constant tells the agent to use a tool that no longer exists

Reply: `'LIMIT' is not a class, function or method (found a variable or attribute); edit it with \`edit\` instead.`
There is no `edit` tool in v2 (it is `edit_symbol(edits=[...])`). Also, `unused(kind="constant")` offers constants as
dead code, so the natural follow-up (delete) is refused. Test `test_f3_delete_of_a_constant_does_not_point_at_a_removed_tool`.
Seen: srxy-1-16 `MIN_OCR_QUALITY_SCORE`, srxy-1-18 `_HF`, click-1-5 `V` (3 of the 4 refused deletes).
Expected: delete the assignment (or an exact `edit_symbol(edits=[...])` call), never a removed tool name.

## #4 (low, I5): ambiguity "Retry with one of" entries are not calls, and rename cannot take the position they ask for

When a name is defined twice in one file (class attribute `UNSET` and module-level `UNSET`), rename replies:

    2 symbols match 'UNSET'; they all live in one file and share a name ...; rename one of them first ...:
    Nothing was changed. Retry with one of (other arguments unchanged):
      [Constant] src/click/_utils.py:15:5
      [Constant] src/click/_utils.py:25:1
      (... use lookup with file_path, line and column from the listing.)

The entries are bare locations, not copy-pastable calls (v2 principle 2), and `rename` has no `line`/`column`
parameters, so the advice cannot be followed ("rename one of them first" is circular). Test
`test_f4_ambiguity_retry_entries_are_calls`. Seen: click-1-7. Expected: a call of an existing tool with the parameters
it needs (e.g. rename gaining `line`/`column`, or a qualified name), or an honest "cannot be renamed by name".

## #5 (medium, consistency): `unused` says "unused", `edit_symbol(delete)` refuses or holds the same symbol

`unused` labels symbols `unused` that `edit_symbol(action="delete")` then refuses ("Not deleted: ... is still used") or
holds ("held back because N reference(s) remain"). Of the 12 delete cases on `unused`-labelled symbols, 3 applied,
5 were held and 4 refused (3 are #3), so the label is not a reliable "safe to delete". Two causes seen:
(a) a string forward reference (`TypeVar("WT", bound="Wrapper")`; test
`test_f5_unused_and_delete_agree_on_a_string_forward_reference`, tomlkit-1-17 `WrapperType`): delete counts it as a use,
`unused` does not; (b) common method names (`close`, `item`, `readlines`, `isolated_filesystem`) with unlinked same-name
attribute accesses elsewhere (click-1-8, click-1-18, tomlkit-1-13, click-1-1): `unused` should lower the confidence
instead of "unused". I4 itself held: no applied delete broke ty or the tests (the hold/refusal is the safety net).

## #6 (medium, I1/usability): renaming a name re-exported with `from .mod import name as name` is held with an unfixable error

`rename` does not rewrite the re-export `from .fmt import wrap as wrap` in the package `__init__` (it appears only as
"2 bare names ... src/pkg/__init__.py:1"), so the type gate sees `Module pkg.fmt has no member wrap` and holds the plan
("NOT applied: 1 new error"); no `u` site or `sites=` option fixes it, only `edits(apply)` writing the broken plan.
Test `test_f6_rename_updates_the_package_reexport`. Seen: click-1-17 `rename wrap_text` (`src/click/__init__.py:41`).
Expected: the import-from (and its alias) rewritten as a linked reference.

## #7 (low, I1): mentions in non-Python files (docs) are counted, not listed, so `sites=` cannot opt them in

Renaming a class that a committed Markdown reference documents leaves a docs-sync test red. The reply only says
"`QNodeType` also appears in 3 non-Python file (not listed: plain words, other symbols), e.g. docs/api-reference.md:66",
and the closing line already warns "docs not checked", but the sites get no `u` id (design: v2 "uncertain sites" are
strings and comments). Test `test_f7_markdown_mentions_of_the_symbol_get_an_optional_site_id`. Seen: srxy-1-13
(`tests.unit.test_public_api::...matches_committed_docs`). Possibly by design; kept as a finding because I1 says
failures must trace to a listed site.

## Observations that are not findings

- `sites=["all"]` opts in unchecked attribute accesses and can rewrite a different symbol: srxy-1-14
  `InstallOptions.srxy_spec` also rewrote `args.srxy_spec` of an argparse namespace, breaking 4 tests. This is the
  documented price of "all" (reported as over-rename damage, not counted as I1).
- Moves into a new module with `keep_reexport=true` are refused as an import cycle in 5 of 13 cases (the moved body
  uses its old module). The refusal is correct and names `allow_import_cycle=true`; the fuzzer retries with it so the
  move itself is still exercised.
- A held rename plan replies with `is_error=true` while a held delete does not (inconsistent, harmless).
- Round trips (I2) held in all 39 applied renames (default and `sites=["all"]`); I3 held in all 80 cases.
