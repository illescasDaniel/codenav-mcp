"""Combining edit plans: several plans into one (multi-edit), and optional sites into a plan (`sites=`).

An `EditPlan` holds whole-file texts, not the edits that produced them, so both operations work on lines: a
file's change is turned into hunks (`difflib`), hunks from different plans must not overlap, and a site's
position is carried across the plan's own edits through unchanged lines or spans.
"""

from __future__ import annotations

import difflib
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
		opcodes = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()
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
		opcodes = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()
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
