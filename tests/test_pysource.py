"""Fast unit tests for `codenav_mcp.pysource`."""

from __future__ import annotations

import ast

import pytest

from codenav_mcp.pysource import (
	PythonSyntaxError,
	check_syntax,
	convert_indent_unit,
	definitions_in,
	detect_indent_unit,
	find_definition,
	fit_snippet,
	indent_block,
	leading_comment_start,
	loaded_names,
	names_used_outside,
	parse_python,
	source_lines,
	top_level_names,
)


_SOURCE = '''\
import os


@decorator
def top(a):
	"""Doc."""
	return a


class Box:
	value = 1

	@property
	def size(self):
		return 1

	async def run(self):
		return await other()

	class Inner:
		def deep(self):
			pass
'''


def test_given_nested_source_when_definitions_then_qualnames_kinds_and_spans():
	# given / when
	found = {d.qualname: d for d in definitions_in(_SOURCE)}
	# then
	assert list(found) == ["top", "Box", "Box.size", "Box.run", "Box.Inner", "Box.Inner.deep"]
	assert (found["top"].first_line, found["top"].def_line, found["top"].last_line) == (4, 5, 7)
	assert found["top"].kind == "function" and found["Box.size"].kind == "method" and found["Box"].kind == "class"
	assert found["Box.size"].first_line == 13 and found["Box.size"].indent == "\t"
	assert found["Box.Inner.deep"].indent == "\t\t"


def test_given_name_line_when_find_definition_then_matches_def_line_not_decorator():
	# given / when
	by_def_line = find_definition(_SOURCE, name_line=5)
	by_decorator_line = find_definition(_SOURCE, name_line=4)
	by_qualname = find_definition(_SOURCE, qualname="Box.run")
	# then
	assert by_def_line is not None and by_def_line.name == "top"
	assert by_decorator_line is None
	assert by_qualname is not None and by_qualname.node.name == "run"


def test_given_definitions_in_if_and_try_when_iterating_then_found():
	# given
	text = "if True:\n\tdef a(): ...\ntry:\n\tclass B: ...\nexcept Exception:\n\tdef c(): ...\n"
	# when / then
	assert [d.qualname for d in definitions_in(text)] == ["a", "B", "c"]


def test_given_invalid_python_when_parse_then_syntax_error_with_line():
	with pytest.raises(PythonSyntaxError, match=r"x\.py: syntax error at line 2"):
		parse_python("a = 1\ndef (:\n", "x.py")
	with pytest.raises(PythonSyntaxError):
		check_syntax("print(\n", "y.py")


def test_given_comment_block_above_when_leading_comment_start_then_includes_it():
	# given
	lines = source_lines("x = 1\n\n# one\n# two\ndef f(): ...\n")
	# when / then
	assert leading_comment_start(lines, 5) == 3
	assert leading_comment_start(lines, 1) == 1


@pytest.mark.parametrize(
	("text", "unit"),
	[
		("def f():\n\treturn 1\n", "\t"),
		("def f():\n    return 1\n", "    "),
		("def f():\n  return 1\n", "  "),
		("x = 1\n", "    "),
	],
)
def test_given_files_when_detect_indent_unit_then_first_indent_wins(text, unit):
	assert detect_indent_unit(text) == unit


def test_given_spaces_snippet_when_converted_to_tabs_then_depth_preserved():
	# given
	snippet = "def f():\n    if x:\n        return 1\n    return 2\n"
	# when
	converted = convert_indent_unit(snippet, "\t")
	# then
	assert converted == "def f():\n\tif x:\n\t\treturn 1\n\treturn 2\n"


def test_given_multiline_string_when_indent_block_then_non_docstring_string_left_alone():
	# given
	snippet = 'def f():\n\t"""Doc\n\tmore"""\n\ttext = """a\nb"""\n\treturn text\n'
	# when
	indented = indent_block(snippet, "\t")
	# then — the docstring continuation is indented, the data string's second line is not
	assert indented == '\tdef f():\n\t\t"""Doc\n\t\tmore"""\n\t\ttext = """a\nb"""\n\t\treturn text\n'


def test_given_snippet_for_method_when_fit_snippet_then_dedented_converted_and_indented():
	# given — spaces snippet pasted with arbitrary base indentation, into a tab-indented class
	snippet = "        def go(self):\n            return 1\n"
	# when
	fitted = fit_snippet(snippet, indent="\t", file_text="class A:\n\tx = 1\n")
	# then
	assert fitted == "\tdef go(self):\n\t\treturn 1\n"
	ast.parse("class A:\n" + fitted)


def test_given_module_when_top_level_names_then_defs_imports_and_assignments():
	# given
	tree = ast.parse(
		"import os.path\nfrom a import b as c\nX: int = 1\ndef f(): ...\nclass K: ...\nif True:\n\tY = 2\n"
	)
	# when / then
	assert top_level_names(tree) == {"os", "c", "X", "f", "K", "Y"}


def test_given_function_when_loaded_names_then_free_variables_only():
	# given
	node = ast.parse("def f(a):\n\tb = a + CONST\n\treturn os.path.join(helper(b), c) + [z for z in range(3)]\n").body[
		0
	]
	# when / then
	assert loaded_names(node) == {"CONST", "os", "helper", "c", "range"}


def test_given_symbol_span_when_names_used_outside_then_ignores_span_and_imports():
	# given
	text = "from m import helper\n\n\ndef moved():\n\treturn helper()\n\n\ndef stay():\n\treturn other\n\n\n__all__ = ['moved']\n"
	tree = ast.parse(text)
	# when
	used = names_used_outside(tree, 4, 5)
	# then — `helper` is only used inside the span; `moved` is mentioned in __all__
	assert "helper" not in used
	assert {"other", "moved"} <= used
