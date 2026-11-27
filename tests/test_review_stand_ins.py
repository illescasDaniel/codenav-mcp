"""Review G1: change_signature names same-named test fakes the type checker does not link (they keep the old
parameters and fail once callers pass the new one), and leaves unrelated same-name methods out."""

from __future__ import annotations

from pathlib import Path

import pytest

from codenav_mcp import refactor_tools
from codenav_mcp.fakes import find_stand_ins

from .live_support import make_loop_fixture, make_workspace, needs_ty


PORT = (
	"from typing import Protocol\n\n\n"
	"class SearchPort(Protocol):\n"
	"\tdef execute(self, query: str, *, limit: int = 0) -> list[str]: ...\n\n\n"
	"class Search:\n"
	"\tdef execute(self, query: str, *, limit: int = 0) -> list[str]:\n"
	"\t\treturn [query][:limit or None]\n\n\n"
	"def run(port: SearchPort) -> list[str]:\n"
	"\treturn port.execute('x', limit=1)\n"
)
TEST = (
	"import sqlite3\n"
	"from unittest.mock import MagicMock\n\n\n"
	"def test_local_fake():\n"
	"\tclass _Fake:\n"
	"\t\tdef execute(self, query, *, limit=0):\n"
	"\t\t\treturn []\n\n"
	"\tassert _Fake().execute('q') == []\n\n\n"
	"def test_side_effect():\n"
	"\tdef _execute(_query, *, limit=0):\n"
	"\t\treturn []\n\n"
	"\tport = MagicMock()\n"
	"\tport.execute.side_effect = _execute\n"
	"\tassert port.execute('q') == []\n\n\n"
	"def test_unrelated():\n"
	"\tclass Cursor:\n"
	"\t\tdef execute(self, sql, params=()):\n"
	"\t\t\treturn None\n\n"
	"\tsqlite3.connect(':memory:').execute('select 1')\n"
)


def _files(root: Path) -> dict[str, str]:
	return {"src/pkg/__init__.py": "", "src/pkg/port.py": PORT, "tests/test_port.py": TEST}


def test_given_fakes_and_an_unrelated_method_then_only_the_fakes_are_found(tmp_path):
	for rel, text in _files(tmp_path).items():
		(tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
		(tmp_path / rel).write_text(text)
	port = (tmp_path / "src/pkg/port.py").resolve()
	found = find_stand_ins(
		[tmp_path / rel for rel in _files(tmp_path)],
		tmp_path,
		name="execute",
		target_params=["self", "query", "limit"],
		changed={(port, 5), (port, 9)},
	)
	assert [(item.path.name, item.label) for item in found] == [
		("test_port.py", "_Fake.execute"),
		("test_port.py", "_execute (wired in as .execute, line 18)"),
	]


loop = make_loop_fixture()


@pytest.mark.integration
@needs_ty
def test_given_change_signature_passing_a_new_argument_then_the_fakes_are_named(tmp_path, monkeypatch, loop):
	project = make_workspace(tmp_path, monkeypatch, _files(tmp_path))
	text = loop.run_until_complete(
		refactor_tools.change_signature(
			name="SearchPort.execute",
			add=[{"name": "case_sensitive", "annotation": "bool", "keyword_only": True, "value": "False"}],
			apply=False,
			max_new_errors=None,
		)
	)
	assert "possible fakes with the same name and parameters" in text, text
	assert "`case_sensitive`" in text
	assert "tests/test_port.py:7  _Fake.execute" in text
	assert "tests/test_port.py:14  _execute (wired in as .execute, line 18)" in text
	assert "Cursor" not in text
	assert "case_sensitive" not in (project / "tests/test_port.py").read_text()


@pytest.mark.integration
@needs_ty
def test_given_a_change_callers_do_not_see_then_no_fakes_note(tmp_path, monkeypatch, loop):
	make_workspace(tmp_path, monkeypatch, _files(tmp_path))
	text = loop.run_until_complete(
		refactor_tools.change_signature(
			name="SearchPort.execute",
			add=[{"name": "strict", "default": "False"}],
			apply=False,
			max_new_errors=None,
		)
	)
	assert "possible fakes" not in text, text
