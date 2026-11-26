"""D6 (F13, F14): writes say they apply by default; `old_name` aliases `name`; a bad `show` lists the valid sections."""

from __future__ import annotations

import asyncio

import pytest

from codenav_mcp.args import apply_aliases, argument_problem, signature_of
from codenav_mcp.server import mcp


WRITE_TOOLS = ["rename", "change_signature", "move", "edit_symbol", "fix"]


@pytest.fixture(scope="module")
def tools():
	return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


@pytest.mark.parametrize("name", WRITE_TOOLS)
def test_given_a_write_tool_then_its_schema_and_description_say_apply_defaults_to_true(tools, name):
	tool = tools[name]
	assert tool.input_schema["properties"]["apply"]["default"] is True
	assert "Writes by default; apply=false previews." in tool.description


def test_given_a_read_tool_then_the_description_does_not_claim_to_write(tools):
	assert all("Writes by default" not in tools[name].description for name in ("lookup", "search", "check", "edits"))


@pytest.mark.parametrize("tool", ["rename", "lookup", "change_signature"])
def test_given_old_name_then_it_means_name(tool):
	fn = signature_of(tool)
	assert fn is not None
	assert apply_aliases(tool, fn, {"old_name": "A.run"}) == ({"name": "A.run"}, None)


def test_given_rename_with_old_name_and_name_then_they_conflict_only_when_different():
	fn = signature_of("rename")
	assert fn is not None
	assert apply_aliases("rename", fn, {"name": "a", "old_name": "a", "new_name": "b"})[1] is None
	assert apply_aliases("rename", fn, {"name": "a", "old_name": "c", "new_name": "b"})[1] is not None


def test_given_a_bad_show_section_then_the_refusal_lists_the_valid_sections():
	fn = signature_of("lookup")
	assert fn is not None
	problem = argument_problem("lookup", fn, {"name": "X", "show": ["tests"]})
	assert problem is not None
	assert "'tests'" in problem and "valid:" in problem
	assert all(section in problem for section in ("signature", "callers", "references", "mentions"))
	assert argument_problem("lookup", fn, {"name": "X", "show": ["callers"]}) is None
