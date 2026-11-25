"""Protocol-level failure status, including partial batch outcomes."""

from __future__ import annotations

import pytest
from mcp import Client

from codenav_mcp import server

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]
loop = make_loop_fixture()


def test_mcp_error_status_and_batches(tmp_path, monkeypatch, loop):
	root = make_workspace(tmp_path, monkeypatch, {"src/a.py": "x = 1\nx = 1\n"})
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))

	async def scenario():
		async with Client(server.mcp, mode="legacy") as client:
			bad = {"file_path": "src/a.py", "old_string": "x = 1", "new_string": "x = 2"}
			failed = await client.call_tool("edit", bad)
			preview = await client.call_tool("edit", {"file_path": "src/a.py", "new_text": "x = 2\n", "apply": False})
			nochange = await client.call_tool("edit", {"file_path": "src/a.py", "new_text": "x = 1\nx = 1\n"})
			mixed = await client.call_tool(
				"batch",
				{
					"calls": [{"tool": "edit", "args": bad}, {"tool": "outline", "args": {"file_path": "src/a.py"}}],
					"stop_on_error": False,
				},
			)
			stopped = await client.call_tool(
				"batch",
				{
					"calls": [{"tool": "edit", "args": bad}, {"tool": "outline", "args": {"file_path": "src/a.py"}}],
					"stop_on_error": True,
				},
			)
			all_failed = await client.call_tool("batch", {"calls": [{"tool": "edit", "args": bad}]})
			return failed, preview, nochange, mixed, stopped, all_failed

	failed, preview, nochange, mixed, stopped, all_failed = loop.run_until_complete(scenario())
	assert failed.is_error
	assert failed.content[0].text == "old_string matches 2 times (lines 1, 2); add context or pass replace_all=true"
	assert not preview.is_error and "Preview only" in preview.content[0].text
	assert not nochange.is_error
	assert not mixed.is_error and "1 failed" in mixed.content[0].text
	assert stopped.is_error and "1 skipped" in stopped.content[0].text
	assert all_failed.is_error and "1 failed" in all_failed.content[0].text


def test_mcp_read_and_blocked_delete_are_errors(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{"src/a.py": "def target():\n    return 1\n\ndef use():\n    return target()\n"},
	)
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))

	async def scenario():
		async with Client(server.mcp, mode="legacy") as client:
			missing = await client.call_tool("outline", {"file_path": "src/missing.py"})
			blocked = await client.call_tool("edit_symbol", {"action": "delete", "name": "target"})
			search = await client.call_tool("search_symbol", {"name": "zzzz_not_found"})
			batch = await client.call_tool(
				"batch",
				{
					"calls": [
						{"tool": "outline", "args": {"file_path": "src/missing.py"}},
						{"tool": "outline", "args": {"file_path": "src/a.py"}},
					],
					"stop_on_error": False,
				},
			)
			return missing, blocked, search, batch

	missing, blocked, search, batch = loop.run_until_complete(scenario())
	assert missing.is_error
	assert blocked.is_error and "Not deleted:" in blocked.content[0].text
	assert not search.is_error
	assert not batch.is_error and "1 failed" in batch.content[0].text
