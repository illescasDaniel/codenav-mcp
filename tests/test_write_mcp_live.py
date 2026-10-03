"""The write tools through the real MCP protocol: registration, schemas, annotations, a full round trip."""

from __future__ import annotations

import pytest
from mcp import Client

from codenav_mcp import server as codenav_server

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

WRITE_TOOLS = {
	"edit",
	"edit_symbol",
	"rename_symbol",
	"change_signature",
	"move",
	"quick_fix",
	"verify_changes",
	"apply_edit",
	"undo_edit",
}
RETIRED_TOOLS = {"check_edit", "replace_symbol", "insert_symbol", "safe_delete", "move_symbol", "move_module"}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	monkeypatch.setattr(codenav_server, "_selector", codenav_server._selector)
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/core.py": "def compute(value: int) -> int:\n\treturn value\n",
			"src/pkg/user.py": "from pkg.core import compute\n\n\ndef use() -> int:\n\treturn compute(1)\n",
		},
	)
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(codenav_server, "_selector", codenav_server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))
	return root


def _text(result) -> str:
	return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def test_given_server_when_listing_tools_then_write_tools_have_schemas_and_safety_annotations(project, loop):
	async def scenario():
		async with Client(codenav_server.mcp, mode="legacy") as client:
			return (await client.list_tools()).tools

	# when
	tools = {tool.name: tool for tool in loop.run_until_complete(scenario())}
	# then
	assert WRITE_TOOLS <= set(tools)
	assert not RETIRED_TOOLS & set(tools)
	assert tools["verify_changes"].annotations.read_only_hint is True
	assert tools["rename_symbol"].annotations.destructive_hint is True
	assert "ctx" not in tools["rename_symbol"].input_schema["properties"]  # the context is injected, never a parameter
	assert tools["rename_symbol"].input_schema["required"] == ["new_name"]
	assert {"name", "new_name", "apply", "parameter", "linked", "max_new_errors"} <= set(
		tools["rename_symbol"].input_schema["properties"]
	)
	assert "add" in tools["change_signature"].input_schema["properties"]


def test_given_mcp_client_when_rename_preview_apply_undo_then_round_trip_works(project, loop):
	async def scenario():
		async with Client(codenav_server.mcp, mode="legacy") as client:
			preview = _text(
				await client.call_tool("rename_symbol", {"name": "compute", "new_name": "calculate", "apply": False})
			)
			edit_id = preview.split('apply_edit(id="')[1].split('"')[0]
			applied = _text(await client.call_tool("apply_edit", {"id": edit_id}))
			undone = _text(await client.call_tool("undo_edit", {}))
			return preview, applied, undone

	# when
	preview, applied, undone = loop.run_until_complete(scenario())
	# then
	assert "Preview only" in preview
	assert "Applied edit" in applied
	assert "Reverted edit" in undone
	assert "def compute(" in (project / "src/pkg/core.py").read_text()


def test_given_structured_argument_when_change_signature_over_mcp_then_list_of_objects_accepted(project, loop):
	async def scenario():
		async with Client(codenav_server.mcp, mode="legacy") as client:
			return _text(
				await client.call_tool(
					"change_signature",
					{
						"name": "compute",
						"add": [{"name": "scale", "annotation": "int", "default": "1", "value": "2"}],
						"apply": True,
					},
				)
			)

	# when
	text = loop.run_until_complete(scenario())
	# then
	assert "Applied as edit" in text
	assert "compute(1, scale=2)" in (project / "src/pkg/user.py").read_text()
