"""Source transforms on generated modules (hypothesmith): the output still parses and unrelated code is unchanged.

Heavy (hypothesmith builds Python from the grammar / a syntax tree), so this module runs only with `-m fuzz`.
The generators are built inside the tests, never at import time.
"""

from __future__ import annotations

import ast
import difflib
from pathlib import Path

import pytest
from hypothesis import assume, given, settings, strategies as st

from codenav_mcp import imports, moves, tidy
from codenav_mcp.pysource import convert_indent_unit, indent_block
from codenav_mcp.signature import SourceMap


pytestmark = pytest.mark.fuzz

hypothesmith = pytest.importorskip("hypothesmith")


def modules() -> st.SearchStrategy[str]:
	return hypothesmith.from_node() | hypothesmith.from_grammar()


def compiles(text: str) -> bool:
	try:
		compile(text, "<generated>", "exec")
	except (SyntaxError, ValueError, RecursionError, MemoryError):
		return False
	return True


def usable(text: str) -> bool:
	# FINDINGS-h3 #11: a form feed at the start of an indented line breaks indent_block/convert_indent_unit
	return compiles(text) and "\x0c" not in text


def without_docstrings(tree: ast.Module) -> ast.Module:
	"""The tree with module/class/function docstrings blanked: indentation changes may touch their inner whitespace."""
	for node in ast.walk(tree):
		if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
			first = node.body[0]
			if (
				isinstance(first, ast.Expr)
				and isinstance(first.value, ast.Constant)
				and isinstance(first.value.value, str)
			):
				first.value.value = ""
	return tree


def dump(text: str) -> str:
	return ast.dump(without_docstrings(ast.parse(text)))


# -- indentation helpers ------------------------------------------------------------------------------------------


@settings(max_examples=150)
@given(modules(), st.sampled_from(["\t", "    ", "  ", " "]))
def test_given_a_module_then_converting_its_indent_unit_keeps_the_syntax_tree(source, unit):
	assume(usable(source))
	converted = convert_indent_unit(source, unit)
	assert compiles(converted), f"{source!r}\n--->\n{converted!r}"
	assert dump(converted) == dump(source)


@settings(max_examples=150)
@given(modules(), st.sampled_from(["\t", "    ", "  "]))
def test_given_a_module_then_indenting_it_under_a_block_keeps_every_statement(source, indent):
	assume(usable(source))
	assume(ast.parse(source).body)
	indented = indent_block(source, indent)
	wrapped = "if 1:\n" + indented
	assert compiles(wrapped), f"{source!r}\n--->\n{indented!r}"
	original = without_docstrings(ast.parse(source)).body
	block = without_docstrings(ast.parse(wrapped)).body[0]
	assert isinstance(block, ast.If)
	inner = block.body
	assert [ast.dump(stmt) for stmt in inner] == [ast.dump(stmt) for stmt in original]


# -- offsets ---------------------------------------------------------------------------------------------------------


@settings(max_examples=150)
@given(modules())
def test_given_a_module_then_node_offsets_stay_inside_the_text_and_cut_out_the_node_source(source):
	assume(usable(source))
	smap = SourceMap(source)
	for node in ast.walk(ast.parse(source)):
		if isinstance(node, (ast.expr, ast.stmt)) and getattr(node, "end_lineno", None):
			start, end = smap.start(node), smap.end(node)
			assert 0 <= start <= end <= len(source)
			assert smap.segment(node) == ast.get_source_segment(source, node)


# -- imports -----------------------------------------------------------------------------------------------------------


@st.composite
def modules_with_imports(draw: st.DrawFn) -> str:
	lines = draw(
		st.lists(
			st.sampled_from(
				[
					"import os",
					"import sys as system",
					"from a.b import c, d as e",
					"from . import sibling",
					"from m import (\n\tx,\n\ty,\n)",
					"import json  # keep",
				]
			),
			max_size=4,
			unique=True,
		)
	)
	header = draw(st.sampled_from(["", '"""Doc."""\n', "#!/usr/bin/env python\n", "# -*- coding: utf-8 -*-\n"]))
	body = draw(modules())
	return header + "\n".join(lines) + ("\n" if lines else "") + body


def _non_import_dumps(text: str) -> list[str]:
	return [
		ast.dump(stmt)
		for stmt in without_docstrings(ast.parse(text)).body
		if not isinstance(stmt, (ast.Import, ast.ImportFrom))
	]


@settings(max_examples=150)
@given(modules_with_imports(), st.sampled_from(["zz_mod", "pkg.zz_mod"]), st.one_of(st.none(), st.just("zz")))
def test_given_a_module_then_add_import_parses_adds_it_once_and_leaves_other_statements_alone(source, module, asname):
	assume(usable(source))
	result = imports.add_import(source, module, asname)
	assert compiles(result), f"{source!r}\n--->\n{result!r}"
	assert _non_import_dumps(result) == _non_import_dumps(source)
	assert imports.add_import(result, module, asname) == result, "adding the same import twice must change nothing"
	bound = [
		(a, b)
		for s in imports.module_imports(ast.parse(result))
		if not s.is_from and not s.conditional
		for a, b in s.aliases
	]
	assert (module, asname) in bound


@settings(max_examples=150)
@given(modules_with_imports(), st.sampled_from(["Foo", "c", "os"]))
def test_given_a_module_then_add_from_import_parses_and_binds_the_name_without_touching_other_statements(source, name):
	assume(usable(source))
	result = imports.add_from_import(source, "zz_mod", [name])
	assert compiles(result), f"{source!r}\n--->\n{result!r}"
	assert _non_import_dumps(result) == _non_import_dumps(source)
	bound = {b for s in imports.module_imports(ast.parse(result)) if not s.conditional for b in s.bound_names}
	assert name in bound
	assert imports.add_from_import(result, "zz_mod", [name]) == result


@settings(max_examples=150)
@given(modules_with_imports(), st.sets(st.sampled_from(["os", "system", "c", "e", "x", "json", "sibling"]), min_size=1))
def test_given_a_module_then_remove_names_parses_drops_exactly_those_names_and_keeps_other_code(source, names):
	assume(usable(source))
	before = ast.parse(source)
	result = imports.remove_names(source, names)
	assert compiles(result), f"{source!r}\n--->\n{result!r}"
	after = imports.module_imports(ast.parse(result))
	assert not {b for s in after if True for b in s.bound_names} & names or any(
		s.conditional for s in imports.module_imports(before)
	)
	kept_before = [b for s in imports.module_imports(before) for b in s.bound_names if b not in names]
	assert [b for s in after for b in s.bound_names] == kept_before
	assert _non_import_dumps(result) == _non_import_dumps(source)


# -- moves: __future__ and __all__ ---------------------------------------------------------------------------------


@settings(max_examples=150)
@given(
	# FINDINGS-h3 #9: a shebang or an encoding line is not a valid header here (see the pinned tests in test_all_entry_props)
	st.sampled_from(["", '"""Doc."""\n', "# comment\n"]),
	modules(),
)
def test_given_a_module_then_adding_the_future_annotations_import_keeps_it_valid_and_everything_else_as_it_was(
	header, body
):
	source = header + body
	assume(usable(source))
	result = moves._with_future_annotations(source)
	assert compiles(result), f"{source!r}\n--->\n{result!r}"
	assert result.count("from __future__ import annotations") >= 1
	original_lines = [line for line in source.splitlines() if line.strip()]
	new_lines = [line for line in result.splitlines() if line.strip()]
	if "from __future__ import annotations" in source:
		assert result == source
		return
	added = [
		line
		for tag, _, _, j1, j2 in difflib.SequenceMatcher(None, original_lines, new_lines, autojunk=False).get_opcodes()
		if tag != "equal"
		for line in new_lines[j1:j2]
	]
	assert added == ["from __future__ import annotations"], f"{source!r}\n--->\n{result!r}"


# -- tidy ------------------------------------------------------------------------------------------------------------


@settings(max_examples=60)
@given(source=modules_with_imports())
def test_given_an_edited_module_then_sorting_imports_keeps_every_non_import_line(source, tmp_path_factory):
	assume(usable(source))
	edited = (
		"import zz_added\n" + source if not source.startswith(("#!", "#", '"""')) else source + "\nimport zz_added\n"
	)
	assume(usable(edited))
	workspace = Path(tmp_path_factory.getbasetemp())
	result = tidy.sort_imports(workspace, source, edited, "m.py")
	assert compiles(result), f"{edited!r}\n--->\n{result!r}"

	def code_lines(text: str) -> list[str]:
		return sorted(line.strip() for line in text.splitlines() if line.strip() and "import" not in line)

	assert code_lines(result) == code_lines(edited)
