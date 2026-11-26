"""Combining edit plans: several plans into one (multi-edit), and optional sites into a plan (`sites=`).

An `EditPlan` holds whole-file texts, not the edits that produced them, so both operations work on lines: a
file's change is turned into hunks (`difflib`), hunks from different plans must not overlap, and a site's
position is carried across the plan's own edits through the lines the plan left unchanged.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from pathlib import Path

from mcp_nav_shared.edits import (
	EditPlan,
	FileChange,
	TextEdit,
	apply_text_edits,
	hash_bytes,
	read_source,
	relative_name,
	split_lines,
)
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp.plan_sites import OptionalSite
from codenav_mcp.pysource import check_syntax


Hunk = tuple[int, int, list[str]]  # (first old line index, one past the last, replacement lines)


def _hunks(old_text: str, new_text: str) -> list[Hunk]:
	old, new = split_lines(old_text, keepends=True), split_lines(new_text, keepends=True)
	matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
	return [(i1, i2, new[j1:j2]) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"]


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
	for i1, i2, _order, lines, label in hunks:
		if last is not None and (i1, i2, lines) == last[:3]:
			continue  # the same edit from two plans (say, the same import added twice)
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


def _carry(
	site: OptionalSite, old_lines: list[str], new_lines: list[str], blocks: list[tuple[int, int, int]]
) -> TextEdit:
	"""The site's edit in the coordinates of the plan's new text.

	Sites are positioned against the file as it was when the plan was made (the plan's old text). If the line there
	is the one the site describes and the plan left it alone, the edit moves with the lines before it. A site
	that was positioned against the new text already (its line matches there) is taken as it is."""
	edit = site.edit
	first, last = edit.start_line, edit.end_line
	if last < len(old_lines) and first < len(old_lines) and old_lines[first].strip() == site.text:
		for i1, i2, j1 in blocks:  # unchanged runs of lines: old [i1, i2) is new [j1, j1 + i2 - i1)
			if i1 <= first and last < i2:
				shift = j1 - i1
				return TextEdit(first + shift, edit.start_character, last + shift, edit.end_character, edit.new_text)
		raise ToolInputError(f"{site.id} ({site.path}:{site.line}) is on a line the plan itself changes; skip it")
	if last < len(new_lines) and first < len(new_lines) and new_lines[first].strip() == site.text:
		return edit
	raise ToolInputError(f"{site.id} ({site.path}:{site.line}) no longer matches the file (`{site.text}`)")


def _unchanged_blocks(old_lines: list[str], new_lines: list[str]) -> list[tuple[int, int, int]]:
	matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
	return [(i1, i2, j1) for tag, i1, i2, j1, _j2 in matcher.get_opcodes() if tag == "equal"]


def add_sites(plan: EditPlan, chosen: Sequence[OptionalSite], workspace: Path) -> EditPlan:
	"""The plan with the chosen optional sites written too; a site that cannot be placed raises `ToolInputError`."""
	if not chosen:
		return plan
	by_path: dict[Path, list[OptionalSite]] = {}
	for site in chosen:
		by_path.setdefault((workspace / site.path).resolve(), []).append(site)
	changes = {change.path: change for change in plan.changes}
	hashes = dict(plan.base_hashes)
	for path, sites in by_path.items():
		change = changes.get(path)
		if change is None:
			if not path.is_file():
				raise ToolInputError(f"{sites[0].id}: {sites[0].path} no longer exists")
			text, bom = read_source(path)
			change = FileChange(path, text, text, bom, bom)
			hashes[path] = hash_bytes(change.old_bytes() or b"")
		if change.old_text is None or change.new_text is None:
			raise ToolInputError(f"{sites[0].id}: {sites[0].path} is created or deleted by the plan")
		old_lines = split_lines(change.old_text, keepends=True)
		new_lines = split_lines(change.new_text, keepends=True)
		blocks = _unchanged_blocks(old_lines, new_lines)
		edits = [_carry(site, old_lines, new_lines, blocks) for site in sites]
		new_text = apply_text_edits(change.new_text, edits, bom=bool(change.new_bom), bom_in_positions=False)
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
		list(changes.values()),
		plan.notes,
		hashes,
	)
