"""B24: RC-001 residual: form-feed (U+000C) lines through symbol_info / edit_symbol / move / import sorting."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from codenav_mcp import edit_tools, read_tools, refactor_tools, tidy
from codenav_mcp.pysource import find_definition, source_lines

from .live_support import make_loop_fixture, make_workspace, needs_ty


loop = make_loop_fixture()

# `def b` is on line 8 in an editor and in ty; the old splitting saw it on line 9.
_FF = "import os\n\n\ndef a():\n\treturn 1\n\n\x0c\ndef b():\n\treturn os.sep\n\n\ndef c():\n\treturn a()\n\n\n\x0c\ndef d():\n\treturn b()\n"
_SEPS = 's = "a b\u0085c\x0bd\x1ce"  # x y\n\n\ndef f():\n\treturn s\n\n\ndef g():\n\treturn f()\n'
_needs_ruff = pytest.mark.skipif(tidy.find_ruff(Path.cwd()) is None, reason="ruff not available")


def test_rc_001_definitions_keep_editor_line_numbers_after_form_feed_and_unicode_separators():
	# given / when / then — only \n, \r\n and \r end a line; FF, VT, FS, NEL and U+2028 stay inside it
	lines = {}
	for name in ("b", "c", "d"):
		definition = find_definition(_FF, qualname=name)
		assert definition is not None
		lines[name] = definition.def_line
	assert lines == {"b": 8, "c": 12, "d": 17}
	definition = find_definition(_SEPS, qualname="g")
	assert definition is not None and definition.def_line == 8
	assert len(source_lines(_SEPS)) == len(_SEPS.split("\n")) - 1


@needs_ty
@pytest.mark.integration
def test_rc_001_symbol_info_reports_ty_lines_after_a_form_feed(tmp_path, monkeypatch, loop):
	# given
	make_workspace(tmp_path, monkeypatch, {"ff.py": _FF})
	# when
	text = loop.run_until_complete(read_tools.lookup(name="c", file_path="ff.py"))
	# then
	assert "ff.py:12:5" in text, text
	assert "def c()" in text, text


@needs_ty
@pytest.mark.integration
def test_rc_001_edit_symbol_delete_and_replace_after_a_form_feed(tmp_path, monkeypatch, loop):
	# given
	project = make_workspace(tmp_path, monkeypatch, {"ff.py": _FF})
	# when
	deleted = loop.run_until_complete(edit_tools.edit_symbol(action="delete", name="b", file_path="ff.py", force=True))
	replaced = loop.run_until_complete(
		edit_tools.edit_symbol(action="replace", name="c", file_path="ff.py", source="def c():\n\treturn 99\n")
	)
	# then
	assert "Applied as edit" in deleted, deleted
	assert "Applied as edit" in replaced, replaced
	text = (project / "ff.py").read_text()
	assert "def b" not in text and "return 99" in text
	assert text.count("\x0c") == 2, "form feeds are untouched"


@_needs_ruff
def test_rc_001_import_sort_does_not_drop_form_feed_lines(tmp_path):
	# given — ruff's isort fix removes a form feed that follows the import block
	original = "import os\n\x0c\nfrom a import helper\n\n\x0c\ndef f():\n\treturn helper(), os.sep\n"
	edited = "import os\nfrom c import helper\n\x0c\n\n\x0c\ndef f():\n\treturn helper(), os.sep\n"
	# when
	result = tidy.sort_imports(tmp_path, original, edited, "b.py")
	# then
	assert result.count("\x0c") == original.count("\x0c")
	assert "from c import helper" in result
	reason = tidy.sort_failure()
	assert reason is not None and "form-feed" in reason


@needs_ty
@_needs_ruff
@pytest.mark.integration
def test_rc_001_move_keeps_form_feeds_in_importers(tmp_path, monkeypatch, loop):
	# given
	files = {
		"pyproject.toml": '[project]\nname = "x"\nversion = "0"\n',
		"a.py": "import sys\n\x0c\ndef helper():\n\treturn sys.platform\n\n\x0c\ndef keep():\n\treturn 1\n",
		"b_ff.py": "import os\n\x0c\nfrom a import helper\n\n\x0c\ndef f():\n\treturn helper(), os.sep\n",
		"b2.py": "from a import helper, keep\n\x0c\nx = helper()\ny = keep()\n",
	}
	project = make_workspace(tmp_path, monkeypatch, files)
	# when
	text = loop.run_until_complete(
		refactor_tools._move_symbol(
			name="helper", to_file="c.py", apply=True, max_new_errors=0, allow_import_cycle=True
		)
	)
	# then
	assert "Applied as edit" in text, text
	for name in files:
		if name.endswith(".py"):
			ast.parse((project / name).read_text())
	for name in ("a.py", "b_ff.py", "b2.py"):
		assert (project / name).read_text().count("\x0c") == files[name].count("\x0c"), name
