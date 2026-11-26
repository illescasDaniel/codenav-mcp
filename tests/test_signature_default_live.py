"""change_signature: an added parameter with a default and no `position` is appended keyword-only (live ty)."""

from __future__ import annotations

import pytest

from codenav_mcp import refactor_tools, server

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/search.py": "def find(root: str, query: str, limit: int = 10) -> list[str]:\n\treturn [root, query][:limit]\n",
	"src/pkg/use.py": (
		"from pkg.search import find\n\n\ndef go() -> list[str]:\n\treturn find('a', 'b', 3)\n\n\nHANDLER = find\n"
	),
}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	root = make_workspace(tmp_path, monkeypatch, _FILES)
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))
	return root


def test_given_default_without_position_then_only_the_signature_is_written_even_if_used_as_a_value(project, loop):
	# when: `HANDLER = find` is a use as a value, which would hold back a plan that rewrites callers
	text = loop.run_until_complete(
		refactor_tools.change_signature(name="find", add=[{"name": "exact", "annotation": "bool", "default": "False"}])
	)
	# then
	assert "Applied as edit" in text and "needs manual attention" not in text
	assert "limit: int = 10, *, exact: bool = False" in (project / "src/pkg/search.py").read_text()
	assert (project / "src/pkg/use.py").read_text() == _FILES["src/pkg/use.py"]


def test_given_explicit_position_then_callers_matter_and_the_value_use_holds_the_plan_back(project, loop):
	text = loop.run_until_complete(
		refactor_tools.change_signature(
			name="find", add=[{"name": "exact", "default": "False", "value": "True", "position": 2}]
		)
	)
	assert "NOT applied" in text and "used as a value, not called" in text
	assert (project / "src/pkg/search.py").read_text() == _FILES["src/pkg/search.py"]


def test_given_keyword_only_false_then_callers_matter_and_the_value_use_holds_the_plan_back(project, loop):
	text = loop.run_until_complete(
		refactor_tools.change_signature(
			name="find", add=[{"name": "exact", "default": "False", "keyword_only": False}], apply=False
		)
	)
	assert "+def find(root: str, query: str, limit: int = 10, exact: bool = False)" in text
	assert "used as a value, not called" in text
