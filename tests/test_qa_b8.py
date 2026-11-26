from __future__ import annotations

import ast

import pytest
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp import refactor_tools
from codenav_mcp.refactor_tools import _validate_params
from codenav_mcp.signature import (
	NORMAL,
	POSONLY,
	CallArg,
	Change,
	ManualCallError,
	Param,
	Signature,
	SourceMap,
	call_edit,
	new_params,
	rebind_call,
	tokenize_source,
)

from .live_support import make_loop_fixture, make_workspace, needs_ty


def test_rc_021_effectful_reorder_becomes_keywords_in_the_original_order():
	args = [CallArg("next(it)", None, "next(it)", 0, 8), CallArg("next(it)", None, "next(it)", 10, 18)]
	result = rebind_call(
		args, [Param("a", NORMAL), Param("b", NORMAL)], [Param("b", NORMAL), Param("a", NORMAL)], {}, set()
	)
	assert result == ["a=next(it)", "b=next(it)"]  # same evaluation order, no positional shuffle


def test_effectful_reorder_of_positional_only_parameter_is_manual():
	args = [CallArg("g()", None, "g()", 0, 3), CallArg("h()", None, "h()", 5, 8)]
	old = [Param("a", POSONLY), Param("b", POSONLY)]
	with pytest.raises(ManualCallError, match="positional-only"):
		rebind_call(args, old, [old[1], old[0]], {}, set())


def test_effectful_reorder_with_extra_positional_is_manual():
	args = [CallArg("g()", None, "g()", 0, 3), CallArg("h()", None, "h()", 5, 8), CallArg("k()", None, "k()", 10, 13)]
	with pytest.raises(ManualCallError, match="captured by"):
		rebind_call(args, [Param("a", NORMAL), Param("b", NORMAL)], [Param("b", NORMAL), Param("a", NORMAL)], {}, set())


def test_given_added_keyword_value_when_arguments_are_effectful_then_the_call_is_rewritten_in_order():
	# `f(root / "a", "q", flag=False)` + a new keyword argument: nothing the call evaluates moves or disappears
	args = [
		CallArg('root / "a"', None, 'root / "a"', 0, 10),
		CallArg('"q"', None, '"q"', 12, 15),
		CallArg("flag=False", "flag", "False", 17, 27),
	]
	old = [Param("path", NORMAL), Param("query", NORMAL)]
	new = [*old, Param("case_sensitive", NORMAL, "bool", "False")]
	result = rebind_call(args, old, new, {"case_sensitive": "False"}, set())
	assert result == ['root / "a"', '"q"', "case_sensitive=False", "flag=False"]


def test_given_effectful_argument_dropped_or_moved_when_rebinding_then_still_manual():
	call = CallArg("g()", None, "g()", 0, 3)
	old = [Param("a", NORMAL), Param("b", NORMAL)]
	with pytest.raises(ManualCallError, match="effectful"):
		rebind_call([call], old, [old[1]], {}, {"a"})


def test_rc_062_comments_are_manual():
	text = "f(a, # comment\n b)"
	smap = SourceMap(text)
	statement = ast.parse(text).body[0]
	assert isinstance(statement, ast.Expr)
	call = statement.value
	assert isinstance(call, ast.Call)
	from codenav_mcp.signature import read_call_args

	with pytest.raises(ManualCallError, match="comments"):
		call_edit(smap, tokenize_source(text), call, read_call_args(smap, call), ["b", "a"])


def test_rc_119_parameter_error():
	with pytest.raises(ToolInputError, match="the new parameter list is not valid Python"):
		_validate_params("a=1, b", "f")


def test_rc_121_unknown_key():
	with pytest.raises(ToolInputError, match="unknown key"):
		new_params(Signature([], False, False, (0, 0)), Change(add=[{"nam": "x"}]), "f")


loop = make_loop_fixture()


def _run_case(tmp_path, monkeypatch, loop, files, **kwargs):
	project = make_workspace(tmp_path, monkeypatch, files)
	result = loop.run_until_complete(refactor_tools.change_signature(apply=True, max_new_errors=None, **kwargs))
	return project, result


@needs_ty
def test_rc_022_constructor_references(tmp_path, monkeypatch, loop):
	project, result = _run_case(
		tmp_path,
		monkeypatch,
		loop,
		{"lib.py": "class Cart:\n def __init__(self, items): pass\n", "use.py": "from lib import Cart\nCart([])\n"},
		name="Cart.__init__",
		file_path="lib.py",
		add=[{"name": "cap", "default": "None", "value": "10"}],
	)
	assert "cap=10" in (project / "use.py").read_text(), result


@needs_ty
def test_rc_023_alias_is_accounted_for(tmp_path, monkeypatch, loop):
	project, result = _run_case(
		tmp_path,
		monkeypatch,
		loop,
		{"lib.py": "def send(x): pass\n", "use.py": "from lib import send as s\ns('a')\n"},
		name="send",
		file_path="lib.py",
		add=[{"name": "n", "default": "0", "value": "1"}],
	)
	assert "n=1" in (project / "use.py").read_text() or ("imported alias" in result and "use.py:2" in result)


@needs_ty
def test_rc_120_decorator_note(tmp_path, monkeypatch, loop):
	_, result = _run_case(
		tmp_path,
		monkeypatch,
		loop,
		{"lib.py": "from functools import cache\n@cache\ndef cached(x): return x\n"},
		name="cached",
		file_path="lib.py",
		add=[{"name": "y", "default": "0", "value": "0"}],
	)
	assert (
		"Note: cached is decorated with @cache; callers of the decorated object may have a different signature."
		in result
	)


@needs_ty
def test_rc_122_docstring_note(tmp_path, monkeypatch, loop):
	_, result = _run_case(
		tmp_path,
		monkeypatch,
		loop,
		{"lib.py": 'def send(to, cc):\n """:param cc: copy"""\n return to\n'},
		name="send",
		file_path="lib.py",
		remove=["cc"],
	)
	assert "Note: docstring of send still mentions parameter(s) 'cc'; update it manually." in result


def test_effectful_keyword_reorder_is_manual():
	args = [CallArg("a=g()", "a", "g()", 0, 5), CallArg("b=h()", "b", "h()", 7, 12)]
	with pytest.raises(ManualCallError, match="effectful"):
		rebind_call(args, [Param("a", NORMAL), Param("b", NORMAL)], [Param("b", NORMAL), Param("a", NORMAL)], {}, set())


def test_removed_effectful_argument_is_manual():
	args = [CallArg("g()", None, "g()", 0, 3)]
	with pytest.raises(ManualCallError, match="effectful"):
		rebind_call(args, [Param("a", NORMAL)], [], {}, {"a"})


def test_keyword_order_preserved_by_manual_refusal():
	args = [CallArg("b=h()", "b", "h()", 0, 5), CallArg("a=g()", "a", "g()", 7, 12)]
	with pytest.raises(ManualCallError, match="effectful"):
		rebind_call(args, [Param("a", NORMAL), Param("b", NORMAL)], [Param("a", NORMAL), Param("b", NORMAL)], {}, set())


def test_parameter_nul_returns_input_error():
	with pytest.raises(ToolInputError, match="null"):
		_validate_params("x=\x00", "f")


@pytest.mark.parametrize("name", ["Cart", "Cart.__init__"])
@needs_ty
def test_constructor_alias_is_accounted_for(tmp_path, monkeypatch, loop, name):
	project, result = _run_case(
		tmp_path,
		monkeypatch,
		loop,
		{"lib.py": "class Cart:\n def __init__(self, items): pass\n", "use.py": "from lib import Cart as C\nC([])\n"},
		name=name,
		file_path="lib.py",
		add=[{"name": "cap", "default": "None", "value": "10"}],
	)
	assert "cap=10" in (project / "use.py").read_text() or ("imported alias" in result and "use.py:2" in result)
