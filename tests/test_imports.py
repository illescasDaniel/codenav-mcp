"""Fast unit tests for `codenav_mcp.imports`."""

from __future__ import annotations

import ast

from codenav_mcp.imports import add_from_import, add_import, module_imports, remove_names, render_import


def _compiles(text: str) -> None:
	ast.parse(text)


def test_given_mixed_imports_when_module_imports_then_aliases_and_conditional_flag():
	# given
	text = "import os, sys as system\nfrom a.b import c as d, e\nif TYPE_CHECKING:\n\tfrom x import y\n"
	# when
	found = module_imports(ast.parse(text))
	# then
	assert [(s.module, s.bound_names, s.conditional) for s in found] == [
		(None, ["os", "system"], False),
		("a.b", ["d", "e"], False),
		("x", ["y"], True),
	]


def test_given_long_import_when_render_then_wrapped_with_trailing_commas():
	# given
	names = [(f"name_number_{i}", None) for i in range(12)]
	# when
	text = render_import("some.module.path", names, indent_unit="\t")
	# then
	assert text.startswith("from some.module.path import (\n\tname_number_0,\n")
	assert text.endswith("\tname_number_11,\n)\n")
	_compiles(text)


def test_given_names_when_remove_names_then_aliases_dropped_or_statement_removed():
	# given
	text = "import os\nfrom a import b, c\nfrom d import e\n\nuse(b, os)\n"
	# when
	result = remove_names(text, {"c", "e"})
	# then
	assert result == "import os\nfrom a import b\n\nuse(b, os)\n"


def test_given_multiline_import_when_remove_names_then_rewritten_compactly():
	# given
	text = "from a import (\n\tone,\n\ttwo,\n\tthree,\n)\n"
	# when / then
	assert remove_names(text, {"two"}) == "from a import one, three\n"


def test_given_existing_import_from_same_module_when_add_then_merged():
	# given
	text = "from pkg.mod import a\n\nprint(a)\n"
	# when / then
	assert add_from_import(text, "pkg.mod", ["b"]) == "from pkg.mod import a, b\n\nprint(a)\n"


def test_given_name_already_imported_when_add_then_unchanged():
	# given
	text = "from pkg.mod import a\n"
	# when / then
	assert add_from_import(text, "pkg.mod", ["a"]) == text
	assert add_from_import("from other import a\n", "pkg.mod", ["a"]) == "from other import a\n"


def test_given_imports_when_add_then_inserted_after_last_import_of_the_block():
	# given
	text = "import os\nfrom a import b\n\n\ndef f():\n\treturn 1\n"
	# when
	result = add_from_import(text, "new.mod", ["thing"])
	# then
	assert result == "import os\nfrom a import b\nfrom new.mod import thing\n\n\ndef f():\n\treturn 1\n"


def test_given_docstring_only_when_add_then_blank_line_after_docstring_and_two_before_def():
	# given
	text = '"""Doc."""\n\n\ndef f():\n\treturn 1\n'
	# when
	result = add_from_import(text, "m", ["x"])
	# then
	assert result == '"""Doc."""\n\nfrom m import x\n\n\ndef f():\n\treturn 1\n'


def test_given_no_header_when_add_then_import_goes_first_with_two_blank_lines_before_def():
	# given / when
	result = add_from_import("def f():\n\treturn 1\n", "m", ["x"])
	# then
	assert result == "from m import x\n\n\ndef f():\n\treturn 1\n"


def test_given_empty_file_when_add_then_only_the_statement():
	assert add_from_import("", "m", ["x"]) == "from m import x\n"


def test_given_relative_level_when_add_then_dots_rendered():
	# given / when
	result = add_from_import("import os\n", "sibling", ["thing"], level=1)
	# then
	assert result == "import os\nfrom .sibling import thing\n"


def test_given_type_checking_block_when_add_then_goes_after_it():
	# given
	text = "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n\tfrom x import Y\n\n\nclass A: ...\n"
	# when
	result = add_from_import(text, "m", ["z"])
	# then
	_compiles(result)
	assert result.index("from m import z") > result.index("from x import Y")


def test_given_crlf_file_when_add_then_statement_uses_crlf():
	# given / when
	result = add_from_import("import os\r\n\r\nx = 1\r\n", "m", ["z"])
	# then
	assert result == "import os\r\nfrom m import z\r\n\r\nx = 1\r\n"


def test_given_plain_import_when_add_import_then_idempotent():
	# given
	once = add_import("x = 1\n", "pkg.mod")
	# then
	assert once == "import pkg.mod\n\n\nx = 1\n" or once.startswith("import pkg.mod\n")
	assert add_import(once, "pkg.mod") == once
