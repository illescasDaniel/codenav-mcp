"""Python in shell heredocs: found, parsed, and offered as optional rename sites by role."""

from __future__ import annotations

import os
import re

import pytest

from codenav_mcp import edits_tools, write_tools
from codenav_mcp.heredocs import embedded_python_sites, python_heredocs, site_label

from .live_support import make_loop_fixture, make_workspace, needs_ty


_SCRIPT = """#!/usr/bin/env bash
set -euo pipefail
lines=$(wc -l < notes.txt)
echo "lines: $lines"
uv run python - "$@" <<'PY'
from pkg.models import Result, build

r = Result(lines=[1], score=1.0)
print(r.lines, {"lines": 1}["lines"])
lines = 3
build(lines=lines)
PY
cat <<'EOF'
r.lines = 1
EOF
python3 <<PY
print(r.lines)
PY
"""


def test_given_shell_script_when_scanned_then_only_quoted_python_heredocs_count():
	heredocs = python_heredocs(_SCRIPT)

	# The unquoted `<<PY` body is expanded by the shell (columns not trustworthy) and `cat <<'EOF'` is not Python.
	assert [start for start, _ in heredocs] == [5]
	assert heredocs[0][1][0] == "from pkg.models import Result, build"


def test_given_heredoc_when_sites_requested_then_roles_are_exact_and_strings_and_shell_are_skipped():
	sites = embedded_python_sites(_SCRIPT, "lines")

	assert [(s.line, s.role) for s in sites] == [
		(8, "keyword"),
		(9, "attr"),
		(10, "name"),
		(11, "keyword"),
		(11, "name"),
	]
	row = _SCRIPT.split("\n")[7]
	assert row[sites[0].column : sites[0].column + 5] == "lines"


def test_given_dash_heredoc_with_tab_indent_when_scanned_then_columns_point_at_the_name():
	# `<<-` strips every leading tab, so the Python inside indents with spaces.
	script = "if true; then\n\tpython3 - <<-'PY'\n\t\tif True:\n\t\t    x.lines = 1\n\tPY\nfi\n"

	sites = embedded_python_sites(script, "lines")

	assert [(s.line, s.role) for s in sites] == [(4, "attr")]
	assert script.split("\n")[3][sites[0].column :].startswith("lines")


def test_given_body_that_does_not_parse_when_scanned_then_it_is_skipped():
	assert embedded_python_sites("python3 <<'PY'\nx.lines = (\nPY\n", "lines") == []


def test_given_unterminated_heredoc_when_scanned_then_nothing_is_found():
	assert python_heredocs("python3 <<'PY'\nx.lines = 1\n") == []


def _at(script: str, line: int, column: int) -> str:
	return script.split("\n")[line - 1][column:]


def _roles(script: str, name: str = "lines") -> list[tuple[int, str]]:
	sites = embedded_python_sites(script, name)
	for site in sites:
		assert _at(script, site.line, site.column).startswith(name), (site, script)
	return [(s.line, s.role) for s in sites]


@pytest.mark.parametrize(
	("script", "starts"),
	[
		# a Python delimiter is enough, without a python command on the line
		("uv run - <<'PY'\nx = 1\nPY\n", [1]),
		("run <<'py'\nx = 1\npy\n", [1]),
		# a python command is enough, with any delimiter
		("python3 <<'EOF'\nx = 1\nEOF\n", [1]),
		('python3.12 - <<"END"\nx = 1\nEND\n', [1]),
		# neither: not Python
		("cat <<'EOF'\nx = 1\nEOF\n", []),
		("pythonista <<'EOF'\nx = 1\nEOF\n", []),
		# an empty body, then a second heredoc right after the first one's delimiter
		("python3 <<'PY'\nPY\npython3 <<'PY'\nx = 1\nPY\n", [1, 3]),
		("python3 <<'PY'\na = 1\nPY\npython3 <<'PY'\nb = 2\nPY\n", [1, 4]),
	],
)
def test_given_heredoc_header_when_scanned_then_python_is_recognised_by_command_or_delimiter(script, starts):
	assert [start for start, _ in python_heredocs(script)] == starts


def test_given_crlf_script_when_scanned_then_the_delimiter_still_ends_the_body():
	script = "python3 - <<'PY'\r\nx.lines = 1\r\nPY\r\necho done\r\n"

	assert python_heredocs(script) == [(1, ["x.lines = 1"])]
	assert _roles(script) == [(2, "attr")]


def test_given_tab_indented_delimiter_when_heredoc_is_not_dashed_then_it_does_not_end_the_body():
	# Only `<<-` strips tabs from the delimiter line; here "\tPY" is text inside a Python string.
	script = 'python3 <<\'PY\'\nx.lines = """\n\tPY\n"""\nPY\n'

	assert _roles(script) == [(2, "attr")]


def test_given_dashed_heredoc_when_a_body_line_is_space_indented_delimiter_then_it_does_not_end_the_body():
	# `<<-` strips tabs only: "  PY" is still Python (an element of the list).
	script = "python3 <<-'PY'\n\tx.lines = [\n  PY,\n\t]\n\tPY\n"

	assert _roles(script) == [(2, "attr")]


def test_given_dashed_heredoc_with_uneven_tabs_when_scanned_then_every_tab_is_stripped_before_parsing():
	# Without stripping all leading tabs, the body would be "\tx.lines = 1" then "y = 2": an IndentationError.
	script = "python3 <<-'PY'\n\t\tx.lines = 1\n\ty = 2\n\tPY\n"

	assert _roles(script) == [(2, "attr")]


def test_given_space_indented_body_when_scanned_then_the_common_indent_is_removed_and_columns_kept():
	script = (
		"if true; then\n  python3 <<'PY'\n    import os\n    x.lines = os.sep\n    if x:\n        y.lines = 2\nPY\nfi\n"
	)

	assert _roles(script) == [(4, "attr"), (6, "attr")]


def test_given_unevenly_prefixed_body_when_scanned_then_no_indent_is_removed():
	# "  " and "\t\t" are both 2 characters but not the same prefix: Python sees the mixed indent and refuses.
	script = "python3 <<'PY'\n  x.lines = 1\n\t\ty = 2\nPY\n"

	assert embedded_python_sites(script, "lines") == []


def test_given_body_that_does_not_parse_when_another_heredoc_follows_then_the_next_one_is_still_read():
	script = "python3 <<'PY'\nx.lines = (\nPY\npython3 <<'PY'\ny.lines = 1\nPY\n"

	assert _roles(script) == [(5, "attr")]


def test_given_definitions_and_imports_when_scanned_then_def_and_import_roles_point_at_the_name():
	script = (
		"python3 <<'PY'\n"
		"import os\n"
		"from pkg import lines\n"
		"import a.b.lines\n"
		"import lines.lines as both\n"
		"import a.lines.sub\n"
		"def lines():\n"
		"    pass\n"
		"class  lines:\n"
		"    pass\n"
		"PY\n"
	)

	assert _roles(script) == [(3, "import"), (4, "import"), (5, "import"), (7, "def"), (9, "def")]
	# `import lines.lines`: the last component is the one that names the symbol
	site = [s for s in embedded_python_sites(script, "lines") if s.line == 5][0]
	assert site.column == len("import lines.")


def test_given_keyword_in_a_call_when_scanned_then_the_callee_is_named():
	script = "python3 <<'PY'\nr = FileSearchResult(path=p,\n    lines=[1])\nmodels.build(lines=2)\nclass C(Base, lines=3):\n    pass\nPY\n"

	sites = embedded_python_sites(script, "lines")

	assert [(s.line, s.role, s.callee) for s in sites] == [
		(3, "keyword", "FileSearchResult"),
		(4, "keyword", "models.build"),
		(5, "keyword", ""),
	]
	assert site_label("keyword", "FileSearchResult") == "Python heredoc: keyword of FileSearchResult()"
	assert site_label("keyword") == "Python heredoc: keyword"
	assert site_label("attr") == "Python heredoc: attr"


def test_given_long_callee_when_scanned_then_it_is_shortened_from_the_left():
	callee = "package.module.submodule.factory.builder.make_result"
	sites = embedded_python_sites(f"python3 <<'PY'\n{callee}(lines=1)\nPY\n", "lines")

	assert sites[0].callee == "..." + callee[-37:]
	assert len(sites[0].callee) == 40


def test_given_name_not_in_text_when_scanned_then_nothing_is_parsed():
	assert embedded_python_sites("python3 <<'PY'\nx.rows = 1\nPY\n", "lines") == []


def test_given_dashed_heredoc_when_a_string_line_is_the_delimiter_indented_with_spaces_then_the_body_goes_on():
	# Only tabs are stripped before the delimiter comparison: "  PY" inside the string does not end the heredoc.
	script = "python3 <<-'PY'\n\tx.lines = '''\n  PY\n'''\n\tPY\n"

	assert [start for start, _ in python_heredocs(script)] == [1]
	assert python_heredocs(script)[0][1] == ["\tx.lines = '''", "  PY", "'''"]
	assert _roles(script) == [(2, "attr")]


def test_given_plain_heredoc_with_nested_tab_indent_when_scanned_then_tabs_are_kept_as_python_indentation():
	# Without `<<-` the shell keeps every tab, so "\ty.lines" is the body of the `if`, not a top-level line.
	script = "python3 <<'PY'\nif x:\n\ty.lines = 1\nPY\n"

	assert _roles(script) == [(3, "attr")]


def test_given_empty_heredoc_when_the_name_is_elsewhere_in_the_script_then_nothing_is_found():
	assert embedded_python_sites("echo lines\npython3 <<'PY'\n\nPY\n", "lines") == []


def test_given_callee_of_exactly_the_width_when_scanned_then_it_is_kept_whole():
	callee = "pkg." + "m" * 33 + ".mk"
	assert len(callee) == 40
	sites = embedded_python_sites(f"python3 <<'PY'\n{callee}(lines=1)\nPY\n", "lines")

	assert sites[0].callee == callee


def test_given_bare_names_when_scanned_then_their_columns_point_at_the_name():
	# `lines = 3` and `build(lines=lines)`: the name role, checked at its column like the others.
	assert _roles(_SCRIPT) == [(8, "keyword"), (9, "attr"), (10, "name"), (11, "keyword"), (11, "name")]


_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/models.py": (
		"from dataclasses import dataclass\n\n\n@dataclass\nclass Result:\n\tlines: list[int]\n\tscore: float\n\n\n"
		"def build(lines: int) -> int:\n\treturn lines\n"
	),
	"src/pkg/use.py": "from pkg.models import Result\n\n\ndef total(r: Result) -> int:\n\treturn len(r.lines)\n",
	"scripts/demo.sh": _SCRIPT,
}


loop = make_loop_fixture()


@pytest.mark.integration
@needs_ty
class TestRename:
	def test_given_field_rename_when_previewed_then_heredoc_keyword_and_attr_are_sites_and_shell_is_untouched(
		self, tmp_path, monkeypatch, loop
	):
		root = make_workspace(tmp_path, monkeypatch, _FILES)
		os.chmod(root / "scripts/demo.sh", 0o700)

		preview = str(loop.run_until_complete(write_tools.rename(name="Result.lines", new_name="rows", apply=False)))

		listed = re.findall(r"\bu\d+\s+scripts/demo\.sh:(\d+)\b", preview)
		# keyword in Result(...), r.lines, and `build(lines=...)` (a keyword of another callable, still offered: opt-in)
		assert listed == ["8", "9", "11"], preview
		# Each says what it is, so `lines=[1]` is not mistaken for shell text.
		assert re.search(r"demo\.sh:8\s+Python heredoc: keyword of Result\(\)\s+r = Result\(lines=", preview), preview
		assert re.search(r"demo\.sh:9\s+Python heredoc: attr\s+print\(r\.lines", preview), preview
		assert re.search(r"demo\.sh:11\s+Python heredoc: keyword of build\(\)", preview), preview
		assert (root / "scripts/demo.sh").read_text(encoding="utf-8") == _SCRIPT

		ids = re.findall(r"\b(u\d+)\s+scripts/demo\.sh:(?:8|9)\b", preview)
		text = str(loop.run_until_complete(write_tools.rename(name="Result.lines", new_name="rows", sites=ids)))
		script = (root / "scripts/demo.sh").read_text(encoding="utf-8")
		assert "r = Result(rows=[1], score=1.0)" in script, text
		assert 'print(r.rows, {"lines": 1}["lines"])' in script, text
		assert "lines=$(wc -l < notes.txt)" in script and "build(lines=lines)" in script
		assert os.access(root / "scripts/demo.sh", os.X_OK)

		applied = re.search(r'undo", id="([0-9a-f]+)"', text)
		assert applied, text
		loop.run_until_complete(edits_tools.edits(action="undo", id=applied.group(1)))
		assert (root / "scripts/demo.sh").read_text(encoding="utf-8") == _SCRIPT

	def test_given_function_rename_when_previewed_then_heredoc_import_and_call_are_sites(
		self, tmp_path, monkeypatch, loop
	):
		make_workspace(tmp_path, monkeypatch, _FILES)

		preview = str(loop.run_until_complete(write_tools.rename(name="build", new_name="make", apply=False)))

		assert re.findall(r"\bu\d+\s+scripts/demo\.sh:(\d+)\b", preview) == ["6", "11"], preview
