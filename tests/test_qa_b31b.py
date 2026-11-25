"""QA block B31b regression tests (live, real `ty`): NUL-text files refuse cleanly, module-level cycles of 3+ modules."""

from __future__ import annotations

import pytest

from codenav_mcp import edit_tools, moves, refactor_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

NUL_REFUSAL = "contains a NUL byte; not a text file"
# a NUL after an indented block: the tokenizer raised SystemError here (escaped the old guard)
NUL_TEXT = "class A:\n\tdef f(self):\n\t\treturn 1\n\x00"
TARGET = "def foo(a: int) -> int:\n\treturn a\n\n\ndef f() -> int:\n\treturn 1\n"


def _run(loop, coro) -> str:
	return loop.run_until_complete(coro)


def test_bug_lang_r01_change_signature_with_nul_text_file_refuses_cleanly(tmp_path, monkeypatch, loop):
	make_workspace(tmp_path, monkeypatch, {"nul.py": NUL_TEXT, "target.py": TARGET})
	text = _run(
		loop,
		refactor_tools.change_signature(
			name="foo", file_path="target.py", add=[{"name": "p", "default": "0"}], apply=True
		),
	)
	assert NUL_REFUSAL in text and "Error executing tool" not in text and "Applied" not in text
	assert (tmp_path / "target.py").read_text() == TARGET
	# the server stays responsive: a later write in the same session still works
	after = _run(
		loop,
		edit_tools.edit_symbol(
			action="replace", name="f", file_path="target.py", source="def f() -> int:\n\treturn 2\n", apply=True
		),
	)
	assert "Applied" in after


def test_bug_lang_r02_edit_symbol_delete_with_nul_text_file_refuses_cleanly(tmp_path, monkeypatch, loop):
	make_workspace(tmp_path, monkeypatch, {"nul.py": NUL_TEXT, "target.py": TARGET})
	text = _run(loop, edit_tools.edit_symbol(action="delete", name="f", file_path="target.py", apply=True, force=True))
	assert NUL_REFUSAL in text and "Error executing tool" not in text and "Applied" not in text
	assert (tmp_path / "target.py").read_text() == TARGET


_STATIC_CYCLE = {
	"pkg/__init__.py": "",
	"pkg/a.py": "from pkg.src import g\n\n\ndef ga() -> int:\n\treturn g()\n",
	"pkg/dest.py": "from pkg.a import ga\n\n\ndef d() -> int:\n\treturn ga()\n",
	"pkg/src.py": "def f() -> int:\n\treturn 1\n\n\ndef g() -> int:\n\treturn f()\n",
}


def _move_f(loop, **kwargs) -> str:
	return _run(loop, refactor_tools._move_symbol(name="f", file_path="pkg/src.py", to_file="pkg/dest.py", **kwargs))


def test_bug_deps_d3a_three_module_static_cycle_is_refused_and_named(tmp_path, monkeypatch, loop):
	root = make_workspace(tmp_path, monkeypatch, _STATIC_CYCLE)
	text = _move_f(loop, apply=True, max_new_errors=None)
	assert "creates an import cycle (pkg.dest -> pkg.a -> pkg.src -> pkg.dest)" in text
	assert "pass allow_import_cycle=true to move anyway" in text
	assert "Applied" not in text
	assert (root / "pkg/src.py").read_text() == _STATIC_CYCLE["pkg/src.py"]
	assert (root / "pkg/dest.py").read_text() == _STATIC_CYCLE["pkg/dest.py"]


def test_bug_deps_d3a_three_module_static_cycle_applies_with_override(tmp_path, monkeypatch, loop):
	make_workspace(tmp_path, monkeypatch, _STATIC_CYCLE)
	text = _move_f(loop, apply=True, max_new_errors=None, allow_import_cycle=True)
	assert "Applied as edit" in text and "import cycle" not in text


def test_bug_deps_d3a_cycle_search_cap_hit_never_refuses(tmp_path, monkeypatch, loop):
	make_workspace(tmp_path, monkeypatch, _STATIC_CYCLE)
	monkeypatch.setattr(moves, "_CYCLE_SEARCH_LIMIT", 1)  # the search stops before it reaches the cycle
	text = _move_f(loop, apply=True, max_new_errors=None)
	assert "Applied as edit" in text and "creates an import cycle" not in text


def test_bug_deps_d3a_lazy_three_module_cycle_is_noted_not_refused(tmp_path, monkeypatch, loop):
	files = {
		**_STATIC_CYCLE,
		"pkg/a.py": "def ga() -> int:\n\tfrom pkg.src import g\n\n\treturn g()\n",
	}
	make_workspace(tmp_path, monkeypatch, files)
	text = _move_f(loop, apply=True, max_new_errors=None)
	assert "Applied as edit" in text and "creates an import cycle" not in text
	assert (
		"Note: the import cycle pkg.dest -> pkg.a -> pkg.src -> pkg.dest goes only through function-level imports"
		in text
	)
