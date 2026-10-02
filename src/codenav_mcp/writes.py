"""The write tools' shared machinery: per-workspace state and the preview -> check -> apply flow.

Every write tool ends in `finish_plan`: it simulates the plan against the type
checker, then either stores it as a preview (the default for refactorings) or
applies it atomically and records it for undo. Nothing here talks MCP; the
tool functions in `write_tools` do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mcp_nav_shared.diagnostics_delta import DiagnosticsDelta, entries_from_lsp, format_delta
from mcp_nav_shared.edits import EditPlan, read_source, relative_name
from mcp_nav_shared.errors import TOOL_ERRORS, ToolInputError
from mcp_nav_shared.lsp_client import LspClient
from mcp_nav_shared.transaction import EditJournal, PlanStore, WriteGuard, read_only_from_env

from codenav_mcp.simulate import simulate_plan


READ_ONLY_ENV = "CODENAV_MCP_READ_ONLY"
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
	"""The journal and pending previews for `root`; a different workspace starts fresh."""
	global _state
	read_only = read_only_from_env(READ_ONLY_ENV)
	if _state is None or _state.root != root:
		guard = WriteGuard(root=root, allowed_suffixes=PYTHON_SUFFIXES, read_only=read_only)
		_state = WriteState(root=root, guard=guard, journal=EditJournal(guard))
	elif _state.guard.read_only != read_only:
		_state.guard = WriteGuard(root=root, allowed_suffixes=PYTHON_SUFFIXES, read_only=read_only)
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
	await client.refresh()
	lines: list[str] = []
	total = 0
	for change in plan.changes:
		if change.new_text is None or change.path.suffix.lower() not in PYTHON_SUFFIXES:
			continue
		try:
			items = await client.diagnostics(str(change.path))
		except TOOL_ERRORS:
			continue
		for entry in entries_from_lsp(relative_name(change.path, root), change.new_text, items):
			if entry.is_error:
				total += 1
				if len(lines) < limit:
					lines.append(f"  {entry.render()}")
	if total == 0:
		return "After writing: no errors in the edited files."
	more = f"\n  ... {total - limit} more" if total > limit else ""
	return (
		f"After writing: {_count_label(total, 'error')} in the edited files (includes any that existed before):\n"
		+ "\n".join(lines)
		+ more
	)


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
	state = write_state(workspace)
	if plan.is_empty:
		return Outcome(f"{title}\nNo changes: the edit leaves every file as it is.")
	for change in plan.changes:
		state.guard.check_path(change.path)
	delta = await simulate_plan(client, workspace, roots, plan, include_dependents=include_dependents)
	lines = [title, ""]
	blocked = ""
	if apply and max_new_errors is not None and len(delta.new_errors) > max_new_errors:
		blocked = (
			f"NOT applied: {_count_label(len(delta.new_errors), 'new error')} (allowed: {max_new_errors}). "
			"Review the diagnostics below; apply anyway with apply_edit, or adjust and rerun."
		)
	applied_id = None
	preview_id = None
	if apply and not blocked:
		entry = state.journal.apply(plan, allow_large=allow_large)
		applied_id = entry.id
		lines.append(f'Applied as edit {entry.id} (undo_edit(id="{entry.id}") reverts it).')
	else:
		preview_id = state.plans.add(plan)
		lines.append(blocked or "Preview only, nothing written.")
		lines.append(f'Write it with apply_edit(id="{preview_id}") or rerun with apply=true.')
	lines += ["", "Files:", plan.summary(workspace)]
	if plan.notes:
		lines += ["", "Notes:", *(f"  - {note}" for note in plan.notes)]
	lines += ["", format_delta(delta)]
	if applied_id is None and diff_lines > 0:
		lines += ["", plan.diff(workspace, max_lines=diff_lines).rstrip()]
	if applied_id is not None:
		lines += ["", await errors_after_write(client, workspace, plan)]
	return Outcome("\n".join(lines), applied_id=applied_id, preview_id=preview_id, delta=delta)


def read_text(path: Path) -> str:
	return read_source(path)[0]


def require_workspace_file(workspace: Path, file_path: str) -> Path:
	"""A file path (workspace-relative or absolute) that exists inside the workspace."""
	path = Path(file_path)
	if not path.is_absolute():
		path = workspace / path
	resolved = path.resolve()
	try:
		resolved.relative_to(workspace.resolve())
	except ValueError:
		raise ToolInputError(f"{file_path} is outside the workspace {workspace}") from None
	if not resolved.is_file():
		raise ToolInputError(f"File not found: {file_path} (relative paths resolve against the workspace root).")
	return resolved
