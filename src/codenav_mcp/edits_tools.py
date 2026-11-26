"""v2 tools - WS3: `edits` (persistent previews and undo). Registered by server.py via TOOLS; see docs/v2-design.md."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context
from mcp_nav_shared.edits import EditPlan, relative_name
from mcp_nav_shared.errors import TOOL_ERRORS, ToolInputError, format_tool_error

from codenav_mcp import edit_ops, tool_base
from codenav_mcp.edit_state import StoredPlan, age_text, changed_files
from codenav_mcp.reply import Reply, call, paginate
from codenav_mcp.writes import WriteState, write_state


_ACTIONS = ("list", "apply", "undo", "show")
_DIFF_LINES = 150


async def edits(
	action: str = "list",
	id: str | None = None,  # noqa: A002
	sites: list[str] | None = None,
	allow_large: bool = False,
	limit: int = 10,
	ctx: Context | None = None,
) -> str:
	"""Apply, undo or show a previewed edit by id (kept across restarts). Example: edits(action="apply", id="ab12", sites=["u1"]).
	action: list|apply|undo|show. sites: uncertain-site ids or ["all"]. Refuses if files changed."""
	try:
		if action not in _ACTIONS:
			raise ToolInputError(
				f"unknown action {action!r}: use list, apply, undo or show. Example: {call('edits', action='list')}"
			)
		if sites is not None and action != "apply":
			raise ToolInputError('`sites` only applies to action="apply"')
		if allow_large and action != "apply":
			raise ToolInputError('`allow_large` only applies to action="apply"')
		if action == "apply":
			return await _apply(ctx, id, sites, allow_large)
		workspace, state = await _workspace_state(ctx)
		if action == "undo":
			return _undo(state, id)
		if action == "show":
			return _show(state, workspace, id)
		return _list(state, limit)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)


async def _workspace_state(ctx: Context | None) -> tuple[Any, WriteState]:
	"""The workspace and its write state, without starting the language server (list/show/undo never need it)."""
	srv = tool_base.server()
	await srv._use_workspace(ctx)
	return srv.WORKSPACE_ROOT, write_state(srv.WORKSPACE_ROOT)


def _count(count: int, noun: str) -> str:
	return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _unsaved_note(state: WriteState) -> list[str]:
	error = state.plans.save_error or state.journal.save_error
	return (
		[f"could not save to the state directory ({error}); previews and history last only until restart"]
		if error
		else []
	)


# -- list


def _list(state: WriteState, limit: int) -> str:
	pending = state.plans.pending()
	history = state.journal.history()
	if not pending and not history:
		reply = Reply("No previews or applied edits for this workspace.", notes=_unsaved_note(state))
		return reply.render()
	reply = Reply(f"{_count(len(pending), 'preview')} waiting, {_count(len(history), 'applied edit')} (newest first)")
	more_calls: list[str] = []
	if pending:
		page = paginate(pending, limit)
		reply.items.append("Previews (nothing written yet):")
		for stored in page.items:
			reply.items.append(_preview_line(stored, state))
		if page.more:
			reply.items.append(f"  ... {page.more} older")
			more_calls.append(call("edits", action="list", limit=len(pending)))
	if history:
		page = paginate(history, limit)
		reply.items.append("Applied (undo reverts):")
		for entry in page.items:
			stale = _entry_changed(entry, state)
			age = (
				age_text(state.journal.applied_at.get(entry.id, 0.0)) if state.journal.applied_at.get(entry.id) else ""
			)
			files = _count(len(entry.plan.changes), "file")
			flag = f"  [changed since: {', '.join(stale[:3])}]" if stale else ""
			reply.items.append(f"  {entry.id}  {entry.plan.description}  [{files}]  {age}{flag}".rstrip())
		if page.more:
			reply.items.append(f"  ... {page.more} older")
			more_calls.append(call("edits", action="list", limit=len(history)))
	reply.notes += _unsaved_note(state)
	if pending:
		reply.next_calls.append(call("edits", action="apply", id=pending[0].id))
	if history:
		newest = history[0]
		# An undo of an entry whose files changed afterwards is refused: point at `show` instead.
		verb = "show" if _entry_changed(newest, state) else "undo"
		reply.next_calls.append(call("edits", action=verb, id=newest.id))
	if more_calls:
		reply.next_calls.append(more_calls[0])
	return reply.render()


def _entry_changed(entry: Any, state: WriteState) -> list[str]:
	names = []
	for path, expected in entry.applied_hashes.items():
		if edit_ops._current(path) != expected:
			names.append(relative_name(path, state.root))
	return names


def _preview_line(stored: StoredPlan, state: WriteState) -> str:
	plan = stored.plan.plan
	parts = [_count(len(plan.changes), "file")]
	if stored.plan.optional_sites:
		parts.append(_count(len(stored.plan.optional_sites), "optional site"))
	stale = changed_files(plan, state.root)
	flag = f"  STALE ({', '.join(stale[:3])} changed): {edit_ops.rerun_hint(stored.rerun)}" if stale else ""
	return f"  {stored.id}  {plan.description}  [{', '.join(parts)}]  {age_text(stored.created)}{flag}"


# -- apply / undo


async def _apply(ctx: Context | None, plan_id: str | None, sites: list[str] | None, allow_large: bool) -> str:
	session = await tool_base.open_session(ctx)
	state = session.state
	if plan_id is None:
		pending = state.plans.pending()
		if len(pending) != 1:
			which = f"{len(pending)} previews are waiting" if pending else "no previews are waiting"
			raise ToolInputError(f"give the preview to apply: {which}. Example: {call('edits', action='list')}")
		plan_id = pending[0].id
	result = await edit_ops.apply_stored(session, plan_id, sites=sites, allow_large=allow_large)
	reply = Reply(f"Applied edit {result.entry.id}: {result.plan.description}")
	reply.items += result.plan.summary(session.workspace).splitlines()
	reply.items += result.after.splitlines()
	reply.notes += result.plan.notes[:8]
	if result.left:
		ids = ", ".join(f"{site.id} ({site.path}:{site.line})" for site in result.left[:6])
		reply.notes.append(
			f"{_count(len(result.left), 'optional site')} not applied: {ids}; redo the edit to see them again"
		)
	reply.notes += _unsaved_note(state)
	reply.next_calls.append(call("edits", action="undo", id=result.entry.id))
	return reply.render()


def _undo(state: WriteState, edit_id: str | None) -> str:
	entry = edit_ops.undo_stored(state, edit_id)
	reply = Reply(f"Reverted edit {entry.id}: {entry.plan.description}")
	reply.items += entry.plan.summary(state.root).splitlines()
	reply.notes += _unsaved_note(state)
	return reply.render()


# -- show


def _show(state: WriteState, workspace: Any, plan_id: str | None) -> str:
	pending = state.plans.pending()
	history = state.journal.history()
	if plan_id is None:
		if pending:
			plan_id = pending[0].id
		elif history:
			plan_id = history[0].id
		else:
			raise ToolInputError(f"no previews or applied edits to show. Example: {call('edits', action='list')}")
	stored = next((item for item in pending if item.id == plan_id), None)
	entry = next((item for item in history if item.id == plan_id), None)
	if stored is None and entry is None:
		state.plans.get_stored(plan_id)  # raises the "unknown / already applied" refusal with the next step
		raise ToolInputError(f"no preview or applied edit {plan_id!r}")
	plan: EditPlan = stored.plan.plan if stored is not None else entry.plan  # ty: ignore[possibly-unresolved-reference]
	reply = Reply("")
	files = _count(len(plan.changes), "file")
	if stored is not None:
		sites = stored.plan.optional_sites
		extra = f", {_count(len(sites), 'optional site')}" if sites else ""
		reply.answer = f"Preview {plan_id} (not written): {plan.description} - {files}{extra}"
		reply.items += plan.summary(workspace).splitlines()
		if sites:
			reply.items.append("Optional sites (opt in by id):")
			reply.items += [f"  {site.id}  {site.path}:{site.line}  [{site.kind}]  {site.text}" for site in sites]
		stale = changed_files(plan, state.root)
		if stale:
			reply.notes.append(
				f"changed since the preview: {', '.join(stale[:5])}. {edit_ops.rerun_hint(stored.rerun)}"
			)
		else:
			apply_args: dict[str, Any] = {"action": "apply", "id": plan_id}
			if sites:
				apply_args["sites"] = ["all"]
			reply.next_calls.append(call("edits", **apply_args))
	else:
		reply.answer = f"Applied edit {plan_id}: {plan.description} - {files}"
		reply.items += plan.summary(workspace).splitlines()
		reply.next_calls.append(call("edits", action="undo", id=plan_id))
	reply.notes += plan.notes[:8]
	diff = plan.diff(workspace, max_lines=_DIFF_LINES).rstrip()
	if diff:
		reply.items += ["", *diff.splitlines()]
	return reply.render()


TOOLS: list[tuple[Any, Any]] = [(edits, tool_base.WRITES)]
