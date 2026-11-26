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
	# edit_symbol and change_signature carry the shape of nested items (`edits`, `add`), which no schema can show.
	assert tool_sizes[name] <= {"edit_symbol": 960, "change_signature": 890}.get(name, 800)


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


def _description(name: str) -> str:
	tools = asyncio.run(mcp.list_tools())
	return next(tool.description or "" for tool in tools if tool.name == name)


def test_nested_argument_shapes_are_visible_before_the_first_call():
	from typing import get_args

	from codenav_mcp.edit_tools import _Action
	from codenav_mcp.read_tools import _SECTIONS

	edit = _description("edit_symbol")
	assert '"file_path","old_string","new_string"' in edit and '"name","action","source"' in edit
	assert all(action in edit for action in get_args(_Action) if action != "insert")
	assert "all-or-nothing" in edit and "one undo id" in edit
	change = _description("change_signature")
	assert all(key in change for key in ("name", "annotation?", "default?", "value?", "position?", "keyword_only?"))
	assert "keyword-only" in change
	assert all(section in _description("lookup") for section in _SECTIONS)
	assert "sites=" in _description("rename") and "u1" in _description("rename")
	assert all(action in _description("edits") for action in ("list", "show", "apply", "undo"))


def test_instructions_say_how_writes_and_previews_work():
	text = mcp.instructions or ""
	assert "apply=true" in text and "apply=false always previews" in text
	assert "held as a preview" in text and "checks types only" in text
	assert "grep" in text and "lookup" in text
