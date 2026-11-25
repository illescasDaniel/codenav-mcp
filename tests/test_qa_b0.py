from __future__ import annotations

import asyncio

from codenav_mcp import batch_tools, server


def test_rc_161_every_registered_tool_is_capped() -> None:
	for tool in server.mcp._tool_manager._tools.values():
		assert getattr(tool.fn, "__output_capped__", False), tool.name


def test_rc_161_batch_total_cap(monkeypatch) -> None:
	monkeypatch.setenv("CODENAV_MCP_MAX_OUTPUT_CHARS", "5000")

	async def large() -> str:
		return "x" * 20000

	monkeypatch.setattr(batch_tools, "_registry", {})
	batch_tools.register("large", large, writes=False)
	result = asyncio.run(batch_tools.run_batch([{"tool": "large"}] * 5, False, None))
	assert len(result) <= 5200
	assert "#1, #2, #3, #4, #5" in result


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
			return await server.references("target.py", 1, 5, limit=1000)
		finally:
			if server._client is not None:
				await server._client.stop()

	result = asyncio.run(run())
	assert len(result) <= 5000
	assert "more lines" in result
