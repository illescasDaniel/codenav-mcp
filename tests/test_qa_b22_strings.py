"""Wire-level regression for optional string arguments resembling JSON."""

from __future__ import annotations

from mcp import Client

from codenav_mcp import batch_tools, server

from .live_support import make_loop_fixture, make_workspace, needs_ty


loop = make_loop_fixture()


@needs_ty
def test_optional_literal_strings_reach_edit_unchanged(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{"src/a.py": "a = {}\nb = [1]\nc = None\n", "src/repeat.py": "a = {}\nb = {}\n", "src/same.py": "{}\n"},
	)
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))

	server._notices.post("literal string regression notice")

	async def run():
		async with Client(server.mcp, mode="legacy") as client:
			results = [
				await client.call_tool(
					"edit", {"file_path": "src/a.py", "old_string": value, "new_string": "0", "apply": False}
				)
				for value in ("{}", "[1]", "null")
			]
			results.append(
				await client.call_tool("edit", {"file_path": "src/repeat.py", "old_string": "{}", "new_string": "0"})
			)
			results.append(await client.call_tool("edit", {"file_path": "src/same.py", "new_text": "{}\n"}))
			return results

	results = loop.run_until_complete(run())
	assert not results[0].is_error and "Preview" in results[0].content[0].text
	assert "literal string regression notice" in results[0].content[0].text
	assert results[0].structured_content is None, "tools answer text only; no structuredContent is emitted"
	assert not results[1].is_error and "Preview" in results[1].content[0].text
	assert "literal string regression notice" not in results[1].content[0].text
	assert results[2].is_error and "not found" in results[2].content[0].text
	assert results[3].is_error and "matches 2 times" in results[3].content[0].text
	assert not results[4].is_error and "No changes" in results[4].content[0].text
	assert (root / "src/a.py").read_text() == "a = {}\nb = [1]\nc = None\n"


def test_literal_detection_does_not_bypass_normal_inputs():
	fn = batch_tools.signature_of("edit")
	assert server._needs_literal_string_dispatch(fn, {"old_string": "{}"})
	assert server._needs_literal_string_dispatch(fn, {"new_text": "null"})
	assert not server._needs_literal_string_dispatch(fn, {"new_text": None})
	assert not server._needs_literal_string_dispatch(fn, {"new_text": "x = 1"})
