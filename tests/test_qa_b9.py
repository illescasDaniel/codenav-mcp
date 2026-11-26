from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from codenav_mcp import symbol_tools
from codenav_mcp.pysource import definitions_in
from codenav_mcp.symbol_tools import _apply_imports, _delete_span, fix
from tests.live_support import make_loop_fixture, make_workspace, needs_ty


def test_rc_073_import_alias():
	assert "import os.path as osp" in _apply_imports("", ["import os.path as osp"])


def test_rc_117_keep_comments():
	text = "# section\ndef f():\n    pass\n"
	assert "# section" in _delete_span(text, definitions_in(text)[0])


def test_rc_116_negative_line():
	assert "line must be 1 or more" in asyncio.run(fix("a.py", line=-1))


loop = make_loop_fixture()


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(
		tmp_path,
		monkeypatch,
		{
			"a.py": "from os import getcwd\n\ndef doomed():\n    return getcwd()\n",
			"b.py": "def doomed():\n    return 1\n",
			"bindings.py": "x = 1\ny: int = 2\nimport os\nclass C:\n    z = 3\n",
		},
	)


@needs_ty
def test_rc_052_prunes_import(project, loop):
	result = loop.run_until_complete(symbol_tools._safe_delete("doomed", file_path="a.py"))
	assert "Also removed now-unused import(s): getcwd." in result
	assert "getcwd" not in (project / "a.py").read_text()


@needs_ty
def test_rc_133_other_definition(project, loop):
	result = loop.run_until_complete(symbol_tools._safe_delete("doomed", file_path="a.py"))
	assert "Applied as edit" in result


@needs_ty
def test_rc_132_held_back_hint(project, loop):
	(project / "dynamic.py").write_text('value = "doomed"\n')
	result = loop.run_until_complete(symbol_tools._safe_delete("doomed", file_path="a.py"))
	assert "held back because" in result and "rerun with force=true" in result
	assert "rerun with apply=true" not in result


@needs_ty
def test_rc_061_binding_clashes(project, loop):
	for source, kwargs in [("x = 4", {}), ("y = 4", {}), ("os = 4", {}), ("z = 4", {"into": "C"})]:
		result = loop.run_until_complete(
			symbol_tools._insert_symbol(source, "bindings.py", **cast(dict[str, Any], kwargs))
		)
		assert "already exists in bindings.py at line" in result


@needs_ty
def test_rc_118_missing(project, loop):
	result = loop.run_until_complete(symbol_tools._insert_symbol("def f(): pass", "missing.py"))
	assert "missing.py does not exist; create it with edit(file_path=..., new_text=...) first" in result


@needs_ty
def test_rc_078_syntax(project, loop):
	(project / "bad.py").write_text("def broken(:\n")
	result = loop.run_until_complete(symbol_tools.fix("bad.py"))
	assert "bad.py has a syntax error at line 1; fix it first" in result


def _diagnostic(line, code):
	return {
		"range": {"start": {"line": line, "character": 0}, "end": {"line": line, "character": 1}},
		"code": code,
		"message": code,
	}


def _action(title):
	return {"title": title, "edit": {}}


class _Client:
	def __init__(self, diagnostics, actions):
		self.items, self.actions = diagnostics, actions

	async def diagnostics(self, path):
		return self.items

	async def code_actions(self, path, start, end, **kwargs):
		return self.actions.get(start[0], [])


def _mock_session(monkeypatch, tmp_path, diagnostics, actions):
	(tmp_path / "a.py").write_text("x = 1\ny = 2\n")

	async def open_session(ctx):
		return SimpleNamespace(workspace=tmp_path, roots=[tmp_path], client=_Client(diagnostics, actions))

	monkeypatch.setattr(symbol_tools, "open_session", open_session)


def test_rc_077_suppression(monkeypatch, tmp_path):
	_mock_session(monkeypatch, tmp_path, [_diagnostic(0, "x")], {1: [_action("Ignore x")]})
	assert "Only suppression fixes are available" in asyncio.run(fix("a.py"))


def test_rc_135_invalid_choice(monkeypatch, tmp_path):
	_mock_session(monkeypatch, tmp_path, [_diagnostic(0, "x")], {1: [_action("first"), _action("second")]})
	assert asyncio.run(fix("a.py", choice=3)).startswith("Choices: 1. first 2. second")


def test_rc_136_fixable_lines(monkeypatch, tmp_path):
	_mock_session(monkeypatch, tmp_path, [_diagnostic(0, "a"), _diagnostic(1, "b")], {2: [_action("fix")]})
	result = asyncio.run(fix("a.py", line=8))
	assert "Lines with fixes: 2." in result


def test_rc_137_fixable_codes(monkeypatch, tmp_path):
	_mock_session(monkeypatch, tmp_path, [_diagnostic(0, "a"), _diagnostic(1, "b")], {2: [_action("fix")]})
	result = asyncio.run(fix("a.py", code="missing"))
	assert "Available codes with fixes: b." in result and "fixes: a" not in result


@needs_ty
def test_rc_076_duplicate_import(project, loop):
	(project / "models.py").write_text("class Cart: pass\n")
	(project / "uses.py").write_text(
		'"""Doc."""\nimport os\n\ndef a(): return Cart(), os.sep\ndef b(): return Cart()\n'
	)
	result = loop.run_until_complete(fix("uses.py"))
	assert "Applied as edit" in result
	assert (project / "uses.py").read_text().count("from models import Cart") == 1


def test_rc_160_sorted_import_placement():
	text = symbol_tools._apply_imports("import itertools\nimport re\n", ["from urllib.parse import urlsplit"])
	assert text.index("import re") < text.index("from urllib.parse import urlsplit")


@needs_ty
def test_duplicate_inserted_bindings_and_nested_scope_bindings(project, loop):
	path = project / "scopes.py"
	path.write_text("with open(__file__) as existing:\n    pass\nmatch 1:\n    case captured:\n        pass\n")
	for source in ("existing = 4", "captured = 4", "fresh = 1\nfresh = 2", "def dup(): pass\ndef dup(): pass"):
		result = loop.run_until_complete(symbol_tools._insert_symbol(source, "scopes.py"))
		assert "already exists" in result
	assert "fresh" not in path.read_text()


@needs_ty
def test_other_definition_with_dynamic_same_line_reference_is_held_back(project, loop):
	(project / "b.py").write_text("from typing import Any, cast\nobj: Any\ndef doomed(x=obj.doomed()):\n    pass\n")
	result = loop.run_until_complete(symbol_tools._safe_delete("doomed", file_path="a.py"))
	assert "held back because" in result
	assert "def doomed" in (project / "a.py").read_text()
