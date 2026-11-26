from __future__ import annotations

import asyncio

from codenav_mcp import read_tools, server


def test_rc_161_every_registered_tool_is_capped() -> None:
	for tool in server.mcp._tool_manager._tools.values():
		assert getattr(tool.fn, "__output_capped__", False), tool.name


def test_rc_049_references_capped(tmp_path, monkeypatch) -> None:
	from .live_support import make_workspace

	monkeypatch.setenv("CODENAV_MCP_MAX_OUTPUT_CHARS", "5000")
	make_workspace(
		tmp_path,
		monkeypatch,
		{
			"target.py": "def target():\n    pass\n",
			"use.py": "from target import target\n" + "          target()\n" * 600,
		},
	)

	async def run() -> str:
		try:
			return await server._notices.tool(read_tools.lookup)(
				file_path="target.py", line=1, column=5, limit=1000, show=["references"]
			)
		finally:
			if server._client is not None:
				await server._client.stop()

	result = asyncio.run(run())
	assert len(result) <= 5000
	assert "more lines" in result
