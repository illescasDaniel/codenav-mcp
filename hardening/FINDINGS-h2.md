# FINDINGS-h2: differential checks against jedi and rope

Target: `/tmp/cnav-sandbox/srxy-t3` (srxy, 335 Python files, `src/` layout; its `pyproject.toml` makes ty skip
`scripts/**`, `packaging/**` and `examples/**`). codenav is this branch (v2 @ 5ead15e). Raw per-symbol results: the two 40-symbol runs are
committed in `hardening/results/`; the larger seeds (`*_n*_seed*.json`, git-ignored) are in `/tmp/cnav-analysis/v2/h2-results/`. How to run: `docs/hardening.md`.

## Runs

| run | symbols | agreement | notes | time |
| --- | --- | --- | --- | --- |
| `diff_references.py --symbols 40 --seed 0` | 40 | 40/40 symbols agree (100 %), 139 shared locations, 0 errors | all small symbols | 7.7 s of work (10 s wall) |
| `diff_rename.py --symbols 40 --seed 0`, certain sites | 40 | 39/40 agree (97.5 %); line overlap 99.1 % | 1 symbol compared by file counts (diff cut off) | 35 s |
| same, `sites=["all"]` against rope `docs=True` + all unsure | 40 | 37/40 (92.5 %); 90.8 % of codenav's lines are also rope's, rope has 237 lines codenav does not | rope's docs mode rewrites every comment and string spelling a common name, so the overlap rate means little | (same run) |
| `diff_references.py --symbols 400 --seed 1` (extra) | 400 | 376/387 compared agree (97.2 %), 1417 shared, 102 only codenav, 11 only jedi; 13 errors (11 not-found, 2 ambiguous) | | 44 s |
| `diff_rename.py --symbols 150 --seed 1` (extra) | 150 | certain: 115/146 agree (78.8 %), 4 errors; all: 123/146 (84.3 %) | all 33 codenav-only lines are legitimate sites rope misses (see below) | 118 s |

The 40-symbol runs are mostly tests and small helpers, so they agree almost completely; the larger seeds found
everything below. Rename is slower because rope rescans the project for every rename and codenav type-checks the plan.

## Findings (codenav or ty misses)

Each has a strict-xfail test in `tests/test_hardening_h2_findings.py`; the number matches the `reason`.

### #1 (medium): references silently omit calls in code ty narrows to `Never`

Input (`src/pkg/core.py` has `class Ctl` with `activity: str | None`, `start()`, `stop()`, `target()`):

```python
def check() -> None:
	c = Ctl()
	c.start()
	assert c.activity is not None
	c.stop()
	assert c.activity is None  # ty keeps the narrowing from two lines up, so everything below is Never
	c.target()
```

`lookup(name="Ctl.target", show=["references"])` gives `References (1)` (only the declaration); the call on the last
line is missing and nothing says so. At that position `lookup(file_path=..., line=..., column=...)` reports the type
`Never`. `rename` offers the call as an optional site (`+1 optional site`), so it is not lost there, but the
references list and its count are wrong. Seen in srxy: `InstallerController.i18nTr` (tests/gui/test_installer.py:194,195),
`SearchController.resetPreferences` (tests/gui/test_gui_recent_searches.py:310) and `SearchController._set_searching`
(tests/gui/test_gui_controller.py:587,677,711), all found by jedi only. Invariant broken: a runtime-reachable
`obj.method()` on a typed object is a reference to `method`. Test: `test_references_include_calls_after_ty_narrows_the_receiver_to_never`.
Classification: ty gap (flow narrowing is not invalidated by the intervening method call); codenav could at least
note receivers of type `Never`.

### #2 (low): `monkeypatch.setattr(obj, "name", ...)` strings are neither edited nor offered

`monkeypatch.setattr(core.Ctl, 'target', lambda self: 2)` names the method as a string. `rename(name="Ctl.target",
new_name="renamed", apply=false, sites=["all"])` edits the call site but not that string; the reply only counts it
("`target` also appears in 1 string (not listed ...)"), so an agent cannot opt in with a site id. After applying, the type
check reports 0 new errors and the test fails at runtime. Only dotted patch-target strings (`"pkg.mod.Class.old"`) are
handled. rope with `docs=True` does edit it. In srxy: `monkeypatch.setattr(store, "download_semantic_image_model", ...)`
(tests/unit/test_model_store.py:384). Test: `test_rename_all_sites_edits_setattr_style_name_strings`.

### #3 (low): a definition in a file the ty config excludes is "not found", and references omit its users silently

With `[tool.ty.src] exclude = ["scripts/**"]`, `lookup(name="helper", file_path="scripts/tool.py")` answers
`No symbol found matching 'helper'.` although the file defines it. In srxy this made 11 of 400 sampled symbols
(scripts/, examples/) errors with that text. `lookup(name="Ctl", show=["references"])` lists 4 references and does not
mention `scripts/tool.py:1,5` that use it (rename does note them: "also appears in 2 bare names ... scripts/tool.py:1").
jedi finds them (`cache_db_path`: scripts/bench_file_search.py:96,98; `MatchType`: examples/playground.py:19,91,92).
Expected, since the project excludes the files; the defect is the misleading wording and the silent omission. Test:
`test_lookup_in_a_file_the_ty_config_excludes_says_so`.

### #4 (low): rename preview diff shows unchanged lines as removed and added

Two edits a few lines apart in a file of 200+ lines (many repeated lines such as blank lines) produce a diff in which the
lines between them appear as `-`/`+` pairs although only two lines change; the `Files:` summary correctly says `+2 -2`.
Example: tests/gui/test_gui_flows.py in srxy, `rename(name="capture_qt_messages", ...)`: `Files` says `+2 -2`, the diff shows
five `-` and five `+` lines (435 to 439). Minimal repro in the test: a file with a local import, a call, and 40 repeated filler
functions gives 141 removed lines for 3 real edits. Cause: `mcp_nav_shared.edits.unified_diff` uses `difflib.unified_diff`,
whose `autojunk` heuristic (on from 200 lines) treats frequent lines as noise; `count_changed_lines` uses
`SequenceMatcher(autojunk=False)`. Harm: misleading review, and the 150-line cap on the diff cuts real edits sooner. The
fix belongs in the shared package (not edited here). Test: `test_rename_preview_diff_does_not_show_unchanged_lines_as_edited`.
The differential script discounts such pairs, so this does not distort its numbers.

## Hand classification of every disagreement seen

### References (seed 1, 400 symbols): 11 disagreements and 13 errors

| symbol | difference | verdict |
| --- | --- | --- |
| `cache_db_path` | only jedi: scripts/bench_file_search.py:96,98 | excluded by project ty config (#3) |
| `MatchType` | only jedi: examples/playground.py:19,91,92 | excluded by project ty config (#3) |
| `_AnchoredRule` | only codenav: ignore_rules.py:183 (`tuple[int, str, "_AnchoredRule \| None"]`) | jedi miss (string annotation); codenav correct |
| `online_server` (tests/unit/test_installer_online.py:276) | codenav 32, jedi 4 | codenav links pytest fixture parameters by name; jedi does not. Intended |
| `qapp` (test_gui_qml_load.py:25) | codenav 52, jedi 11 | same, fixture |
| `qapp` (test_gui_query_layout.py:24) | codenav 19, jedi 4 | same, fixture |
| `qapp` (test_qt_theme.py:19) | codenav 16, jedi 7 | same, fixture |
| `GuiHarness.apply_dialog_ok` | only codenav: test_gui_flows.py:59,64,282,315,351,413, test_gui_ignore.py:1053,1059 | `harness = load_main(...)` is annotated `-> GuiHarness`; jedi miss, codenav correct |
| `InstallerController.i18nTr` | only jedi: tests/gui/test_installer.py:194,195 | ty `Never` narrowing (#1) |
| `SearchController.resetPreferences` | only jedi: test_gui_recent_searches.py:310 | ty `Never` narrowing (#1) |
| `SearchController._set_searching` | only jedi: test_gui_controller.py:587,677,711 | ty `Never` narrowing (#1) |

Errors: 11 `not-found` for symbols in `scripts/` and `examples/` (`main`, `_header`, `make_badge`, `_count`, `_run`,
`_report`, `ChecksOptions`, `running_inside_project_venv`, `demo_nested_boolean`, `_kill_process_group`,
`_wait_after_interrupt`): #3. 2 `ambiguous` (`collect_files`, `reveal_path`): a module-level function and a method of the same
name in the same file; `name` plus `file_path` cannot tell them apart, the reply lists exact retries. Expected, not a bug.

### Rename (seed 0 and seed 1)

Seed 0, certain: `InstallerController.privacyAck`, codenav also edits `{controller.privacyAck}` inside an f-string
(tests/gui/test_installer_snapshots.py:32), only the expression, not the literal text: rope miss, codenav correct. Seed 0, all-sites:
`_ErrorModalHostApp.compose` (+9 same-named `compose` overrides in other Textual test apps) and `InstallerController.status` (3 test lines on
another class' `status`) are codenav's name-based uncertain sites, by design; `download_semantic_image_model` is #2.

Seed 1, certain mode, 33 codenav-only lines and 2 rope-only lines: 19 are `__all__` entries (`"name",`, which rope does not touch but are correct to
rename), 8 are dotted patch strings (`patch("pkg.mod.name")`, certain by design), 5 are real references rope misses
(function-local imports and uses: `capture_qt_messages` test_gui_flows.py:435,439, `export_app_screenshot`
test_display.py:8,41,54), 1 is the string annotation in `_AnchoredRule` (jedi/rope miss). The 2 rope-only lines are scripts/bench_file_search.py:96,98 (#3). Five
symbols were compared by file counts because the diff was cut at 150 lines (the four with count differences come from `__all__` entries and the
excluded `examples/playground.py`). 4 errors: 3 `not-found` (scripts/ and examples/, #3), 1 rope `RefactoringError` on an
`async def` method (`SrxyApp._run_search_with_queue`; rope's offset check, codenav is fine), recorded as an error, not as an empty result.

No codenav apply-path bug was found by these checks; the rename preview contents otherwise match rope everywhere rope
can see the site.
