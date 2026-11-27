"""Python in shell heredocs: found, parsed, and offered as optional rename sites by role."""

from __future__ import annotations

import os
import re

import pytest

from codenav_mcp import edits_tools, write_tools
from codenav_mcp.heredocs import embedded_python_sites, python_heredocs

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
