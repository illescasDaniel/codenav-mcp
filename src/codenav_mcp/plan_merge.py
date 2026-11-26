"""Combining edit plans: several plans into one (multi-edit), and optional sites into a plan (`sites=`).

An `EditPlan` holds whole-file texts, not the edits that produced them, so both operations work on lines: a
file's change is turned into hunks (`line_changes`: a patience diff that aligns runs of repeated lines in place,
never `difflib`'s arbitrary choice), hunks from different plans must not overlap, and a site's position is carried
across the plan's own edits through unchanged lines or spans.
"""

from __future__ import annotations

import bisect
import difflib
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from mcp_nav_shared.edits import (
	EditPlan,
	FileChange,
	TextEdit,
	apply_text_edits,
	hash_bytes,
	position_to_offset,
	read_source,
	relative_name,
	split_lines,
	utf16_length,
)
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp.plan_sites import CodenavPlan, OptionalSite
from codenav_mcp.pysource import check_syntax


Hunk = tuple[int, int, list[str]]  # (first old line index, one past the last, replacement lines)

_DP_LIMIT = 40_000  # cells of the edit-distance table for a block of repeated lines; bigger blocks are one hunk


def _anchors(old: list[str], new: list[str]) -> list[tuple[int, int]]:
	"""Lines that occur exactly once on each side, kept in order (the longest increasing run): patience anchors."""
	old_count, new_count = Counter(old), Counter(new)
	where = {line: index for index, line in enumerate(new) if new_count[line] == 1}
	pairs = [(i, where[line]) for i, line in enumerate(old) if old_count[line] == 1 and line in where]
	tails: list[int] = []  # tails[k]: the smallest new index that ends a run of k + 1 anchors
	ends: list[int] = []  # ends[k]: which pair that is
	previous = [-1] * len(pairs)
	for index, (_, j) in enumerate(pairs):
		k = bisect.bisect_left(tails, j)
		previous[index] = ends[k - 1] if k else -1
		if k == len(tails):
			tails.append(j)
			ends.append(index)
		else:
			tails[k], ends[k] = j, index
	run: list[tuple[int, int]] = []
	cursor = ends[-1] if ends else -1
	while cursor >= 0:
		run.append(pairs[cursor])
		cursor = previous[cursor]
	return run[::-1]


def _align_repeated(old: list[str], new: list[str]) -> list[tuple[int, int, int, int]]:
	"""Changed blocks (i1, i2, j1, j2) of two line runs with no unique line and different first and last lines.

	`difflib` aligns repeated lines arbitrarily (it can report a one-line edit at the other end of a run of equal
	lines, or as a delete plus an insert far apart), so a plan's edit would land on the wrong line when merged with
	another's. Here lines are compared in place when the runs are as long as each other, otherwise by the cheapest
	edit script that prefers replacing a line to deleting one and adding another."""
	n, m = len(old), len(new)
	if n == m:
		spans: list[tuple[int, int, int, int]] = []
		for index in range(n):
			if old[index] != new[index]:
				if spans and spans[-1][1] == index:
					spans[-1] = (spans[-1][0], index + 1, spans[-1][2], index + 1)
				else:
					spans.append((index, index + 1, index, index + 1))
		return spans
	if (n + 1) * (m + 1) > _DP_LIMIT:
		return [(0, n, 0, m)]
	cost = [[0] * (m + 1) for _ in range(n + 1)]
	for i in range(n + 1):
		cost[i][0] = i
	for j in range(m + 1):
		cost[0][j] = j
	for i in range(1, n + 1):
		for j in range(1, m + 1):
			same = cost[i - 1][j - 1] + (old[i - 1] != new[j - 1])
			cost[i][j] = min(same, cost[i - 1][j] + 1, cost[i][j - 1] + 1)
	blocks: list[tuple[int, int, int, int]] = []
	i, j = n, m
	while i or j:
		if i and j and old[i - 1] == new[j - 1] and cost[i][j] == cost[i - 1][j - 1]:
			i, j = i - 1, j - 1
			continue
		if i and j and cost[i][j] == cost[i - 1][j - 1] + 1:
			step = (i - 1, j - 1)
		elif i and cost[i][j] == cost[i - 1][j] + 1:
			step = (i - 1, j)
		else:
			step = (i, j - 1)
		if blocks and blocks[-1][0] == i and blocks[-1][2] == j:
			blocks[-1] = (step[0], blocks[-1][1], step[1], blocks[-1][3])
		else:
			blocks.append((step[0], i, step[1], j))
		i, j = step
	return blocks[::-1]


def line_changes(old: list[str], new: list[str]) -> list[tuple[int, int, int, int]]:
	"""Where `new` differs from `old`: blocks (i1, i2, j1, j2), old[i1:i2] becoming new[j1:j2], in order.

	Common first and last lines are peeled off, lines that occur once on each side anchor the rest (patience diff),
	and what lies between anchors is aligned in place (`_align_repeated`)."""
	found: list[tuple[int, int, int, int]] = []
	work = [(0, len(old), 0, len(new))]
	while work:
		i1, i2, j1, j2 = work.pop()
		while i1 < i2 and j1 < j2 and old[i1] == new[j1]:
			i1, j1 = i1 + 1, j1 + 1
		while i1 < i2 and j1 < j2 and old[i2 - 1] == new[j2 - 1]:
			i2, j2 = i2 - 1, j2 - 1
		if i1 == i2 and j1 == j2:
			continue
		if i1 == i2 or j1 == j2:
			found.append((i1, i2, j1, j2))
			continue
		anchors = _anchors(old[i1:i2], new[j1:j2])
		if not anchors:
			found += [(i1 + a, i1 + b, j1 + c, j1 + d) for a, b, c, d in _align_repeated(old[i1:i2], new[j1:j2])]
			continue
		bounds = [(i1 - 1, j1 - 1), *((i1 + i, j1 + j) for i, j in anchors), (i2, j2)]
		work += [(a + 1, b, c + 1, d) for (a, c), (b, d) in zip(bounds, bounds[1:], strict=False)]
	return sorted(found)


def line_opcodes(old: list[str], new: list[str]) -> list[tuple[str, int, int, int, int]]:
	"""`difflib`-style opcodes (equal, replace, delete, insert) built on `line_changes`."""
	codes: list[tuple[str, int, int, int, int]] = []
	i = j = 0
	for i1, i2, j1, j2 in [*line_changes(old, new), (len(old), len(old), len(new), len(new))]:
		if i1 > i:
			codes.append(("equal", i, i1, j, j1))
		if i2 > i1 or j2 > j1:
			tag = "replace" if i2 > i1 and j2 > j1 else "delete" if i2 > i1 else "insert"
			codes.append((tag, i1, i2, j1, j2))
		i, j = i2, j2
	return codes


def _hunks(old_text: str, new_text: str) -> list[Hunk]:
	old, new = split_lines(old_text, keepends=True), split_lines(new_text, keepends=True)
	return [(i1, i2, new[j1:j2]) for i1, i2, j1, j2 in line_changes(old, new)]


def merge_plans(labelled: Sequence[tuple[str, EditPlan]], description: str) -> EditPlan:
	"""One plan out of several that were each made from the files as they are now.

	Plans touching different files are just joined. Plans touching one file are merged hunk by hunk;
	hunks that overlap, or a file that one plan creates or deletes and another touches, raise `ToolInputError`
	naming the labels. Inserts at the same line are kept in the order given."""
	by_path: dict[Path, list[tuple[str, FileChange]]] = {}
	notes: list[str] = []
	hashes: dict[Path, str | None] = {}
	for label, plan in labelled:
		notes += [f"{label}: {note}" for note in plan.notes]
		for change in plan.changes:
			if change.is_noop:
				continue
			by_path.setdefault(change.path, []).append((label, change))
			hashes.setdefault(change.path, plan.base_hashes.get(change.path))
	changes = [_merge_file(path, items) for path, items in by_path.items()]
	return EditPlan(description, changes, notes, {change.path: hashes[change.path] for change in changes})


def _merge_file(path: Path, items: list[tuple[str, FileChange]]) -> FileChange:
	first_label, first = items[0]
	if len(items) == 1:
		return first
	name = path.name
	labels = " and ".join(label for label, _ in items[:2])
	old_text = first.old_text
	if old_text is None or any(change.kind != "modify" for _, change in items):
		raise ToolInputError(f"{labels} both change {name}, and one of them creates or deletes it; split them")
	if any(change.old_text != old_text for _, change in items):
		raise ToolInputError(f"{labels} were planned from different contents of {name}; rerun")
	old_lines = split_lines(old_text, keepends=True)
	hunks: list[tuple[int, int, int, list[str], str]] = []
	for order, (label, change) in enumerate(items):
		hunks += [(i1, i2, order, lines, label) for i1, i2, lines in _hunks(old_text, change.new_text or "")]
	hunks.sort(key=lambda hunk: (hunk[0], hunk[1], hunk[2]))
	merged: list[str] = []
	cursor = 0
	last: tuple[int, int, list[str], str] | None = None
	seen: set[tuple[int, int, tuple[str, ...]]] = set()
	for i1, i2, _order, lines, label in hunks:
		if (i1, i2, tuple(lines)) in seen:
			continue  # the same edit from two plans (say, the same import added twice)
		seen.add((i1, i2, tuple(lines)))
		if last is not None and i1 < last[1]:
			raise ToolInputError(f"{last[3]} and {label} both change {name} around line {i1 + 1}; give them separately")
		merged += old_lines[cursor:i1]
		merged += lines
		cursor = i2
		last = (i1, i2, lines, label)
	merged += old_lines[cursor:]
	return FileChange(
		path,
		old_text,
		"".join(merged),
		first.old_bom,
		first.new_bom,
		old_mode=first.old_mode,
		new_mode=first.new_mode,
	)


# ---------------------------------------------------------------------------------------------------------------
# Optional sites


def _line_matches(lines: list[str], index: int, snippet: str) -> bool:
	if not 0 <= index < len(lines):
		return False
	row = lines[index].strip()
	return row == snippet or (len(snippet) == 140 and row.startswith(snippet))


def _carry(
	site: OptionalSite,
	old_lines: list[str],
	new_lines: list[str],
	opcodes: Sequence[tuple[str, int, int, int, int]],
) -> TextEdit:
	"""Move a site's LSP range through the certain edits using unchanged lines or unchanged spans."""
	edit = site.edit
	first, last = edit.start_line, edit.end_line
	if last < len(old_lines) and _line_matches(old_lines, first, site.text):
		for tag, i1, i2, j1, j2 in opcodes:
			if not (i1 <= first and last < i2):
				continue
			shift = j1 - i1
			if tag == "equal":
				return replace(edit, start_line=first + shift, end_line=last + shift)
			if tag == "replace" and first == last and i2 - i1 == j2 - j1:
				carried = _carry_on_changed_line(edit, old_lines[first], new_lines[first + shift], first + shift)
				if carried is not None:
					return carried
			break
		raise ToolInputError(f"{site.id} ({site.path}:{site.line}) overlaps a change in the plan; skip it")
	if last < len(new_lines) and _line_matches(new_lines, first, site.text):
		return edit
	raise ToolInputError(f"{site.id} ({site.path}:{site.line}) no longer matches the file (`{site.text}`)")


def _carry_on_changed_line(edit: TextEdit, old: str, new: str, new_line: int) -> TextEdit | None:
	start = position_to_offset(old, 0, edit.start_character)
	end = position_to_offset(old, 0, edit.end_character)
	for old_start, new_start, size in difflib.SequenceMatcher(None, old, new, autojunk=False).get_matching_blocks():
		if old_start <= start < end <= old_start + size:
			shift = new_start - old_start
			return TextEdit(
				new_line, utf16_length(new[: start + shift]), new_line, utf16_length(new[: end + shift]), edit.new_text
			)
	return None


def _site_change(
	site: OptionalSite, path: Path, changes: dict[Path, FileChange], hashes: dict[Path, str | None]
) -> FileChange:
	change = changes.get(path)
	if change is None:
		if not path.is_file():
			raise ToolInputError(f"{site.id}: {site.path} no longer exists")
		text, bom = read_source(path)
		change = FileChange(path, text, text, bom, bom)
		hashes.setdefault(path, hash_bytes(change.old_bytes() or b""))
	return change


def _sites_by_path(sites: Sequence[OptionalSite], workspace: Path) -> dict[Path, list[OptionalSite]]:
	by_path: dict[Path, list[OptionalSite]] = {}
	for site in sites:
		path = (workspace / site.path).resolve()
		if not path.is_relative_to(workspace.resolve()):
			raise ToolInputError(f"{site.id}: {site.path} is outside the workspace")
		by_path.setdefault(path, []).append(site)
	return by_path


def _site_lines(change: FileChange, sites: list[OptionalSite]) -> tuple[list[str], list[str]]:
	if change.old_text is None or change.new_text is None:
		raise ToolInputError(f"{sites[0].id}: {sites[0].path} is created or deleted by the plan")
	return split_lines(change.old_text, keepends=True), split_lines(change.new_text, keepends=True)


def track_optional_files(plan: EditPlan, sites: Sequence[OptionalSite], workspace: Path) -> EditPlan:
	"""Record the baseline even for files the certain edits leave alone."""
	hashes = dict(plan.base_hashes)
	for path in _sites_by_path(sites, workspace):
		if path not in hashes:
			hashes[path] = hash_bytes(path.read_bytes())
	return replace(plan, base_hashes=hashes)


def site_followup(plan: EditPlan, sites: Sequence[OptionalSite], workspace: Path) -> CodenavPlan | None:
	"""A plan with only remaining site ranges, anchored to the bytes expected after this write."""
	if not sites:
		return None
	changes = {change.path: change for change in plan.changes}
	hashes = dict(plan.base_hashes)
	rebased = {}
	for path, group in _sites_by_path(sites, workspace).items():
		change = _site_change(group[0], path, changes, hashes)
		old_lines, new_lines = _site_lines(change, group)
		opcodes = line_opcodes(old_lines, new_lines)
		for site in group:
			edit = _carry(site, old_lines, new_lines, opcodes)
			rebased[site.id] = replace(
				site, line=edit.start_line + 1, text=new_lines[edit.start_line].strip()[:140], edit=edit
			)
	for change in plan.changes:
		data = change.new_bytes()
		hashes[change.path] = hash_bytes(data) if data is not None else None
	return CodenavPlan(
		EditPlan(f"Follow-up to {plan.description}", [], base_hashes=hashes), [rebased[site.id] for site in sites]
	)


def add_sites(plan: EditPlan, chosen: Sequence[OptionalSite], workspace: Path) -> EditPlan:
	"""The plan with the chosen optional sites written too; a site that cannot be placed raises `ToolInputError`."""
	if not chosen:
		return plan
	changes = {change.path: change for change in plan.changes}
	hashes = dict(plan.base_hashes)
	for path, sites in _sites_by_path(chosen, workspace).items():
		change = _site_change(sites[0], path, changes, hashes)
		old_lines, new_lines = _site_lines(change, sites)
		opcodes = line_opcodes(old_lines, new_lines)
		edits = list(dict.fromkeys(_carry(site, old_lines, new_lines, opcodes) for site in sites))
		new_text = apply_text_edits("".join(new_lines), edits, bom=bool(change.new_bom), bom_in_positions=False)
		if path.suffix.lower() in (".py", ".pyi"):
			check_syntax(new_text, relative_name(path, workspace))
		changes[path] = FileChange(
			path,
			change.old_text,
			new_text,
			change.old_bom,
			change.new_bom,
			old_mode=change.old_mode,
			new_mode=change.new_mode,
		)
	return EditPlan(
		f"{plan.description} (+{len(chosen)} optional site{'s' if len(chosen) != 1 else ''})",
		[change for change in changes.values() if not change.is_noop],
		plan.notes,
		hashes,
	)
