"""B4 regression tests: text model (line splitting, BOM, EOL, syntax gate, NUL, overloads)."""

from __future__ import annotations

import sys

import pytest
from mcp_nav_shared.edits import FileChange, TextEdit, apply_text_edits

from codenav_mcp import edit_tools, write_tools
from codenav_mcp.pysource import (
	PythonSyntaxError,
	check_syntax,
	definitions_in,
	detect_indent_unit,
	find_definition,
	fit_snippet,
	line_start_offsets,
	parse_python,
	reject_nul,
	source_lines,
)

from .live_support import make_loop_fixture, make_workspace, needs_ty


loop = make_loop_fixture()

_FF_SOURCE = "def a():\n\treturn 1\n\x0c\ndef b():\n\treturn 2\n\n\ndef c():\n\treturn 3\n"


def test_rc_001_source_lines_do_not_split_on_form_feed():
	# given / when
	lines = source_lines(_FF_SOURCE)
	# then — `\x0c\n` is ONE line, so `def c()` is line 8 as the editor shows it
	assert lines[2] == "\x0c\n"
	assert len(lines) == 9
	assert lines[7] == "def c():\n"
	assert source_lines("a\u2028b\x85c\x1cd\n") == ["a\u2028b\x85c\x1cd\n"]
	assert source_lines("a\rb\r\nc") == ["a\r", "b\r\n", "c"]


def test_rc_001_definitions_after_a_form_feed_keep_their_line_numbers():
	# given
	text = "class K:\n\tdef a(self):\n\t\treturn 1\n\x0c\n\tdef b(self):\n\t\treturn 2\n"
	# when
	by_name = {d.name: d for d in definitions_in(text)}
	# then — the indent comes from the real line 5, not from a line shifted by the form feed
	assert (by_name["b"].def_line, by_name["b"].indent) == (5, "\t")
	found = find_definition(_FF_SOURCE, name_line=8)
	assert found is not None and found.name == "c"


def test_rc_001_line_start_offsets_agree_with_source_lines():
	# given
	text = "a\x0cb\nc\rd\r\ne\n"
	# when / then — a lone CR starts a line too, a form feed does not
	assert line_start_offsets(text) == [0, 4, 6, 9, 11]
	assert [len(line) for line in source_lines(text)] == [4, 2, 3, 2]


def test_rc_001_detect_indent_unit_ignores_form_feed_lines():
	assert detect_indent_unit("def a():\n\x0c\n    return 1\n") == "    "


@needs_ty
@pytest.mark.integration
def test_rc_001_rename_by_position_after_a_form_feed(tmp_path, monkeypatch, loop):
	# given — `def b` is on line 4 in the editor and in ty; the old splitting saw it on line 5
	project = make_workspace(tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/ff.py": _FF_SOURCE})
	# when
	text = loop.run_until_complete(
		write_tools.rename_symbol(file_path="src/pkg/ff.py", line=8, column=5, new_name="cc", apply=True)
	)
	# then
	assert "Applied as edit" in text
	assert (project / "src/pkg/ff.py").read_text() == _FF_SOURCE.replace("def c()", "def cc()")


@needs_ty
@pytest.mark.integration
def test_rc_008_rename_in_bom_importer_line_one(tmp_path, monkeypatch, loop):
	# given — the importer is not open in the language server, which counts the BOM on line 1
	project = make_workspace(
		tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/bomdef.py": "def bomfn() -> int:\n\treturn 1\n"}
	)
	(project / "src/pkg/bomimp.py").write_bytes(
		b"\xef\xbb\xbffrom pkg.bomdef import bomfn\n\n\ndef go() -> int:\n\treturn bomfn()\n"
	)
	# when
	text = loop.run_until_complete(write_tools.rename_symbol(name="bomfn", new_name="bomfn2", apply=True))
	# then
	assert "Applied as edit" in text
	assert (project / "src/pkg/bomimp.py").read_bytes() == (
		b"\xef\xbb\xbffrom pkg.bomdef import bomfn2\n\n\ndef go() -> int:\n\treturn bomfn2()\n"
	)


@needs_ty
@pytest.mark.integration
def test_rc_030_edit_symbol_on_a_bom_file_is_not_refused_as_stale(tmp_path, monkeypatch, loop):
	# given
	project = make_workspace(tmp_path, monkeypatch, {"src/pkg/__init__.py": ""})
	(project / "src/pkg/bom.py").write_bytes(b"\xef\xbb\xbfdef f():\n\treturn 1\n")
	# when
	text = loop.run_until_complete(
		edit_tools.edit_symbol(action="replace", name="f", file_path="src/pkg/bom.py", source="def f():\n\treturn 2\n")
	)
	# then
	assert "changed since the preview was made" not in text
	assert "Applied as edit" in text
	assert (project / "src/pkg/bom.py").read_bytes() == b"\xef\xbb\xbfdef f():\n\treturn 2\n"


def test_rc_030_a_change_built_without_flags_keeps_the_bom(tmp_path):
	# given — the shape of the four sites that omitted the flag
	path = tmp_path / "bom.py"
	path.write_bytes(b"\xef\xbb\xbfx = 1\n")
	# when
	change = FileChange(path, "x = 1\n", "x = 2\n")
	# then
	assert change.old_bytes() == b"\xef\xbb\xbfx = 1\n"
	assert change.new_bytes() == b"\xef\xbb\xbfx = 2\n"


@pytest.mark.parametrize(
	"snippet",
	[
		"def f():\n\treturn 1\n",
		"def f():\r\n\treturn 1\r\n",
		"    def f():\n        return 1\n",
		"def f():\n\treturn 1",
	],
)
def test_rc_012_fit_snippet_uses_the_files_line_ending(snippet):
	# given
	crlf_file = "class A:\r\n\tx = 1\r\n"
	lf_file = "class A:\n\tx = 1\n"
	# when
	crlf = fit_snippet(snippet, indent="\t", file_text=crlf_file, match_eol=True)
	lf = fit_snippet(snippet, indent="\t", file_text=lf_file, match_eol=True)
	# then — one EOL style per block
	assert crlf == "\tdef f():\r\n\t\treturn 1\r\n"
	assert lf == "\tdef f():\n\t\treturn 1\n"
	assert "\n" not in crlf.replace("\r\n", "")


def test_rc_012_fit_snippet_default_stays_lf_for_callers_that_convert_themselves():
	# given — `symbol_tools._insert_block` does `snippet.replace("\n", eol)`: CRLF here would give `\r\r\n`
	fitted = fit_snippet("def f():\r\n\treturn 1\r\n", indent="\t", file_text="class A:\r\n\tx = 1\r\n")
	# then
	assert fitted == "\tdef f():\n\t\treturn 1\n"
	assert fitted.replace("\n", "\r\n") == "\tdef f():\r\n\t\treturn 1\r\n"


def test_rc_012_crlf_file_stays_crlf():
	# given — a block placed in a CRLF file through the shared edit machinery
	text = "class A:\r\n\tx = 1\r\n"
	block = fit_snippet("def f():\n\treturn 1\n", indent="\t", file_text=text, match_eol=True)
	# when
	result = apply_text_edits(text, [TextEdit(2, 0, 2, 0, block)])
	# then
	assert result == "class A:\r\n\tx = 1\r\n\tdef f():\r\n\t\treturn 1\r\n"
	assert result.count("\r\n") == result.count("\n")
	assert "\r\r" not in result


def test_rc_111_lf_text_for_a_crlf_file_is_converted_by_fit_snippet():
	# given / when
	fitted = fit_snippet("a = 1\nb = 2\n", indent="", file_text="x = 0\r\ny = 0\r\n", match_eol=True)
	# then
	assert fitted == "a = 1\r\nb = 2\r\n"


def test_rc_020_future_import_after_code_is_refused():
	# given — `ast.parse` accepts this, the compiler does not
	text = "import os\nfrom __future__ import annotations\n"
	# when / then
	parse_python(text)
	with pytest.raises(
		PythonSyntaxError,
		match=r"^future\.py: not written: .*\(line 2: from __future__ imports must occur at the beginning of the file\)",
	):
		check_syntax(text, "future.py")


@pytest.mark.parametrize(
	"text",
	["return 1\n", "def f():\n\tpass\nyield 1\n", "class A:\n\tbreak\n", "nonlocal x\n", "def f():\n\tawait g()\n"],
)
def test_rc_020_compile_time_errors_are_refused(text):
	with pytest.raises(PythonSyntaxError, match=r"^m\.py: not written: "):
		check_syntax(text, "m.py")


def test_rc_020_valid_python_passes_and_has_no_side_effects(recwarn):
	# given — an invalid escape would raise a SyntaxWarning in a raw compile
	text = '"""Doc."""\nfrom __future__ import annotations\n\nimport re\n\nPATTERN = re.compile("\\d+")\n\n\ndef f(x: Later) -> Later:\n\treturn x\n'
	# when
	check_syntax(text, "ok.py")
	# then
	assert len(recwarn) == 0


def test_rc_034_syntax_error_names_the_file_and_the_line():
	with pytest.raises(PythonSyntaxError) as info:
		check_syntax("x = 1\ndef broken(:\n\tpass\n", "pkg/lib.py")
	message = str(info.value)
	assert message.startswith("pkg/lib.py: not written: ")
	assert "(line 2: " in message
	assert "<source>" not in message
	assert "SyntaxError(" not in message


def test_rc_034_newer_syntax_gets_a_version_hint(monkeypatch):
	# given — `except A, B:` without parentheses is Python 3.14 syntax
	text = "try:\n\tpass\nexcept A, B:\n\tpass\n"
	# when
	with pytest.raises(PythonSyntaxError) as info:
		check_syntax(text, "lib.py")
	# then
	message = str(info.value)
	if sys.version_info < (3, 14):
		v = sys.version_info
		assert f"this server's Python {v.major}.{v.minor}.{v.micro} parser" in message
		assert "PEP 758" in message
		assert "run the server under a newer Python" in message
	else:  # pragma: no cover - the server itself accepts it
		pytest.skip("this interpreter parses the construct")


def test_rc_034_parse_python_has_the_same_messages():
	with pytest.raises(PythonSyntaxError, match=r"^lib\.py: syntax error at line 1: "):
		parse_python("def broken(:\n", "lib.py")


def test_rc_089_nul_byte_is_refused_with_a_short_message():
	# given
	text = "def ok():\n\treturn 1\n\0def bad():\n\treturn 2\n"
	# when / then
	with pytest.raises(PythonSyntaxError, match=r"^nul\.py: contains a NUL byte; not a text file$"):
		parse_python(text, "nul.py")
	with pytest.raises(PythonSyntaxError, match=r"^nul\.py: contains a NUL byte; not a text file$"):
		check_syntax(text, "nul.py")
	with pytest.raises(PythonSyntaxError, match=r"contains a NUL byte"):
		reject_nul("a\0b", "x.py")
	reject_nul("no nul here", "x.py")


_OVERLOADED = (
	"import typing as t\n"
	"from typing import overload\n\n\n"
	"class Context:\n"
	"\t@t.overload\n"
	"\tdef invoke(self, a: int) -> int: ...\n\n"
	"\t@overload\n"
	"\tdef invoke(self, a: str) -> str: ...\n\n"
	"\tdef invoke(self, a):\n"
	"\t\treturn a\n"
)


def test_rc_099_find_definition_prefers_the_implementation_over_overload_stubs():
	# given — ty's definition position is the FIRST overload (line 7)
	by_line = find_definition(_OVERLOADED, name_line=7, prefer_implementation=True)
	by_name = find_definition(_OVERLOADED, qualname="Context.invoke")
	# then
	assert by_line is not None and by_name is not None
	assert by_line.def_line == by_name.def_line == 12
	assert by_line.first_line == 12


def test_rc_099_all_overloads_returns_the_first():
	# given
	text = "from typing import overload\n\n\n@overload\ndef f(a: int) -> int: ...\n\n\n@overload\ndef f(a: str) -> str: ...\n"
	# when
	found = find_definition(text, qualname="f")
	# then
	assert found is not None and found.def_line == 5


def test_rc_099_non_overload_redefinitions_keep_the_old_behaviour():
	# given — a property getter/setter pair is not an overload set
	text = "class A:\n\t@property\n\tdef x(self): ...\n\n\t@x.setter\n\tdef x(self, v): ...\n"
	# when
	found = find_definition(text, qualname="A.x")
	# then
	assert found is not None and found.def_line == 3


def test_rc_105_leading_bom_is_ignored_by_the_syntax_gate():
	check_syntax("\ufeffdef f():\n\treturn 3\n", "bom.py")


def test_rc_105_interior_bom_refused_without_raw_parser_text():
	with pytest.raises(PythonSyntaxError) as info:
		check_syntax("x = 1\n\ufeffy = 2\n", "bom.py")
	assert str(info.value) == "bom.py: remove the byte-order mark (U+FEFF) from new_text; the file's own BOM is kept"


def test_rc_105_leading_bom_in_new_text_does_not_double_the_files_bom(tmp_path):
	# given
	path = tmp_path / "bom.py"
	path.write_bytes(b"\xef\xbb\xbfdef f():\n\treturn 1\n")
	new_text = "\ufeffdef f():\n\treturn 3\n"
	check_syntax(new_text, "bom.py")
	# when
	change = FileChange(path, "def f():\n\treturn 1\n", new_text)
	# then
	assert change.new_bytes() == b"\xef\xbb\xbfdef f():\n\treturn 3\n"


def test_interior_bom_allowed_in_strings_and_comments():
	for text in ('x = "\ufeff"\n', "# \ufeff\nx = 1\n"):
		check_syntax(text, "a.py")


def test_rename_bom_convention_requires_unique_identifier_match(tmp_path):
	from codenav_mcp.rename import _rename_bom_convention

	path = tmp_path / "a.py"
	path.write_text("\ufeffab abc\n", encoding="utf-8")
	assert _rename_bom_convention(path, [TextEdit(0, 1, 0, 3, "z")], "ab") is True
	assert _rename_bom_convention(path, [TextEdit(0, 0, 0, 2, "z")], "ab") is False
	# Both positions select the same character; no inference is safe.
	path.write_text("\ufeffaaa\n", encoding="utf-8")
	assert _rename_bom_convention(path, [TextEdit(0, 1, 0, 2, "z")], "a") is None


def test_compiler_only_syntax_error_includes_version_hint():
	if sys.version_info < (3, 14):
		with pytest.raises(PythonSyntaxError, match="does not parse with this server's Python"):
			check_syntax("return 1\n", "a.py")


@needs_ty
@pytest.mark.integration
def test_rename_bom_does_not_trust_open_state_after_response(tmp_path, monkeypatch, loop):
	from mcp_nav_shared.lsp_client import LspClient

	project = make_workspace(
		tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/defn.py": "def helper():\n\treturn 1\n"}
	)
	importer = project / "src/pkg/importer.py"
	importer.write_bytes(b"\xef\xbb\xbffrom pkg.defn import helper\n")
	original = LspClient.rename

	async def race(client, *args, **kwargs):
		result = await original(client, *args, **kwargs)
		# ty calculated the edit while the importer was unopened. Model a
		# different task opening it just before the caller receives the result.
		uri = importer.resolve().as_uri()
		assert uri not in client._open_files
		client._open_files[uri] = next(iter(client._open_files.values()))
		loop.call_soon(client._open_files.pop, uri, None)
		return result

	monkeypatch.setattr(LspClient, "rename", race)
	result = loop.run_until_complete(write_tools.rename_symbol(name="helper", new_name="assist", apply=True))
	assert "Applied as edit" in result
	assert importer.read_bytes() == b"\xef\xbb\xbffrom pkg.defn import assist\n"


def test_explicit_overload_line_remains_exact():
	found = find_definition(_OVERLOADED, name_line=7)
	assert found is not None and found.def_line == 7


def test_unrelated_overload_decorator_is_not_redirected():
	text = "@custom.overload\ndef f(a): ...\ndef f(b): return b\n"
	found = find_definition(text, name_line=2, prefer_implementation=True)
	assert found is not None and found.def_line == 2
