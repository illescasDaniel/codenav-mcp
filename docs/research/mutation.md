---
id: DOC-008
title: "Research: mutation testing of codenav's own tests"
summary: "mutmut on the rename-site, mention, heredoc and edit-history modules with the full test suite: 2608 mutants, what survived and why, the test gaps it found and the tests added, and whether mutation testing is worth running"
type: reference
status: current
updated: 2026-11-27
verified:
covers: [src/codenav_mcp/heredocs.py, src/codenav_mcp/mentions.py, src/codenav_mcp/rename_sites.py, src/codenav_mcp/edit_state.py, pyproject.toml]
related: [DOC-004, DOC-005]
supersedes:
superseded_by:
---

# Research: mutation testing of codenav's own tests

Mutation testing changes one small thing in the code (a `<` into `<=`, a `continue` into `break`, a string into
`"XX…XX"`) and runs the tests. If they still pass, the "mutant" survived: no test notices that change. This page has
the results for 4 modules that decide which optional rename sites codenav offers and how edits are stored, the gaps the
survivors showed, and the tests added for them. How to run it is in
[Hardening checks](../hardening.md#mutation-testing-mutmut) (DOC-004).

## Setup

- **Tool:** mutmut 3.8.1, 12 workers, on a 32-core machine.
- **Mutated:** `heredocs.py`, `mentions.py`, `rename_sites.py` and `edit_state.py`, 2608 mutants.
- **Tests:** the full suite (`tests/`, 1268 tests by default at the end), including the integration tests that start ty.
  An earlier run used only the property and unit tests of other modules; its numbers are not comparable and are
  not reported here.
- **Earlier work:** `signature.py`, `plan_merge.py`, `args.py` and `reply.py` were mutated before, with their own test
  files: 1437 killed and 190 survived after 117 added tests
  ([FINDINGS-h3](../../hardening/FINDINGS-h3.md#mutation-survivors)).

## Finding M1: the full suite catches about 84% of mutants; the tests added cover the gaps that mattered

| module | mutants | survived, run 2 | survived, final | caught, final |
|---|---|---|---|---|
| `heredocs.py` | 269 | 22 | 16 | 94% |
| `mentions.py` | 385 | 62 | 48 | 88% |
| `rename_sites.py` | 1305 | 255 | 250 | 81% |
| `edit_state.py` | 649 | 92 | 92 | 86% |
| **total** | 2608 | 431 | 406 | 84% |

- **Run 2** is the baseline: the full suite at f25bc84, after the pilot fixes. 2171 killed, 5 timeouts (a mutant that
  makes the tests hang counts as caught), 1 not covered, 431 survived.
- **Run 3** added the tests in M2 and left 409. Three of those tests didn't yet catch their mutant (a test string 39
  characters long instead of 40, no unreadable file, no comment right after a class); they were fixed, and the
  final run reports all three killed.
- **Final run** (the committed configuration, all 8 modules, 18 minutes): 4719 mutants, 4031 killed, 5 timeouts,
  9 not covered, 674 survived. For the 4 modules above, 406 survived. The other 4 modules have grown since their
  earlier baseline (`signature.py` from 798 to 892 mutants, `plan_merge.py` from 487 to 799), so their numbers aren't
  compared with it: `signature` 89 survived and 8 not covered, `plan_merge` 98, `args` 78, `reply` 3.

Per-mutant results: [run 2](data/mutation/results-run2.txt), [run 3](data/mutation/results-run3.txt),
[final](data/mutation/results-final.txt), and the diff of every run-2 survivor in
[survivors-run2.diff](data/mutation/survivors-run2.diff).

## Finding M2: most survivors don't matter, a few were real gaps

Of the 431 survivors of run 2, 99 only change the text of a message or hint. These are left on purpose: pinning every
sentence would make rewording a test failure. The other 332 were read grouped by function. They fall into:

- **Equivalent mutants**, which can't change behaviour: `min(indents, default=1)` where an empty list is then never
  used, `rsplit(".", 2)[-1]` against `rsplit(".", 1)[-1]`, `mode=449` against `0o700` on a directory created with
  `exist_ok`, `None` against `""` for a value only tested for truth, `encoding=None` (UTF-8 on Linux).
- **Bookkeeping that only affects order or wording:** the column stored for an optional site is used to sort sites
  on the same line; the counts in notes ("3 keyword arguments were not checked").
- **Limits not tested at their exact boundary:** at most 60 text sites, 400 text files, 100 remembered undo IDs.
- **Fallbacks for unusual language-server replies:** a `LocationLink` without `range`, a location without `line`.
- **Real gaps**, where the behaviour matters and no test checked it. Run 3 adds tests for these:

| gap | what a mutant could break unnoticed | test added |
|---|---|---|
| a skipped file ended the scan | a symlink, an oversized file or a filtered path before other files made the mention scan stop (`continue` turned into `break`, 5 mutants) | `test_mentions.py::test_given_skipped_files_first_when_finding_mentions_then_later_files_are_still_scanned` |
| a shell script's sites replaced earlier ones | `found = [...]` instead of `found += [...]` dropped every Python mention found before the first `.sh` file | the same test |
| symlinked text files | a symlink to a file outside the workspace was read | the same test |
| nested checkouts in a plain folder | `third_party/lib/.git` was scanned; the only test had the checkout under a dot-folder, which is skipped anyway | `test_mentions.py::test_given_nested_checkout_in_a_plain_folder_when_finding_mentions_then_it_is_skipped` |
| prose boundary | a string of exactly 4 words counts as prose | `test_mentions.py::test_given_strings_of_three_and_four_words_when_finding_mentions_then_four_words_is_prose` |
| heredoc delimiter | a `<<-` body line `  PY` (spaces) ended the heredoc; the existing test had `  PY,`, so it passed by accident | `test_heredoc_sites.py::test_given_dashed_heredoc_when_a_string_line_is_the_delimiter_indented_with_spaces_then_the_body_goes_on` |
| tabs in a plain heredoc | tabs stripped without `<<-` turned nested Python into an `IndentationError` | `test_heredoc_sites.py::test_given_plain_heredoc_with_nested_tab_indent_when_scanned_then_tabs_are_kept_as_python_indentation` |
| empty heredoc | `min()` without a default crashed on a heredoc with no lines | `test_heredoc_sites.py::test_given_empty_heredoc_when_the_name_is_elsewhere_in_the_script_then_nothing_is_found` |
| callee width | a callee of exactly 40 characters was shortened | `test_heredoc_sites.py::test_given_callee_of_exactly_the_width_when_scanned_then_it_is_kept_whole` |
| bare-name columns | the column of a bare name in a heredoc was never checked | `test_heredoc_sites.py::test_given_bare_names_when_scanned_then_their_columns_point_at_the_name` |
| a class's own member | a class body `matches = ...` (plain assignment) or `matches: int` wasn't checked: the test's docstring came after the field, where it is not a docstring, so it was never a site | `test_rename_sites.py::test_given_class_assigning_the_name_when_scanned_then_only_that_class_prose_is_skipped`, and the `Fields` case of the existing test fixed |

## Finding M3: the run also exposed a flaky test setup

The first full-suite run stopped before mutating anything: one test failed in the clean run. `server.py` keeps three
module-level `asyncio.Lock`s, and a lock binds to the first event loop that waits on it. Each test makes its own loop,
so a test that waited on a lock left it bound to a closed loop, and a later test that waited on it failed. mutmut's
slower code changed the timing enough to make that happen. The server runs one loop, so this is a test-isolation bug,
not a product bug. `tests/live_support.py` now gives each test fresh locks. A script that binds the lock to a closed
loop first reproduces the failure without the change and passes with it.

Also fixed: two tests in `test_qa_b21.py` built the ty path from the repository folder, which doesn't exist in
mutmut's copy. They now use the ty next to the running interpreter, the same file in a normal checkout.

## Is it worth running?

Yes, for these modules and used this way: run it after changing one of them, and read the survivors of the functions
that changed. Not as a CI gate, and not to chase a score.

- **What it found:** about a dozen tests that weren't checking what they seemed to. Two passed by accident: a trailing
  comma in one, and in the other a docstring placed where Python doesn't treat it as one. The first run also exposed
  a test setup that depended on timing (M3). The earlier pass on 4 other modules led to 117 added tests.
- **What it didn't find:** product bugs. Those came from the property tests, the refactor fuzzer and watching agents
  use codenav ([DOC-006](correctness.md), [DOC-007](agent-ab.md)). Mutation testing measures the tests, not the code.
- **Cost:** a full run took 15-16 minutes for these 4 modules and 18 minutes for all 8, on 32 cores. Most survivors are noise (message text,
  equivalent mutants), so reading them is the expensive part; grouping them by function makes it quick.
- **Not worth it for:** the wording of messages (left unpinned on purpose), and limits nobody hits at their exact
  boundary.

## Limits

- One run per configuration; mutmut's choice of mutations is fixed, so a re-run gives the same mutants.
- The survivors were read grouped by function, not each labelled one by one; the categories above are a reading, not
  a count, except the 99 message-text mutants (found by pattern).
- The 4 modules were chosen because the pilots and the fuzzer exercise them most. Other modules have not been mutated.
