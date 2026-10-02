"""Glue between the write tools and the running server module.

Tool functions live in plain modules so tests can call them directly; the
server binds itself here once at import and registers them on its MCP app.
Looking the server up at call time (rather than importing its globals) keeps
workspace switches, which reassign those globals, visible to the tools.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from mcp.types import ToolAnnotations
from mcp_nav_shared.lsp_client import LspClient

from codenav_mcp.writes import WriteState, import_roots, write_state


# Preview-capable tools can also write; only the ones that never write say so.
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)
READS = ToolAnnotations(read_only_hint=True, open_world_hint=False)

_server: ModuleType | None = None


def bind(server: ModuleType) -> None:
	global _server
	_server = server


def server() -> Any:
	if _server is None:
		raise RuntimeError("codenav_mcp.server has not bound itself yet")
	return _server


@dataclass
class Session:
	client: LspClient
	workspace: Path
	roots: list[Path]
	state: WriteState


async def open_session(ctx: Any) -> Session:
	srv = server()
	await srv._use_workspace(ctx)
	client = await srv.get_client()
	workspace = srv.WORKSPACE_ROOT
	return Session(
		client=client,
		workspace=workspace,
		roots=import_roots(workspace, srv.SOURCE_ROOT, list(srv.EXTRA_SOURCE_ROOTS)),
		state=write_state(workspace),
	)
