"""Review G2/G4: CLI-style search queries map to the Python name; docstring prose is not an "other string"."""

from __future__ import annotations

import io
import tokenize

import pytest

from codenav_mcp.mentions import _is_prose, _python_mentions
from codenav_mcp.read_tools import _python_name_for


@pytest.mark.parametrize(
	("query", "name"),
	[
		("--max-file-size", "max_file_size"),
		("-v", "v"),
		("max-file-size", "max_file_size"),
		("max_file_size", None),
		("Cart.total", None),
		("a b", None),
		("---", None),
		("pkg.some-mod.f", "pkg.some_mod.f"),
	],
)
def test_given_a_query_then_its_python_name_reading(query, name):
	assert _python_name_for(query) == name


def _string_token(source: str) -> tokenize.TokenInfo:
	return next(t for t in tokenize.generate_tokens(io.StringIO(source).readline) if t.type == tokenize.STRING)


@pytest.mark.parametrize(
	("source", "prose"),
	[
		('"""Count the lines."""\n', True),
		("r'''lines'''\n", True),
		('x = "lines"\n', False),
		('getattr(obj, "lines")\n', False),
		('x = "Count and forward the lines here"\n', True),
	],
)
def test_given_a_string_then_prose_is_told_apart_from_data(source, prose):
	assert _is_prose(_string_token(source)) is prose


def test_given_a_docstring_and_a_key_then_only_the_key_is_data(tmp_path):
	path = tmp_path / "m.py"
	path.write_text('def f(d):\n\t"""Return the lines of d."""\n\treturn d["lines"]\n')
	found = {(m.line, m.prose) for m in _python_mentions(path, "m.py", "lines", set()) if m.kind == "string"}
	assert found == {(2, True), (3, False)}


def test_given_blocks_of_mixed_widths_then_the_indent_conversion_keeps_the_tree():
	import ast

	from codenav_mcp.pysource import convert_indent_unit

	source = "class A:\n  pass\nclass A:\n class A:\n  pass\n"
	converted = convert_indent_unit(source, "\t")
	assert ast.dump(ast.parse(converted)) == ast.dump(ast.parse(source))
	assert convert_indent_unit("def f():\n    if x:\n        return 1\n", "\t") == "def f():\n\tif x:\n\t\treturn 1\n"
