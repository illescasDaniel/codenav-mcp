"""Applying a stored preview and undoing an applied edit: the one implementation behind `edits` (v2) and
`apply_edit` / `undo_edit` (1.x)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mcp_nav_shared.edits import EditPlan, hash_bytes, relative_name
from mcp_nav_shared.errors import ToolInputError
from mcp_nav_shared.transaction import AppliedEdit, StaleFileError

from codenav_mcp.edit_state import StoredPlan, changed_files
from codenav_mcp.plan_merge import add_sites, site_followup
from codenav_mcp.plan_sites import OptionalSite
from codenav_mcp.reply import call
from codenav_mcp.simulate import simulate_plan
from codenav_mcp.tool_base import Session
from codenav_mcp.writes import WriteState, errors_after_write, type_check_report


MAX_REPEATED_CALL = 1500
_SHOWN_FILES = 5


@dataclass
class Applied:
	entry: AppliedEdit
	plan: EditPlan  # what was written (the stored plan plus any chosen sites)
	after: str  # the type check of the edited files, as on disk now
	chosen: list[OptionalSite]
	left: list[OptionalSite]  # optional sites the agent did not choose


def rerun_hint(rerun: str | None) -> str:
	"""How to make the preview again: the exact call that made it when it is known and short enough."""
	if rerun and len(rerun) <= MAX_REPEATED_CALL:
		return f"Redo the edit on the current files: {rerun}"
	if rerun:
		return f"Redo your original {rerun.partition('(')[0]}(...) call on the current files"
	return "Redo the tool call that made the preview"


def _names(names: Sequence[str]) -> str:
	shown = ", ".join(names[:_SHOWN_FILES])
	return shown + (f" (+{len(names) - _SHOWN_FILES} more)" if len(names) > _SHOWN_FILES else "")


def pick_sites(stored: StoredPlan, sites: Sequence[str] | None) -> list[OptionalSite]:
	"""The optional sites named by `sites` (`["u1", "u3"]` or `["all"]`), in the plan's order."""
	if not sites:
		return []
	available = stored.plan.optional_sites
	if not available:
		raise ToolInputError(
			f"preview {stored.id} has no optional sites; drop `sites` "
			f"({call('edits', action='show', id=stored.id)} lists what it holds)"
		)
	wanted = {site.strip() for site in sites}
	if "all" in {w.lower() for w in wanted}:
		return list(available)
	unknown = sorted(wanted - {site.id for site in available})
	if unknown:
		known = ", ".join(site.id for site in available)
		raise ToolInputError(f"unknown site id(s) {', '.join(unknown)}; preview {stored.id} has: {known}")
	return [site for site in available if site.id in wanted]


async def apply_stored(
	session: Session, plan_id: str, *, sites: Sequence[str] | None = None, allow_large: bool = False
) -> Applied:
	"""Write the stored preview `plan_id` (plus the optional sites chosen by id), refusing if a file changed."""
	state = session.state
	parent_id = None
	followup = state.journal.followup(plan_id) if sites else None
	if followup is not None:
		parent_id = plan_id
		stored = StoredPlan(plan_id, 0, None, followup)
	elif sites and state.journal.entry(plan_id) is not None:
		raise ToolInputError(
			f"applied edit {plan_id} has no available optional sites. {call('edits', action='list')} shows recent edits."
		)
	else:
		stored = state.plans.get_stored(plan_id)
	stale = changed_files(stored.plan.plan, state.root)
	if stale:
		if parent_id is not None:
			raise StaleFileError(f"changed since edit {parent_id} was applied: {_names(stale)}. Nothing was written.")
		raise StaleFileError(
			f"changed since the preview was made: {_names(stale)}. Nothing was written. {rerun_hint(stored.rerun)}"
		)
	chosen = pick_sites(stored, sites)
	plan = add_sites(stored.plan.plan, chosen, session.workspace)
	if parent_id is not None:
		if plan.is_empty:
			raise ToolInputError("the requested optional sites make no changes")
		state.guard.check_plan(plan, allow_large=allow_large)
		delta = await simulate_plan(session.client, session.workspace, session.roots, plan)
		if delta.unchecked or delta.new_errors:
			raise ToolInputError(f"Optional sites were not applied. {type_check_report(delta)}")
	left = [site for site in stored.plan.optional_sites if site not in chosen]
	remaining = site_followup(plan, left, session.workspace)
	entry = state.journal.apply(
		plan,
		allow_large=allow_large,
		entry_id=plan_id if parent_id is None else None,
		followup=remaining,
		parent_id=parent_id,
	)
	if parent_id is None:
		state.plans.discard(plan_id)
	after = await errors_after_write(session.client, session.workspace, plan)
	return Applied(entry, plan, after, chosen, remaining.optional_sites if remaining else [])


def undo_stored(state: WriteState, edit_id: str | None) -> AppliedEdit:
	"""Revert an applied edit (the latest by default), refusing when a file changed after it."""
	history = state.journal.history()
	target = next((entry for entry in history if entry.id == edit_id), None) if edit_id else next(iter(history), None)
	if target is not None:
		stale = [
			relative_name(path, state.root)
			for path, expected in target.applied_hashes.items()
			if _current(path) != expected
		]
		# A file changed by a newer journal edit is the journal's to explain (it names the edit to undo first).
		if stale and not _changed_by_later_edit(state, target, history):
			raise StaleFileError(
				f"changed since edit {target.id} was applied: {_names(stale)}. Nothing was undone, so your later "
				f"changes stay. Review the edit with {call('edits', action='show', id=target.id)} and revert it by hand."
			)
	return state.journal.undo(edit_id)


def _current(path: Path) -> str | None:
	try:
		return hash_bytes(path.read_bytes())
	except FileNotFoundError:
		return None


def _changed_by_later_edit(state: WriteState, target: AppliedEdit, history: list[AppliedEdit]) -> bool:
	"""Whether the newer edits in the history hold the current bytes of every changed file of `target`."""
	newer = history[: history.index(target)]
	for path, expected in target.applied_hashes.items():
		if _current(path) == expected:
			continue
		if not any(entry.applied_hashes.get(path, "") == _current(path) for entry in newer):
			return False
	return True
