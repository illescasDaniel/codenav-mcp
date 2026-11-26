"""Bound the discovery payload and pin the exported v2 tools."""

import asyncio
import json

import pytest

from codenav_mcp.server import mcp


TOOL_NAMES = {
	"lookup",
	"search",
	"outline",
	"check",
	"unused",
	"impact",
	"rename",
	"change_signature",
	"move",
	"edit_symbol",
	"fix",
	"edits",
}


@pytest.fixture(scope="module")
def tool_sizes():
	tools = asyncio.run(mcp.list_tools())
	return {tool.name: len(tool.description or "") + len(json.dumps(tool.input_schema)) for tool in tools}


@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_description_and_schema_fit_the_tool_budget(name, tool_sizes):
	assert tool_sizes[name] <= 700


def test_v2_discovery_surface_and_total_budget(tool_sizes):
	assert set(tool_sizes) == TOOL_NAMES
	assert len(mcp.instructions or "") + sum(tool_sizes.values()) <= 9000


@pytest.mark.parametrize(
	("tool", "alias", "parameter"),
	[
		("lookup", "col", "column"),
		("search", "symbol", "query"),
		("outline", "filepath", "file_path"),
		("check", "file", "file_path"),
		("unused", "file", "path"),
		("impact", "qualified_name", "name"),
		("rename", "symbol_name", "name"),
		("change_signature", "query", "name"),
		("move", "file", "file_path"),
		("edit_symbol", "path", "file_path"),
		("fix", "lineno", "line"),
		("edits", "edit_id", "id"),
	],
)
def test_every_tool_registers_argument_aliases(tool, alias, parameter):
	from codenav_mcp.args import apply_aliases, signature_of

	fn = signature_of(tool)
	assert fn is not None
	assert apply_aliases(tool, fn, {alias: "value"}) == ({parameter: "value"}, None)
