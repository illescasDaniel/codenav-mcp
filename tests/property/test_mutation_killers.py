"""Example tests added after the mutmut run on signature, plan_merge, args and reply (see hardening/FINDINGS-h3.md).

Each test pins behaviour a surviving mutant could change without any other test noticing.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Literal

import pytest
from mcp_nav_shared.edits import EditPlan, FileChange, TextEdit, hash_bytes
from mcp_nav_shared.errors import ToolInputError
from pydantic import StrictBool

from codenav_mcp.args import argument_problem, resolve_aliases
from codenav_mcp.plan_merge import _line_matches, add_sites, merge_plans, site_followup, track_optional_files
from codenav_mcp.plan_sites import OptionalSite
from codenav_mcp.reply import Page, Reply, call, paginate
from codenav_mcp.signature import (
	Change,
	CommentedCallError,
	Param,
	SourceMap,
	_default_insert_index,
	added_keyword_only,
	apply_offset_edits,
	paren_span,
	read_call_args,
	render_params,
	tokenize_source,
)

from .sigcases import rewrite, signature_of


# -- apply_offset_edits ----------------------------------------------------------------------------------------


def test_given_edits_that_touch_then_both_apply():
	assert apply_offset_edits("abcd", [(0, 2, "X"), (2, 4, "Y")]) == "XY"


def test_given_an_insert_at_the_end_of_a_replaced_span_then_it_follows_the_replacement_whatever_the_order_given():
	assert apply_offset_edits("abcd", [(2, 2, "I"), (0, 2, "R")]) == "RIcd"
	assert apply_offset_edits("abcd", [(0, 2, "R"), (2, 2, "I")]) == "RIcd"


def test_given_inserts_at_one_point_then_they_keep_the_order_given():
	assert apply_offset_edits("xy", [(1, 1, "b"), (1, 1, "a")]) == "xbay"


def test_given_edits_in_any_order_then_the_text_is_edited_by_position():
	assert apply_offset_edits("abcde", [(4, 5, "Z"), (0, 1, "A")]) == "AbcdZ"


def test_given_an_overlap_between_the_first_two_of_three_edits_then_it_is_refused():
	with pytest.raises(ToolInputError, match="overlap"):
		apply_offset_edits("0123456789", [(0, 5, ""), (2, 3, ""), (7, 8, "")])


def test_given_an_overlap_between_the_last_two_of_three_edits_then_it_is_refused():
	with pytest.raises(ToolInputError, match="overlap"):
		apply_offset_edits("0123456789", [(0, 1, ""), (3, 8, ""), (5, 6, "")])


def test_given_no_edits_then_the_text_is_unchanged():
	assert apply_offset_edits("abc", []) == "abc"


# -- SourceMap and paren_span --------------------------------------------------------------------------------------


TEXT = "x = 1\ns = 'é𝄞'; f(1,\n\t2)\n"


def test_given_non_ascii_text_then_offsets_count_characters_not_bytes():
	smap = SourceMap(TEXT)
	tree = ast.parse(TEXT)
	string = next(n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "é𝄞")
	assert smap.segment(string) == "'é𝄞'"
	call_node = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
	assert smap.segment(call_node) == "f(1,\n\t2)"
	assert smap.offset(1, 0) == 0
	assert smap.offset(2, 0) == len("x = 1\n")
	assert smap.offset(4, 0) == len(TEXT), "the line after the last maps to the end of the text"


def test_given_a_node_ending_after_a_non_bmp_character_then_the_lsp_end_counts_utf16_units():
	smap = SourceMap(TEXT)
	string = next(n for n in ast.walk(ast.parse(TEXT)) if isinstance(n, ast.Constant) and n.value == "é𝄞")
	line, column = smap.lsp_end(string)
	assert line == 1  # 0-based
	assert column == len("s = '") + 1 + 2 + 1, "é is one UTF-16 unit and the astral clef two"
	call_node = next(n for n in ast.walk(ast.parse(TEXT)) if isinstance(n, ast.Call))
	assert smap.lsp_end(call_node) == (2, len("\t2)"))


@pytest.mark.parametrize(
	("source", "inner"),
	[
		("def f(a={1: [2]}, b=(3, 4)): pass", "a={1: [2]}, b=(3, 4)"),
		("def f[T](a: dict[str, T] = {}): pass", "a: dict[str, T] = {}"),
		("def f(a=[(1)]): pass", "a=[(1)]"),
		("def f(a={'k': (1, 2)}): pass", "a={'k': (1, 2)}"),
	],
)
def test_given_brackets_inside_the_parameters_then_the_span_is_the_whole_parameter_list(source, inner):
	smap = SourceMap(source)
	span = paren_span(smap, tokenize_source(source), source.index("f"))
	assert span is not None
	assert source[span[0] : span[1]] == inner


def test_given_a_start_after_the_first_parenthesis_then_the_next_one_is_used():
	source = "f(1)(2)"
	span = paren_span(SourceMap(source), tokenize_source(source), 4)
	assert span is not None and source[span[0] : span[1]] == "2"


def test_given_text_without_a_parenthesis_then_there_is_no_span():
	source = "x = [1, 2]"
	assert paren_span(SourceMap(source), tokenize_source(source), 0) is None


# -- parameters ------------------------------------------------------------------------------------------------------


def test_given_every_kind_of_parameter_then_they_render_in_python_order():
	params = [
		Param("a", "posonly"),
		Param("b", "normal", default="1"),
		Param("args", "vararg"),
		Param("c", "kwonly", annotation="int", default="2"),
		Param("kw", "kwarg"),
	]
	assert render_params(params, multiline=False, indent="", unit="\t") == "a, /, b=1, *args, c: int = 2, **kw"


def test_given_keyword_only_params_without_a_vararg_then_a_bare_star_precedes_them():
	params = [Param("a", "normal"), Param("c", "kwonly")]
	assert render_params(params, multiline=False, indent="", unit="\t") == "a, *, c"


def test_given_multiline_params_then_each_is_on_its_own_line_with_a_trailing_comma():
	params = [Param("a", "normal"), Param("b", "normal")]
	assert render_params(params, multiline=True, indent="\t", unit="\t") == "\n\t\ta,\n\t\tb,\n\t"


@pytest.mark.parametrize(
	("spec", "expected"),
	[
		({"name": "n", "default": 1}, True),
		({"name": "n", "default": 0}, True),
		({"name": "n"}, False),
		({"name": "n", "default": 1, "position": 0}, False),
		({"name": "n", "default": 1, "keyword_only": False}, False),
		({"name": "n", "keyword_only": True}, True),
	],
)
def test_given_an_added_parameter_then_it_is_keyword_only_when_asked_or_by_default_when_appended(spec, expected):
	assert added_keyword_only(spec) is expected


def test_given_a_positional_add_to_a_signature_with_varargs_then_it_goes_before_the_star_args():
	definition = "def f(a, *args, **kw):\n\tpass\n"
	outcome = rewrite(
		definition,
		Change(add=[{"name": "n", "default": 1, "keyword_only": False, "value": 5}]),
		"result = f(1)",
		method=False,
		qualname="f",
	)
	assert outcome.new_definition == "def f(a, n=1, *args, **kw):\n\tpass\n"


def test_given_a_keyword_only_add_to_a_signature_with_varargs_then_it_goes_after_the_star_args_and_before_kwargs():
	definition = "def f(a, *args, **kw):\n\tpass\n"
	outcome = rewrite(
		definition, Change(add=[{"name": "n", "default": 1, "value": 5}]), "result = f(1)", method=False, qualname="f"
	)
	assert outcome.new_definition == "def f(a, *args, n=1, **kw):\n\tpass\n"


def test_given_a_keyword_only_add_to_a_signature_without_varargs_then_a_star_is_written():
	outcome = rewrite(
		"def f(a, **kw):\n\tpass\n",
		Change(add=[{"name": "n", "default": 1, "value": 5}]),
		"result = f(1)",
		method=False,
		qualname="f",
	)
	assert outcome.new_definition == "def f(a, *, n=1, **kw):\n\tpass\n"


def test_given_keyword_only_defaults_and_a_missing_one_then_the_signature_is_read_with_each_default():
	signature = signature_of("def f(a, *, b=2, c):\n\tpass\n", "f")
	assert [(p.name, p.kind, p.default) for p in signature.callable_params] == [
		("a", "normal", None),
		("b", "kwonly", "2"),
		("c", "kwonly", None),
	]


def test_given_a_reorder_across_the_slash_then_it_is_refused():
	with pytest.raises(ToolInputError, match="across"):
		rewrite("def f(a, /, b):\n\tpass\n", Change(reorder=["b", "a"]), "result = f(1, 2)", method=False, qualname="f")


def test_given_a_reorder_within_groups_then_it_is_accepted():
	outcome = rewrite(
		"def f(a, b, *, c, d):\n\tpass\n",
		Change(reorder=["b", "a", "d", "c"]),
		"result = f(1, 2, c=3, d=4)",
		method=False,
		qualname="f",
	)
	assert outcome.new_definition == "def f(b, a, *, d, c):\n\tpass\n"


# -- reply ---------------------------------------------------------------------------------------------------------------


def test_given_non_ascii_arguments_then_a_call_keeps_them_readable():
	assert call("lookup", name="ñandú 𝄞") == 'lookup(name="ñandú 𝄞")'


def test_given_none_arguments_then_a_call_leaves_them_out():
	assert call("lookup", name="X", show=None, offset=0) == 'lookup(name="X", offset=0)'


def test_given_a_cut_page_then_add_page_writes_the_rest_count_and_the_next_call_with_the_given_arguments():
	reply = Reply("answer")
	page = paginate(list(range(10)), 3, 3)
	reply.add_page(page, lambda item: f"item {item}", "lookup", name="X", show=["callers"])
	assert reply.items == ["item 3", "item 4", "item 5", "... 4 more"]
	assert reply.next_calls == ['lookup(name="X", show=["callers"], offset=6)']


def test_given_the_last_page_then_add_page_adds_no_next_call():
	reply = Reply("answer")
	reply.add_page(Page([1, 2], 8, 10), str, "lookup", name="X")
	assert reply.items == ["1", "2"] and reply.next_calls == []


def test_given_notes_and_next_calls_then_render_lists_them_after_the_items():
	reply = Reply("answer", ["one"], ["careful", "very"], ['lookup(name="X")'])
	assert reply.render() == 'answer\none\nNotes:\n  - careful\n  - very\nnext: lookup(name="X")'
	assert Reply("only").render() == "only"


def test_given_a_non_positive_limit_or_negative_offset_then_paginate_refuses():
	with pytest.raises(ValueError, match="limit must be at least 1"):
		paginate([1], 0)
	with pytest.raises(ValueError, match="offset must be at least 0"):
		paginate([1], 1, -1)
	assert paginate([1, 2, 3], 1, 0).items == [1]


# -- args -----------------------------------------------------------------------------------------------------------------


def test_given_an_alias_and_its_target_with_different_values_then_the_call_is_refused_naming_both():
	result, problem = resolve_aliases("t", {"file_path"}, {"file": "a.py", "file_path": "b.py"})
	assert problem is not None and "'file'" in problem and "'file_path'" in problem
	assert result == {"file": "a.py", "file_path": "b.py"}


def test_given_an_alias_and_its_target_with_the_same_value_then_one_argument_remains():
	assert resolve_aliases("t", {"file_path"}, {"file": "a.py", "file_path": "a.py"}) == ({"file_path": "a.py"}, None)


def test_given_an_explicit_null_for_an_alias_then_it_adds_nothing_to_the_value_already_given():
	assert resolve_aliases("t", {"file_path"}, {"file_path": "a.py", "file": None}) == ({"file_path": "a.py"}, None)


def test_given_an_alias_for_a_parameter_the_tool_lacks_then_the_name_is_left_alone():
	assert resolve_aliases("t", {"name"}, {"file": "a.py"}) == ({"file": "a.py"}, None)


# -- plan_merge: merging plans -------------------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
	return tmp_path.resolve()


def test_given_plans_with_notes_hashes_and_a_noop_first_then_everything_is_carried_into_the_merge(workspace):
	a, b = workspace / "a.py", workspace / "b.py"
	noop = FileChange(a, "same\n", "same\n")
	plan_one = EditPlan(
		"one",
		[noop, FileChange(b, "1\n2\n", "1\nX\n", True, True, old_mode=0o644, new_mode=0o755)],
		["n1"],
		{b: "hash-b"},
	)
	plan_two = EditPlan(
		"two",
		[FileChange(b, "1\n2\n", "Y\n2\n", True, True, old_mode=0o644, new_mode=0o755)],
		["n2", "n3"],
		{b: "other"},
	)
	merged = merge_plans([("first", plan_one), ("second", plan_two)], "both")
	assert merged.description == "both"
	assert merged.notes == ["first: n1", "second: n2", "second: n3"]
	assert [change.path for change in merged.changes] == [b], (
		"the no-op change is dropped and the next one still merged"
	)
	change = merged.changes[0]
	assert change.new_text == "Y\nX\n"
	assert (change.old_bom, change.new_bom, change.old_mode, change.new_mode) == (True, True, 0o644, 0o755)
	assert merged.base_hashes == {b: "hash-b"}, "the first plan's baseline for a file wins"


def test_given_a_single_plan_then_its_change_comes_through_untouched(workspace):
	path = workspace / "a.py"
	change = FileChange(path, "a\n", "b\n")
	assert merge_plans([("only", EditPlan("p", [change]))], "d").changes == [change]


def test_given_two_plans_where_one_creates_the_file_then_the_merge_is_refused_naming_the_labels(workspace):
	path = workspace / "a.py"
	with pytest.raises(ToolInputError, match="first and second both change a.py"):
		merge_plans(
			[
				("first", EditPlan("p", [FileChange(path, None, "new\n")])),
				("second", EditPlan("p", [FileChange(path, "old\n", "x\n")])),
			],
			"d",
		)


def test_given_two_plans_from_different_contents_then_the_merge_is_refused(workspace):
	path = workspace / "a.py"
	with pytest.raises(ToolInputError, match="different contents"):
		merge_plans(
			[
				("first", EditPlan("p", [FileChange(path, "a\n", "b\n")])),
				("second", EditPlan("p", [FileChange(path, "c\n", "d\n")])),
			],
			"d",
		)


def test_given_overlapping_hunks_then_the_refusal_names_both_labels_and_the_line(workspace):
	path = workspace / "a.py"
	with pytest.raises(ToolInputError, match=r"first and second both change a.py around line 2"):
		merge_plans(
			[
				("first", EditPlan("p", [FileChange(path, "1\n2\n3\n", "1\nA\n3\n")])),
				("second", EditPlan("p", [FileChange(path, "1\n2\n3\n", "1\nB\n3\n")])),
			],
			"d",
		)


# -- plan_merge: optional sites --------------------------------------------------------------------------------------


def _site(path: str, index: int, text: str, *, kind: str = "text") -> OptionalSite:
	return OptionalSite(f"u{index}", path, index + 1, kind, text.strip()[:140], TextEdit(index, 0, index, 1, "Z"))


def test_given_a_snippet_cut_at_140_characters_then_a_longer_line_matches_by_prefix():
	long_line = "x" + "y" * 200
	assert _line_matches([long_line + "\n"], 0, long_line[:140])
	assert not _line_matches(["x" + "y" * 200 + "\n"], 0, "x" + "y" * 100), "a short snippet needs the whole line"
	assert _line_matches(["abc\n"], 0, "abc")
	assert not _line_matches(["abc\n"], 1, "abc"), "an index past the end never matches"
	assert not _line_matches(["abc\n"], -1, "abc")


def test_given_a_plan_that_creates_the_file_then_a_site_in_it_is_refused(workspace):
	path = workspace / "a.py"
	path.write_text("a\n")
	plan = EditPlan("p", [FileChange(path, None, "a\n")])
	with pytest.raises(ToolInputError, match="created or deleted"):
		add_sites(plan, [_site("a.py", 0, "a")], workspace)


def test_given_a_plan_that_deletes_the_file_then_a_site_in_it_is_refused(workspace):
	path = workspace / "a.py"
	path.write_text("a\n")
	plan = EditPlan("p", [FileChange(path, "a\n", None)])
	with pytest.raises(ToolInputError, match="created or deleted"):
		site_followup(plan, [_site("a.py", 0, "a")], workspace)


def test_given_a_site_outside_the_workspace_then_it_is_refused_naming_the_site(workspace):
	with pytest.raises(ToolInputError, match=r"u0: \.\./x\.py is outside the workspace"):
		add_sites(EditPlan("p", []), [_site("../x.py", 0, "a")], workspace)


def test_given_a_site_in_a_file_that_is_gone_then_it_is_refused(workspace):
	with pytest.raises(ToolInputError, match="u0: gone.py no longer exists"):
		add_sites(EditPlan("p", []), [_site("gone.py", 0, "a")], workspace)


def test_given_sites_then_the_description_counts_them_and_the_notes_and_baseline_are_kept(workspace):
	path = workspace / "a.py"
	path.write_text("aa\nbb\ncc\n")
	plan = EditPlan("do it", [], ["note"], {path: "h"})
	one = add_sites(plan, [_site("a.py", 0, "aa")], workspace)
	two = add_sites(plan, [_site("a.py", 0, "aa"), _site("a.py", 1, "bb")], workspace)
	assert one.description == "do it (+1 optional site)" and two.description == "do it (+2 optional sites)"
	assert two.notes == ["note"] and two.base_hashes == {path: "h"}
	assert two.changes[0].new_text == "Za\nZb\ncc\n"
	assert add_sites(plan, [], workspace) is plan


def test_given_a_site_on_a_line_the_plan_edited_in_place_then_the_followup_moves_it_within_the_line(workspace):
	path = workspace / "a.py"
	path.write_text("one = 1\nfoo(bar)\n")
	site = OptionalSite("u0", "a.py", 2, "text", "foo(bar)", TextEdit(1, 4, 1, 7, "baz"))
	plan = EditPlan("p", [FileChange(path, "one = 1\nfoo(bar)\n", "one = 1\nfoo(x, bar)\n")])
	follow = site_followup(plan, [site], workspace)
	assert follow is not None
	carried = follow.optional_sites[0]
	assert (carried.line, carried.text) == (2, "foo(x, bar)")
	assert (carried.edit.start_line, carried.edit.start_character, carried.edit.end_character) == (1, 7, 10)
	assert follow.plan.description == "Follow-up to p" and follow.plan.changes == []
	assert path in follow.plan.base_hashes


def test_given_a_site_on_a_line_that_moved_down_then_the_followup_points_at_its_new_line(workspace):
	path = workspace / "a.py"
	path.write_text("aa\nbb\n")
	plan = EditPlan("p", [FileChange(path, "aa\nbb\n", "new\naa\nbb\n")])
	follow = site_followup(plan, [_site("a.py", 1, "bb")], workspace)
	assert follow is not None
	assert (follow.optional_sites[0].line, follow.optional_sites[0].edit.start_line) == (3, 2)
	assert site_followup(plan, [], workspace) is None


def test_given_a_site_whose_line_the_plan_replaced_then_it_is_refused(workspace):
	path = workspace / "a.py"
	path.write_text("aa\nbb\n")
	plan = EditPlan("p", [FileChange(path, "aa\nbb\n", "aa\nQQ\nRR\n")])
	with pytest.raises(ToolInputError, match=r"u1 \(a.py:2\) overlaps a change"):
		site_followup(plan, [_site("a.py", 1, "bb")], workspace)


def test_given_a_site_on_a_line_that_no_longer_reads_what_it_did_then_it_is_refused_naming_the_text(workspace):
	path = workspace / "a.py"
	path.write_text("zz\n")
	with pytest.raises(ToolInputError, match=r"no longer matches the file \(`aa`\)"):
		add_sites(EditPlan("p", []), [_site("a.py", 0, "aa")], workspace)


# -- call rewriting: the paths the property run reaches rarely (mutmut found `_keyword_call` unreached) ---------------


D3 = "def f(a, b, c):\n\tpass\n"


def _rewritten(definition, change, statement, qualname="f", *, method=False):
	outcome = rewrite(definition, change, statement, method=method, qualname=qualname)
	return outcome.new_statement, outcome.reason


def test_given_a_reorder_with_effectful_arguments_then_each_is_named_in_the_order_written():
	assert _rewritten(D3, Change(reorder=["c", "a", "b"]), "result = f(g(), h(), k())") == (
		"result = f(a=g(), b=h(), c=k())",
		None,
	)


def test_given_a_reorder_with_only_constants_then_the_arguments_swap_positions():
	assert _rewritten(D3, Change(reorder=["c", "a", "b"]), "result = f(1, 2, 3)") == ("result = f(3, 1, 2)", None)


def test_given_a_reorder_with_one_effectful_argument_then_every_positional_argument_is_named():
	assert _rewritten(D3, Change(reorder=["c", "a", "b"]), "result = f(1, h(), 3)") == (
		"result = f(a=1, b=h(), c=3)",
		None,
	)


def test_given_a_reorder_of_a_method_called_through_the_class_then_the_receiver_stays_first():
	statement, _ = _rewritten(
		"class A:\n\tdef m(self, a, b):\n\t\tpass\n",
		Change(reorder=["b", "a"]),
		"result = A.m(inst, g(), h())",
		"A.m",
		method=True,
	)
	assert statement == "result = A.m(inst, a=g(), b=h())"


def test_given_keyword_arguments_only_then_a_reorder_keeps_every_binding():
	statement, _ = _rewritten(D3, Change(reorder=["b", "a", "c"]), "result = f(a=1, b=2, c=3)")
	assert statement is not None
	call_node = next(n for n in ast.walk(ast.parse(statement)) if isinstance(n, ast.Call))
	assert not call_node.args and {k.arg: ast.unparse(k.value) for k in call_node.keywords} == {
		"a": "1",
		"b": "2",
		"c": "3",
	}


@pytest.mark.parametrize(
	("change", "statement", "expected"),
	[
		(Change(remove=["b"]), "result = f(g(), 2, k())", "result = f(g(), k())"),
		(Change(remove=["b"]), "result = f(1, 2, 3)", "result = f(1, 3)"),
		(Change(remove=["a"]), "result = f(1, 2, 3)", "result = f(2, 3)"),
		(Change(remove=["c"]), "result = f(1, 2, 3)", "result = f(1, 2)"),
		(Change(remove=["a", "b", "c"]), "result = f(1, 2, 3)", "result = f()"),
		(Change(remove=["b"]), "result = f(1, 2, 3)  # keep", "result = f(1, 3)  # keep"),
		(Change(remove=["c"]), "result = f(\n\t1,\n\t2,\n\t3,\n)", "result = f(\n\t1,\n\t2,\n)"),
	],
)
def test_given_removed_parameters_then_their_arguments_are_dropped_and_the_rest_kept_as_written(
	change, statement, expected
):
	assert _rewritten(D3, change, statement) == (expected, None)


def test_given_a_removed_keyword_argument_then_the_others_stay():
	assert _rewritten("def f(a, b):\n\tpass\n", Change(remove=["a"]), "result = f(b=2, a=1)") == (
		"result = f(b=2)",
		None,
	)
	assert _rewritten("def f(a, b=2):\n\tpass\n", Change(remove=["a"]), "result = f(1)") == ("result = f()", None)


@pytest.mark.parametrize(
	("definition", "change", "statement", "reason"),
	[
		(D3, Change(remove=["b"]), "result = f(g(), h(), k())", "discard side effects"),
		("def f(a, /, b):\n\tpass\n", Change(remove=["a"]), "result = f(g(), 2)", "discard side effects"),
		(D3, Change(remove=["b"]), "result = f(*xs)", "unpacking"),
		(D3, Change(remove=["b"]), "result = f(a, **kw)", "unpacking"),
		("def f(a, b, *args):\n\tpass\n", Change(reorder=["b", "a"]), "result = f(g(), h(), 3)", "captured by"),
	],
)
def test_given_a_call_the_tool_cannot_rewrite_safely_then_it_says_why(definition, change, statement, reason):
	new_statement, why = _rewritten(definition, change, statement)
	assert new_statement is None and why is not None and reason in why


def test_given_a_call_with_comments_in_its_arguments_then_it_is_left_to_the_user():
	with pytest.raises(CommentedCallError):
		_rewritten(D3, Change(remove=["b"]), "result = f(1, # why\n\t2, 3)")


def test_given_a_positional_only_parameter_removed_from_a_call_then_the_rest_still_binds_positionally():
	assert _rewritten("def f(a, b, /):\n\tpass\n", Change(remove=["a"]), "result = f(1, 2)") == ("result = f(2)", None)


@pytest.mark.parametrize(
	("definition", "change", "statement", "new_def", "expected"),
	[
		(
			"def f():\n\tpass\n",
			Change(add=[{"name": "n", "value": 9, "default": 1}]),
			"result = f()",
			"def f(*, n=1):",
			"result = f(n=9)",
		),
		(
			"def f(a, b):\n\tpass\n",
			Change(add=[{"name": "n", "value": 9, "position": 1}]),
			"result = f(1, 2)",
			"def f(a, n, b):",
			"result = f(1, n=9, b=2)",
		),
		(
			"def f(a, b):\n\tpass\n",
			Change(add=[{"name": "n", "value": 9, "position": 0}]),
			"result = f(1, 2)",
			"def f(n, a, b):",
			"result = f(n=9, a=1, b=2)",
		),
	],
)
def test_given_an_added_parameter_then_the_definition_and_the_call_gain_it(
	definition, change, statement, new_def, expected
):
	outcome = rewrite(definition, change, statement, method=False, qualname="f")
	assert outcome.new_definition.splitlines()[0] == new_def
	assert outcome.new_statement == expected


def test_given_an_added_argument_to_a_multiline_call_then_the_call_still_parses_and_keeps_the_old_arguments():
	outcome = rewrite(
		D3,
		Change(add=[{"name": "n", "value": 9, "default": 1}]),
		"result = f(\n\t1,\n\t2,\n\t3,\n)",
		method=False,
		qualname="f",
	)
	assert outcome.new_statement is not None
	tree = ast.parse(outcome.new_statement)
	call_node = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
	assert [ast.unparse(a) for a in call_node.args] == ["1", "2", "3"]
	assert [(k.arg, ast.unparse(k.value)) for k in call_node.keywords] == [("n", "9")]


D4 = "def f(a, b, c, d):\n\tpass\n"
KW_ADD = Change(add=[{"name": "n", "value": 9, "default": 1, "keyword_only": True}])
POS_ADD = Change(add=[{"name": "n", "value": 9, "default": 1, "keyword_only": False}])


def _first_line(definition, change, statement, qualname="f"):
	outcome = rewrite(definition, change, statement, method=False, qualname=qualname)
	return outcome.new_definition.splitlines()[0], outcome.new_statement


def test_given_a_reorder_with_effectful_and_keyword_arguments_then_keywords_stay_as_written_and_positionals_are_named():
	assert _rewritten(D3, Change(reorder=["c", "a", "b"]), "result = f(g(), b=h(), c=k())") == (
		"result = f(a=g(), b=h(), c=k())",
		None,
	)


def test_given_an_effectful_reorder_that_would_name_a_positional_only_parameter_then_it_is_refused():
	_, why = _rewritten("def f(a, /, b, c):\n\tpass\n", Change(reorder=["a", "c", "b"]), "result = f(g(), h(), k())")
	assert why is not None and "positional-only parameter 'a'" in why


def test_given_a_reorder_and_a_removal_of_a_constant_then_the_remaining_effectful_arguments_are_all_kept_in_order():
	assert _rewritten(D3, Change(reorder=["c", "a"], remove=["b"]), "result = f(g(), 2, h())") == (
		"result = f(a=g(), c=h())",
		None,
	)


def test_given_an_added_parameter_with_an_annotation_then_it_is_written_with_it():
	definition, statement = _first_line(
		D3, Change(add=[{"name": "n", "value": 9, "annotation": "int", "default": 1}]), "result = f(1, 2, 3)"
	)
	assert definition == "def f(a, b, c, *, n: int = 1):" and statement == "result = f(1, 2, 3, n=9)"


def test_given_an_added_parameter_with_an_unknown_key_then_it_is_refused_naming_the_key():
	with pytest.raises(ToolInputError, match=r"unknown key\(s\) 'bogus'"):
		rewrite(
			D3, Change(add=[{"name": "n", "value": 9, "bogus": 1}]), "result = f(1, 2, 3)", method=False, qualname="f"
		)


def test_given_an_added_parameter_without_a_name_then_it_is_refused():
	with pytest.raises(ToolInputError, match="not a valid parameter name"):
		rewrite(D3, Change(add=[{"value": 9}]), "result = f(1, 2, 3)", method=False, qualname="f")


def test_given_an_added_parameter_at_the_end_position_then_it_is_last_and_past_it_is_refused():
	assert _first_line(D3, Change(add=[{"name": "n", "value": 9, "position": 3}]), "result = f(1, 2, 3)") == (
		"def f(a, b, c, n):",
		"result = f(1, 2, 3, n=9)",
	)
	with pytest.raises(ToolInputError, match=r"out of range \(0..3\)"):
		rewrite(
			D3,
			Change(add=[{"name": "n", "value": 9, "position": 4}]),
			"result = f(1, 2, 3)",
			method=False,
			qualname="f",
		)


def test_given_a_reorder_that_leaves_out_a_parameter_then_it_is_refused_listing_the_parameters():
	with pytest.raises(ToolInputError, match="exactly these parameters: a, b, c"):
		rewrite(D3, Change(reorder=["a", "b"]), "result = f(1, 2, 3)", method=False, qualname="f")


@pytest.mark.parametrize(
	("definition", "change", "expected"),
	[
		("def f(a, b=1, *, k=2):\n\tpass\n", KW_ADD, "def f(a, b=1, *, k=2, n=1):"),
		("def f(a, b=1, *, k=2):\n\tpass\n", POS_ADD, "def f(a, b=1, n=1, *, k=2):"),
		("def f(a, b=1, *, k=2, **kw):\n\tpass\n", KW_ADD, "def f(a, b=1, *, k=2, n=1, **kw):"),
		("def f(a, *args, **kw):\n\tpass\n", POS_ADD, "def f(a, n=1, *args, **kw):"),
		("def f(a, **kw):\n\tpass\n", KW_ADD, "def f(a, *, n=1, **kw):"),
		("def f(a):\n\tpass\n", KW_ADD, "def f(a, *, n=1):"),
	],
)
def test_given_an_added_parameter_then_it_joins_the_end_of_its_own_group(definition, change, expected):
	assert _first_line(definition, change, "result = f(1)")[0] == expected


def test_given_removing_one_of_four_arguments_of_a_multiline_call_then_the_rest_keep_their_lines_and_indent():
	assert _rewritten(D4, Change(remove=["b"]), "result = f(\n\t1,\n\t2,\n\t3,\n\t4,\n)") == (
		"result = f(\n\t1,\n\t3,\n\t4,\n)",
		None,
	)


def test_given_a_comment_right_after_the_opening_parenthesis_then_the_call_is_left_to_the_user():
	with pytest.raises(CommentedCallError):
		_rewritten(D3, Change(remove=["b"]), "result = f(# c\n\t1, 2, 3)")


def test_given_a_comment_outside_the_arguments_then_the_call_is_still_rewritten():
	assert _rewritten(D3, Change(remove=["b"]), "# before\nresult = f(1, 2, 3)  # after") == (
		"# before\nresult = f(1, 3)  # after",
		None,
	)


# -- plan_merge: files, BOMs, modes and baselines -------------------------------------------------------------------


def test_given_a_file_with_a_bom_that_the_plan_does_not_touch_then_a_site_keeps_the_bom_and_the_baseline_is_recorded(
	workspace,
):
	path = workspace / "a.py"
	original = b"\xef\xbb\xbfaa\nbb\n"
	path.write_bytes(original)
	plan = add_sites(EditPlan("p", []), [_site("a.py", 0, "aa")], workspace)
	change = plan.changes[0]
	assert (change.old_bom, change.new_bom) == (True, True)
	assert change.new_text == "Za\nbb\n"
	assert plan.base_hashes == {path: hash_bytes(original)}


def test_given_a_plan_that_changes_mode_and_bom_then_adding_a_site_keeps_them(workspace):
	path = workspace / "a.py"
	path.write_text("aa\nbb\n")
	change = FileChange(path, "aa\nbb\n", "aa\nbb\ncc\n", True, False, old_mode=0o644, new_mode=0o755)
	result = add_sites(EditPlan("p", [change]), [_site("a.py", 0, "aa")], workspace).changes[0]
	assert (result.old_bom, result.new_bom, result.old_mode, result.new_mode) == (True, False, 0o644, 0o755)
	assert result.new_text == "Za\nbb\ncc\n"


def test_given_a_site_edit_that_breaks_python_then_a_py_or_pyi_file_is_refused_and_other_files_are_not(workspace):
	for name in ("a.py", "a.pyi", "a.PY"):
		(workspace / name).write_text("x = 1\n")
	breaking = TextEdit(0, 0, 0, 1, "(")
	for name in ("a.py", "a.pyi", "a.PY"):
		site = OptionalSite("u0", name, 1, "text", "x = 1", breaking)
		with pytest.raises(ToolInputError):
			add_sites(EditPlan("p", []), [site], workspace)
	(workspace / "a.txt").write_text("x = 1\n")
	site = OptionalSite("u0", "a.txt", 1, "text", "x = 1", breaking)
	assert add_sites(EditPlan("p", []), [site], workspace).changes[0].new_text == "( = 1\n"


def test_given_a_site_in_an_untouched_file_then_the_followup_records_the_new_baselines_of_every_changed_file(workspace):
	a, b, gone = workspace / "a.py", workspace / "b.py", workspace / "gone.py"
	a.write_text("aa\n")
	b.write_text("bb\n")
	gone.write_text("old\n")
	plan = EditPlan(
		"p",
		[FileChange(b, "bb\n", "BB\n"), FileChange(gone, "old\n", None)],
		base_hashes={b: "old-b", gone: "old-gone"},
	)
	follow = site_followup(plan, [_site("a.py", 0, "aa")], workspace)
	assert follow is not None
	assert follow.plan.base_hashes[b] == hash_bytes(b"BB\n")
	assert follow.plan.base_hashes[gone] is None
	assert follow.plan.base_hashes[a] == hash_bytes(b"aa\n")


def test_given_a_long_line_then_the_followup_shows_its_first_140_characters(workspace):
	path = workspace / "a.py"
	line = "x = " + "1" * 300
	path.write_text(line + "\nother\n")
	site = OptionalSite("u0", "a.py", 1, "text", line[:140], TextEdit(0, 0, 0, 1, "y"))
	plan = EditPlan("p", [FileChange(path, line + "\nother\n", "new\n" + line + "\nother\n")])
	follow = site_followup(plan, [site], workspace)
	assert follow is not None and follow.optional_sites[0].text == line[:140]


def test_given_an_edit_that_ends_where_an_unchanged_stretch_of_a_changed_line_ends_then_it_is_carried(workspace):
	path = workspace / "a.py"
	path.write_text("one = 1\nfoo(bar)\n")
	site = OptionalSite("u0", "a.py", 2, "text", "foo(bar)", TextEdit(1, 4, 1, 8, "baz)"))
	plan = EditPlan("p", [FileChange(path, "one = 1\nfoo(bar)\n", "one = 1\nfoo(x, bar)\n")])
	follow = site_followup(plan, [site], workspace)
	assert follow is not None
	assert (follow.optional_sites[0].edit.start_character, follow.optional_sites[0].edit.end_character) == (7, 11)


def test_given_a_site_edit_spanning_the_text_the_plan_changed_on_its_line_then_it_is_refused(workspace):
	path = workspace / "a.py"
	path.write_text("foo(bar)\n")
	site = OptionalSite("u0", "a.py", 1, "text", "foo(bar)", TextEdit(0, 2, 0, 6, "ZZ"))
	plan = EditPlan("p", [FileChange(path, "foo(bar)\n", "foo(x, bar)\n")])
	with pytest.raises(ToolInputError, match="overlaps a change"):
		site_followup(plan, [site], workspace)


def test_given_a_site_line_that_the_plan_leaves_alone_in_a_shifted_file_then_the_edit_moves_with_it_in_both_directions(
	workspace,
):
	path = workspace / "a.py"
	path.write_text("aa\nbb\ncc\n")
	up = site_followup(
		EditPlan("p", [FileChange(path, "aa\nbb\ncc\n", "bb\ncc\n")]), [_site("a.py", 2, "cc")], workspace
	)
	assert up is not None and up.optional_sites[0].edit.start_line == 1


def test_given_a_plan_and_an_untracked_optional_file_then_its_baseline_is_added_and_existing_baselines_kept(workspace):
	a, b = workspace / "a.py", workspace / "b.py"
	a.write_text("aa\n")
	b.write_text("bb\n")
	tracked = track_optional_files(
		EditPlan("p", [], base_hashes={a: "kept"}), [_site("a.py", 0, "aa"), _site("b.py", 0, "bb")], workspace
	)
	assert tracked.base_hashes == {a: "kept", b: hash_bytes(b"bb\n")}


def test_given_three_plans_where_a_later_one_deletes_the_file_then_the_refusal_names_the_first_two_labels(workspace):
	path = workspace / "a.py"
	with pytest.raises(
		ToolInputError, match=r"first and second both change a.py, and one of them creates or deletes it"
	):
		merge_plans(
			[
				("first", EditPlan("p", [FileChange(path, "a\n", "b\n")])),
				("second", EditPlan("p", [FileChange(path, "a\n", None)])),
				("third", EditPlan("p", [FileChange(path, "a\n", "c\n")])),
			],
			"d",
		)


def test_given_plans_merged_from_a_file_with_a_bom_then_the_bom_flags_survive(workspace):
	path = workspace / "a.py"
	merged = merge_plans(
		[
			("first", EditPlan("p", [FileChange(path, "1\n2\n", "A\n2\n", True, True)])),
			("second", EditPlan("p", [FileChange(path, "1\n2\n", "1\nB\n", True, True)])),
		],
		"d",
	)
	change = merged.changes[0]
	assert (change.new_text, change.old_bom, change.new_bom) == ("A\nB\n", True, True)


def test_given_hunks_given_in_reverse_file_order_then_the_merge_is_by_position(workspace):
	path = workspace / "a.py"
	merged = merge_plans(
		[
			("late", EditPlan("p", [FileChange(path, "1\n2\n3\n", "1\n2\nC\n")])),
			("early", EditPlan("p", [FileChange(path, "1\n2\n3\n", "A\n2\n3\n")])),
		],
		"d",
	)
	assert merged.changes[0].new_text == "A\n2\nC\n"


# -- reading calls -------------------------------------------------------------------------------------------------------


def test_given_a_call_then_its_arguments_are_read_in_written_order_with_their_text_and_kind():
	text = "f(1, g(2), b=3, *xs, **kw)"
	call_node = ast.parse(text, mode="eval").body
	assert isinstance(call_node, ast.Call)
	args = read_call_args(SourceMap(text), call_node)
	assert [(a.text, a.keyword, a.value, a.starred) for a in args] == [
		("1", None, "1", False),
		("g(2)", None, "g(2)", False),
		("b=3", "b", "3", False),
		("*xs", None, "*xs", True),
		("**kw", None, "kw", True),
	]


# -- args: what a refusal says -----------------------------------------------------------------------------------------


async def _tool(ctx, name: str, line: int = 1, flag: StrictBool = False, kind: Literal["a", "b"] = "a", loose=None): ...  # noqa: ANN001


@pytest.mark.parametrize(
	("args", "expected"),
	[
		({"name": "x"}, None),
		({"name": "x", "loose": object}, None),
		({"nam": "x"}, "unknown argument 'nam' for t; did you mean 'name'?; allowed: name, line, flag, kind, loose."),
		(
			{"name": "x", "zzz": 1, "yyy": 2},
			"unknown arguments 'zzz', 'yyy' for t; allowed: name, line, flag, kind, loose.",
		),
		({"name": "x", "flag": "yes"}, "t: flag must be true or false (got 'yes')."),
		({"name": "x", "line": "1"}, "t: line must be a whole number (got '1')."),
		({"name": "x", "kind": "c"}, "t: kind must be one of 'a', 'b' (got 'c')."),
	],
)
def test_given_arguments_then_the_refusal_names_what_is_wrong_and_what_is_allowed(args, expected):
	assert argument_problem("t", _tool, args) == expected


def test_given_a_missing_required_argument_then_the_refusal_shows_the_signature():
	problem = argument_problem("t", _tool, {"line": 1})
	assert problem is not None and problem.startswith("t: missing required 'name'.")
	assert "t(name, line=..., flag=..., kind=..., loose=...)" in problem


# -- effectful arguments and where added parameters land (from the second mutmut round) --------------------------


def _new_definition(definition, change):
	return rewrite(definition, change, "result = f(1)", method=False, qualname="f").new_definition


def test_given_an_effectful_argument_the_change_does_not_touch_then_the_call_keeps_it_and_gets_the_new_value():
	assert _rewritten(
		"def f(a, b):\n\tpass\n", Change(add=[{"name": "n", "default": 1, "value": 5}]), "result = f(g(), 2)"
	) == (
		"result = f(g(), 2, n=5)",
		None,
	)


@pytest.mark.parametrize("gone", ["a", "b"])
def test_given_a_removed_parameter_whose_argument_has_side_effects_then_the_call_is_left_for_the_user(gone):
	new_statement, reason = _rewritten("def f(a, b):\n\tpass\n", Change(remove=[gone]), "result = f(g(), h())")
	assert new_statement is None
	assert reason == "changing effectful arguments could change evaluation order or discard side effects"


def test_given_effectful_keywords_written_out_of_parameter_order_then_the_call_is_left_for_the_user():
	# They would be re-emitted in parameter order, which changes the order they run in (see also FINDINGS-h3 #2).
	new_statement, reason = _rewritten(
		"def f(a, b):\n\tpass\n", Change(add=[{"name": "n", "default": 1, "value": 5}]), "result = f(b=g(), a=h())"
	)
	assert new_statement is None
	assert reason is not None and "evaluation order" in reason


def test_given_an_effectful_keyword_in_parameter_order_then_the_call_is_rewritten_with_it_kept():
	assert _rewritten(
		"def f(a, b):\n\tpass\n", Change(add=[{"name": "n", "default": 1, "value": 5}]), "result = f(1, b=g())"
	) == ("result = f(1, b=g(), n=5)", None)


def test_given_effectful_positionals_and_a_removed_constant_then_the_rest_keep_their_order():
	assert _rewritten("def f(a, b, c):\n\tpass\n", Change(remove=["b"]), "result = f(g(), 2, h())") == (
		"result = f(g(), h())",
		None,
	)


def test_given_default_insert_index_then_a_star_args_is_passed_unless_the_new_parameter_is_keyword_only():
	rest = [Param("args", "vararg"), Param("kw", "kwarg")]
	assert _default_insert_index(rest, False) == 0  # a normal parameter goes before *args
	assert _default_insert_index(rest, True) == 1  # a keyword-only one goes after *args, before **kwargs
	assert _default_insert_index([Param("a", "normal")], True) == 1
	assert _default_insert_index([], False) == 0


@pytest.mark.parametrize(
	("definition", "spec", "expected"),
	[
		("def f(*args):\n\tpass\n", {}, "def f(*args, n=2):\n\tpass\n"),
		("def f(*args, **kw):\n\tpass\n", {}, "def f(*args, n=2, **kw):\n\tpass\n"),
		("def f(a, *, k=1):\n\tpass\n", {"keyword_only": False}, "def f(a, n=2, *, k=1):\n\tpass\n"),
		("def f(a, *, k=1):\n\tpass\n", {}, "def f(a, *, k=1, n=2):\n\tpass\n"),
		("def f(a, *, k=1, **kw):\n\tpass\n", {}, "def f(a, *, k=1, n=2, **kw):\n\tpass\n"),
		("def f(a, b, *args):\n\tpass\n", {"keyword_only": False}, "def f(a, b, n=2, *args):\n\tpass\n"),
		("def f(**kw):\n\tpass\n", {"keyword_only": False}, "def f(n=2, **kw):\n\tpass\n"),
		("def f(**kw):\n\tpass\n", {}, "def f(*, n=2, **kw):\n\tpass\n"),
	],
)
def test_given_an_added_parameter_without_a_position_then_it_lands_next_to_its_own_kind(definition, spec, expected):
	assert _new_definition(definition, Change(add=[{"name": "n", "default": 2, **spec}])) == expected


def test_given_two_common_leading_arguments_then_only_the_tail_that_differs_is_rewritten():
	assert _rewritten(
		"def f(a, b, c):\n\tpass\n", Change(add=[{"name": "n", "default": 1, "value": 7}]), "result = f(1, 2, 3)"
	) == (
		"result = f(1, 2, 3, n=7)",
		None,
	)
	assert _rewritten("def f(a, b, c):\n\tpass\n", Change(remove=["c"]), "result = f(1, 2, 3)") == (
		"result = f(1, 2)",
		None,
	)
	assert _rewritten("def f(a, b, c):\n\tpass\n", Change(remove=["a"]), "result = f(1, 2, 3)") == (
		"result = f(2, 3)",
		None,
	)


def test_given_a_call_with_no_arguments_but_a_space_then_a_new_argument_goes_in_front_of_the_space():
	assert _rewritten(
		"def f(a=1):\n\tpass\n", Change(add=[{"name": "n", "default": 1, "value": 5}]), "result = f( )"
	) == (
		"result = f(n=5 )",
		None,
	)


def test_given_a_plan_with_inserts_a_deletion_and_an_edit_in_place_then_every_site_follows_its_own_line(workspace):
	path = workspace / "a.py"
	base = "l0 = 0\nl1 = 1\nl2 = 2\nl3 = 3\nl4 = 4\nl5 = 5\n"
	new = "top = 0\nl0 = 0\nl2 = 2\nl3 = 33\nl4 = 4\nmid = 1\nl5 = 5\n"
	path.write_text(base)
	plan = EditPlan("p", [FileChange(path, base, new)])
	lines = base.splitlines()

	def site(index: int) -> OptionalSite:
		return OptionalSite(
			f"u{index}", "a.py", index + 1, "text", lines[index], TextEdit(index, 0, index, 2, f"y{index}")
		)

	follow = site_followup(plan, [site(i) for i in (0, 2, 3, 4, 5)], workspace)
	assert follow is not None
	assert [
		(s.id, s.line, s.edit.start_line, s.edit.end_line, s.edit.start_character, s.edit.end_character, s.text)
		for s in follow.optional_sites
	] == [
		("u0", 2, 1, 1, 0, 2, "l0 = 0"),
		("u2", 3, 2, 2, 0, 2, "l2 = 2"),
		("u3", 4, 3, 3, 0, 2, "l3 = 33"),
		("u4", 5, 4, 4, 0, 2, "l4 = 4"),
		("u5", 7, 6, 6, 0, 2, "l5 = 5"),
	]
	with pytest.raises(ToolInputError, match=r"u1 \(a.py:2\) overlaps a change in the plan; skip it"):
		site_followup(plan, [site(1)], workspace)
