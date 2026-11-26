"""`__all__` editing in `moves.py` (`_drop_all_entry`, `_add_all_entry`) on many layouts of the literal."""

from __future__ import annotations

import ast

import pytest
from hypothesis import assume, given, strategies as st

from codenav_mcp import moves
from codenav_mcp.pysource import indent_block


NAMES = ["a", "b", "c", "d"]


@st.composite
def all_sources(draw: st.DrawFn) -> tuple[str, list[str]]:
	"""A module with an `__all__` literal in a randomly laid out list/tuple, plus the names it lists."""
	names = draw(st.lists(st.sampled_from(NAMES), unique=True, max_size=4))
	kind = draw(st.sampled_from(["list", "paren", "bare"]))
	multiline = draw(st.booleans())
	trailing = draw(st.booleans())
	comments = draw(st.booleans())
	quote = draw(st.sampled_from(['"', "'"]))
	items = [f"{quote}{n}{quote}" for n in names]
	if kind == "bare":
		body = ", ".join(items) + ("," if items and (trailing or len(items) == 1) else "")
		if not items:
			body = "()"
		text = f"__all__ = {body}\n"
	else:
		open_, close = ("[", "]") if kind == "list" else ("(", ")")
		if multiline and items:
			rows = [f"\t{item}," + ("  # why" if comments and i == 0 else "") for i, item in enumerate(items)]
			if not trailing and not (kind == "paren" and len(items) == 1):
				rows[-1] = rows[-1].replace(",", "", 1)
			text = f"__all__ = {open_}\n" + "\n".join(rows) + f"\n{close}\n"
		else:
			joined = ", ".join(items) + ("," if items and (trailing or (kind == "paren" and len(items) == 1)) else "")
			text = f"__all__ = {open_}{joined}{close}\n"
	return "import os\n\n" + text + "\n\ndef a():\n\treturn os\n", names


def _all_became_a_string(text: str) -> bool:
	return any(
		isinstance(stmt, ast.Assign)
		and any(isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets)
		and isinstance(stmt.value, ast.Constant)
		for stmt in ast.parse(text).body
	)


def listed(text: str) -> list[str]:
	return sorted(moves._all_names(ast.parse(text)))


@given(all_sources(), st.sampled_from(NAMES + ["zz"]))
def test_given_any_all_layout_then_dropping_an_entry_keeps_the_module_valid_and_removes_only_that_name(case, name):
	source, names = case
	# FINDINGS-h3 #8: dropping the only entry of a parenthesis-less tuple leaves `__all__ = ` (a syntax error)
	assume(not (source.count("__all__ = ") and '__all__ = "' in source.replace("'", '"') and names == [name]))
	result = moves._drop_all_entry(source, name)
	ast.parse(result)  # must stay valid Python
	# FINDINGS-h3 #10: dropping the first of two entries of a tuple leaves `("b")`, a plain string
	assume(not _all_became_a_string(result))
	assert listed(result) == sorted(set(names) - {name}), f"{source!r}\n--->\n{result!r}"
	assert result.count("def a():") == 1 and "import os" in result


@given(all_sources(), st.sampled_from(["zz", "yy"]))
def test_given_any_all_layout_then_adding_an_entry_keeps_the_module_valid_and_adds_exactly_that_name(case, name):
	source, names = case
	assume(names)  # an empty tuple is left alone on purpose
	result = moves._add_all_entry(source, name)
	ast.parse(result)
	assert listed(result) == sorted({*names, name}), f"{source!r}\n--->\n{result!r}"


@given(all_sources(), st.sampled_from(NAMES))
def test_given_an_entry_that_is_added_and_dropped_then_the_listed_names_are_as_before(case, name):
	source, names = case
	assume(names)
	added = moves._add_all_entry(source, "zz")
	back = moves._drop_all_entry(added, "zz")
	ast.parse(back)
	assert listed(back) == sorted(names)


@pytest.mark.xfail(strict=True, reason="FINDINGS-h3 #8")
def test_given_a_bare_tuple_all_with_one_entry_then_dropping_that_entry_keeps_the_module_valid():
	source = 'import os\n\n__all__ = "a",\n\n\ndef a():\n\treturn os\n'
	ast.parse(moves._drop_all_entry(source, "a"))


@pytest.mark.xfail(strict=True, reason="FINDINGS-h3 #9")
def test_given_a_shebang_then_adding_the_future_import_keeps_the_shebang_on_the_first_line():
	result = moves._with_future_annotations("#!/usr/bin/env python\nimport os\n")
	assert result.startswith("#!/usr/bin/env python\n"), result


@pytest.mark.xfail(strict=True, reason="FINDINGS-h3 #9")
def test_given_an_encoding_line_then_adding_the_future_import_keeps_it_within_the_first_two_lines():
	result = moves._with_future_annotations("# -*- coding: latin-1 -*-\nx = 1\n")
	assert "coding: latin-1" in "\n".join(result.splitlines()[:2]), result


@pytest.mark.xfail(strict=True, reason="FINDINGS-h3 #10")
def test_given_a_two_entry_tuple_all_then_dropping_the_first_entry_leaves_a_tuple_not_a_string():
	result = moves._drop_all_entry('__all__ = ("a", "b")\n', "a")
	assert moves._all_names(ast.parse(result)) == {"b"}, result


@pytest.mark.xfail(strict=True, reason="FINDINGS-h3 #11")
def test_given_a_form_feed_in_an_indented_line_then_indenting_the_block_keeps_it_valid_python():
	source = "class A:\n\x0c pass\n"
	compile(source, "<original>", "exec")
	compile("if 1:\n" + indent_block(source, "\t"), "<indented>", "exec")
