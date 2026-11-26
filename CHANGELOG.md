# Changelog

## Unreleased

### Fixed

- `change_signature` no longer writes a plan it knows is incomplete. A reorder that would change the evaluation order of effectful positional arguments (`f(args.query, args.path)`) now rewrites the call with keywords in the original order (`f(path=args.query, query=args.path)`) instead of leaving it as is; only calls that cannot take keywords (positional-only parameters, arguments captured by `*args`) stay manual. A plan with any call site left under `needs manual attention` (`*args` calls, uses as a value, import aliases, unrewritable calls) is held as a preview like a plan with new errors: `NOT applied: the plan is incomplete: ...`, and `apply_edit(id=...)` writes it anyway. Before, such plans were written when the type check found nothing, which a wrong argument order between two `str` parameters never does. `allow_large=true` also writes a held plan, as it does for `rename_symbol` and `move`.
- `move(keep_reexport=true)` no longer breaks the re-export it keeps: the old module's `__all__` keeps the entry (it used to be replaced by a whitespace-only line, so `from old import *` lost the name), and the re-export import joins the module's import block instead of replacing the definition mid-file (it keeps the definition's place only when the destination imports names back from the old module). An `__all__` entry alone no longer counts as a use, so without `keep_reexport` the entry leaves with the code, removing its whole line (never a whitespace-only line). A new destination file gets two blank lines after its imports. Excluded directories (`.venv/`, `.pytest_cache/`, caches, installed packages) are no longer reported as importers that could not be parsed or read; they are skipped silently.
- `references` lists every file whose hits are within `limit`; it used to stop at 25 files (`… and 5 more file(s)`) even with `limit=200` and fewer hits. `symbol_info`'s compact per-file references keep their 25-file summary, but the `… and N more file(s)` line now says `references(file_path="...", line=L, column=C, limit=<total>) lists every hit` (the exact position call).

- `rename_symbol(include_unlinked_attrs=true)` no longer renames attribute accesses the type checker resolves to another symbol: each candidate `.name` is looked up with ty first, and only an access with no definition (an untyped object, a `Mock`) or one that points at the renamed member itself is renamed. `connection.execute` on a typed `sqlite3.Connection`, or a same-named method of an unrelated class, stays and is counted in a separate note (`left N attribute access(es) alone: ... resolves them to another symbol`) instead of the "probably the renamed member" list. A lookup that fails or runs past 300 accesses leaves the access alone (also counted). The mention scan (the notes of every rename, move and delete) no longer looks into directories whose name starts with a dot (`.agents/`, `.cursor/`, `.github/`), so tooling files are neither listed nor rewritten.
- `edit_symbol(action="delete")` no longer holds a delete because of a bare-name or attribute use that ty resolves to a different definition (the other function with the same name in another module): those are dropped from the blockers and counted in a note (`N other use(s) of the name ... resolve to a different symbol and were ignored`). Uses with no definition, strings, and uses of the deleted symbol still hold the delete.
- `change_signature`: adding a parameter with a `value` next to effectful positional arguments (`f(root / "a", "q", names=False)` becomes `f(root / "a", "q", exact=False, names=False)`) is written; it used to be held as "changing effectful arguments could change evaluation order". The check now looks at what really matters: whether an argument the call already had is dropped or moved relative to another effectful one; an argument the change adds does not count. When a plan is held because of call sites that `keyword_only=true` on the added parameters would let it rewrite (a positional-only function, positions that would shift), the reply says so.
- `implementations` says which root it scanned and names `CODENAV_MCP_EXTRA_SOURCE_ROOTS` when a `tests/` directory outside it was left out (one line, only then), so test doubles are not silently missed.

### Added

- Argument aliases on every tool, direct and in `batch`: `symbol`, `symbol_name`, `qualified_name` for `name`; `file`, `filepath`, `path` for `file_path`; `col` for `column`; `line_number`, `lineno` for `line`; `old_str`, `new_str` for `old_string`, `new_string`. An alias applies only when the tool has no parameter of that name (`search_symbol` keeps its own `path`); an alias and its target with different values are refused. This replaces the 1.0.2 "no aliases" stance, for agents whose client defers tool schemas and guesses argument names.
- `references` also takes `name` (dotted `Class.method` ok, `query` as an alias, `file_path` as a filter) like `callers`. `file_path`, `line` and `column` became optional; giving neither a name nor a full position is refused with an example. Positional calls keep working.
- `rename_symbol(include_unlinked_attrs=true)` also renames attribute accesses after a dot that the type checker could not link (mocks, untyped objects) for a method or field, in the same plan (so `apply=false` previews them and the type check covers them). Default behaviour is unchanged; the note about those accesses now says how to include them (`undo_edit` then rerun when the rename was written).

### Improved

- Refusals for an unknown or missing argument end with the tool's example call (`Example: callers(name="create_user")`).
- `symbol_info`, `callers`, `references` by name and `implementations` answer an ambiguous name with a normal reply, not an error: `N symbols are named 'x'. Retry with one of:` and one line per candidate with its kind, `file:line` and the exact retry call. The write tools still refuse an ambiguous name (and write nothing), but the refusal now ends with the exact retry calls too: `Nothing was changed. Retry with one of (other arguments unchanged):` and `edit_symbol(action="delete", name="x", file_path="src/a.py")`, `rename_symbol(new_name="y", name="x", file_path=...)`, `change_signature(name="x", file_path=...)`, `move(to_file="...", name="x", file_path=...)`.
- The type-check result of every write says it is only that: `Type check: 0 new error(s), 0 fixed (21 file(s) checked, 0 unchanged) - tests and docs not checked`, and `After writing: no errors in the edited files (type check only; tests and docs not checked).` A green check says nothing about tests, generated docs or other repo invariants.
- `callers` of a `Protocol` member now says implementations are not included and points to `implementations`; the result itself is unchanged.
- The server instructions list the call shapes (`name=` tools and position tools), and the `edit` description says it is the built-in Edit plus a type check (one replacement per call; several edits go in one `batch`).

## [1.0.4] - 2026-11-26

### Improved

- `rename_symbol` notes now list leftover mentions that are attribute accesses (`mock.old`, untyped objects) first and almost in full (up to 50), under their own heading, because they are the likeliest real uses the type checker missed. Bare names, imports and `def`s (often a different symbol with the same name) keep the short list of 6. The attribute heading is softer when the renamed symbol is a module-level function or class. Edits are unchanged. `delete_symbol` shows the same split.

## [1.0.3] - 2026-11-26

### Fixed

- Shorten the package and MCP Registry descriptions to meet the registry schema limit.
- Preserve MCP context injection on Python 3.14 and label standard-library paths on Windows without exposing full paths.
- Correct cross-platform test fixtures and verify CI on Linux, macOS and Windows.

## 1.0.2

### Changed

- Tool replies are text-only: `structuredContent` is no longer emitted and tools no longer advertise an output schema. Agents and hosts read the same text as before; a programmatic client that parsed `structuredContent` should parse the text instead. A typed payload alongside the report is a candidate for a future minor version.

### Improved

- Refusals for an unknown argument suggest the closest real name (`unknown argument 'file' for outline; did you mean 'file_path'?; allowed: file_path, depth`), so an agent gets there on the first retry. The surface stays strict: no aliases were added.
- A `server.json` manifest (MCP Registry format) ships in the repository root, kept in sync with the package metadata and documented settings by a test.
- The README gained an Examples section with real tool output (navigation, references, a refused unsafe edit, a type-checked rename).

## 1.0.1

### Fixed

- A workspace with a huge number of Python files (for example a whole home directory, 700k+ files) no longer exhausts memory: the server counts Python files before starting the language server and refuses a tree over the cap (startup exit code 2 with a one-line message; a client-driven switch to such a tree is refused the same way).

### Added

- `CODENAV_MCP_MAX_FILES` (default 250,000) sets that cap. See [configuration](docs/configuration.md).

## 1.0.0

Release summary by area. Details and examples are in the [tool reference](docs/tools.md); settings are in [configuration](docs/configuration.md).

### Added

- Settings: `CODENAV_MCP_MAX_OUTPUT_CHARS` (output cap, default 30,000 characters), `CODENAV_MCP_EXCLUDE_DIRS` and `CODENAV_MCP_INCLUDE_DIRS` (directory names never scanned or written, adjusted), and `CODENAV_MCP_ALLOW_READONLY_FILES` (let write tools edit read-only Python files).
- Tool parameters: `references(limit=)` and `callers(limit=)` (cap the list; the first line is the exact count), `outline(depth=)` (top-level definitions only), and `move(allow_import_cycle=)`.
- The shared package gained the `mcp_nav_shared.output` module (output cap helpers).

### Output

- Every tool result is capped at the output budget, and `batch` shares one budget among its calls. Shortened results say so and name the calls.
- `references` and `callers` always print the exact count, and `search_symbol` says when it shows fuzzy matches only.

### Writes and safety

- Hard-linked files are refused, because writing them would change names outside the workspace.
- Read-only files are refused unless `CODENAV_MCP_ALLOW_READONLY_FILES` is set. Case-only name collisions are refused.
- Nested git checkouts, excluded directories and symlinks that leave the workspace are never written.
- Failed writes name the file and the reason, roll back what was written, and report `COULD NOT RESTORE` if rollback fails.
- Undo keeps the last 20 applied edits and removes the files and directories an edit created.
- `verify_changes` checks modified Python files and reports added ones as not checked; it refuses only when every changed Python file is new.

### Language server

- Previews no longer leak unsaved text into concurrent reads.
- Hung `uvx` and ty processes are stopped with their whole process group.
- Same-size, same-mtime rewrites are detected through ctime.
- A missing ty or uvx gives a clear message. The exit message no longer shows the command line or raw stderr.
- ty restarts are automatic, and only a restart caused by your own edit of a config file is reported.

### Text model

- Line endings follow Python and the language server: only LF, CRLF or a lone CR end a line. Form feed, NEL, U+2028 and U+2029 are ordinary characters.
- A UTF-8 byte-order mark is kept by every write tool and never counted as a column.
- The syntax gate uses the compiler's checks and names the file and Python version. NUL bytes and stray byte-order marks are refused with short messages.
- Text inserted into a CRLF file uses CRLF. Plan summaries list at most 60 files.

### Imports and moves

- Importer discovery no longer skips importers silently. Excluded directories, nested repositories and unreadable or unparsable files are reported by name.
- `move` holds back a move when an importer cannot be parsed or when more than 2000 importers exist, unless `apply=true` and `allow_large=true` are given.
- `move` refuses a module-level import cycle, and a function that assigns a module global.
- Side-effect imports stay in the source module. Relative imports keep their form. `__all__` entries move with the symbol.
- The most specific source root names the moved module. New imports go into the first import block.
- Imports merged into an existing `from` line are sorted case-insensitively, and existing comments are kept.

### Rename and signatures

- `rename_symbol` refuses dunder methods and overrides of base classes outside the workspace, and refuses a new name that is already bound.
- Rename updates a literal `__all__`, and renames `.pyi` stubs together with their `.py` files.
- `change_signature` finds constructor calls, and lists calls it cannot rewrite safely.
- `edit_symbol` deletes keep comments above the definition, and overloads act on the implementation.

### Navigation reads

- Paths outside the workspace are refused before the file is read.
- `definition` shows library locations as `<stdlib>/...` and `<site-packages>/...`, and lists `.pyi` stubs first.
- `symbol_info` matches names after NFKC normalisation, suggests close names, and resolves dotted module paths.
- `hover` explains a `def` or `class` keyword position, and `search_symbol` has a total order.
- `references` and `callers` exclude results outside the workspace and list production files before tests.

### Tool contract and errors

- Refusals and failed calls return `isError: true`. Previews and clean no-op answers do not.
- Unknown or mistyped arguments are refused by name, with the allowed names.
- A batch reports `batch: N/M ok, K failed`, and refuses a result reference such as `$1`.
- Calls before `initialize` are refused.

### Configuration

- The pinned workspace and `CODENAV_MCP_SOURCE_ROOT` are checked at startup.
- A `src/` layout is detected as the source root, and `workspace` reports it.
- Invalid `extra source root` entries are skipped with a note.
- A monorepo root without `pyproject.toml` is documented, with the workaround.

### Known limits

- After SIGKILL, during a preview or in a worker-thread write, an edit can be left partly applied, and preview files are left for manual inspection. See [known limits](docs/tools.md#known-limits).
- A rename of a symbol whose stub or alias twins live in a `.pyi` file or a sibling re-export package (attrs-style) rewrites the module and the package `__all__`, but re-exports in sibling packages can be left unrewritten; they are listed, not silently skipped.
- A pre-existing diagnostic that moves with the code can still count as a new error when `allow_import_cycle=true` is passed.
- A module that is moved out and then back leaves the previous destination file empty (named in the reply).
- Replies larger than about 1 MB hit the language-server timeout (~20 s) and come back as an error; the call can be retried with a `limit=`.
- Raw malformed input on stdin is dropped by the MCP SDK with no reply (by design, see development.md).
- Files using Python 3.13/3.14-only syntax are refused for writes (the bundled parser is older); run the server under a newer Python to edit them.
- A move that would create a module-level import cycle (including one that already exists through the destination) is refused unless `allow_import_cycle=true` is passed; cycles through function-level imports only get a note.
- Changes under excluded directories are not scanned, written or watched; the directories are named in the replies.
- `verify_changes` lists added Python files as not checked (no committed baseline) and refuses only when nothing can be compared.
