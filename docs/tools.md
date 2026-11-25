---
id: DOC-001
title: Tool reference
summary: Navigation, edits, safety rules, batch calls, output limits and known limits
type: reference
status: current
updated: 2026-11-25
verified:
covers: [src/codenav_mcp/**]
related: []
supersedes:
superseded_by:
---

# Tool reference

Use this reference to choose navigation, editing and batch tools.

## Tools

**Start with the name-based tools:**

| Tool | Answers |
|------|---------|
| `symbol_info` | What is this? Header, hover, definition and references in one call |
| `outline` | What's in this file? (classes, methods, functions; `depth=1` lists top-level definitions only) |
| `callers` | Who actually calls this function? (call hierarchy, not imports; `limit=` caps the list) |
| `implementations` | Which classes structurally implement this `Protocol`? (type-checked) |
| `search_symbol` | Workspace symbol search by name (ranked, capped; optional `kind=` / `path=` filters; production code before tests; fuzzy-only hits summarised unless `fuzzy=true`) |
| `workspace` | Which directory is being navigated, and why |

Then use the position tools once you have a `path:line:col`:

| Tool | Answers |
|------|---------|
| `hover` | Type and docs at a position |
| `definition` | Go to definition (resolves through injected parameters) |
| `references` | All usages across the workspace (`limit=` caps the list; the first line is the exact total) |
| `diagnostics` | ty type-check diagnostics for one file |

`name` and `query` are accepted as aliases on the name-based tools
(`port_name` / `name` / `query` on `implementations`). A wrong or missing
argument is refused by name, with the allowed names (see
[Errors and arguments](#errors-and-arguments)). Dotted names
may nest (`Outer.Inner.method`). `implementations` also takes `file_path` to
pick one port when the name exists in several files, and counts inherited
members and dataclass/`self.x` fields toward a port's required names. Classes
defined only in a `.pyi` stub are listed too and marked `(stub)`. The result
states how many classes were type-verified, and lists the method-name matches
that could not be verified as the port, so a zero count is never reported as
verified.

### Position reads

- `hover`, `definition` and `symbol_info`: a path outside the workspace is refused before the file is read: `../x.py is outside the workspace; give a path inside it`.
- `hover` on a `def` or `class` keyword answers `No hover information at that position; put the cursor on the name, not the 'def' keyword.` When ty cannot infer a type, hover shows `Unknown` (for example `-> Unknown`). That is ty's own answer, not a codenav error.
- `definition` shows library locations as a label such as `<stdlib>/json/__init__.py:185:5` or `<site-packages>/pkg/mod.py:10:1`, never as an absolute path. When a `.py` file has a sibling `.pyi` stub, the stub is listed first and the implementation follows as `Also: <file>:<line>:<column> (implementation)`. A name imported from a module that does not resolve returns the import line with `Note: the import does not resolve.`
- `diagnostics` and `symbol_info` replace the workspace root in ty's output with `<workspace root>`, and other search paths outside the workspace with `<outside workspace>`.

### Name reads

- `symbol_info`: names match after Unicode NFKC normalisation, so `ﬁ` finds `fi`. A dotted module path such as `pkg.mod.func` resolves the function in that module first. A name with no match gets `Did you mean: <name>?` when the language server returns close names. A local variable or parameter gets a note that locals are only visible through hover and definition at their position. With `file_path`, the local in that file is named even when a module-level symbol has the same name elsewhere. Overloaded functions resolve to the implementation, not to an `@overload` stub. `name` and `query` are aliases; passing both with different values is refused with `give either name or query, not both`.
- `search_symbol`: `path=` is a workspace-relative prefix: `src/` matches `src/...`, and `p` also matches `plugins/...`. A value with `*`, `?` or `[` is a glob matched against the whole relative path (`src/**/*.py`; `*` also matches `/`). An absolute path inside the workspace is accepted and shown relative. A dotted query (`Cart.total`) returns a member only when it is inside the named class. Results outside the workspace are not shown (`N result(s) outside the workspace were not shown.`). When there are only looser fuzzy matches, the reply counts them; pass `fuzzy=true` to list them with a fuzzy marker.
- `search_symbol` order: exact name, case-insensitive exact, prefix, substring, other. Within a tier, production code comes before tests, then declarations before properties and fields, then path, line and column.

### List reads

- `references(file_path, line, column, limit=200)`: `limit` caps the hits listed and must be 1 or more. The first line is always the exact total, `N reference(s) in M file(s):`, also for 8 hits or fewer. When hits are cut, the last line is `… N more omitted; raise limit= to see them`. Order: production files before test files, then path, line and column, the same on every call. Overriding methods are not included; `rename_symbol` does rename them. Hits outside the workspace are not listed (see the note under `search_symbol`).
- `callers(..., limit=200)`: the same cap and count, as `N caller(s) in M file(s):`. A name that is only used as a value (passed or stored, never called) gets `<name> is used as a value only (not called); see references for those uses.` A method of a class that has subclasses ends with `Static callers only; overriding methods are not included.`
- `outline(file_path, depth=None)`: `depth` is 1 or more. `depth=1` lists only the top-level definitions; the default lists every level. A file with a syntax error starts with `Warning: <file> has a syntax error at line N; the outline may be incomplete.` A file that contains a NUL byte is refused, as `edit` refuses it.
- `diagnostics`: each line starts with the file's workspace-relative path: `path:L:C [severity code] message`.

## Write tools: preview and check changes

The navigation tools above are read-only. Edit operations simulate changes and compare
ty diagnostics for edited files and their importers before applying them. `apply=false`
keeps the result as a preview; `apply_edit` explicitly applies a saved preview, and
`undo_edit` restores an applied edit.

| Tool | Does |
|------|------|
| `edit` | Type-checked text edit of a Python file: `old_string`/`new_string` or a whole `new_text`. For anything that is not a whole definition |
| `edit_symbol` | `action="replace"` / `"insert"` / `"delete"` a function, method or class by name: re-indented to the file's style, PEP 8 spacing, `imports=[...]` added, syntax-checked. Delete refuses while anything uses the symbol and prunes now-dead imports |
| `rename_symbol` | Rename across the workspace (imports, aliases, keyword arguments, `parameter=` for a function's parameter). Also renames what ty's rename misses: overriding methods, `super().name()` calls, and Protocol members together with the classes that satisfy them (`linked=false` to opt out). Lists remaining mentions of the old name (untyped accesses, strings, comments, docs; other worktrees are skipped) |
| `change_signature` | Add / remove / reorder parameters and rewrite every call site (found through ty's references, so injected dependencies and typed attributes work). Overrides follow. `*args` calls and uses as a value are listed as manual work |
| `move` | With `name`: move a module-level function or class to another file with the imports it needs; without it, move or rename the module `file_path`. Rewrites imports everywhere, drops imports only the moved code used, and refuses an import cycle unless `allow_import_cycle=true` |
| `quick_fix` | Apply ty's own fixes (missing imports) with correct placement; ambiguous fixes are listed instead of guessed |
| `verify_changes` | After editing with any tool: which type errors the working tree gained or lost since a git revision (default `HEAD`) |
| `apply_edit` / `undo_edit` | Write a previewed edit by id; revert an applied one |

Edit operations normally write when the edit adds no more than `max_new_errors`
(default 0) type errors in the checked files. `max_new_errors` must be 0 or more, or
`null` for no limit. Otherwise they return diagnostics and a preview id. `apply=false`
always previews; `apply_edit` can explicitly apply that preview despite the diagnostic
limit, and `undo_edit` restores an edit without a new pre-write check. Forced deletion
can bypass the error limit. Hint-level diagnostics (unused, deprecated) are not counted.
Files whose diagnostics could not be checked are reported as unchecked, and the default
error gate refuses an incomplete check. With `include_dependents=false`, dependent files
are not checked. An edit that leaves its file unchanged is not written or added to the
undo history.

Deleting a Python module, including moving or renaming a module, cannot be simulated
accurately: the language server still sees the old module during preview. These plans
are marked unchecked and the default gate refuses to apply them. Review the preview and
its importers, then apply it with `apply_edit` or rerun with `max_new_errors=null` if you
accept an incomplete diagnostic check. Check the importers after writing.

### Per-tool details

- `edit`: the arguments are exclusive: give either `new_text` (the whole file) or `old_string` with `new_string` (a replacement), not both. `old_string` and `new_string` go together, and `old_string` must not be empty. `old_string` must match once unless `replace_all=true`; the refusal lists the matching line numbers (first 10). A replacement reports how many it made (`Replaced N occurrence(s).`). When `old_string` is not found, the refusal names a CRLF/LF or tab/space mismatch if that is the cause. `new_text` and `new_string` are limited to 1,000,000 characters each; split a larger one.
- `edit_symbol`: insertion refuses a name already bound in its destination scope; create a missing file with `edit(file_path=..., new_text=...)` first. Deletion keeps comments directly above the definition and prunes imports made unused by the deletion when `prune_imports` is enabled. A deletion held back by unlinked references can be confirmed with `apply_edit` or rerun with `force=true`. An `imports` entry already in the file is not duplicated, and `TYPE_CHECKING` imports stay under their `if TYPE_CHECKING:` block.
- `quick_fix`: `choice` accepts a 1-based number or a title substring. Restored imports are placed in the import block rather than their original line. Files with syntax errors must be fixed first; line numbers start at 1; suppression fixes require `allow_suppress=true`. It sorts the import block of each file it changes; if the sort cannot run, the reply names the file and the reason.
- `change_signature`: constructor calls are found when targeting either a class or its `__init__` method. Add entries accept `name`, `default`, `value`, `annotation`, `keyword_only`, and `position`. Calls through imported aliases that cannot be resolved are listed for manual attention, and so are calls that contain comments or effectful arguments whose evaluation order could change. Decorated definitions and docstrings that mention removed parameters produce notes.
- `rename_symbol`: refuses before it edits anything when the target is an external contract: a special method (a dunder such as `__eq__`, which Python calls by protocol), or an override of a method of a base class defined outside the workspace. Renaming the base method is the fix only if you own it. A new name that collides with an existing one in the target module, or in a module that imports the target, is refused, and names are compared after Unicode normalisation (NFKC), so `ﬁx` and `fix` count as the same name. A function's or class's `.pyi` stub is renamed too, and the preview lists both files. A literal module-level `__all__` that exports the old name is updated, in the defining module and in any re-exporting `__init__.py` the rename touches. The string mentions list still reports other occurrences of the old name in strings, comments and docs; they are not changed. A new name may shadow a builtin (for example `list` or `id`); the rename is allowed and nothing warns. With `file_path`, the name must be declared in that file or its `.pyi` twin, and a file outside the workspace is refused. When `line` and `column` are also given, they must point at the same symbol as `name`.
- `move`: lists the importers it cannot check or rewrite in a report, as `Importers not updated (...): <files>`. Importers that cannot be parsed, and any beyond the 2000-importer scan limit, hold the move back: `NOT applied: importers that cannot be checked would be left broken (...)`. Fix or exclude those importers, or rerun with `apply=true` and `allow_large=true` to move anyway. A module-level import cycle is refused: `moving <x> to <dest> creates an import cycle (<a> -> <b> -> <a>); pass allow_import_cycle=true to move anyway`. A cycle that only goes through function-level imports is reported as a note instead. A function that assigns a module global is refused, because moving it would raise `NameError`; reading a global is not refused. A moved symbol's entry in a literal `__all__` moves with it. A side-effect import (one whose names the moved code does not use, such as `import json  # registers`) stays in the source module, and the answer says `kept in <file>: side-effect import (...); execution order unchanged`. A relative import in an importer keeps its relative form when the moved module stays in the same package. Importers that use `from <module> import *` are listed, not rewritten. A nested function or method cannot be moved: `nested functions cannot be moved; move the enclosing top-level function or class`. A `def` inside `if`, `try` or `with` is refused, and so is a destination that is not a `.py` file (`the destination must be a .py file, got <path>`). A package's `__init__.py` cannot be moved. When several source roots contain the file, the most specific (deepest) root decides the new dotted name. A destination folder without `__init__.py` is used as a namespace package, and the moved module imports under its dotted name. A destination that did not exist is reported as `<dest> was created by this move`. A source left empty is reported as `<file> is empty after the move; delete it with trash if you no longer need it`; it is not deleted.
- `verify_changes` is read-only. If Python files were added since the revision, it refuses instead of guessing: `Cannot verify changes: the historical comparison cannot hide added Python files (...); inspect the changes manually.` It compares Python files only, so when none differ it says `No Python files differ from HEAD.`, not that the whole working tree is clean. A changed `pyproject.toml`, `ty.toml` or `.ty.toml` changes the rules for every file: the reply then says `Config files changed: ...`, checks the whole project and lists every current diagnostic, because the old configuration cannot be re-checked.

### Imports (`imports=[...]`)

- New imports go into the first import block, after the shebang, encoding cookie, module docstring and future imports. That places them above any `sys.path` change in the file; the tool does not move them below path setup.
- Merged from-import names are sorted case-insensitively, and existing comments are kept.
- `imports=["from pathlib import *"]` is written verbatim, with no warning, and changes the module's names.
- Names used only in type comments (`# type: List[int]`) are not analysed when imports are carried along, so `move` can drop such an import.

### Line endings and new files

- Lines end at `\n`, `\r\n` or a lone `\r`, as in Python and the language server. Form feed (U+000C), NEL (U+0085), U+2028 and U+2029 are ordinary characters, so line numbers stay correct in files that contain them.
- Text inserted into a file (symbol inserts, replaced definitions, moved code) uses the file's own dominant line ending. A block never mixes CRLF and LF.
- New files an edit would create are written to disk for the few moments of the type check (ty finds modules through the file system) and removed again afterwards. If removing one fails, the file is kept and its workspace-relative path is reported.
- New files take the mode the process umask gives them (for example `0600` under `umask 077`). Existing files keep their mode. A newly created destination for a symbol move preserves the source file mode.

### Undo

- Applied edits can be undone with `undo_edit` while the server keeps its in-memory journal, provided the files have not changed since. The journal keeps the last 20 applied edits, and the preview store keeps the last 20 previews.
- `undo_edit` with no id reverts the latest edit. With an id that is not in the history it says `edit <id> is not in the undo history (it was never applied, already undone, or older than the last 20)`. An edit that a newer applied edit still depends on is refused until that newer edit is undone.
- An applied edit keeps the id of its preview, so `undo_edit(id)` works with the id that `apply_edit` printed.
- Undo also removes the files an edit created, together with the directories it created.

Positions are **1-indexed**. `column` is a UTF-16 character offset (a leading
tab counts as one character). Python only (`.py` / `.pyi`).

### Safety rules

Enforced in code:

- Only `.py`/`.pyi` files inside the workspace are written. Virtualenvs, `site-packages`, typeshed, symlinks that leave the workspace, and nested git checkouts (a folder with its own `.git`) are refused, because the write tools do not touch other repositories. A path outside the workspace is refused with its workspace-relative name, never an absolute path.
- Files with more than one hard link are refused, because writing them would also change their other names, which may be outside the workspace. Check all the linked files and edit them manually.
- Read-only (0444) files are refused unless `CODENAV_MCP_ALLOW_READONLY_FILES` is set (see [configuration](configuration.md#environment)); their mode is kept.
- Creating a file whose name differs only by letter case from an existing file in the same directory is refused (`<file> differs only in letter case from existing <other>; pick another name`).
- Output is never written if it is not valid Python. The syntax gate also refuses what only the compiler rejects, such as a `from __future__` import after other code, or `return`, `yield` or `await` outside a function. A text that does not parse is refused as `<file>: not written: it does not parse with this server's Python <version> parser (line N: <message>)`, followed by what to do. The server checks code with the Python it runs under, so syntax newer than that (for example PEP 696 `class C[T = int]:` from Python 3.13, or `except A, B:` and `t"..."` strings from Python 3.14) cannot be checked and is refused. See [Known limits](#known-limits).
- A text containing a NUL byte is refused as `<file>: contains a NUL byte; not a text file`.
- A UTF-8 byte-order mark at the start of a file is kept by every write tool and is never counted as a column. A leading U+FEFF in `new_text` is dropped; a U+FEFF elsewhere that the parser rejects is refused.
- A preview remembers the exact bytes of every file, and `apply_edit` refuses if any file changed since. Files are replaced atomically. A failure part-way restores the files already written and reports the file and the reason (for example `could not write b.py: Permission denied; nothing was changed`). If the rollback itself fails, the message says `COULD NOT RESTORE: <file>` and names the file.
- If SIGTERM or SIGINT arrives during a write or an undo, the server tries to roll back and reports `interrupted; nothing was changed`. This is best effort; see [Known limits](#known-limits).
- Edits touching more than 40 files are refused unless `allow_large=true` is given: `preview it with apply=false, then pass allow_large=true to apply it`. Nothing is written. A plan that touches many files lists the first 60 and ends with `... and N more files`.
- `CODENAV_MCP_READ_ONLY=1` turns every write into a refusal (previews still work).
- When a name has several definitions in one scope (`@overload` stubs plus the implementation), `replace`, `delete` and `insert` act on the implementation, the definition without `@overload`. If every definition is an overload, the first one is used.
## Several calls in one request: `batch`

Reading three files or changing five symbols no longer costs one round-trip each.
`batch` takes a list of calls for any tool above (reads and writes mixed):

```json
{"calls": [
  {"tool": "outline", "args": {"file_path": "src/pkg/core.py"}},
  {"tool": "symbol_info", "args": {"name": "Cart.total"}},
  {"tool": "edit", "args": {"file_path": "src/pkg/core.py", "old_string": "x = 1", "new_string": "x = 2"}},
  {"tool": "edit_symbol", "args": {"action": "delete", "name": "legacy_parse"}}
]}
```

- Calls run **in order**, each through the real tool, so its usual preview, check and
  apply behavior still applies; later calls see what earlier ones wrote.
- One numbered section per call, then a summary line (`batch: 3/4 ok, 1 failed`).
- A bad tool name or argument is reported in its own section with a hint; later calls run only if `stop_on_error` permits it.
- `stop_on_error` takes `true` or `false`; anything else is refused. When it is not given, the batch stops at the first failure if it contains a write call, and runs every call if it only reads. Stopping skips the remaining calls after the first failure or write that was not applied.
- Not atomic: calls that ran stay applied. After an edit, prefer name-based calls over
  line/column positions computed earlier.
- A call cannot refer to an earlier call's result. A value that is only a result reference (`"$1"`) is refused: `result references ($1) are not supported in v1; make the calls independent or run them one by one`. Text that merely contains `$5` (for example in `new_string`) is ordinary text and runs.
- The tool is annotated as possibly destructive because a batch may contain write calls; a batch made only of read tools writes nothing, so it is safe to use for reads.
- Limits: 25 calls at most, no nesting, and each call's output is capped at 20,000 characters. The whole batch also shares the output budget (see [Output limit](#output-limit)).

## Output limit

- Every tool result is capped at `CODENAV_MCP_MAX_OUTPUT_CHARS` characters (default 30,000, about 8,000 tokens; values below 2,000 are raised to 2,000). A result over the cap is cut at a line boundary and ends with `... N more lines (M characters) omitted. Narrow with path=, kind=, limit= or a more specific name.` A result under the cap is unchanged.
- An invalid value falls back to the default, and the server says so once in a notice.
- In `batch`, the calls share the budget. When their outputs together exceed it, each call gets an equal share (at least 400 characters), and the summary names the shortened calls: `Output limit: call(s) #2, #5 were shortened. Run them alone or narrow them (path=, limit=).`
- Write tools put their status lines (`Applied as edit`, `NOT applied:`, `After writing:`) at the top of their text, so a cut drops only the tail (the diff or the file list).
- To see more, narrow the request with `path=`, `kind=`, `limit=`, `depth=`, or a more specific name.

## Errors and arguments

- A refusal or a failed call comes back with `isError: true` and its text, for example `File not found: nope.py (relative paths resolve against the workspace root).` Previews (`Preview only, nothing written.`) and clean no-op answers (`No changes`, `No diagnostics to fix`) are normal answers with `isError: false`.
- A batch is an error when a failure stopped it, or when every call failed. A batch with some failed calls and no stop is a normal answer, and its last line says `batch: N/M ok, K failed`.
- Paths in messages are relative to the workspace. No absolute path is printed.
- An argument the tool does not take is refused by name, with the allowed names: `unknown argument 'colour' for outline; allowed: file_path`. A wrong type gets a short hint instead of a validation dump: `hover: line must be a whole number (got 'x')`. A missing one names the parameters: `hover: missing required 'column'. Parameters: hover(file_path, line, column).` The same wording applies to direct calls and to calls inside `batch`.
- The name-based tools (`symbol_info`, `callers`, `rename_symbol` and the others) refuse a blank, dot-only, NUL-containing or otherwise non-identifier name up front: `name must be a non-empty identifier or dotted path`. Names such as `Cart.` and `a b` are also rejected with a validation message. For PEP 695 type parameters, a specific refusal is available only when the server Python can parse the file (Python 3.12 or later for that syntax); otherwise the normal symbol-not-found message may appear.
- A call sent before `initialize` is refused with `the server is not initialized yet; send initialize first`. Calls sent after `initialize` but before `notifications/initialized` are accepted.

## Directories never scanned or written

- The built-in list is `node_modules`, `.git`, `vendor`, `dist`, `build`, `__pycache__`, `.venv`, `venv`, `.venv-bare`, `env`, `.env`, `site-packages`, `.tox`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `.pytest-testmon` and `.eggs`. `CODENAV_MCP_EXCLUDE_DIRS` adds names to it, and `CODENAV_MCP_INCLUDE_DIRS` removes names from it. Importer discovery, search scans and the write guard all use the same list.
- Importers inside excluded directories or nested git repositories (a directory below the workspace with its own `.git`) are not updated. `move` and `rename_symbol` list them, as `Importers not updated (excluded directories): <dir> ...` or `Importers not updated (nested git repositories, treated as other projects): ...`. Edits list importers in excluded directories as `Importers not checked (excluded directories): ...` rather than passing silently.
- Pass `CODENAV_MCP_INCLUDE_DIRS=<name>` (for example `build`) to include a directory. Refusals for excluded directories name this variable.
- VCS folders (`.git/`, `.hg/`, `.svn/`, `.bzr/`) are never listed as unparsable or unreadable.

## Troubleshooting

- If the language server (ty) dies, the next call starts a new one and the answer is correct. No notice is shown, except when your own edit of `pyproject.toml` or `ty.toml` caused the restart. A server that crashes reports `the language server exited unexpectedly (exit code N); the next call restarts it`; its command line and stderr go to the server log only.
- If the server cannot start ty at all, the message is ``could not start the language server (command not found: uvx); install ty (for example `pip install ty`) or make sure uv/uvx is on PATH``.

## Known limits

- Writes are not fully transactional. A SIGKILL, a write from a worker thread, or a signal that arrives after the final check can leave an edit partly applied, or leave a temporary file. Temporary files are not swept automatically, because their names do not prove that codenav created them. Inspect them and remove them yourself with `trash`. After a SIGKILL during a preview, the next start reports the leftover path of a new file once and leaves the file for manual inspection; nothing is deleted automatically. Cleanup of leftover preview files also has a known race between its check and its delete, which can remove content that is byte-identical to the preview.
- Files with more than one hard link are refused (see [Safety rules](#safety-rules)).
- `verify_changes` refuses when Python files were added since the revision.
- Results are capped at the output budget (see [Output limit](#output-limit)).
- Change detection uses filesystem ctime. A rewrite with the same size and the same mtime is noticed, but on filesystems with coarse or unsuitable ctime a change can still be missed.
- Python only (`.py` / `.pyi`).
- Newer Python syntax cannot be checked by the server. A file that uses syntax from a newer Python than the one the server runs under (3.13 or 3.14 constructs) is refused for writes, not checked and not written. Run the server under a newer Python to edit such a file, or edit it with another tool.
- Raw malformed input on stdin (a JSON array, a number or an object that is not a request, or a lone surrogate inside a JSON string) is handled by the MCP SDK before any codenav code runs. The SDK drops it without a reply, and codenav has no hook to send its own error text for it. Valid requests around it are still answered. This is by design; see [Dropped input](development.md#dropped-input).
