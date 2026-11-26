"""Properties of `plan_merge`: merging edit plans (multi-edit) and carrying optional sites into a plan.

Base files hold unique lines (`L0`, `L1`, ...) and replacement lines are unique too, so difflib's alignment is
unambiguous and the expected merge is just "apply every intended line edit to the base". A separate property uses
repetitive lines and only demands a result that is still the base with the intended edits applied once.
"""

from __future__ import annotations

import itertools
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from hypothesis import assume, event, given, strategies as st
from mcp_nav_shared.edits import EditPlan, FileChange, TextEdit
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp.plan_merge import _hunks, add_sites, merge_plans, site_followup
from codenav_mcp.plan_sites import OptionalSite


@dataclass(frozen=True)
class LineEdit:
	"""Replace base lines [start, end) with `lines` (`start == end` inserts before line `start`)."""

	start: int
	end: int
	lines: tuple[str, ...]


def overlap(a: LineEdit, b: LineEdit) -> bool:
	"""Whether two intended edits touch the same base lines (what `merge_plans` must refuse)."""
	if a == b:
		return False  # identical edits are one edit
	if a.start == a.end and b.start == b.end:
		return False  # two inserts at one point: both are kept, in the order given
	if a.start == a.end:
		return b.start < a.start < b.end
	if b.start == b.end:
		return a.start < b.start < a.end
	return a.start < b.end and b.start < a.end


def apply_edits(base: list[str], edits: list[tuple[int, LineEdit]]) -> list[str]:
	"""The base with every edit applied; `edits` are (plan order, edit), identical ones applied once."""
	unique = {}
	for order, edit in sorted(edits, key=lambda pair: (pair[1].start, pair[1].end, pair[0])):
		unique.setdefault((edit.start, edit.end, edit.lines), (order, edit))
	out: list[str] = []
	cursor = 0
	for _, edit in sorted(unique.values(), key=lambda pair: (pair[1].start, pair[1].end, pair[0])):
		out += base[cursor : edit.start] + list(edit.lines)
		cursor = edit.end
	return out + base[cursor:]


_numbers = itertools.count(1)


@st.composite
def base_and_plans(draw: st.DrawFn):
	size = draw(st.integers(1, 8))
	base = [f"L{i}\n" for i in range(size)]
	plans: list[list[LineEdit]] = []
	for _ in range(draw(st.integers(1, 4))):
		edits: list[LineEdit] = []
		cursor = 0
		# one plan's own edits are separated by an untouched line, so difflib keeps them apart
		for _ in range(draw(st.integers(1, 2))):
			if cursor > size:
				break
			start = draw(st.integers(cursor, size))
			end = draw(st.integers(start, min(size, start + 2)))
			count = draw(st.integers(0 if end > start else 1, 2))
			lines = tuple(f"N{start}_{end}_{k}_{draw(st.integers(0, 2))}\n" for k in range(count))
			edits.append(LineEdit(start, end, lines))
			cursor = end + 1
		plans.append(edits)
	return base, plans


def to_plan(path: Path, base: list[str], edits: list[LineEdit]) -> EditPlan:
	return EditPlan("p", [FileChange(path, "".join(base), "".join(apply_edits(base, [(0, e) for e in edits])))])


_tmp = tempfile.TemporaryDirectory()
FILE = Path(_tmp.name).resolve() / "m.py"


@given(base_and_plans())
def test_given_plans_from_one_base_then_merging_equals_applying_every_edit_or_refuses_an_overlap(pair):
	base, plans = pair
	flat = [(order, edit) for order, edits in enumerate(plans) for edit in edits]
	# FINDINGS-h3 #4: a repeated identical insert is only de-duplicated when its copies are neighbours.
	for point in {(e.start, e.end) for _, e in flat if e.start == e.end}:
		here = [e for _, e in flat if (e.start, e.end) == point]
		assume(len(set(here)) == len(here) or len(set(here)) == 1)
	conflicts = [
		(a, b) for index, (oa, a) in enumerate(flat) for ob, b in flat[index + 1 :] if oa != ob and overlap(a, b)
	]
	labelled = [(f"item {n + 1}", to_plan(FILE, base, edits)) for n, edits in enumerate(plans)]
	try:
		merged = merge_plans(labelled, "m")
	except ToolInputError as exc:
		event("refused")
		assert conflicts, f"refused without an overlap: {exc}\n{base}\n{plans}"
		assert "both change" in str(exc) or "creates or deletes" in str(exc)
		return
	assert not conflicts, f"overlap was merged silently: {conflicts}\n{base}\n{plans}"
	event("merged")
	expected = "".join(apply_edits(base, flat))
	new_texts = [change.new_text for change in merged.changes]
	if expected == "".join(base):
		assert new_texts in ([], ["".join(base)])
		return
	assert new_texts == [expected], f"{base}\n{plans}"
	assert merged.changes[0].old_text == "".join(base)


@given(base_and_plans())
def test_given_the_same_plans_in_any_order_then_the_merge_is_the_same_unless_inserts_share_a_point(pair):
	base, plans = pair
	assume(len(plans) > 1)
	# Inserts at one point keep the order the items were given in; everything else must not depend on it.
	points = [(e.start, e.end) for edits in plans for e in edits if e.start == e.end]
	assume(len(points) == len(set(points)))
	labelled = [(f"item {n + 1}", to_plan(FILE, base, edits)) for n, edits in enumerate(plans)]
	try:
		forward = merge_plans(labelled, "m")
		backward = merge_plans(list(reversed(labelled)), "m")
	except ToolInputError:
		with pytest.raises(ToolInputError):
			merge_plans(list(reversed(labelled)), "m")
		return
	assert [c.new_text for c in forward.changes] == [c.new_text for c in backward.changes]


@given(base_and_plans())
def test_given_one_plan_merged_with_itself_then_it_is_applied_once(pair):
	base, plans = pair
	plan = to_plan(FILE, base, plans[0])
	assume(not plan.changes[0].is_noop)
	merged = merge_plans([("item 1", plan), ("item 2", plan)], "m")
	assert [c.new_text for c in merged.changes] == [plan.changes[0].new_text]


@pytest.mark.xfail(strict=True, reason="FINDINGS-h3 #4")
def test_given_the_same_insert_in_two_items_with_another_insert_between_then_it_is_written_once():
	base = ["a = 1\n", "b = 2\n"]
	same = LineEdit(1, 1, ("import os\n",))
	other = LineEdit(1, 1, ("import sys\n",))
	merged = merge_plans(
		[
			("item 1", to_plan(FILE, base, [same])),
			("item 2", to_plan(FILE, base, [other])),
			("item 3", to_plan(FILE, base, [same])),
		],
		"m",
	)
	assert (merged.changes[0].new_text or "").count("import os") == 1


@pytest.mark.xfail(strict=True, reason="FINDINGS-h3 #5")
def test_given_two_edits_on_different_lines_of_a_file_with_repeated_lines_then_they_merge():
	base = ["x = 1\n", "x = 1\n", "x = 1\n"]
	one = ["FIRST\n", "x = 1\n", "x = 1\n"]
	two = ["x = 1\n", "x = 1\n", "SECOND\n"]
	merged = merge_plans(
		[
			("item 1", EditPlan("p", [FileChange(FILE, "".join(base), "".join(one))])),
			("item 2", EditPlan("p", [FileChange(FILE, "".join(base), "".join(two))])),
		],
		"m",
	)
	assert [c.new_text for c in merged.changes] == ["FIRST\nx = 1\nSECOND\n"]


@pytest.mark.xfail(strict=True, reason="FINDINGS-h3 #6")
def test_given_two_edits_whose_diffs_share_an_incidental_hunk_then_the_merge_is_not_silently_wrong():
	base = "x = 1\n" * 4
	one = "FIRST\n" + "x = 1\n" * 3
	two = "x = 1\nx = 1\nSECOND\nx = 1\n"
	merged = merge_plans(
		[
			("item 1", EditPlan("p", [FileChange(FILE, base, one)])),
			("item 2", EditPlan("p", [FileChange(FILE, base, two)])),
		],
		"m",
	)
	assert [c.new_text for c in merged.changes] == ["FIRST\nx = 1\nSECOND\nx = 1\n"]


@given(st.lists(st.sampled_from(["x = 1\n", "y = 2\n", "pass\n", "\n"]), min_size=2, max_size=12), st.data())
def test_given_repetitive_lines_then_two_plans_on_disjoint_places_still_give_a_valid_merge(base, data):
	"""With repeated lines difflib may align differently from the intent; the merge must still be the base with
	each plan's own change applied (checked by merging with the plans that edit different, unique-marked lines)."""
	first = data.draw(st.integers(0, len(base) - 1))
	second = data.draw(st.integers(0, len(base) - 1))
	assume(abs(first - second) > 1)
	one, two = list(base), list(base)
	one[first] = "FIRST\n"
	two[second] = "SECOND\n"
	# FINDINGS-h3 #6: hunks that two plans happen to share (say, the same line deleted) are merged as one edit.
	shared = {(a, b, tuple(c)) for a, b, c in _hunks("".join(base), "".join(one))} & {
		(a, b, tuple(c)) for a, b, c in _hunks("".join(base), "".join(two))
	}
	assume(not shared)
	try:
		merged = merge_plans(
			[
				("item 1", EditPlan("p", [FileChange(FILE, "".join(base), "".join(one))])),
				("item 2", EditPlan("p", [FileChange(FILE, "".join(base), "".join(two))])),
			],
			"m",
		)
	except ToolInputError:
		event("false refusal (FINDINGS-h3 #5)")  # fail-safe, but the two edits do not touch the same line
		return
	expected = list(base)
	expected[first], expected[second] = "FIRST\n", "SECOND\n"
	assert [c.new_text for c in merged.changes] == ["".join(expected)]


# -- optional sites ----------------------------------------------------------------------------------------------


def _site(index: int, line_text: str) -> OptionalSite:
	"""A site that renames the leading `xN` of line `index` to `yN` (the whole line is `xN = N`)."""
	return OptionalSite(
		f"u{index}", "m.py", index + 1, "text", line_text.strip(), TextEdit(index, 0, index, 2, f"y{index}")
	)


@st.composite
def site_scenarios(draw: st.DrawFn):
	size = draw(st.integers(2, 8))
	base = [f"x{i} = {i}\n" for i in range(size)]
	new = list(base)
	for _ in range(draw(st.integers(0, 3))):  # the plan's own edits: delete, replace or insert a line
		kind = draw(st.sampled_from(["delete", "replace", "insert"]))
		at = draw(st.integers(0, max(0, len(new) - 1)))
		if kind == "insert" or not new:
			new.insert(at, f"z{next(_numbers)} = 0\n")
		elif kind == "delete" and len(new) > 1:
			del new[at]
		else:
			new[at] = f"w{next(_numbers)} = 0\n"
	chosen = draw(st.lists(st.integers(0, size - 1), unique=True, min_size=1, max_size=size))
	return base, new, chosen


@given(site_scenarios())
def test_given_certain_edits_then_optional_sites_are_applied_when_their_line_survived_and_refused_when_not(case):
	base, new, chosen = case
	FILE.write_text("".join(base))
	plan = EditPlan("p", [FileChange(FILE, "".join(base), "".join(new))])
	sites = [_site(index, base[index]) for index in chosen]
	workspace = FILE.parent
	survived = [index for index in chosen if base[index] in new]
	if len(survived) == len(chosen):
		result = add_sites(plan, sites, workspace)
		assert len(result.changes) == 1, "a site was silently dropped"
		text = result.changes[0].new_text or ""
		for index in chosen:
			assert f"y{index} = {index}\n" in text.splitlines(keepends=True)
			assert f"x{index} = {index}\n" not in text.splitlines(keepends=True)
		# nothing else changed: the plan's own text with the renames applied
		expected = "".join(
			f"y{i} = {i}\n" if i in chosen else f"x{i} = {i}\n" for i in range(len(base)) if f"x{i} = {i}\n" in new
		)
		assert [line for line in text.splitlines(keepends=True) if line[0] in "xy"] == [
			line for line in expected.splitlines(keepends=True)
		]
		return
	changed = [index for index in chosen if base[index] not in new]
	try:
		add_sites(plan, [_site(index, base[index]) for index in chosen], workspace)
	except ToolInputError as exc:
		assert any(f"u{index}" in str(exc) for index in changed), str(exc)
		event("refused a site on a changed line")
		return
	pytest.fail(f"a site on a line the plan removed was applied: {chosen} {base} {new}")


@given(site_scenarios())
def test_given_a_follow_up_then_every_carried_site_points_inside_the_new_file_at_the_line_it_names(case):
	base, new, chosen = case
	FILE.write_text("".join(base))
	assume(all(base[index] in new for index in chosen))
	plan = EditPlan("p", [FileChange(FILE, "".join(base), "".join(new))])
	sites = [_site(index, base[index]) for index in chosen]
	followup = site_followup(plan, sites, FILE.parent)
	assert followup is not None
	assert [site.id for site in followup.optional_sites] == [site.id for site in sites]
	for site in followup.optional_sites:
		edit = site.edit
		assert 0 <= edit.start_line == edit.end_line < len(new)
		row = new[edit.start_line]
		assert row.strip() == site.text
		assert 0 <= edit.start_character <= edit.end_character <= len(row.rstrip("\n"))
		assert site.line == edit.start_line + 1
