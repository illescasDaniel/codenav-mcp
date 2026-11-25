---
id: DOC-002
title: Configuration
summary: MCP host setup, workspace selection, environment variables and ty selection
type: reference
status: current
updated: 2026-11-26
verified:
covers: [src/codenav_mcp/server.py, src/codenav_mcp/ty_command.py, src/codenav_mcp/writes.py, src/codenav_mcp/deps.py, pyproject.toml]
related: []
supersedes:
superseded_by:
---

# Configuration

Configure your MCP host and choose the workspace and source roots.

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

## Which `ty` runs

1. The project's own `.venv/bin/ty` (`.venv\Scripts\ty.exe` on Windows), so
   the ty version matches the project's pin and config.
2. The `ty` installed alongside codenav-mcp.
3. `ty` on `PATH`.
4. `uvx ty server` as a last resort.

If `ty.toml` (or `.ty.toml`), or otherwise `pyproject.toml`, cannot be parsed as TOML, `workspace` shows `Note: <file> could not be parsed (<reason>); ty runs with its defaults.` ty itself ignores the broken file.

A workspace folder without its own `pyproject.toml` gets a warning from `workspace` when its parent folder or its sub-folders have one. ty resolves imports from the folder it runs in, so a monorepo root without a `pyproject.toml` gets cross-package navigation errors. Set `CODENAV_MCP_WORKSPACE` to the package folder (one server per package), or to a root that has its own `pyproject.toml`.

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `CODENAV_MCP_WORKSPACE` | unset: follows the client's MCP roots when they name a worktree of the same git repository, else `CLAUDE_PROJECT_DIR`, else the working directory | Pins the project root (never overridden). See the `workspace` tool |
| `CODENAV_MCP_SOURCE_ROOT` | whole workspace | Directory scanned for `implementations` candidates and used to derive dotted import paths (e.g. `src`) |
| `CODENAV_MCP_READ_ONLY` | unset | `1`/`true`/`yes`/`on` (any case): write tools refuse to write (previews and checks still work). Any other value, such as `maybe`, leaves writes enabled |
| `CODENAV_MCP_EXTRA_SOURCE_ROOTS` | none | Comma-separated directories (e.g. `tests`) that `implementations` also scans; matches (test doubles) are listed under a separate heading |
| `CODENAV_MCP_MAX_OUTPUT_CHARS` | `30000` | Maximum characters of one tool result (about 8,000 tokens). Longer results end with `... N more lines (M characters) omitted. Narrow with path=, kind=, limit= or a more specific name.` Values below 2000 are raised to 2000; an invalid value uses the default and the server says so once. See [output limit](tools.md#output-limit) |
| `CODENAV_MCP_ALLOW_READONLY_FILES` | unset | `1`/`true`/`yes`/`on`: write tools may overwrite read-only (0444) Python files and keep their mode. Otherwise such a file is refused with `<file> is read-only; make it writable or set CODENAV_MCP_ALLOW_READONLY_FILES=1` |
| `CODENAV_MCP_EXCLUDE_DIRS` | unset | Comma-separated directory names added to the built-in list of directories that are never scanned or written (for example `generated`). See [directories](tools.md#directories-never-scanned-or-written) |
| `CODENAV_MCP_INCLUDE_DIRS` | unset | Comma-separated directory names removed from that list, for example `build` to treat a first-party `build/` package as source (scanned and writable). Repository metadata folders such as `.git` are never editable, whatever this variable says |
| `CODENAV_MCP_MAX_FILES` | `250000` | Maximum number of Python files (`.py`/`.pyi`) a workspace may hold; the count covers everything ty indexes, so only the folders ty skips by default (`.git`, `.venv`, `venv`, `node_modules`, `dist`, `.tox` and similar) are left out; `CODENAV_MCP_EXCLUDE_DIRS` does not change it. A larger tree is refused before the language server starts: at startup the server exits with code 2 and a one-line message, and a client-driven switch to such a tree is refused with the same message. Meant to stop a whole home directory from exhausting memory. A value that is not a whole number or is below 1 uses the default |

Startup and source-root rules:

- `CODENAV_MCP_WORKSPACE` and `CODENAV_MCP_SOURCE_ROOT` are checked at startup. The server refuses to start when the pinned workspace is not an existing folder, when the workspace is a filesystem root (`/`), when `CODENAV_MCP_SOURCE_ROOT` is not a folder inside the workspace, or when the workspace holds more than `CODENAV_MCP_MAX_FILES` Python files. The error names the setting and its value.
- Without `CODENAV_MCP_SOURCE_ROOT`, a workspace with a `src/` folder that holds a package or module (and has no `__init__.py` itself) scans and derives import paths from `src/`. Set `CODENAV_MCP_SOURCE_ROOT` to `.` to use the whole workspace instead. The `workspace` tool reports this as `Source root: src (auto-detected)`.
- `CODENAV_MCP_EXTRA_SOURCE_ROOTS` entries that are not existing folders inside the workspace are skipped, and the server says so in the next tool reply (`Ignored extra source root: <entry>`).
- `VIRTUAL_ENV` is not read by the server itself; ty reads it, so when it is set, ty resolves imports against that environment's packages.
- ty cannot see extra source roots outside the workspace, so imports that come only from such a root stay unresolved for ty, and writes that depend on them are refused or reported.
- The `workspace` tool also reports the write mode (`Writes: enabled` or `Writes: disabled (CODENAV_MCP_READ_ONLY)`), the ty command it will start, and any configuration warning.

## Requirements

- Python ≥ 3.11
- Installed automatically: [`mcp`](https://pypi.org/project/mcp/),
  [`ty`](https://pypi.org/project/ty/),
  [`ruff`](https://pypi.org/project/ruff/) (sorts imports in files a move touched, only where they were already sorted),
  [`mcp-nav-shared`](https://pypi.org/project/mcp-nav-shared/)
