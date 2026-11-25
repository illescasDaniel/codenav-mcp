---
id: DOC-001
title: Tool reference
summary: Navigation, edits, safety rules and batch calls
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
| `outline` | What's in this file? (classes, methods, functions) |
| `callers` | Who actually calls this function? (call hierarchy, not imports) |
| `implementations` | Which classes structurally implement this `Protocol`? (type-checked) |
| `search_symbol` | Workspace symbol search by name (ranked, capped; optional `kind=` / `path=` filters; production code before tests; fuzzy-only hits summarised unless `fuzzy=true`) |
| `workspace` | Which directory is being navigated, and why |

Then use the position tools once you have a `path:line:col`:

| Tool | Answers |
|------|---------|
| `hover` | Type and docs at a position |
| `definition` | Go to definition (resolves through injected parameters) |
| `references` | All usages across the workspace |
| `diagnostics` | ty type-check diagnostics for one file |

`name` and `query` are accepted as aliases on the name-based tools
(`port_name` / `name` / `query` on `implementations`). A missing or wrong
parameter gets a short hint back instead of a validation error. Dotted names
may nest (`Outer.Inner.method`). `implementations` also takes `file_path` to
pick one port when the name exists in several files, and counts inherited
members and dataclass/`self.x` fields toward a port's required names.

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
| `move` | With `name`: move a module-level function or class to another file with the imports it needs; without it, move or rename the module `file_path`. Rewrites imports everywhere, drops imports only the moved code used, warns about import cycles |
| `quick_fix` | Apply ty's own fixes (missing imports) with correct placement; ambiguous fixes are listed instead of guessed |
| `verify_changes` | After editing with any tool: which type errors the working tree gained or lost since a git revision (default `HEAD`) |
| `apply_edit` / `undo_edit` | Write a previewed edit by id; revert an applied one |

Edit operations normally write when the edit adds no more than `max_new_errors`
(default 0) type errors in the checked files. Otherwise they return diagnostics and a
preview id. `apply=false` always previews; `apply_edit` can explicitly apply that
preview despite the diagnostic limit, and `undo_edit` restores an edit without a new
pre-write check. Forced deletion can bypass the error limit. Hint-level diagnostics
(unused, deprecated) are not counted.

Safety rules, enforced in code:

- Only `.py`/`.pyi` files inside the workspace are written; virtualenvs, `site-packages`,
  typeshed and symlinks that leave the workspace are refused.
- A preview remembers the exact bytes of every file; `apply_edit` refuses if any file changed
  since. Files are replaced atomically, and a failure part-way restores the ones already written.
- Applied edits can be undone with `undo_edit` while the server retains its in-memory journal, provided the files have not changed since.
- Output is never written if it is not valid Python.
- Edits touching more than 40 files need `allow_large=true`.
- `CODENAV_MCP_READ_ONLY=1` turns every write into a refusal (previews still work).

New files an edit would create are written to disk for the few moments of the type check
(ty finds modules through the file system) and removed again, even on errors.

Positions are **1-indexed**. `column` is a UTF-16 character offset (a leading
tab counts as one character).

Python only (`.py` / `.pyi`).

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
- A bad tool name or argument is reported in its own section with a hint; subsequent calls run only if `stop_on_error` permits it.
- `stop_on_error` (default: stop when the batch contains a write, continue when it only
  reads) skips the remaining calls after the first failure or write that was not applied.
- Not atomic: calls that ran stay applied. After an edit, prefer name-based calls over
  line/column positions computed earlier. Max 25 calls, no nesting, each result capped at
  20,000 characters.
