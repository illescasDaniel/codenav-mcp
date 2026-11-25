---
id: DOC-002
title: Configuration
summary: MCP host setup, workspace selection, environment variables and ty selection
type: reference
status: current
updated: 2026-11-25
verified:
covers: [src/codenav_mcp/server.py, src/codenav_mcp/ty_command.py, pyproject.toml]
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
