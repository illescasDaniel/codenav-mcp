"""B12a (QA campaign): position reads (hover, definition, symbol_info) and name resolution against a real `ty`."""

from __future__ import annotations

import asyncio
import sysconfig

import pytest
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp import read_tools, server as codenav_server

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]
loop = make_loop_fixture()

_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/mod.py": "def func(x: int) -> int:\n\treturn x\n",
	"src/pkg/stubbed.py": "def stubfn(x: int) -> int:\n\treturn x\n",
	"src/pkg/stubbed.pyi": "def stubfn(x: int) -> int: ...\n",
	"src/pkg/uses_stub.py": "from pkg.stubbed import stubfn\n\n\nstubfn(1)\n",
	"src/pkg/uses_stdlib.py": "import json\n\n\njson.dumps({})\n",
	"src/pkg/ov.py": (
		"from typing import overload\n\n\n@overload\ndef ov(x: int) -> int: ...\n"
		"@overload\ndef ov(x: str) -> str: ...\ndef ov(x):\n\treturn x\n"
	),
	"src/pkg/kw.py": "def helper(x):\n\treturn x\n",
	"src/pkg/badimport.py": "from nonexistent_mod_xyz import thing\n\n\nthing()\n",
	"src/pkg/localmod.py": "def fn(x):\n\tvalue = x\n\treturn value\n",
	"src/pkg/nfkc.py": "ﬁ = 1\n",
	"src/pkg/cart.py": "class Cart:\n\tdef total(self) -> int:\n\t\treturn 0\n\n\ndef target() -> int:\n\treturn 1\n",
	"src/pkg/end.py": "total_value = 1\nother = total_value\n",
}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(tmp_path, monkeypatch, _FILES)


def _text(result) -> str:
	"""Tool results are plain strings on success and a CallToolResult on refusal."""
	return result if isinstance(result, str) else result.content[0].text


def _run(loop, coro) -> str:
	return _text(loop.run_until_complete(coro))


def test_rc_003_definition_lists_the_stub_first_then_the_implementation(project, loop):
	text = _run(loop, read_tools.lookup(file_path="src/pkg/uses_stub.py", line=4, column=1, show=["signature"]))
	assert "stubbed.pyi:1" in text
	assert text.index("stubbed.pyi:1:5") < text.index("stubbed.py:1:5")


def test_rc_038_library_definition_is_labelled_not_absolute(project, loop):
	text = _run(loop, read_tools.lookup(file_path="src/pkg/uses_stdlib.py", line=4, column=6, show=["signature"]))
	assert "<stdlib>/json/" in text
	assert "/usr/" not in text
	assert "file://" not in text


def test_stdlib_path_uses_a_portable_label(tmp_path, monkeypatch):
	stdlib = tmp_path / "Lib"
	monkeypatch.setattr(sysconfig, "get_path", lambda key: str(stdlib) if key == "stdlib" else None)
	assert (
		codenav_server._display_path((stdlib / "json" / "__init__.py").as_uri(), tmp_path / "project")
		== "<stdlib>/json/__init__.py"
	)
	assert (
		codenav_server._display_path((stdlib / "site-packages" / "private.py").as_uri(), tmp_path / "project")
		== "<site-packages>/private.py"
	)


def test_rc_039_hover_text_drops_the_workspace_root_and_library_paths(tmp_path, monkeypatch):
	monkeypatch.setattr(codenav_server, "WORKSPACE_ROOT", tmp_path)
	contents = {
		"kind": "markdown",
		"value": f"Searched in the following paths:\n{tmp_path}/src\n/usr/lib/python3.14/json/__init__.py",
	}
	text = codenav_server._format_hover_contents(contents)
	assert str(tmp_path) not in text
	assert "/usr/lib" not in text
	assert "<stdlib>/json/__init__.py" in text


def test_rc_048_outside_file_is_refused_by_hover_before_any_read(tmp_path, monkeypatch, loop):
	ws = tmp_path / "ws"
	ws.mkdir()
	make_workspace(ws, monkeypatch, {"pkg/a.py": "x = 1\n"})
	(tmp_path / "outside.py").write_text("secret = 1\n")
	text = _run(loop, read_tools.lookup(file_path="../outside.py", line=1, column=1, show=["signature"]))
	assert text.startswith("../outside.py is outside the workspace; give a path inside it")


def test_rc_079_check_python_file_refuses_a_path_outside_the_workspace(tmp_path, monkeypatch):
	ws = tmp_path / "ws"
	ws.mkdir()
	monkeypatch.setattr(codenav_server, "WORKSPACE_ROOT", ws)
	with pytest.raises(ToolInputError, match="outside the workspace; give a path inside it"):
		codenav_server._check_python_file("../outside.py")


def test_rc_080_dotted_module_path_in_symbol_info_resolves_the_module_function(project, loop):
	text = _run(loop, read_tools.lookup(name="pkg.mod.func"))
	assert text.splitlines()[0].startswith("pkg.mod.func  src/pkg/mod.py:1:5")
	assert "pkg/mod.py:1:5" in text.splitlines()[0]


def test_rc_090_ligature_query_finds_the_nfkc_normalised_name(project, loop):
	text = _run(loop, read_tools.lookup(name="ﬁ", file_path="src/pkg/nfkc.py"))
	assert "No symbol" not in text
	assert "src/pkg/nfkc.py:1:1" in text.splitlines()[0]


def test_rc_099_overloads_resolve_to_the_implementation(project, loop):
	text = _run(loop, read_tools.lookup(name="ov", file_path="src/pkg/ov.py"))
	assert "pkg/ov.py:8:5" in text.splitlines()[0]


def test_rc_101_unknown_name_gets_a_did_you_mean_hint(project, loop):
	text = _run(loop, read_tools.lookup(name="targt"))
	assert "No symbol found matching 'targt'. Did you mean: target?" in text


def test_rc_103_name_and_query_with_different_values_are_refused(project, loop):
	text = _run(loop, codenav_server.mcp.call_tool("lookup", {"name": "Cart", "query": "other"}))
	assert "give either 'name' or 'query', not both" in text


def test_end_of_identifier_resolves_the_identifier_it_touches(project, loop):
	# RC-124 (D25 A): test-only pin of existing behaviour, so it passes on base by design.
	# Column 20 is just past `total_value`.
	text = _run(loop, read_tools.lookup(file_path="src/pkg/end.py", line=2, column=20, show=["signature"]))
	assert "end.py:1:1" in text


def test_rc_139_hover_on_def_keyword_points_at_the_name(project, loop):
	text = _run(loop, read_tools.lookup(file_path="src/pkg/kw.py", line=1, column=1, show=["signature"]))
	assert "Put the cursor on the symbol name" in text


def test_rc_141_definition_on_an_unresolved_import_adds_a_note(project, loop):
	text = _run(loop, read_tools.lookup(file_path="src/pkg/badimport.py", line=4, column=1, show=["signature"]))
	assert "The import does not resolve." in text


def test_rc_144_local_name_in_symbol_info_gets_the_indexing_hint(project, loop):
	text = _run(loop, read_tools.lookup(name="value"))
	assert "value is a local variable/parameter; locals are only visible through lookup at their position." in text


def test_rc_056_the_six_read_tools_are_annotated_read_only():
	tools = {tool.name: tool for tool in asyncio.run(codenav_server.mcp.list_tools())}
	for name in ("lookup", "search", "outline", "check", "unused", "impact"):
		annotations = tools[name].annotations
		assert annotations is not None, name
		assert annotations.read_only_hint is True, name
		assert annotations.open_world_hint is False, name


def test_rc_080_missing_module_path_in_symbol_info_gets_a_module_hint(project, loop):
	text = _run(loop, read_tools.lookup(name="pkg.nomod.func"))
	assert "pkg.nomod looks like a module path; give file_path= and the bare name." in text
