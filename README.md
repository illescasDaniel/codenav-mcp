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
| `CODENAV_MCP_EXTRA_SOURCE_ROOTS` | none | Comma-separated directories (e.g. `tests`) that `implementations` also scans; matches (test doubles) are listed under a separate heading |

## Requirements

- Python ≥ 3.11
- Installed automatically: [`mcp`](https://pypi.org/project/mcp/),
  [`ty`](https://pypi.org/project/ty/),
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
