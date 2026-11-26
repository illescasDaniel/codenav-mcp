# codenav-mcp v2 design (working spec for the v2 branch)

Goal: an agent should reach for codenav the way it reaches for Edit/Read: few tools, name-first,
small outputs, nothing it has to "learn". Evidence behind every decision: /tmp/cnav-analysis/
(report-codex.md, report-opencode.md, analysis.md) and the srxy A/B.

## Principles
1. **Name-first.** Every tool takes `name` (`func`, `Class.method`, `module.func`, optional `file_path` to narrow).
   Position (`file_path`+`line`+`column`) stays as an optional alternative, never the primary path.
2. **One answer per question.** A reply ends with at most one or two "next:" calls, exact and copy-pastable.
3. **Small by default, paginated.** Every list has `limit` (default small) and `offset`; a cut list ends with
   `next: <exact call with offset=...>`. No silent caps.
4. **Do what grep cannot; don't compete with Read/Edit.** No str_replace clone. The value is types:
   linked references, Protocol implementations, safe multi-file refactors.
5. **Honest gate.** Writes are type-checked; every result says "type check only; tests and docs not checked".
   Plans that cannot update every site are never written silently.
6. **Certain vs. uncertain sites.** Refactor plans write the certain sites; uncertain ones (mock attributes,
   strings, comments) are listed with ids the agent can opt in per site. No blanket flags.
7. **State survives restarts.** Preview/undo ids persist on disk per workspace.
8. **Discovery budget:** server `instructions` + all tool descriptions + schemas <= 9,000 chars
   (today ~29,800). Enforced by a test.

## v2 tool surface (12 tools, down from 20 + batch)

| v2 tool | replaces | kind |
|---|---|---|
| `lookup` | symbol_info, hover, definition, references, callers, implementations | read |
| `search` | search_symbol (+ new text/string/comment mention search) | read |
| `outline` | outline | read |
| `check` | diagnostics, verify_changes, workspace | read |
| `unused` | NEW: dead code | read |
| `impact` | NEW: what breaks if X changes | read |
| `rename` | rename_symbol | write |
| `change_signature` | change_signature | write |
| `move` | move | write |
| `edit_symbol` | edit_symbol (+ edit: multi-replacement mode) | write |
| `fix` | quick_fix | write |
| `edits` | apply_edit, undo_edit (+ list) | write |

`batch` is removed as a tool (it existed to save round-trips; `edit_symbol` multi mode and `lookup` sections
cover the real cases). If keeping it is cheap, keep it internal for tests only.

### lookup
```
lookup(name: str | None = None, file_path: str | None = None, line: int | None = None, column: int | None = None,
       show: list[str] | None = None, limit: int = 20, offset: int = 0)
```
- `show` sections: `"signature"` (always), `"doc"`, `"source"` (the full body), `"references"`, `"callers"`,
  `"implementations"` (Protocol/ABC -> classes; method -> implementing methods), `"subclasses"`, `"overrides"`,
  `"mentions"` (unlinked attr accesses, strings incl. exact dotted patch targets, comments).
  Default `show`: signature + doc + location + one-line COUNTS of references/callers/implementations, each with
  the exact `next:` call to expand it. (A count line costs ~1 line; the agent decides.)
- Ambiguous name -> normal reply listing candidates with exact retry calls (reuse ambiguity.py).
- Static callers of a Protocol member: the callers section includes calls through the Protocol AND, labeled
  separately, calls to its implementations (this fixes the "callers misses implementations" gap without a flag).
- Pagination per section with `limit`/`offset`; when several sections are shown, limit applies to each.
- Output format: compact, one item per line `path:line  context`, paths relative to the workspace.

### search
```
search(query: str, kind: str | None = None, path: str | None = None, in_: str = "symbols", limit: int = 30, offset: int = 0)
```
`in_` = `"symbols"` (workspace symbol search, dotted `Class.method` ok) | `"mentions"` (a NAME in strings,
comments, patch targets, non-Python text files; grouped by kind). Param is spelled `scope` if `in_` is awkward.

### check
```
check(file_path: str | None = None, since: str | None = None, limit: int = 30)
```
- `file_path` -> diagnostics of that file. `since="HEAD"` -> verify_changes behaviour. Neither -> workspace
  summary (ty version, roots, python, error count) + the errors (old `workspace` output folds in here).

### unused
```
unused(path: str | None = None, kind: str | None = None, include_private: bool = True, limit: int = 30, offset: int = 0)
```
Top-level functions, classes, methods, module constants with zero linked references outside their own
definition. Must account for: `__all__` exports (report as "exported, unused inside the project"), dunder
methods, Protocol members and their implementations, overrides of library classes, decorators that register
(`@app.route`, `@pytest.fixture`, `@mcp.tool`, entry points in pyproject `[project.scripts]`), names used in
strings (getattr/patch targets: report as "only referenced from strings"), tests. Each item: confidence
(`unused` | `only in tests` | `only in strings` | `exported`). Ground truth to test on srxy-t3:
`content_location_kind` (application/use_cases/search_files.py:172) and cli.py `normalize_max_file_size` are unused.

### impact
```
impact(name: str, file_path: str | None = None, change: str = "any", depth: int = 1, limit: int = 30)
```
"What breaks if I change X?" `change` in `any` | `signature` | `rename` | `delete` | `type`. Reply groups:
direct call sites (with how they pass arguments: positional/keyword/*args), references used as values,
implementations/overrides that must change together, subclasses, importers/re-exports (`__all__`),
string/patch-target mentions, tests that reference it (by test file), and with depth>1 transitive callers.
Ends with the matching write call (`change_signature(...)`, `rename(...)`, `edit_symbol(action="delete", ...)`)
and its preview flag.

### rename
```
rename(name: str, new_name: str, file_path: str | None = None, apply: bool = True, sites: list[str] | None = None)
```
- Certain sites (linked references, implementations/overrides, exact dotted patch-target strings
  `"pkg.mod.Class.old"` naming the renamed symbol, keyword arguments of calls that ty links to the symbol)
  are written.
- Uncertain sites get ids `u1..uN` in the reply: unlinked attribute accesses on Mock/untyped receivers
  (ty-checked as in 1.x round 3: receivers resolving to another symbol are NOT candidates), `Mock(old=...)`
  keyword args, same-named defs in test fakes, comments, docstrings. `sites=["u1","u3"]` or `sites=["all"]`
  on a rerun or on `edits(action="apply", id=..., sites=[...])` adds them.
- Removes `include_unlinked_attrs`.

### change_signature
Same as 1.x (add/remove/reorder, keyword rewrite, holds incomplete plans, keyword_only hint). Add `sites=`
for uncertain sites the same way. Default `add` insertion: if the new parameter has a default, append it
keyword-only unless `position` is given (never forces positional rewrites by default).

### edit_symbol
```
edit_symbol(name: str | None = None, action: str = "replace", source: str | None = None, file_path: str | None = None,
            edits: list[dict] | None = None, apply: bool = True, force: bool = False)
```
- actions: `replace` (whole def/class by name), `replace_body`, `delete`, `insert_after`, `insert_before`.
- `edits=[{"file_path","old_string","new_string"}, {"name","action","source"}, ...]` applies several edits
  atomically as ONE plan with one type check and one undo id (this replaces `edit` and `batch` writes).
- Delete keeps the 1.x round-3 behaviour (ignores uses that resolve to another symbol).

### edits
```
edits(action: str = "list", id: str | None = None, sites: list[str] | None = None, allow_large: bool = False)
```
`list` (pending previews + applied history, newest first), `apply`, `undo` (id or last), `show` (diff of a plan).
Persistent: stored under `$XDG_STATE_HOME/codenav-mcp/<hash of workspace>/` (fallback ~/.local/state),
plans keep file content hashes; applying a stale plan refuses with "file changed since preview" and the
exact rerun call. Keep the last 50 entries. mcp-nav-shared's PlanStore/EditJournal are not editable:
wrap or subclass them inside codenav.

### fix
`quick_fix` renamed; same behaviour.

## Output conventions (all tools)
- First line: the answer in one sentence ("3 callers of FileSearchPort.execute in 3 files").
- Then items, one per line. Then `Notes:` (only if any). Then `next:` (only if useful).
- Paths workspace-relative; no ANSI; no source excerpts unless `show=["source"]` or a diff.
- Errors: what was wrong + the exact corrected call (`Example:` line), never a stack trace.
- Argument aliases from 1.x stay (symbol/file/col/old_str ...), plus v1 tool names are NOT kept.

## Migration
- Version 2.0.0. CHANGELOG has a "Migrating from 1.x" table (old tool/arg -> new call).
- README and docs/tools.md rewritten for 12 tools.
- Existing tests: port the behaviour tests to the new tools; delete tests that only pinned v1 shapes.

## Work split (each workstream on its own worktree branch off `v2`, merged by the orchestrator)
- **WS1 read core** (`lookup`, `search`, pagination, output conventions; folds symbol_info/hover/definition/
  references/callers/implementations). Registers new tools; does NOT delete old ones yet.
- **WS2 analysis** (`unused`, `impact`) in new modules `unused.py`, `impact.py`; registers new tools.
- **WS3 edit state** (`edits` tool, persistence, staleness, `edit_symbol(edits=[...])` atomic multi-edit).
- **WS4 refactor sites** (`rename` with certain/uncertain sites + exact patch-target strings + `sites=`;
  `change_signature` default keyword-only append and `sites=`). Uses a `Plan.optional_sites` contract:
  a list of `OptionalSite(id, path, line, kind, text, edit)`; WS3 persists it as part of a plan.
- **WS5 consolidation** (after 1-4 merge): `check`, `fix`, remove v1 tools and `batch`, instructions under budget
  with a test, README/docs/CHANGELOG/migration, version 2.0.0.
- **WS6 evaluation**: Codex + OpenCode testers on srxy sandboxes; fair A/B rerun (same built-ins for both agents,
  grep-hard tasks).
