"""Workspace (checkout/worktree) selection for the codenav server."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from mcp import Client, types
from mcp_nav_shared.workspace import WorkspaceSelector

from codenav_mcp import server as codenav_server


@pytest.fixture(autouse=True)
def _restore_workspace_state(monkeypatch):
	# `_configure_workspace` rewrites these; register them so teardown restores the originals.
	for name in ("_workspace_source", "_probe_cache_signature"):
		monkeypatch.setattr(codenav_server, name, getattr(codenav_server, name))


def _repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
	main = tmp_path / "main"
	main.mkdir()
	linked = tmp_path / "linked"
	for args in (
		["init", "-q", "-b", "trunk"],
		["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"],
		["worktree", "add", "-q", "-b", "feature", str(linked)],
	):
		subprocess.run(["git", *args], cwd=main, check=True, capture_output=True)  # noqa: S603, S607
	return main.resolve(), linked.resolve()


def test_given_client_reports_worktree_root_when_tool_called_then_server_switches_workspace(tmp_path, monkeypatch):
	# given — the host started the server in the main checkout, the session works in a worktree
	main, linked = _repo_with_worktree(tmp_path)
	monkeypatch.delenv("CODENAV_MCP_WORKSPACE", raising=False)
	monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(main))
	monkeypatch.setattr(codenav_server, "_selector", WorkspaceSelector("CODENAV_MCP_WORKSPACE"))
	monkeypatch.setattr(codenav_server, "WORKSPACE_ROOT", main)
	monkeypatch.setattr(codenav_server, "SOURCE_ROOT", main)
	monkeypatch.setattr(codenav_server, "_client", None)

	async def _roots(_context: object) -> types.ListRootsResult:
		return types.ListRootsResult(roots=[types.Root(uri=linked.as_uri())])

	async def _ask() -> str:
		async with Client(codenav_server.mcp, list_roots_callback=_roots, mode="legacy") as client:
			result = await client.call_tool("workspace", {})
		return str(result.content[0].model_dump()["text"])

	# when
	text = asyncio.run(_ask())
	# then
	assert str(linked) in text
	assert "client roots" in text
	assert linked == codenav_server.WORKSPACE_ROOT


def test_given_workspace_switch_when_configure_then_client_stopped_and_caches_cleared(tmp_path, monkeypatch):
	# given
	class _Running:
		stopped = False

		async def stop(self) -> None:
			self.stopped = True

	running = _Running()
	monkeypatch.setattr(codenav_server, "_client", running)
	monkeypatch.setattr(codenav_server, "WORKSPACE_ROOT", tmp_path)
	monkeypatch.setattr(codenav_server, "SOURCE_ROOT", tmp_path)
	monkeypatch.setitem(codenav_server._probe_cache, ("a", "b", "c", "d"), (True, "x"))
	# when
	asyncio.run(codenav_server._configure_workspace(tmp_path / "other", "client roots"))
	# then
	assert running.stopped
	assert codenav_server._client is None
	assert tmp_path / "other" == codenav_server.WORKSPACE_ROOT
	assert tmp_path / "other" == codenav_server.SOURCE_ROOT
	assert not codenav_server._probe_cache
