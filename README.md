# codenav-mcp

An MCP server that gives AI agents type-resolved Python navigation, backed by
[ty](https://github.com/astral-sh/ty)'s language server. Definitions,
references and call sites go through real type inference (imports,
dependency-injected parameters, dataclass fields), not text grep.

## Quick start

`ty` is installed with the package, so all you need is
[uv](https://docs.astral.sh/uv/) (or `pipx`):

```bash
uvx codenav-mcp
```

Register it with your MCP host. For Claude Code, from the project root:

```bash
claude mcp add codenav -- uvx codenav-mcp
```

Or add it to a project `.mcp.json` (Claude Code) or `.cursor/mcp.json` (Cursor):

```json
{
	"mcpServers": {
		"codenav": {
			"command": "uvx",
			"args": ["codenav-mcp"],
			"env": {
				"CODENAV_MCP_SOURCE_ROOT": "src"
			}
		}
	}
}
```

In Cursor, also set `"CODENAV_MCP_WORKSPACE": "${workspaceFolder}"`, because
Cursor may start MCP servers with your home directory as the working directory.

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

## Write tools: change code with a type check before every write

The navigation tools above are read-only. These change code, and share one rule:
**the edit is applied to in-memory copies first, ty re-checks the edited files and
the files importing them, and the response says which diagnostics the edit would add
or fix. Nothing is written until that is on the table.**

| Tool | Does |
|------|------|
| `rename_symbol` | Rename across the workspace (imports, aliases, keyword arguments, `parameter=` for a function's parameter). Also renames what ty's rename misses: overriding methods, `super().name()` calls, and Protocol members together with the classes that satisfy them (`linked=false` to opt out). Lists remaining mentions of the old name (untyped accesses, strings, comments, docs) |
| `change_signature` | Add / remove / reorder parameters and rewrite every call site (found through ty's references, so injected dependencies and typed attributes work). Overrides follow. `*args` calls and uses as a value are listed as manual work |
| `move_symbol` | Move a module-level function or class to another file with the imports it needs; rewrites `from old import name` everywhere, drops imports only it used, warns about import cycles |
| `move_module` | Move or rename a module file and update every import of it |
| `safe_delete` | Delete a function/method/class only if nothing uses it (users listed otherwise); removes now-dead imports of it |
| `replace_symbol` / `insert_symbol` | Replace or add a function, method or class by name: re-indented to the file's style, PEP 8 spacing, `imports=[...]` added, syntax-checked |
| `quick_fix` | Apply ty's own fixes (missing imports) with correct placement; ambiguous fixes are listed instead of guessed |
| `check_edit` | Dry-run any edit (whole file or `old_string`/`new_string`) and see the new errors, or apply it with `apply=true` |
| `verify_changes` | After editing with any tool: which type errors the working tree gained or lost since a git revision (default `HEAD`) |
| `apply_edit` / `undo_edit` | Write a previewed edit by id; revert an applied one |

Refactorings preview by default (`apply=false`) and return an id for `apply_edit`.
Passing `apply=true` writes only if the new errors stay within `max_new_errors` (default 0).
`replace_symbol`, `insert_symbol` and `quick_fix` write by default and report what they caused.

Safety rules, enforced in code:

- Only `.py`/`.pyi` files inside the workspace are written; virtualenvs, `site-packages`,
  typeshed and symlinks that leave the workspace are refused.
- A preview remembers the exact bytes of every file; `apply_edit` refuses if any file changed
  since. Files are replaced atomically, and a failure part-way restores the ones already written.
- Every applied edit is undoable (`undo_edit`) unless the files were changed after it.
- Output is never written if it is not valid Python.
- Edits touching more than 40 files need `allow_large=true`.
- `CODENAV_MCP_READ_ONLY=1` turns every write into a refusal (previews still work).

New files an edit would create are written to disk for the few moments of the type check
(ty finds modules through the file system) and removed again, even on errors.

Positions are **1-indexed**. `column` is a UTF-16 character offset (a leading
tab counts as one character).

Python only (`.py` / `.pyi`).

## Which `ty` runs

1. The project's own `.venv/bin/ty` (`.venv\Scripts\ty.exe` on Windows), so
   the ty version matches the project's pin and config.
2. The `ty` installed alongside codenav-mcp.
3. `ty` on `PATH`.
4. `uvx ty server` as a last resort.

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `CODENAV_MCP_WORKSPACE` | unset: follows the client's MCP roots when they name a worktree of the same git repository, else `CLAUDE_PROJECT_DIR`, else the working directory | Pins the project root (never overridden). See the `workspace` tool |
| `CODENAV_MCP_SOURCE_ROOT` | whole workspace | Directory scanned for `implementations` candidates and used to derive dotted import paths (e.g. `src`) |
| `CODENAV_MCP_READ_ONLY` | unset | `1`/`true`: write tools refuse to write (previews and checks still work) |
| `CODENAV_MCP_EXTRA_SOURCE_ROOTS` | none | Comma-separated directories (e.g. `tests`) that `implementations` also scans; matches (test doubles) are listed under a separate heading |

## Requirements

- Python ≥ 3.11
- Installed automatically: [`mcp`](https://pypi.org/project/mcp/),
  [`ty`](https://pypi.org/project/ty/),
  [`ruff`](https://pypi.org/project/ruff/) (sorts imports in files a move touched, only where they were already sorted),
  [`mcp-nav-shared`](https://pypi.org/project/mcp-nav-shared/)

## Related

- [`webnav-mcp`](https://pypi.org/project/webnav-mcp/): the same kind of
  navigation for JS/TS/HTML/CSS.
- Design notes (Protocol conformance probe, positioning, output formats):
  [docs/agent-tooling.md](https://github.com/illescasDaniel/SpaceMaker/blob/main/docs/agent-tooling.md).

## Development

Source: [github.com/illescasDaniel/codenav-mcp](https://github.com/illescasDaniel/codenav-mcp). From a
checkout: `uv sync --group dev`, then `uv run codenav-mcp`.

## License

MIT. See [LICENSE](https://github.com/illescasDaniel/codenav-mcp/blob/main/LICENSE).
