"""Fix C2: the preview diff matches difflib's own output on small files and does not autojunk big ones."""

from __future__ import annotations

import difflib
from pathlib import Path

from mcp_nav_shared.edits import EditPlan, FileChange

from codenav_mcp.plan_diff import plan_diff, unified_diff


ROOT = Path("/ws")


def _diff(old: str | None, new: str | None) -> str:
	return unified_diff(FileChange(ROOT / "m.py", old, new, False, False), ROOT)


def test_given_a_small_edit_then_the_diff_is_what_difflib_writes():
	old = "a\nb\nc\nd\ne\nf\ng\n"
	new = "a\nb\nC\nd\ne\nf\ng\nh"
	expected = "".join(
		line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
		for line in difflib.unified_diff(old.splitlines(True), new.splitlines(True), "a/m.py", "b/m.py", n=2)
	)
	assert _diff(old, new) == expected


def test_given_a_new_or_deleted_file_then_the_labels_use_dev_null():
	assert _diff(None, "x\n").startswith("--- /dev/null\n+++ b/m.py\n@@ -0,0 +1 @@\n+x\n")
	assert _diff("x\n", None).startswith("--- a/m.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")


def test_given_a_big_file_with_repeated_lines_then_plan_diff_shows_only_the_changed_lines():
	filler = "def f() -> int:\n\targs = 1\n\n\treturn args\n\n\n" * 60
	old = "import target\n\nx = target\n" + filler
	new = old.replace("import target", "import renamed").replace("x = target", "x = renamed")
	text = plan_diff(EditPlan("p", [FileChange(ROOT / "m.py", old, new, False, False)]), ROOT)
	assert [line for line in text.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")] == [
		"-import target",
		"+import renamed",
		"-x = target",
		"+x = renamed",
	]
