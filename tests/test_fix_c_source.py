"""Fix C3: `__all__` dropping keeps the literal's type, `__future__` goes under the shebang, form feeds survive indenting.

`all_entries.drop_all_entry` and `imports.with_future_annotations` are what `moves._drop_all_entry` and
`moves._with_future_annotations` delegate to (see hardening/NOTES-fix-C.md)."""

from __future__ import annotations

import ast

import pytest

from codenav_mcp.all_entries import drop_all_entry
from codenav_mcp.imports import add_import, with_future_annotations
from codenav_mcp.pysource import convert_indent_unit, indent_block


def _names(text: str) -> list[str] | None:
	for stmt in ast.parse(text).body:
		if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == "__all__":
			value = ast.literal_eval(stmt.value)
			return None if isinstance(value, str) else list(value)
	return None


@pytest.mark.parametrize(
	("source", "name", "expected"),
	[
		('__all__ = "a",\n', "a", "__all__ = ()\n"),
		('__all__ = ("a",)\n', "a", "__all__ = ()\n"),
		('__all__ = ("a", "b")\n', "a", '__all__ = ("b",)\n'),
		('__all__ = ("a", "b")\n', "b", '__all__ = ("a",)\n'),
		('__all__ = "a", "b"\n', "a", '__all__ = "b",\n'),
		('__all__ = "a", "b", "c"\n', "a", '__all__ = "b", "c"\n'),
		('__all__ = ["a", "b"]\n', "a", '__all__ = ["b"]\n'),
		('__all__ = (\n\t"a",\n\t"b",\n)\n', "a", '__all__ = (\n\t"b",\n)\n'),
		('__all__ = (\n\t"a",\n\t"b"\n)\n', "b", '__all__ = (\n\t"a",\n)\n'),
		('__all__ = ("a", "b")\n', "zz", '__all__ = ("a", "b")\n'),
	],
)
def test_given_an_all_literal_then_dropping_an_entry_keeps_its_type(source, name, expected):
	result = drop_all_entry(source, name)
	assert result == expected
	ast.parse(result)


def test_given_a_tuple_all_then_dropping_down_to_one_entry_still_lists_that_entry():
	result = drop_all_entry('__all__ = ("a", "b")\n', "a")
	assert _names(result) == ["b"]


@pytest.mark.parametrize(
	("source", "kept"),
	[
		("#!/usr/bin/env python\nimport os\n", "#!/usr/bin/env python\n"),
		("#!/usr/bin/env python\nx = 1\n", "#!/usr/bin/env python\n"),
		("#!/usr/bin/env python", "#!/usr/bin/env python\n"),
		("# -*- coding: latin-1 -*-\nx = 1\n", "# -*- coding: latin-1 -*-\n"),
		(
			"#!/usr/bin/env python\n# -*- coding: latin-1 -*-\nx = 1\n",
			"#!/usr/bin/env python\n# -*- coding: latin-1 -*-\n",
		),
		('#!/usr/bin/env python\n"""Doc."""\nimport os\n', '#!/usr/bin/env python\n"""Doc."""\n'),
	],
)
def test_given_a_header_then_the_future_import_goes_after_it(source, kept):
	result = with_future_annotations(source)
	assert result.startswith(kept + "from __future__ import annotations\n") or result.startswith(
		kept + "\nfrom __future__ import annotations\n"
	), result
	ast.parse(result)


def test_given_a_file_without_a_header_then_the_future_import_is_unchanged_behaviour():
	assert with_future_annotations("import os\n") == "from __future__ import annotations\n\nimport os\n"
	assert with_future_annotations('"""Doc."""\n\ndef f(): pass\n').startswith(
		'"""Doc."""\n\nfrom __future__ import annotations\n\n\n'
	)
	assert with_future_annotations("from __future__ import annotations\nx = 1\n") == (
		"from __future__ import annotations\nx = 1\n"
	)


def test_given_a_shebang_then_add_import_still_goes_under_it():
	assert add_import("#!/usr/bin/env python\nx = 1\n", "os").startswith("#!/usr/bin/env python\n")


@pytest.mark.parametrize("indent", ["\t", "    ", "\t\t"])
def test_given_a_form_feed_at_the_start_of_an_indented_line_then_indent_block_keeps_it_valid(indent):
	source = "class A:\n\x0c pass\n\n\x0c\ndef g():\n\x0c\treturn 1\n"
	compile(source, "<original>", "exec")
	compile("if 1:\n" + indent_block(source, indent), "<indented>", "exec")


def test_given_a_form_feed_in_an_indented_line_then_convert_indent_unit_keeps_it_valid():
	source = "class A:\n    def f(self):\n\x0c        return 1\n"
	compile(source, "<original>", "exec")
	converted = convert_indent_unit(source, "\t")
	compile(converted, "<converted>", "exec")
	assert converted == "class A:\n\tdef f(self):\n\x0c\t\treturn 1\n"


def test_given_imports_follow_then_the_future_import_is_set_off_by_a_blank_line():
	assert with_future_annotations("import os\n") == "from __future__ import annotations\n\nimport os\n"
	assert with_future_annotations("from __future__ import division\nx = 1\n") == (
		"from __future__ import annotations\nfrom __future__ import division\nx = 1\n"
	)
