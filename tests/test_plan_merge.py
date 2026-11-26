"""Merging plans (multi-edit) and carrying optional sites into a plan (`sites=`): pure text, no language server."""

from __future__ import annotations

import pytest
from mcp_nav_shared.edits import EditPlan, FileChange, TextEdit
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp.plan_merge import add_sites, merge_plans
from codenav_mcp.plan_sites import OptionalSite


BASE = "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n"


def _plan(path, new: str, base: str = BASE) -> EditPlan:
	return EditPlan("p", [FileChange(path, base, new)])


@pytest.fixture
def file(tmp_path):
	path = tmp_path / "m.py"
	path.write_text(BASE)
	return path.resolve()


def test_given_plans_on_different_files_then_they_are_joined(tmp_path):
	one, two = tmp_path / "one.py", tmp_path / "two.py"
	one.write_text("x = 1\n")
	two.write_text("y = 1\n")

	merged = merge_plans(
		[("item 1", _plan(one, "x = 2\n", "x = 1\n")), ("item 2", _plan(two, "y = 2\n", "y = 1\n"))], "two edits"
	)

	assert [change.new_text for change in merged.changes] == ["x = 2\n", "y = 2\n"]
	assert merged.description == "two edits"


def test_given_two_plans_on_distant_lines_of_one_file_then_both_edits_land(file):
	merged = merge_plans(
		[
			("item 1", _plan(file, BASE.replace("b = 2", "b = 20"))),
			("item 2", _plan(file, BASE.replace("e = 5", "e = 50"))),
		],
		"x",
	)

	assert len(merged.changes) == 1
	assert merged.changes[0].old_text == BASE
	assert merged.changes[0].new_text == "a = 1\nb = 20\nc = 3\nd = 4\ne = 50\n"


def test_given_plans_that_change_the_same_line_then_the_conflict_names_both_items(file):
	with pytest.raises(ToolInputError, match=r"item 1 and item 2 both change m\.py around line 2"):
		merge_plans(
			[
				("item 1", _plan(file, BASE.replace("b = 2", "b = 20"))),
				("item 2", _plan(file, BASE.replace("b = 2", "b = 21"))),
			],
			"x",
		)


def test_given_the_same_edit_twice_then_it_is_applied_once(file):
	same = BASE.replace("b = 2", "b = 20")

	merged = merge_plans([("item 1", _plan(file, same)), ("item 2", _plan(file, same))], "x")

	assert merged.changes[0].new_text == same


def test_given_inserts_at_one_line_then_they_keep_the_order_of_the_items(file):
	merged = merge_plans(
		[
			("item 1", _plan(file, "import one\n" + BASE)),
			("item 2", _plan(file, "import two\n" + BASE)),
		],
		"x",
	)

	assert merged.changes[0].new_text == "import one\nimport two\n" + BASE


def test_given_adjacent_hunks_then_they_do_not_conflict(file):
	merged = merge_plans(
		[
			("item 1", _plan(file, BASE.replace("b = 2", "b = 20"))),
			("item 2", _plan(file, BASE.replace("c = 3", "c = 30"))),
		],
		"x",
	)

	assert merged.changes[0].new_text == "a = 1\nb = 20\nc = 30\nd = 4\ne = 5\n"


def test_given_one_plan_creating_a_file_another_touches_then_it_refuses(tmp_path):
	path = tmp_path / "new.py"
	create = EditPlan("p", [FileChange(path, None, "x = 1\n")])
	modify = EditPlan("p", [FileChange(path, "x = 0\n", "x = 2\n")])

	with pytest.raises(ToolInputError, match="creates or deletes"):
		merge_plans([("item 1", create), ("item 2", modify)], "x")


def test_given_noop_changes_then_they_are_dropped(file):
	merged = merge_plans([("item 1", _plan(file, BASE)), ("item 2", _plan(file, BASE.replace("e = 5", "e = 6")))], "x")

	assert merged.changes[0].new_text is not None
	assert merged.changes[0].new_text.endswith("e = 6\n")


def test_given_notes_then_they_are_prefixed_with_the_item(file):
	plan = EditPlan("p", [FileChange(file, BASE, BASE.replace("e = 5", "e = 6"))], ["heads up"])

	assert merge_plans([("item 3", plan)], "x").notes == ["item 3: heads up"]


# -- optional sites


def _site(site_id: str, line: int, text: str, edit: TextEdit, path: str = "m.py") -> OptionalSite:
	return OptionalSite(site_id, path, line, "attr", text, edit)


def test_given_a_site_in_a_file_the_plan_edits_then_it_is_carried_over_the_plan_edits(tmp_path, file):
	# the plan inserts two lines at the top, shifting everything; the site is positioned against the old text
	plan = _plan(file, "import os\nimport sys\n" + BASE)
	site = _site("u1", 4, "d = 4", TextEdit(3, 0, 3, 1, "dd"))

	result = add_sites(plan, [site], tmp_path)

	assert result.changes[0].new_text == "import os\nimport sys\na = 1\nb = 2\nc = 3\ndd = 4\ne = 5\n"
	assert result.changes[0].old_text == BASE
	assert result.description == "p (+1 optional site)"


def test_given_a_site_overlapping_a_plan_edit_then_it_is_refused(tmp_path, file):
	plan = _plan(file, BASE.replace("d = 4", "x = 4"))
	site = _site("u2", 4, "d = 4", TextEdit(3, 0, 3, 1, "dd"))

	with pytest.raises(ToolInputError, match=r"u2 \(m\.py:4\) overlaps a change"):
		add_sites(plan, [site], tmp_path)


def test_given_a_certain_edit_preceding_a_site_on_the_same_line_then_its_utf16_range_moves(tmp_path, file):
	old = "old()  # 🐍 old()\n"
	plan = EditPlan("p", [FileChange(file, old, old.replace("old", "new_name", 1))])
	site = _site("u1", 1, old.strip(), TextEdit(0, 12, 0, 15, "new_name"))

	result = add_sites(plan, [site], tmp_path)

	assert result.changes[0].new_text == "new_name()  # 🐍 new_name()\n"


def test_given_a_site_whose_line_changed_on_disk_then_it_is_refused_not_misplaced(tmp_path, file):
	plan = _plan(file, BASE)
	site = _site("u1", 2, "b = 999", TextEdit(1, 0, 1, 1, "bb"))

	with pytest.raises(ToolInputError, match="no longer matches the file"):
		add_sites(plan, [site], tmp_path)


def test_given_a_site_positioned_against_the_new_text_then_it_is_accepted(tmp_path, file):
	plan = _plan(file, "import os\n" + BASE)
	site = _site("u1", 5, "d = 4", TextEdit(4, 0, 4, 1, "dd"))  # line 5 of the NEW text

	assert add_sites(plan, [site], tmp_path).changes[0].new_text == "import os\na = 1\nb = 2\nc = 3\ndd = 4\ne = 5\n"


def test_given_a_site_in_a_file_outside_the_plan_then_the_file_joins_the_plan_with_its_hash(tmp_path, file):
	other = tmp_path / "other.py"
	other.write_text("v = old\n")
	plan = _plan(file, BASE.replace("e = 5", "e = 6"))
	site = _site("u1", 1, "v = old", TextEdit(0, 4, 0, 7, "new"), path="other.py")

	result = add_sites(plan, [site], tmp_path)

	assert {c.path.name for c in result.changes} == {"m.py", "other.py"}
	assert next(c for c in result.changes if c.path.name == "other.py").new_text == "v = new\n"
	assert other.resolve() in result.base_hashes


def test_given_several_sites_in_one_file_then_all_are_written_in_one_pass(tmp_path, file):
	plan = _plan(file, BASE.replace("e = 5", "e = 6"))
	sites = [_site("u1", 1, "a = 1", TextEdit(0, 0, 0, 1, "aa")), _site("u2", 2, "b = 2", TextEdit(1, 0, 1, 1, "bb"))]

	assert add_sites(plan, sites, tmp_path).changes[0].new_text == "aa = 1\nbb = 2\nc = 3\nd = 4\ne = 6\n"


def test_given_sites_that_would_break_the_syntax_then_nothing_is_written(tmp_path, file):
	plan = _plan(file, BASE.replace("e = 5", "e = 6"))
	site = _site("u1", 1, "a = 1", TextEdit(0, 0, 0, 5, "a = ("))

	with pytest.raises(ToolInputError):
		add_sites(plan, [site], tmp_path)


def test_given_no_chosen_sites_then_the_plan_is_returned_as_is(tmp_path, file):
	plan = _plan(file, BASE.replace("e = 5", "e = 6"))

	assert add_sites(plan, [], tmp_path) is plan
