"""Fix C1: plan merging and site carrying use a diff that aligns repeated lines in place."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, strategies as st
from mcp_nav_shared.edits import EditPlan, FileChange, TextEdit

from codenav_mcp.plan_merge import add_sites, line_changes, merge_plans
from codenav_mcp.plan_sites import OptionalSite


_tmp = tempfile.TemporaryDirectory()
ROOT = Path(_tmp.name).resolve()


def _rebuild(old: list[str], new: list[str]) -> list[str]:
	out: list[str] = []
	cursor = 0
	for i1, i2, j1, j2 in line_changes(old, new):
		assert cursor <= i1 <= i2
		out += old[cursor:i1] + new[j1:j2]
		cursor = i2
	return out + old[cursor:]


@given(
	st.lists(st.sampled_from("abc"), max_size=14),
	st.lists(st.sampled_from("abc"), max_size=14),
)
def test_given_any_two_line_lists_then_the_changes_rebuild_the_new_one(old, new):
	assert _rebuild(old, new) == new


def test_given_a_one_line_edit_in_a_long_repeated_file_then_it_is_one_one_line_change():
	old = ["pass\n", "x = 1\n", "x = 1\n"] * 40
	new = list(old)
	new[61] = "SECOND\n"
	assert line_changes(old, new) == [(61, 62, 61, 62)]


def test_given_edits_at_both_ends_of_a_long_repeated_file_then_they_merge_in_place():
	base = ["x = 1\n", "x = 1\n", "pass\n"] * 300
	one, two = list(base), list(base)
	one[3] = "FIRST\n"
	two[-2] = "SECOND\n"
	path = ROOT / "long.py"
	merged = merge_plans(
		[
			("item 1", EditPlan("p", [FileChange(path, "".join(base), "".join(one))])),
			("item 2", EditPlan("p", [FileChange(path, "".join(base), "".join(two))])),
		],
		"m",
	)
	expected = list(base)
	expected[3], expected[-2] = "FIRST\n", "SECOND\n"
	assert [c.new_text for c in merged.changes] == ["".join(expected)]


def test_given_a_site_on_a_repeated_line_then_it_is_carried_to_that_line_not_a_look_alike():
	path = ROOT / "sites.py"
	base = ["x = 1\n", "x = 1\n", "x = 1\n", "x = 1\n"]
	path.write_text("".join(base))
	new = ["FIRST\n", "x = 1\n", "x = 1\n", "x = 1\n"]
	plan = EditPlan("p", [FileChange(path, "".join(base), "".join(new))])
	site = OptionalSite("u1", path.name, 4, "text", "x = 1", TextEdit(3, 0, 3, 1, "y"))
	result = add_sites(plan, [site], ROOT)
	assert result.changes[0].new_text == "FIRST\nx = 1\nx = 1\ny = 1\n"
