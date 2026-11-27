"""Fast unit tests for `codenav_mcp.rename_sites` and the keyword-only default of `change_signature` (no language server)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest
from mcp_nav_shared.edits import EditPlan, TextEdit
from mcp_nav_shared.lsp_client import LspClient

from codenav_mcp.plan_sites import CodenavPlan, OptionalSite
from codenav_mcp.pysource import definitions_in
from codenav_mcp.rename import _is_unlinked_attr
from codenav_mcp.rename_sites import (
	Declaration,
	UnknownSitesError,
	_code_like_spans,
	_scan_python,
	dotted_targets,
	format_site,
	is_test_path,
	plan_v2_sites,
	select_sites,
)
from codenav_mcp.signature import (
	Change,
	SourceMap,
	added_keyword_only,
	new_params,
	read_signature,
	render_params,
	tokenize_source,
)


def _write(tmp_path: Path, rel: str, text: str) -> Path:
	path = tmp_path / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text)
	return path


def _apply(text: str, edits: list[TextEdit]) -> str:
	lines = text.split("\n")
	for edit in sorted(edits, key=lambda e: (e.start_line, e.start_character), reverse=True):
		row = lines[edit.start_line]
		lines[edit.start_line] = row[: edit.start_character] + edit.new_text + row[edit.end_character :]
	return "\n".join(lines)


DOTTED = {"pkg.runner.Runner.execute"}


def test_given_exact_patch_targets_when_scanned_then_only_the_last_component_is_edited(tmp_path):
	# given: single-quoted, double-quoted, inside a multi-line call, a longer path, a prefixed string, a different class
	text = (
		"def t(monkeypatch):\n"
		"\tmonkeypatch.setattr('pkg.runner.Runner.execute', 1)\n"
		'\tmonkeypatch.setattr("pkg.runner.Runner.execute", 2)\n'
		"\tpatch.multiple(\n"
		"\t\t'pkg.runner.Runner.execute',\n"
		"\t\tnew=3,\n"
		"\t)\n"
		"\tmonkeypatch.setattr('pkg.runner.Runner.execute.more', 4)\n"
		"\tmonkeypatch.setattr(f'pkg.runner.Runner.execute', 5)\n"
		"\tmonkeypatch.setattr('pkg.runner.Other.execute', 6)\n"
	)
	path = _write(tmp_path, "tests/test_a.py", text)
	# when
	scan = _scan_python(path, "tests/test_a.py", "execute", "run", DOTTED, set(), test_file=True)
	# then
	out = _apply(text, scan.patch)
	assert "'pkg.runner.Runner.run', 1" in out and '"pkg.runner.Runner.run", 2' in out
	assert "\t\t'pkg.runner.Runner.run',\n" in out
	assert "Runner.execute.more" in out and "f'pkg.runner.Runner.execute'" in out and "Other.execute" in out
	assert len(scan.patch) == 3


def test_given_mock_keyword_when_scanned_then_it_is_flagged_as_mock_and_other_keywords_are_candidates(tmp_path):
	text = "m = Mock(execute=1)\nn = SimpleNamespace(execute=2)\nc = Config(execute=3)\n"
	path = _write(tmp_path, "tests/test_b.py", text)
	scan = _scan_python(path, "tests/test_b.py", "execute", "run", set(), set(), test_file=True)
	assert [(line, mock) for line, _, mock, *_ in scan.keywords] == [(1, True), (2, True), (3, False)]


def test_given_comments_and_docstrings_when_scanned_then_only_code_like_mentions_are_sites(tmp_path):
	text = (
		'"""Module: call `execute` or .execute() here; execute plain words stay."""\n\n\n'
		"def f() -> None:\n"
		"\t# mock.execute() is called once\n"
		"\t# execute the statements in order\n"
		"\tpass\n"
	)
	path = _write(tmp_path, "src/a.py", text)
	scan = _scan_python(path, "src/a.py", "execute", "run", set(), set(), test_file=False)
	out = _apply(text, [site.edit for site in scan.optional])
	assert "`run` or .run() here; execute plain words stay" in out
	assert "# mock.run() is called once" in out
	assert "# execute the statements in order" in out
	assert {site.kind for site in scan.optional} == {"comment", "docstring"}


def test_given_defs_when_scanned_then_methods_in_test_files_only_are_sites(tmp_path):
	text = "class Fake:\n\tdef execute(self):\n\t\tpass\n\n\ndef execute():\n\tpass\n"
	in_tests = _scan_python(
		_write(tmp_path, "tests/test_c.py", text), "tests/test_c.py", "execute", "run", set(), set(), test_file=True
	)
	in_src = _scan_python(
		_write(tmp_path, "src/c.py", text), "src/c.py", "execute", "run", set(), set(), test_file=False
	)
	assert [(site.kind, site.line) for site in in_tests.optional] == [("def", 2)]
	assert _apply(text, [in_tests.optional[0].edit]).startswith("class Fake:\n\tdef run(self):")
	assert in_src.optional == []


def test_given_already_covered_position_when_scanned_then_it_is_not_a_site(tmp_path):
	text = "class Fake:\n\tdef execute(self):\n\t\tpass\n"
	path = _write(tmp_path, "tests/test_d.py", text)
	scan = _scan_python(path, "tests/test_d.py", "execute", "run", set(), {(1, 5)}, test_file=True)
	assert scan.optional == []


def test_given_test_paths_then_is_test_path_recognises_them():
	assert all(
		is_test_path(p) for p in ("tests/a.py", "src/pkg/test_x.py", "src/x_test.py", "conftest.py", "pkg/tests/x.py")
	)
	assert not any(is_test_path(p) for p in ("src/pkg/a.py", "src/attests.py"))


def test_given_dotted_declarations_then_every_import_root_gives_a_target(tmp_path):
	path = _write(tmp_path, "src/pkg/runner.py", "")
	names = dotted_targets([Declaration(path, 4, "Runner.execute")], [tmp_path, tmp_path / "src"])
	assert names == {"pkg.runner.Runner.execute", "src.pkg.runner.Runner.execute"}


def test_given_code_like_spans_then_after_dot_backtick_and_call_only():
	assert _code_like_spans("a.execute b", "execute") == [(2, 9)]
	assert _code_like_spans("use `execute` now", "execute") == [(5, 12)]
	assert _code_like_spans("call execute(1)", "execute") == [(5, 12)]
	assert _code_like_spans("execute the thing", "execute") == []
	assert _code_like_spans("executes .executes", "execute") == []


def test_given_hyphenated_words_when_spanned_then_cli_flags_are_not_code():
	# `--max-matches` is a CLI flag in a doc, not the Python name `matches` (found on srxy's docs/cli.md)
	assert _code_like_spans("`--max-line-matches` is deprecated; use `--max-matches`.", "matches") == []
	assert _code_like_spans("`matches-all` and `matches`", "matches") == [(19, 26)]


def test_given_class_with_its_own_member_when_scanned_then_its_prose_is_not_a_site(tmp_path):
	# Renaming `_Segment.matches`: the docstring in `Pattern` is about `Pattern.matches` (srxy ignore_rules.py:142).
	text = (
		"class _Segment:\n"
		"\tdef matches(self, name):\n"
		'\t\t"""`matches` one name."""\n'
		"\t\treturn True\n\n\n"
		"class Pattern:\n"
		"\tdef matches(self, parts):\n"
		"\t\treturn self.matches_normalized(parts)\n\n"
		"\tdef matches_normalized(self, parts):\n"
		'\t\t"""Like `matches` for normalized parts."""\n'
		"\t\t# see .matches() above\n"
		"\t\treturn parts\n\n\n"
		"class Fields:\n"
		'\t"""`matches` counts."""\n'
		"\tmatches: int = 0\n"
	)
	path = _write(tmp_path, "src/rules.py", text)
	# The rename edits `_Segment.matches` at line 2 (0-based 1), column 5.
	scan = _scan_python(path, "src/rules.py", "matches", "matches_name", set(), {(1, 5)}, test_file=False)

	assert [(site.kind, site.line) for site in scan.optional] == [("docstring", 3)]

	# Renaming `Pattern.matches` instead: its own prose is offered, the other classes' is not.
	scan = _scan_python(path, "src/rules.py", "matches", "matches_name", set(), {(7, 5)}, test_file=False)

	assert [(site.kind, site.line) for site in scan.optional] == [("comment", 13), ("docstring", 12)]


def test_given_class_assigning_the_name_when_scanned_then_only_that_class_prose_is_skipped(tmp_path):
	# `matches = ...` in a class body is that class's own member; `other = ...` is not.
	text = (
		"class Alias:\n"
		'\t"""`matches` is len."""\n'
		"\tmatches = staticmethod(len)\n"
		"# `matches` above is Alias's; this comment is outside the class\n\n\n"
		"class Other:\n"
		'\t"""`matches` one name."""\n'
		"\tother = 1\n"
	)
	path = _write(tmp_path, "src/alias.py", text)

	scan = _scan_python(path, "src/alias.py", "matches", "matches_name", set(), set(), test_file=False)

	assert sorted((site.kind, site.line) for site in scan.optional) == [("comment", 4), ("docstring", 8)]


def _sites(*ids: str) -> list[OptionalSite]:
	return [OptionalSite(i, "t.py", n, "attr", "x", TextEdit(0, 0, 0, 1, "y")) for n, i in enumerate(ids, 1)]


def test_given_sites_then_select_by_id_or_all_and_unknown_ids_name_the_valid_ones():
	optional = _sites("u1", "u2", "u3")
	assert [s.id for s in select_sites(optional, None)] == []
	assert [s.id for s in select_sites(optional, ["u3", "u1", "u3"])] == ["u3", "u1"]
	assert [s.id for s in select_sites(optional, ["ALL"])] == ["u1", "u2", "u3"]
	with pytest.raises(UnknownSitesError, match=r"unknown site id\(s\) 'u9'; valid ids: u1, u2, u3"):
		select_sites(optional, ["u1", "u9"])
	with pytest.raises(UnknownSitesError, match="no optional sites"):
		select_sites([], ["u1"])


def test_given_a_site_then_the_reply_line_is_id_path_line_kind_snippet():
	site = OptionalSite("u3", "tests/x.py", 12, "attr", "mock.execute(...)", TextEdit(0, 0, 0, 1, "y"))
	assert format_site(site) == "u3  tests/x.py:12  attr  mock.execute(...)"


def test_given_a_plan_then_it_finds_optional_sites_by_id():
	sites = _sites("u1", "u2")
	plan = CodenavPlan(EditPlan("x", []), sites)
	assert plan.site("u2") is sites[1]
	assert plan.site("u3") is None


class _FakeClient:
	"""Answers `definition` for keyword positions with a fixed location (or none)."""

	def __init__(self, answer: dict[tuple[str, int], list[dict]]) -> None:
		self.answer = answer

	async def definition(self, path: str, line: int, column: int) -> list[dict]:
		return self.answer.get((Path(path).name, line), [])


def test_given_keywords_when_planned_then_ty_linked_ones_are_certain_and_the_rest_left_alone(tmp_path):
	# given: a renamed declaration at use.py:2 (0-based line 1, character 4); three `label=` keywords
	_write(tmp_path, "src/pkg/__init__.py", "")
	model = _write(tmp_path, "src/pkg/model.py", "class Job:\n\tlabel: str\n")
	_write(
		tmp_path,
		"src/pkg/use.py",
		"a = Job(label='a')\nb = Other(label='b')\nc = Unknown(label='c')\nd = Mock(label='d')\n",
	)
	renamed = {
		"uri": model.as_uri(),
		"range": {"start": {"line": 1, "character": 1}, "end": {"line": 1, "character": 6}},
	}
	elsewhere = {
		"uri": (tmp_path / "x.py").as_uri(),
		"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 5}},
	}
	client = _FakeClient({("use.py", 1): [renamed], ("use.py", 2): [elsewhere]})
	edits = {model.resolve(): [TextEdit(1, 1, 1, 6, "title")]}

	async def split(*_args, **_kwargs):
		return [], [], 0

	# when
	plan = asyncio.run(
		plan_v2_sites(
			cast(LspClient, client),
			tmp_path,
			[tmp_path, tmp_path / "src"],
			old_name="label",
			new_name="title",
			declarations=[Declaration(model, 2, "Job.label")],
			is_member=True,
			parameter=False,
			edits=edits,
			split_attrs=split,
			is_unlinked_attr=_is_unlinked_attr,
		)
	)
	# then
	use = (tmp_path / "src/pkg/use.py").resolve()
	assert plan.certain_count == 1
	assert _apply("a = Job(label='a')", plan.certain[use]) == "a = Job(title='a')"
	assert [(site.id, site.kind, site.line) for site in plan.optional] == [("u1", "keyword", 4)]


def _signature(text: str, qualname: str):
	definition = next(d for d in definitions_in(text) if d.qualname == qualname)
	return read_signature(SourceMap(text), tokenize_source(text), definition)


@pytest.mark.parametrize(
	("spec", "expected"),
	[
		({"name": "x", "default": "0"}, True),
		({"name": "x", "default": "0", "keyword_only": False}, False),
		({"name": "x", "default": "0", "position": 1}, False),
		({"name": "x", "default": "0", "keyword_only": True, "position": 1}, True),
		({"name": "x", "value": "0"}, False),
	],
)
def test_given_added_parameter_then_keyword_only_by_default_only_with_a_default_and_no_position(spec, expected):
	assert added_keyword_only(spec) is expected


def test_given_default_without_position_when_new_params_then_appended_keyword_only_after_star(tmp_path):
	text = "def f(a, b=1):\n\tpass\n\n\ndef g(a, *args, **kw):\n\tpass\n"
	plain, _, _ = new_params(_signature(text, "f"), Change(add=[{"name": "c", "default": "2"}]), "f")
	assert render_params(plain, multiline=False, indent="", unit="\t") == "a, b=1, *, c=2"
	with_var, _, _ = new_params(_signature(text, "g"), Change(add=[{"name": "c", "default": "2"}]), "g")
	assert render_params(with_var, multiline=False, indent="", unit="\t") == "a, *args, c=2, **kw"
	positional, _, _ = new_params(
		_signature(text, "f"), Change(add=[{"name": "c", "default": "2", "keyword_only": False}]), "f"
	)
	assert render_params(positional, multiline=False, indent="", unit="\t") == "a, b=1, c=2"
