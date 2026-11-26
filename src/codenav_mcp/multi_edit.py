"""`edit_symbol(edits=[...])`: several edits as ONE plan (one type check, one preview/undo id, all or nothing).

Each item runs through the planning code its single-call form uses (`write_tools._edit_text` for
`{file_path, old_string, new_string}` items, `edit_tools.symbol_action` for `{name, action, source}` items) with
`writes.plan_capture` set, so `finish_plan` only records the plan. Every item is planned against the files as they
are now; plans touching one file are merged hunk by hunk (`plan_merge.merge_plans`), and the merged plan goes
through `finish_plan` once. Any failing item (or overlap) writes nothing and is named in the reply.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import Context
from mcp_nav_shared.errors import TOOL_ERRORS, FailedText, ToolInputError, format_tool_error

from codenav_mcp import write_tools
from codenav_mcp.plan_merge import merge_plans
from codenav_mcp.pysource import check_syntax
from codenav_mcp.reply import Reply
from codenav_mcp.tool_base import open_session
from codenav_mcp.writes import CapturedPlan, finish_plan, plan_capture


MAX_ITEMS = 50
_TEXT_KEYS = frozenset({"file_path", "old_string", "new_string", "replace_all", "new_text"})
_SYMBOL_KEYS = frozenset({"name", "action", "source", "file_path", "position", "imports", "force", "prune_imports"})
_TEXT_MARKERS = frozenset({"old_string", "new_string", "new_text", "replace_all"})
_REASON_LINES = 5

SymbolAction = Callable[..., Awaitable[str]]


def _describe(item: dict[str, Any]) -> str:
	if _TEXT_MARKERS & item.keys():
		return str(item.get("file_path", "?"))
	return " ".join(str(part) for part in (item.get("action", "replace"), item.get("name")) if part)


def _reason(text: str) -> str:
	lines = [line for line in str(text).splitlines() if line.strip()]
	shown = " / ".join(line.strip() for line in lines[:_REASON_LINES])
	return shown + (" ..." if len(lines) > _REASON_LINES else "")


async def _plan_item(
	item: Any, symbol_action: SymbolAction, ctx: Context | None
) -> tuple[CapturedPlan | None, str | None]:
	"""(the item's captured plan, None) or (None, why it cannot be planned)."""
	if not isinstance(item, dict):
		return (
			None,
			f"each item is an object like {{file_path, old_string, new_string}} or {{name, action, source}}, got {type(item).__name__}",
		)
	text_item = bool(_TEXT_MARKERS & item.keys())
	allowed = _TEXT_KEYS if text_item else _SYMBOL_KEYS
	unknown = sorted(set(item) - allowed)
	if unknown:
		kind = "a text edit" if text_item else "a symbol edit"
		return None, f"unknown key(s) {', '.join(unknown)} for {kind}; allowed: {', '.join(sorted(allowed))}"
	captured: list[CapturedPlan] = []
	token = plan_capture.set(captured)
	try:
		if text_item:
			if not isinstance(item.get("file_path"), str):
				return None, "a text edit needs `file_path`"
			result = await write_tools._edit_text(
				item["file_path"],
				new_text=item.get("new_text"),
				old_string=item.get("old_string"),
				new_string=item.get("new_string"),
				replace_all=bool(item.get("replace_all", False)),
				apply=True,
				max_new_errors=None,
				ctx=ctx,
			)
		else:
			if not item:
				return (
					None,
					"empty item: give `name` (and `action`, `source`), or `file_path` with `old_string` and `new_string`",
				)
			result = await symbol_action(
				item.get("action") or "replace",
				item.get("name"),
				item.get("source"),
				item.get("file_path"),
				item.get("position"),
				item.get("imports"),
				bool(item.get("force", False)),
				bool(item.get("prune_imports", True)),
				apply=True,
				max_new_errors=None,
				ctx=ctx,
			)
	except TOOL_ERRORS as exc:
		return None, _reason(format_tool_error(exc))
	finally:
		plan_capture.reset(token)
	if not captured:
		return None, _reason(result)
	plan = captured[-1]
	if not plan.apply:  # the tool held its plan back (say, a delete whose uses remain)
		return None, _reason("\n".join([*plan.plan.notes[:1], "it would only be previewed, not written"]))
	return plan, None


async def run(
	items: list[dict[str, Any]],
	*,
	symbol_action: SymbolAction,
	apply: bool,
	max_new_errors: int | None,
	ctx: Context | None,
) -> str:
	if not isinstance(items, list) or not items:
		raise ToolInputError(
			'`edits` is a non-empty list. Example: edits=[{"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}]'
		)
	if len(items) > MAX_ITEMS:
		raise ToolInputError(f"{len(items)} edits in one call (limit {MAX_ITEMS}); split them")
	session = await open_session(ctx)
	plans: list[tuple[str, CapturedPlan]] = []
	failures: list[str] = []
	for number, item in enumerate(items, 1):
		label = f"item {number}"
		plan, problem = await _plan_item(item, symbol_action, ctx)
		if problem is not None:
			shown = _describe(item) if isinstance(item, dict) else "?"
			failures.append(f"{label} ({shown}): {problem}")
		elif plan is not None:
			plans.append((label, plan))
	if failures:
		return _refusal(len(failures), len(items), failures)
	try:
		count = len(items)
		merged = merge_plans(
			[(label, captured.plan) for label, captured in plans], f"Apply {count} edit{'s' * (count != 1)}"
		)
		for change in merged.changes:
			if change.new_text is not None and change.path.suffix.lower() in (".py", ".pyi"):
				check_syntax(change.new_text, change.path.name)
	except TOOL_ERRORS as exc:
		return _refusal(1, len(items), [_reason(format_tool_error(exc))])
	files = len(merged.changes)
	merged.description = f"Apply {len(items)} edit{'s' * (len(items) != 1)} ({files} file{'s' * (files != 1)})"
	title = "\n".join([merged.description, *(f"  {label}: {captured.plan.description}" for label, captured in plans)])
	outcome = await finish_plan(
		session.client,
		workspace=session.workspace,
		roots=session.roots,
		plan=merged,
		title=title,
		apply=apply,
		max_new_errors=max_new_errors,
		edits_style=True,
	)
	return outcome.text


def _refusal(failed: int, total: int, failures: list[str]) -> FailedText:
	reply = Reply(f"Nothing written: {failed} of {total} edit{'s' * (total != 1)} failed.")
	reply.items += [f"  {line}" for line in failures]
	reply.notes.append("every item is matched against the files as they are now, not after the other items")
	reply.notes.append("fix the items above and rerun the whole call; no preview was stored")
	return FailedText(reply.render())
