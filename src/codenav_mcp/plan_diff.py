"""The preview diff of an edit plan, without `difflib`'s autojunk.

`mcp_nav_shared.edits.unified_diff` (used by `EditPlan.diff`) calls `difflib.unified_diff`, whose matcher treats
lines that make up more than 1% of a file of 200+ lines (blank lines, `return x`) as noise, so unchanged lines
between two nearby edits showed up as removed and added again. This module renders previews with autojunk disabled.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from mcp_nav_shared.edits import EditPlan, FileChange, relative_name, split_lines


def _range(start: int, stop: int) -> str:
	length = stop - start
	start += 1
	if length == 1:
		return str(start)
	if not length:
		start -= 1
	return f"{start},{length}"


def unified_diff(change: FileChange, root: Path, *, context: int = 2) -> str:
	name = relative_name(change.path, root)
	old = split_lines(change.old_text or "", keepends=True)
	new = split_lines(change.new_text or "", keepends=True)
	label_old = "/dev/null" if change.old_text is None else f"a/{name}"
	label_new = "/dev/null" if change.new_text is None else f"b/{name}"
	lines: list[str] = []
	for group in difflib.SequenceMatcher(None, old, new, autojunk=False).get_grouped_opcodes(context):
		if not lines:
			lines += [f"--- {label_old}\n", f"+++ {label_new}\n"]
		first, last = group[0], group[-1]
		lines.append(f"@@ -{_range(first[1], last[2])} +{_range(first[3], last[4])} @@\n")
		for tag, i1, i2, j1, j2 in group:
			if tag == "equal":
				lines += [" " + line for line in old[i1:i2]]
				continue
			if tag in ("replace", "delete"):
				lines += ["-" + line for line in old[i1:i2]]
			if tag in ("replace", "insert"):
				lines += ["+" + line for line in new[j1:j2]]
	return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def plan_diff(plan: EditPlan, root: Path, *, max_lines: int = 300) -> str:
	text = "".join(unified_diff(change, root) for change in plan.changes)
	lines = split_lines(text)
	if len(lines) <= max_lines:
		return text
	return "\n".join(lines[:max_lines]) + f"\n... diff truncated ({len(lines) - max_lines} more lines)\n"
