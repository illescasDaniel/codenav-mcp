"""Differential scripts (hardening/diff_references.py, hardening/diff_rename.py): parsers and known-answer runs.

The parser and sampler tests are plain unit tests. The runs start the real scripts (codenav over MCP stdio, jedi,
rope) on a tiny project and are behind the `differential` marker: `uv run pytest -m differential`."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from .live_support import REPO_ROOT, needs_ty, write_files


HARDENING = REPO_ROOT / "hardening"
_spec = importlib.util.spec_from_file_location("hardening_common", HARDENING / "_common.py")
assert _spec is not None and _spec.loader is not None
_common = importlib.util.module_from_spec(_spec)
sys.modules["hardening_common"] = _common  # dataclasses look their module up here
_spec.loader.exec_module(_common)
collect_symbols, parse_references = _common.collect_symbols, _common.parse_references
parse_rename_preview, pick_symbols = _common.parse_rename_preview, _common.pick_symbols


FIXTURE = {
	"pyproject.toml": '[project]\nname = "demo"\nversion = "0"\n[tool.ty.environment]\nroot = ["src", "."]\n',
	"src/pkg/__init__.py": "",
	"src/pkg/core.py": (
		"def helper(x: int) -> int:\n"
		"\treturn x + 1\n"
		"\n\n"
		"class Box:\n"
		"\tdef __init__(self, value: int) -> None:\n"
		"\t\tself.value = value\n"
		"\n"
		"\tdef get(self) -> int:\n"
		"\t\treturn helper(self.value)\n"
	),
	"src/pkg/app.py": (
		"from pkg.core import Box, helper\n"
		"\n\n"
		"def run() -> int:\n"
		"\tbox = Box(helper(1))\n"
		"\treturn box.get() + helper(2)\n"
	),
	# `build` is both a function and a method here: asking for the bare name is ambiguous, an error result
	"src/pkg/dup.py": ("def build() -> int:\n\treturn 1\n\n\nclass Maker:\n\tdef build(self) -> int:\n\t\treturn 2\n"),
}

# qualified name -> expected (path, line) set: references and, identically, the lines a rename edits
EXPECTED = {
	"helper": {
		("src/pkg/core.py", 1),
		("src/pkg/core.py", 10),
		("src/pkg/app.py", 1),
		("src/pkg/app.py", 5),
		("src/pkg/app.py", 6),
	},
	"Box": {("src/pkg/core.py", 5), ("src/pkg/app.py", 1), ("src/pkg/app.py", 5)},
	"Box.get": {("src/pkg/core.py", 9), ("src/pkg/app.py", 6)},
	"run": {("src/pkg/app.py", 4)},
}


# -- parsers and sampler (fast, default suite) ----------------------------------------------------------


def test_parse_references_reads_locations_and_the_omitted_marker():
	text = (
		"helper  src/a.py:1:5\ndef helper() -> int\nReferences (3)\n"
		"src/a.py:1  column 5  declaration\nsrc/b.py:7  column 9\nsrc/b.py:7  column 20\n… 4 more omitted; raise limit= to see them\n"
	)
	assert parse_references(text) == ({("src/a.py", 1), ("src/b.py", 7)}, True)
	assert parse_references("helper  src/a.py:1:5\ndef helper() -> int\n0 references") is None


def test_parse_rename_preview_collects_removed_lines_by_hunk_counts():
	text = (
		"Rename a -> b (a): 3 edit(s) in 1 file(s)\n\nPreview only.\n\nFiles:\n  src/m.py: +3 -3\n\nNotes:\n  - x\n\n"
		"--- a/src/m.py\n+++ b/src/m.py\n@@ -3,6 +3,6 @@\n ctx\n-def a(): pass\n+def b(): pass\n ctx\n"
		"---- a\n+++++ b\n-a()\n+b()\n ctx\n"
	)
	preview = parse_rename_preview(text)
	assert preview is not None
	# `---- a` is a removed line whose text starts with dashes, not a file header
	assert preview.edited == {("src/m.py", 4), ("src/m.py", 6), ("src/m.py", 7)}
	assert preview.file_counts == {"src/m.py": 3}
	assert preview.truncated is False


def test_parse_rename_preview_ignores_unchanged_lines_codenav_shows_as_removed_and_added():
	text = (
		"Rename a -> b (a): 2 edit(s) in 1 file(s)\n\nFiles:\n  m.py: +2 -2\n\n"
		"--- a/m.py\n+++ b/m.py\n@@ -1,5 +1,5 @@\n-import a\n-\n-x = 1\n-a()\n+import b\n+\n+x = 1\n+b()\n ctx\n"
	)
	preview = parse_rename_preview(text)
	assert preview is not None
	assert preview.edited == {("m.py", 1), ("m.py", 4)}


def test_parse_rename_preview_flags_truncation_and_rejects_other_replies():
	assert parse_rename_preview("2 symbols match 'a'; pass file_path to disambiguate:") is None
	cut = "Rename a -> b (a): 9 edit(s) in 1 file(s)\n--- a/m.py\n+++ b/m.py\n@@ -1,4 +1,4 @@\n-a\n+b\n... diff truncated (86 more lines)\n"
	preview = parse_rename_preview(cut)
	assert preview is not None
	assert preview.truncated is True
	assert preview.edited == {("m.py", 1)}


def test_sampler_skips_dunders_and_redefinitions_and_is_seeded(tmp_path):
	write_files(
		tmp_path,
		{
			"m.py": (
				"class C:\n\tdef __init__(self): ...\n\n\t@property\n\tdef p(self): ...\n\n\t@p.setter\n\tdef p(self, v): ...\n\n"
				"\tdef run(self): ...\n\n\nasync def go(): ...\n\n\ndef outer():\n\tdef inner(): ...\n"
			),
			".venv/skipped.py": "def hidden(): ...\n",
		},
	)
	names = {symbol.qualname: symbol for symbol in collect_symbols(tmp_path)}
	assert set(names) == {"C", "C.run", "go", "outer"}
	assert (names["C.run"].line, names["C.run"].column, names["C.run"].kind) == (10, 5, "method")
	assert (names["go"].line, names["go"].column) == (13, 10)
	assert pick_symbols(tmp_path, 2, seed=3) == pick_symbols(tmp_path, 2, seed=3)
	assert len(pick_symbols(tmp_path, 99, seed=0)) == 4


# -- known-answer runs (slow: marker `differential`) ----------------------------------------------------------


def _run(script: str, tmp_path: Path) -> dict:
	project = tmp_path / "project"
	write_files(project, FIXTURE)
	output = tmp_path / "report.json"
	completed = subprocess.run(  # noqa: S603 - this checkout's interpreter, fixed script
		[sys.executable, str(HARDENING / script), str(project), "--symbols", "100", "--output", str(output)],
		capture_output=True,
		text=True,
		timeout=300,
		check=False,
	)
	assert completed.returncode == 0, completed.stdout + completed.stderr
	assert "SUMMARY" in completed.stdout
	return json.loads(output.read_text(encoding="utf-8"))


def _by_name(report: dict) -> dict[str, dict]:
	return {record["symbol"]["qualname"]: record for record in report["symbols"]}


def _pairs(rows: list[list]) -> set[tuple[str, int]]:
	return {(path, line) for path, line in rows}


@pytest.mark.differential
@needs_ty
def test_diff_references_matches_the_known_answer(tmp_path):
	report = _run("diff_references.py", tmp_path)
	records = _by_name(report)
	for name, expected in EXPECTED.items():
		record = records[name]
		assert record["status"] == "ok", record
		assert _pairs(record["shared"]) == expected, name
		assert record["only_in_codenav"] == [] and record["only_in_jedi"] == [], name
	# an ambiguous name is an error result, never "no references"
	assert records["build"]["status"] == "error"
	assert records["build"]["error_kind"] == "ambiguous"
	assert records["Maker.build"]["status"] == "ok"
	assert report["summary"]["errors"] == 1
	assert report["summary"]["agreement_rate"] == 1.0


@pytest.mark.differential
@needs_ty
def test_diff_rename_matches_the_known_answer(tmp_path):
	report = _run("diff_rename.py", tmp_path)
	records = _by_name(report)
	for name, expected in EXPECTED.items():
		for mode in ("certain", "all"):
			result = records[name]["modes"][mode]
			assert result["status"] == "ok", (name, mode, result)
			assert result["basis"] == "lines"
			assert _pairs(result["shared"]) == expected, (name, mode)
			assert result["only_in_codenav"] == [] and result["only_in_rope"] == [], (name, mode)
	assert records["build"]["modes"]["certain"]["status"] == "error"
	assert records["build"]["modes"]["certain"]["error_kind"] == "ambiguous"
	assert report["summary"]["certain"]["agreement_rate"] == 1.0
