"""Minimal, deterministic repros of what the signature properties found (hardening/FINDINGS-h3.md).

Each is `xfail(strict=True)`: when the product bug is fixed the test flips, and the xfail marker goes."""

from __future__ import annotations

import pytest
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp.signature import Change

from .sigcases import parses, rewrite


DEF = "def f(a, b=1):\n\treturn dict(locals())\n"


def _call(statement: str, change: Change) -> str | None:
	return rewrite(DEF, change, statement, method=False, qualname="f").new_statement


@pytest.mark.xfail(strict=True, reason="FINDINGS-h3 #1")
@pytest.mark.parametrize("statement", ["result = f(1,)", "result = f(\n\t1,\n)", "result = f(a=1,)"])
def test_given_the_only_argument_removed_from_a_call_with_a_trailing_comma_then_the_call_still_parses(statement):
	try:
		new = _call(statement, Change(remove=["a"]))
	except ToolInputError:
		return
	assert new is not None and parses(new), new


@pytest.mark.xfail(strict=True, reason="FINDINGS-h3 #2")
def test_given_keyword_arguments_out_of_parameter_order_then_adding_a_parameter_leaves_them_where_they_are():
	new = _call("result = f(b=2, a=1)", Change(add=[{"name": "n", "value": 9, "keyword_only": True}]))
	assert new == "result = f(b=2, a=1, n=9)"


@pytest.mark.xfail(strict=True, reason="FINDINGS-h3 #3")
def test_given_a_keyword_argument_written_with_spaces_then_a_change_elsewhere_keeps_its_spelling():
	new = _call("result = f(1, b = 2)", Change(add=[{"name": "n", "value": 9, "keyword_only": True}]))
	assert new == "result = f(1, b = 2, n=9)"
