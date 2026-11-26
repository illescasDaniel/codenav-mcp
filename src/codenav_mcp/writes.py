"""The write tools' shared machinery: per-workspace state and the preview -> check -> apply flow.

Every write tool ends in `finish_plan`: it simulates the plan against the type
checker, then either stores it as a preview (the default for refactorings) or
applies it atomically and records it for undo. Nothing here talks MCP; the
tool functions in `write_tools` do.
"""

from __future__ import annotations

import contextvars
import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mcp_nav_shared.diagnostics_delta import DiagnosticsDelta, entries_from_lsp, format_delta
from mcp_nav_shared.edits import EditPlan, FileChange, read_source, relative_name
from mcp_nav_shared.errors import TOOL_ERRORS, FailedText, ToolInputError
from mcp_nav_shared.lsp_client import LspClient
from mcp_nav_shared.transaction import WriteGuard, read_only_from_env, refuse_home_path

from codenav_mcp.deps import ENV_PREFIX
from codenav_mcp.edit_state import PersistentJournal, PersistentPlanStore, workspace_state_dir
from codenav_mcp.mentions import TEXT_SUFFIXES
from codenav_mcp.plan_diff import plan_diff
from codenav_mcp.plan_merge import site_followup
from codenav_mcp.plan_sites import CodenavPlan, OptionalSite
from codenav_mcp.reply import call
from codenav_mcp.simulate import simulate_plan
from codenav_mcp.ty_command import ty_coverage


READ_ONLY_ENV = "CODENAV_MCP_READ_ONLY"
ALLOW_READONLY_ENV = "CODENAV_MCP_ALLOW_READONLY_FILES"
PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
DEFAULT_DIFF_LINES = 150


@dataclass
class WriteState:
	root: Path
	guard: WriteGuard
	journal: PersistentJournal
	plans: PersistentPlanStore


_state: WriteState | None = None


def write_state(root: Path) -> WriteState:
	"""The journal and pending previews for `root` (kept on disk, see `edit_state`); another workspace has its own.

	The environment-derived guard settings (read-only mode, read-only files) follow the environment on every
	call; other guard settings already on the state are kept, and the journal keeps its history."""
	global _state
	read_only = read_only_from_env(READ_ONLY_ENV)
	allow_readonly = read_only_from_env(ALLOW_READONLY_ENV)
	if _state is None or _state.root != root:
		guard = WriteGuard(
			root=root,
			allowed_suffixes=PYTHON_SUFFIXES | TEXT_SUFFIXES,
			read_only=read_only,
			allow_readonly=allow_readonly,
			exclude_prefix=ENV_PREFIX,
		)
		directory = workspace_state_dir(root)
		_state = WriteState(
			root=root,
			guard=guard,
			journal=PersistentJournal(guard, directory),
			plans=PersistentPlanStore(root, directory),
		)
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
class CapturedPlan:
	plan: EditPlan
	apply: bool  # what the tool asked finish_plan to do (False: it held the plan back as a preview)
	title: str


# While set, `finish_plan` only records the plan it is given (nothing is simulated, stored or written). The
# multi-edit mode of `edit_symbol` runs each item through its normal planning code this way and merges the plans.
plan_capture: contextvars.ContextVar[list[CapturedPlan] | None] = contextvars.ContextVar("plan_capture", default=None)


def apply_call(preview_id: str, *, large: bool = False) -> str:
	return call("edits", action="apply", id=preview_id, allow_large=True if large else None)


def undo_call(edit_id: str) -> str:
	return call("edits", action="undo", id=edit_id)


def applied_line(edit_id: str, *, unchecked: str = "") -> str:
	"""The one line every applied write reply carries: that it was written (apply=true is the default) and the
	exact call that undoes it. `unchecked` is how many files the error check could not cover, if any."""
	head = f"Applied as edit {edit_id} (undo: {undo_call(edit_id)})."
	return f"{head} {unchecked} file(s) could not be checked for errors." if unchecked else head


@dataclass
class Outcome:
	"""What `finish_plan` did, for tools that want to add to the text."""

	text: str
	applied_id: str | None = None
	preview_id: str | None = None
	delta: DiagnosticsDelta | None = None
	followup: CodenavPlan | None = None


NOT_CHECKED = "tests and docs not checked"


def type_check_report(delta: DiagnosticsDelta) -> str:
	"""`format_delta`'s report, said to be a type check (a green one says nothing about tests, docs or generated files)."""
	head, _, rest = format_delta(delta).partition("\n")
	head = f"{head.replace('Diagnostics:', 'Type check:', 1)} - {NOT_CHECKED}"
	not_checked = getattr(delta, "not_checked", [])
	if not_checked:  # one line: a green result never reads as covering files that were left out
		head += f"; not checked: {', '.join(not_checked)}"
	return f"{head}\n{rest}" if rest else head


def _count_label(count: int, noun: str) -> str:
	return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


async def errors_after_write(client: LspClient, root: Path, plan: EditPlan, *, limit: int = 8) -> str:
	"""Diagnostics of the edited files as they are on disk now (the simulation's second opinion)."""
	files = [c for c in plan.changes if c.new_text is not None and c.path.suffix.lower() in PYTHON_SUFFIXES]
	if not files:
		return "After writing: no edited Python files to type-check."
	coverage = ty_coverage(root)
	excluded = [c for c in files if coverage.excludes(c.path, root)]
	files = [c for c in files if c not in excluded]
	excluded_note = (
		"\nTy config excludes: " + ", ".join(relative_name(c.path, root) for c in excluded) + "." if excluded else ""
	)
	if not files:
		return "After writing: no edited files covered by the type checker." + excluded_note
	try:
		await client.refresh()
	except TOOL_ERRORS:
		unchecked = [relative_name(c.path, root) for c in files]
		return (
			f"Not checked (language server unavailable): {', '.join(unchecked)} ({len(unchecked)} files)."
			+ excluded_note
		)
	lines: list[str] = []
	total = 0
	unchecked: list[str] = []
	created = [str(c.path) for c in files if c.old_text is None]
	snapshot = None
	if created:
		try:
			snapshot = await client.diagnostics_with_open_files([str(c.path) for c in files], open_first=created)
		except TOOL_ERRORS:
			# If one prerequisite cannot be synchronized, none of the dependent diagnostics are reliable.
			names = [relative_name(c.path, root) for c in files]
			return (
				f"Not checked (could not synchronize edited files): {', '.join(names)} ({len(names)} files)."
				+ excluded_note
			)
	for change in files:
		try:
			items = snapshot[str(change.path)] if snapshot is not None else await client.diagnostics(str(change.path))
		except TOOL_ERRORS:
			unchecked.append(relative_name(change.path, root))
			continue
		for entry in entries_from_lsp(relative_name(change.path, root), change.new_text or "", items):
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
		return status + excluded_note
	if total == 0:
		checked = "checked files" if excluded else "the edited files"
		return f"After writing: no errors in {checked} (type check only; {NOT_CHECKED})." + excluded_note
	more = f"\n  ... {total - limit} more" if total > limit else ""
	return (
		f"After writing: {_count_label(total, 'error')} in the edited files (includes any that existed before):\n"
		+ "\n".join(lines)
		+ more
		+ excluded_note
	)


def _preview_new_files(
	state: WriteState,
	workspace: Path,
	plan: EditPlan,
	created: list[FileChange],
	title: str,
	diff_lines: int,
	optional_sites: Sequence[OptionalSite] = (),
) -> Outcome:
	"""Keep an unchecked preview for a plan that would create files.

	Checking a new file needs it materialised in the workspace, which a crash (SIGKILL) could leave behind
	(RC-091): a preview never creates files. The user approves the write through `edits(action="apply", ...)` (or `apply=true`).
	"""
	names = ", ".join(repr(relative_name(change.path, workspace)) for change in created)
	preview_id = state.plans.add(CodenavPlan(plan, list(optional_sites)))
	lines = [
		title,
		"",
		f"Preview only, nothing written. The plan creates {_count_label(len(created), 'new file')} ({names}). "
		"It is not type-checked in a preview (previews never create files).",
		f"To approve the write: {apply_call(preview_id)} creates it "
		f"({undo_call(preview_id)} reverts it), "
		"or rerun the tool with apply=true to run the type check first.",
		"",
		"Files:",
		plan.summary(workspace),
	]
	if diff_lines > 0:
		lines += ["", plan_diff(plan, workspace, max_lines=diff_lines).rstrip()]
	return Outcome("\n".join(lines), preview_id=preview_id)


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
	hold_back: str = "",
	optional_sites: Sequence[OptionalSite] = (),
) -> Outcome:
	"""Simulate `plan`, then preview it or apply it, and describe what happened.

	`optional_sites` are the remaining uncertain edits kept for the agent to opt in to with
	`edits(action="apply", id=..., sites=[...])`."""
	capture = plan_capture.get()
	if capture is not None:
		capture.append(CapturedPlan(plan, apply, title))
		return Outcome("")
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
		plan = EditPlan(plan.description, changes, plan.notes, dict(plan.base_hashes))
	created = [change for change in plan.changes if change.kind == "create"]
	if created and not apply:
		return _preview_new_files(state, workspace, plan, created, title, diff_lines, optional_sites=optional_sites)
	delta = await simulate_plan(client, workspace, roots, plan, include_dependents=include_dependents)
	lines = [title, ""]
	# Only real diagnostics gate the write. Importers past the check limit are a note (simulate.py), not a gate.
	refusal = ""
	if hold_back and not allow_large:
		refusal = hold_back  # files the plan could not rewrite: held back like `move` holds back unreadable importers
	elif max_new_errors is not None and delta.unchecked:
		refusal = (
			"diagnostics could not be checked for all files; review the unchecked files or set max_new_errors=null."
		)
	elif max_new_errors is not None and len(delta.new_errors) > max_new_errors:
		refusal = (
			f"{_count_label(len(delta.new_errors), 'new error')} (allowed: {max_new_errors}). "
			f"Review the diagnostics below; adjust and rerun, or write the plan anyway with edits."
		)
	# The file-count limit is checked when the write starts, after the plan was simulated; check it here too so
	# that a refused plan is kept under an id (edits(action="apply", id=..., allow_large=true) writes it).
	too_large = len(plan.changes) > state.guard.max_files and not allow_large
	if too_large and not refusal:
		refusal = (
			f"the edit touches {len(plan.changes)} files (limit {state.guard.max_files}); "
			"rerun with allow_large=true to apply it"
		)
	blocked = f"NOT applied: {refusal}" if apply and refusal else ""
	applied_id = None
	preview_id = None
	followup = None
	if apply and not blocked:
		followup = site_followup(plan, optional_sites, workspace)
		entry = state.journal.apply(plan, allow_large=allow_large, followup=followup)
		applied_id = entry.id
		status = await errors_after_write(client, workspace, plan)
		if status.startswith("Not checked"):
			unchecked_count = status.splitlines()[0].rsplit(" (", 1)[-1].split()[0]
			lines.append(applied_line(entry.id, unchecked=unchecked_count))
		else:
			lines.append(applied_line(entry.id))
		lines.append(status)
	else:
		preview_id = state.plans.add(CodenavPlan(plan, list(optional_sites)))
		keep = apply_call(preview_id, large=too_large)
		if blocked:
			lines.append(blocked)
			lines.append(f"The plan is kept: {keep} writes it anyway.")
		elif refusal:  # a preview: say what apply=true would run into, and do not promise it would succeed
			lines.append(f"Preview only, nothing written. apply=true would be refused: {refusal}")
			lines.append(f"The plan is kept: {keep} writes it anyway.")
		else:
			lines.append("Preview only, nothing written.")
			lines.append(f"Write it with {apply_call(preview_id)} or rerun with apply=true.")
	if not include_dependents:
		lines.append("Dependent files were not checked.")
	lines += ["", "Files:", plan.summary(workspace)]
	if plan.notes:
		lines += ["", "Notes:", *(f"  - {note}" for note in plan.notes)]
	lines += ["", type_check_report(delta)]
	if applied_id is None and diff_lines > 0:
		lines += ["", plan_diff(plan, workspace, max_lines=diff_lines).rstrip()]
	text = "\n".join(lines)
	return Outcome(
		FailedText(text) if blocked else text,
		applied_id=applied_id,
		preview_id=preview_id,
		delta=delta,
		followup=followup,
	)


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
