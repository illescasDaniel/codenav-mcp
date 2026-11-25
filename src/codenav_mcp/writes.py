"""The write tools' shared machinery: per-workspace state and the preview -> check -> apply flow.

Every write tool ends in `finish_plan`: it simulates the plan against the type
checker, then either stores it as a preview (the default for refactorings) or
applies it atomically and records it for undo. Nothing here talks MCP; the
tool functions in `write_tools` do.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

from mcp_nav_shared.diagnostics_delta import DiagnosticsDelta, entries_from_lsp, format_delta
from mcp_nav_shared.edits import EditPlan, FileChange, read_source, relative_name
from mcp_nav_shared.errors import TOOL_ERRORS, FailedText, ToolInputError
from mcp_nav_shared.lsp_client import LspClient
from mcp_nav_shared.transaction import EditJournal, PlanStore, WriteGuard, read_only_from_env, refuse_home_path

from codenav_mcp.deps import ENV_PREFIX
from codenav_mcp.simulate import simulate_plan


READ_ONLY_ENV = "CODENAV_MCP_READ_ONLY"
ALLOW_READONLY_ENV = "CODENAV_MCP_ALLOW_READONLY_FILES"
PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
DEFAULT_DIFF_LINES = 150


@dataclass
class WriteState:
	root: Path
	guard: WriteGuard
	journal: EditJournal
	plans: PlanStore = field(default_factory=PlanStore)


_state: WriteState | None = None


def write_state(root: Path) -> WriteState:
	"""The journal and pending previews for `root`; a different workspace starts fresh.

	The environment-derived guard settings (read-only mode, read-only files) follow the environment on every
	call; other guard settings already on the state are kept, and the journal keeps its history."""
	global _state
	read_only = read_only_from_env(READ_ONLY_ENV)
	allow_readonly = read_only_from_env(ALLOW_READONLY_ENV)
	if _state is None or _state.root != root:
		guard = WriteGuard(
			root=root,
			allowed_suffixes=PYTHON_SUFFIXES,
			read_only=read_only,
			allow_readonly=allow_readonly,
			exclude_prefix=ENV_PREFIX,
		)
		_state = WriteState(root=root, guard=guard, journal=EditJournal(guard))
	elif _state.guard.read_only != read_only or _state.guard.allow_readonly != allow_readonly:
		_state.guard = dataclasses.replace(_state.guard, read_only=read_only, allow_readonly=allow_readonly)
		_state.journal.guard = _state.guard
	return _state


def reset_state() -> None:
	global _state
	_state = None


def import_roots(workspace: Path, source_root: Path, extra_roots: list[Path]) -> list[Path]:
	"""Directories a dotted import path can start from."""
	roots = [source_root, workspace, *extra_roots]
	if (workspace / "src").is_dir():
		roots.append(workspace / "src")
	return list(dict.fromkeys(r.resolve() for r in roots))


@dataclass
class Outcome:
	"""What `finish_plan` did, for tools that want to add to the text."""

	text: str
	applied_id: str | None = None
	preview_id: str | None = None
	delta: DiagnosticsDelta | None = None


def _count_label(count: int, noun: str) -> str:
	return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


async def errors_after_write(client: LspClient, root: Path, plan: EditPlan, *, limit: int = 8) -> str:
	"""Diagnostics of the edited files as they are on disk now (the simulation's second opinion)."""
	try:
		await client.refresh()
	except TOOL_ERRORS:
		unchecked = [
			relative_name(c.path, root)
			for c in plan.changes
			if c.new_text is not None and c.path.suffix.lower() in PYTHON_SUFFIXES
		]
		return f"Not checked (language server unavailable): {', '.join(unchecked)} ({len(unchecked)} files)."
	lines: list[str] = []
	total = 0
	unchecked: list[str] = []
	files = [c for c in plan.changes if c.new_text is not None and c.path.suffix.lower() in PYTHON_SUFFIXES]
	created = [str(c.path) for c in files if c.old_text is None]
	snapshot = None
	if created:
		try:
			snapshot = await client.diagnostics_with_open_files([str(c.path) for c in files], open_first=created)
		except TOOL_ERRORS:
			# If one prerequisite cannot be synchronized, none of the dependent diagnostics are reliable.
			names = [relative_name(c.path, root) for c in files]
			return f"Not checked (could not synchronize edited files): {', '.join(names)} ({len(names)} files)."
	for change in plan.changes:
		if change.new_text is None or change.path.suffix.lower() not in PYTHON_SUFFIXES:
			continue
		try:
			items = snapshot[str(change.path)] if snapshot is not None else await client.diagnostics(str(change.path))
		except TOOL_ERRORS:
			unchecked.append(relative_name(change.path, root))
			continue
		for entry in entries_from_lsp(relative_name(change.path, root), change.new_text, items):
			if entry.is_error:
				total += 1
				if len(lines) < limit:
					lines.append(f"  {entry.render()}")
	if unchecked:
		status = f"Not checked (language server unavailable): {', '.join(unchecked)} ({len(unchecked)} files)."
		if total:
			more = f"\n  ... {total - limit} more" if total > limit else ""
			status += (
				f"\nAfter writing: {_count_label(total, 'error')} in the checked files:\n" + "\n".join(lines) + more
			)
		return status
	if total == 0:
		return "After writing: no errors in the edited files."
	more = f"\n  ... {total - limit} more" if total > limit else ""
	return (
		f"After writing: {_count_label(total, 'error')} in the edited files (includes any that existed before):\n"
		+ "\n".join(lines)
		+ more
	)


def _refuse_new_file_preview(
	state: WriteState,
	workspace: Path,
	plan: EditPlan,
	created: list[FileChange],
	title: str,
	diff_lines: int,
) -> Outcome:
	"""Refuse a preview that would create a file, and keep the plan for the existing approval flow.

	Checking a new file needs it materialised in the workspace, which a crash (SIGKILL) could leave behind
	(RC-091): a preview never creates files. The user approves the write through `apply_edit` (or `apply=true`).
	"""
	names = ", ".join(repr(relative_name(change.path, workspace)) for change in created)
	preview_id = state.plans.add(plan)
	lines = [
		title,
		"",
		f"Preview refused: the plan creates {_count_label(len(created), 'new file')} ({names}). Checking a new file "
		"needs a temporary file in the workspace that a crash could leave behind; previews never create files.",
		f'To approve the write: apply_edit(id="{preview_id}") creates it (undo_edit(id="{preview_id}") reverts it), '
		"or rerun the tool with apply=true to run the type check first.",
		"",
		"Files:",
		plan.summary(workspace),
	]
	if diff_lines > 0:
		lines += ["", plan.diff(workspace, max_lines=diff_lines).rstrip()]
	return Outcome(FailedText("\n".join(lines)), preview_id=preview_id)


async def finish_plan(
	client: LspClient,
	*,
	workspace: Path,
	roots: list[Path],
	plan: EditPlan,
	title: str,
	apply: bool,
	max_new_errors: int | None,
	allow_large: bool = False,
	include_dependents: bool = True,
	diff_lines: int = DEFAULT_DIFF_LINES,
) -> Outcome:
	"""Simulate `plan`, then preview it or apply it, and describe what happened."""
	if max_new_errors is not None and max_new_errors < 0:
		raise ToolInputError("max_new_errors must be 0 or more (or null for no limit)")
	state = write_state(workspace)
	for change in plan.changes:
		state.guard.check_path(change.path)
	if plan.changes and all(change.is_noop for change in plan.changes):
		name = relative_name(plan.changes[0].path, workspace)
		return Outcome(f"{title}\nNo changes: the edit leaves {name} as it is.")
	if plan.is_empty:
		return Outcome(f"{title}\nNo changes: the edit leaves every file as it is.")
	if any(change.is_noop for change in plan.changes):
		changes = [change for change in plan.changes if not change.is_noop]
		plan = EditPlan(plan.description, changes, plan.notes, {c.path: plan.base_hashes[c.path] for c in changes})
	created = [change for change in plan.changes if change.kind == "create"]
	if created and not apply:
		return _refuse_new_file_preview(state, workspace, plan, created, title, diff_lines)
	delta = await simulate_plan(client, workspace, roots, plan, include_dependents=include_dependents)
	lines = [title, ""]
	# Only real diagnostics gate the write. Importers past the check limit are a note (simulate.py), not a gate.
	refusal = ""
	if max_new_errors is not None and delta.unchecked:
		refusal = (
			"diagnostics could not be checked for all files; review the unchecked files or set max_new_errors=null."
		)
	elif max_new_errors is not None and len(delta.new_errors) > max_new_errors:
		refusal = (
			f"{_count_label(len(delta.new_errors), 'new error')} (allowed: {max_new_errors}). "
			"Review the diagnostics below; adjust and rerun, or write the plan anyway with apply_edit."
		)
	# The file-count limit is checked when the write starts, after the plan was simulated; check it here too so
	# that a refused plan is kept under an id (apply_edit(id, allow_large=true) writes it).
	too_large = len(plan.changes) > state.guard.max_files and not allow_large
	if too_large and not refusal:
		refusal = (
			f"the edit touches {len(plan.changes)} files (limit {state.guard.max_files}); "
			"rerun with allow_large=true to apply it"
		)
	blocked = f"NOT applied: {refusal}" if apply and refusal else ""
	applied_id = None
	preview_id = None
	if apply and not blocked:
		entry = state.journal.apply(plan, allow_large=allow_large)
		applied_id = entry.id
		status = await errors_after_write(client, workspace, plan)
		if status.startswith("Not checked"):
			unchecked_count = status.splitlines()[0].rsplit(" (", 1)[-1].split()[0]
			lines.append(f"Applied as edit {entry.id}. {unchecked_count} file(s) could not be checked for errors.")
		else:
			lines.append(f'Applied as edit {entry.id} (undo_edit(id="{entry.id}") reverts it).')
		lines.append(status)
	else:
		preview_id = state.plans.add(plan)
		keep = f'apply_edit(id="{preview_id}", allow_large=true)' if too_large else f'apply_edit(id="{preview_id}")'
		if blocked:
			lines.append(blocked)
			lines.append(f"The plan is kept: {keep} writes it anyway.")
		elif refusal:  # a preview: say what apply=true would run into, and do not promise it would succeed
			lines.append(f"Preview only, nothing written. apply=true would be refused: {refusal}")
			lines.append(f"The plan is kept: {keep} writes it anyway.")
		else:
			lines.append("Preview only, nothing written.")
			lines.append(f'Write it with apply_edit(id="{preview_id}") or rerun with apply=true.')
	if not include_dependents:
		lines.append("Dependent files were not checked.")
	lines += ["", "Files:", plan.summary(workspace)]
	if plan.notes:
		lines += ["", "Notes:", *(f"  - {note}" for note in plan.notes)]
	lines += ["", format_delta(delta)]
	if applied_id is None and diff_lines > 0:
		lines += ["", plan.diff(workspace, max_lines=diff_lines).rstrip()]
	text = "\n".join(lines)
	return Outcome(FailedText(text) if blocked else text, applied_id=applied_id, preview_id=preview_id, delta=delta)


def read_text(path: Path) -> str:
	return read_source(path)[0]


def require_workspace_file(workspace: Path, file_path: str) -> Path:
	"""A file path (workspace-relative or absolute) that exists inside the workspace."""
	refuse_home_path(file_path)
	path = Path(file_path)
	if not path.is_absolute():
		path = workspace / path
	resolved = path.resolve()
	try:
		resolved.relative_to(workspace.resolve())
	except ValueError:
		display = f"<outside workspace>/{path.name}" if Path(file_path).is_absolute() else file_path
		raise ToolInputError(f"{display} is outside the workspace; give a path inside it") from None
	if resolved.is_dir():
		raise ToolInputError(f"{file_path} is a directory, not a file")
	if not resolved.is_file():
		raise ToolInputError(f"File not found: {file_path} (relative paths resolve against the workspace root).")
	return resolved
