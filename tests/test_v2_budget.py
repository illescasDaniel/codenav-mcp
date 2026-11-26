"""v2 discovery budget: what an agent must read before calling a tool stays small (docs/v2-design.md)."""

import asyncio
import json

import pytest

from codenav_mcp import analysis_tools, edits_tools, read_tools, server as codenav_server


# Description + JSON input schema per v2 tool, in characters.
PER_TOOL_BUDGET = 700
V2_TOOL_NAMES = {fn.__name__ for module in (read_tools, analysis_tools, edits_tools) for fn, _ in module.TOOLS}


def _tool_sizes() -> dict[str, int]:
	tools = asyncio.run(codenav_server.mcp.list_tools())
	return {tool.name: len(tool.description or "") + len(json.dumps(tool.input_schema)) for tool in tools}


@pytest.mark.parametrize("name", sorted(V2_TOOL_NAMES) or ["(none yet)"])
def test_given_v2_tool_then_description_and_schema_fit_the_budget(name):
	if name == "(none yet)":
		pytest.skip("no v2 tools registered yet")
	size = _tool_sizes()[name]
	assert size <= PER_TOOL_BUDGET, f"{name}: {size} chars > {PER_TOOL_BUDGET}"
