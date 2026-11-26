"""D2: call rewriting changes only what it must; multi-line layout is kept (FINDINGS-h3 observations, deep tester)."""

from __future__ import annotations

import pytest

from codenav_mcp.signature import Change

from .property.sigcases import parses, rewrite


DEF = "def f(a, b=1):\n\treturn dict(locals())\n"
DEF2 = "def f(a, b):\n\treturn dict(locals())\n"
NEW = {"name": "n", "value": 9, "keyword_only": True}


def _call(statement: str, change: Change, definition: str = DEF) -> str | None:
	return rewrite(definition, change, statement, method=False, qualname="f").new_statement


@pytest.mark.parametrize(
	("statement", "expected"),
	[
		("result = f(\n\t1,\n\t2,\n)", "result = f(\n\t1,\n\t2,\n\tn=9,\n)"),
		("result = f(\n\t1,\n\t2\n)", "result = f(\n\t1,\n\t2,\n\tn=9\n)"),
		("result = f(\n\t1,\n)", "result = f(\n\t1,\n\tn=9,\n)"),
		("result = f(1, 2)", "result = f(1, 2, n=9)"),
		("result = f(1,\n      2)", "result = f(1,\n      2,\n      n=9)"),
	],
)
def test_given_a_multiline_call_when_an_argument_is_added_then_it_gets_its_own_line(statement, expected):
	assert _call(statement, Change(add=[NEW])) == expected


def test_given_two_added_arguments_then_each_gets_its_own_line():
	change = Change(add=[NEW, {"name": "m", "value": 8, "keyword_only": True}])
	assert _call("result = f(\n\t1,\n\t2,\n)", change) == "result = f(\n\t1,\n\t2,\n\tn=9,\n\tm=8,\n)"


def test_given_a_continuation_line_then_a_reorder_keeps_its_indentation():
	new = _call("result = f(1,\n\t2)", Change(reorder=["b", "a"]), DEF2)
	assert new == "result = f(2,\n\t1)"


def test_given_arguments_each_on_a_line_then_a_reorder_keeps_one_per_line():
	assert _call("result = f(\n\t1,\n\t2,\n)", Change(reorder=["b", "a"]), DEF2) == "result = f(\n\t2,\n\t1,\n)"


@pytest.mark.parametrize(
	"statement", ["result = f(1,)", "result = f(\n\t1,\n)", "result = f(a=1,)", "result = f( 1 , )"]
)
def test_given_the_only_argument_removed_then_the_parentheses_are_emptied(statement):
	assert _call(statement, Change(remove=["a"])) == "result = f()"


def test_given_the_last_argument_removed_from_a_multiline_call_then_the_rest_keeps_its_layout():
	new = _call("result = f(\n\t1,\n\t2,\n)", Change(remove=["b"]))
	assert new == "result = f(\n\t1,\n)"
	assert new is not None and parses(new)
