"""Run the type checker on an edit before it is written.

`diagnostics_delta` shows ty the edited texts in place of the disk's (an
overlay that never touches the disk), asks for diagnostics on the edited files
and on the files importing them, and compares against the same files as they
are now. The result is what the edit would do to the project's type errors.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable
from pathlib import Path

from mcp_nav_shared.diagnostics_delta import (
	HINT_SEVERITY,
	DiagEntry,
	DiagnosticsDelta,
	diff_diagnostics,
	entries_from_lsp,
)
from mcp_nav_shared.edits import EditPlan, read_source, relative_name
from mcp_nav_shared.errors import TOOL_ERRORS
from mcp_nav_shared.lsp_client import LspClient

from codenav_mcp.deps import Skipped, find_dependents_report, format_skipped


CHECK_LIMIT = 60
_PYTHON_SUFFIXES = {".py", ".pyi"}


async def _collect(
	client: LspClient, root: Path, overlays: dict[Path, str], paths: list[Path], skip: set[Path]
) -> tuple[list[DiagEntry], list[str]]:
	entries: list[DiagEntry] = []
	unchecked: list[str] = []
	scope = client.overlay(overlays) if overlays else contextlib.nullcontext()
	async with scope:
		for path in paths:
			if path in skip:
				continue
			text = overlays.get(path)
			if text is None:
				try:
					text = read_source(path)[0]
				except (OSError, UnicodeDecodeError):
					unchecked.append(relative_name(path, root))
					continue
			try:
				items = await client.diagnostics(str(path))
			except TOOL_ERRORS:
				unchecked.append(relative_name(path, root))
				continue
			entries.extend(entries_from_lsp(relative_name(path, root), text, items))
	return entries, unchecked


async def diagnostics_delta(
	client: LspClient,
	root: Path,
	*,
	before: dict[Path, str],
	after: dict[Path, str],
	check: Iterable[Path],
	skip_before: set[Path] | None = None,
	skip_after: set[Path] | None = None,
	renames: dict[str, str] | None = None,
) -> DiagnosticsDelta:
	"""Diagnostics of `check` with the `after` overlay minus the same with the `before` overlay
	(an empty overlay means the disk as it is). `skip_*` are files that don't exist on that side."""
	paths = list(dict.fromkeys(Path(p).resolve() for p in check))
	before_entries, unchecked_before = await _collect(client, root, before, paths, skip_before or set())
	after_entries, unchecked_after = await _collect(client, root, after, paths, skip_after or set())
	# Hints are editor niceties (`x` is unused, deprecated): adding a parameter before its body
	# uses it would otherwise be reported as a "new warning" on every such edit.
	before_entries = [e for e in before_entries if e.severity != HINT_SEVERITY]
	after_entries = [e for e in after_entries if e.severity != HINT_SEVERITY]
	# A missing explicit mapping is not evidence that two identical files are a move.
	delta = diff_diagnostics(before_entries, after_entries, renames=renames)
	delta.files_checked = len(paths) - len(set(unchecked_before) | set(unchecked_after))
	delta.unchecked = sorted(set(unchecked_before) | set(unchecked_after))
	return delta


def files_to_check(
	workspace: Path,
	roots: list[Path],
	edited: Iterable[Path],
	*,
	include_dependents: bool = True,
	limit: int = CHECK_LIMIT,
	is_excluded: Callable[[Path], bool] | None = None,
	skipped: list[Skipped] | None = None,
) -> tuple[list[Path], int]:
	"""(edited Python files first, then their importers; how many were left out by `limit`).

	Importers that were not examined (excluded directories, nested checkouts, unreadable files) are added to
	`skipped` when a list is given.
	"""
	edited_py = [p.resolve() for p in edited if p.suffix.lower() in _PYTHON_SUFFIXES]
	dependents: list[Path] = []
	if include_dependents:
		report = find_dependents_report(workspace, edited_py, roots)
		dependents = report.files
		if skipped is not None:
			skipped.extend(report.skipped)
	if is_excluded is not None:
		dependents = [p for p in dependents if not is_excluded(p)]
	ordered = list(dict.fromkeys([*edited_py, *dependents]))
	return ordered[:limit], max(0, len(ordered) - limit)


async def simulate_plan(
	client: LspClient,
	workspace: Path,
	roots: list[Path],
	plan: EditPlan,
	*,
	include_dependents: bool = True,
) -> DiagnosticsDelta:
	"""What applying `plan` would do to the diagnostics, without writing anything."""
	after: dict[Path, str] = {}
	created: set[Path] = set()
	deleted: set[Path] = set()
	for change in plan.changes:
		path = change.path.resolve()
		if change.path.suffix.lower() not in _PYTHON_SUFFIXES:
			continue
		after[path] = change.new_text if change.new_text is not None else ""  # a deleted module shows up as empty
		if change.kind == "create":
			created.add(path)
		elif change.kind == "delete":
			deleted.add(path)
	skipped: list[Skipped] = []
	paths, left_out = files_to_check(
		workspace, roots, [c.path for c in plan.changes], include_dependents=include_dependents, skipped=skipped
	)
	# Pair unambiguous deletion/creation plans as file moves.
	renames: dict[str, str] = {}
	if plan.description.startswith("Move module ") and len(created) == 1 and len(deleted) == 1:
		renames[relative_name(next(iter(deleted)), workspace)] = relative_name(next(iter(created)), workspace)
	delta = await diagnostics_delta(
		client, workspace, before={}, after=after, check=paths, skip_before=created, skip_after=deleted, renames=renames
	)
	if deleted:
		# An empty open document is not a deleted module: ty can still resolve the path.
		# Keep the normal gate closed unless the caller explicitly waives diagnostics.
		unverified = {relative_name(path, workspace) for path in deleted}
		newly_unchecked = unverified.intersection(relative_name(path, workspace) for path in paths) - set(
			delta.unchecked
		)
		delta.unchecked = sorted(set(delta.unchecked) | unverified)
		delta.files_checked = max(0, delta.files_checked - len(newly_unchecked))
		delta.notes.append(
			"Deleted Python modules cannot be accurately simulated; check their importers after writing."
		)
	if left_out:  # a note, not an unchecked diagnostic: the write tools gate only on real unchecked files
		delta.notes.append(f"{left_out} more importing file(s) beyond the {CHECK_LIMIT}-file limit were not checked")
	# Importers in excluded directories are not checked on purpose; say so instead of passing silently.
	delta.notes.extend(format_skipped([s for s in skipped if s.reason == "excluded dir"], verb="checked"))
	return delta
