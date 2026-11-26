"""Definition editing and moves, backed by the shared write planner."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import Context
from mcp_nav_shared.errors import ToolInputError, format_tool_error

from codenav_mcp import multi_edit, refactor_tools, symbol_tools, tool_base


_Action = Literal["replace", "replace_body", "insert", "insert_after", "insert_before", "delete"]
_Position = Literal["after", "before", "into", "end"]


async def edit_symbol(
	action: _Action | None = None,
	name: str | None = None,
	source: str | None = None,
	file_path: str | None = None,
	position: _Position | None = None,
	imports: list[str] | None = None,
	force: bool = False,
	prune_imports: bool = True,
	apply: bool = True,
	max_new_errors: int | None = 0,
	edits: list[dict[str, Any]] | None = None,
	ctx: Context | None = None,
) -> str:
	"""Edit definitions atomically. Example: `edit_symbol(name="parse", action="delete")`."""
	try:
		if edits is not None:
			_reject(
				{
					"action": action,
					"name": name,
					"source": source,
					"file_path": file_path,
					"position": position,
					"imports": imports,
					"force": force or None,
				},
				"edits",
				what="`edits` replaces",
			)
			return await multi_edit.run(
				edits, symbol_action=symbol_action, apply=apply, max_new_errors=max_new_errors, ctx=ctx
			)
		return await symbol_action(
			action or "replace",
			name,
			source,
			file_path,
			position,
			imports,
			force,
			prune_imports,
			apply=apply,
			max_new_errors=max_new_errors,
			ctx=ctx,
		)
	except ToolInputError as exc:
		return format_tool_error(exc)


async def symbol_action(
	action: str,
	name: str | None,
	source: str | None,
	file_path: str | None,
	position: str | None,
	imports: list[str] | None,
	force: bool,
	prune_imports: bool,
	*,
	apply: bool,
	max_new_errors: int | None,
	ctx: Context | None,
) -> str:
	"""One edit_symbol action (what `edit_symbol` does without `edits`, and what each symbol item of `edits` runs)."""
	if action in ("insert_after", "insert_before"):
		if position is not None:
			raise ToolInputError(f'`position` does not apply to action="{action}"')
		position = action.removeprefix("insert_")
		action = "insert"
	if action == "replace":
		_reject({"position": position, "force": force or None}, action)
		if not name or source is None:
			raise ToolInputError('action="replace" needs `name` and `source`')
		return await symbol_tools._replace_symbol(
			name, source, file_path, imports, apply=apply, max_new_errors=max_new_errors, ctx=ctx
		)
	if action == "replace_body":
		_reject({"position": position, "force": force or None}, action)
		if not name or source is None:
			raise ToolInputError('action="replace_body" needs `name` and `source` (the new body)')
		return await symbol_tools._replace_body(
			name, source, file_path, imports, apply=apply, max_new_errors=max_new_errors, ctx=ctx
		)
	if action == "insert":
		_reject({"force": force or None}, action)
		if source is None or not file_path:
			raise ToolInputError('action="insert" needs `source` and `file_path`')
		where = position or ("after" if name else "end")
		if where == "end" and name:
			raise ToolInputError('position="end" appends to the file; drop `name`, or use "after"/"before"/"into"')
		if where != "end" and not name:
			raise ToolInputError(f'position="{where}" needs `name` (the symbol to place it relative to)')
		anchors: dict[str, Any] = {"after": None, "before": None, "into": None}
		if where != "end":
			anchors[where] = name
		return await symbol_tools._insert_symbol(
			source, file_path, imports=imports, apply=apply, max_new_errors=max_new_errors, ctx=ctx, **anchors
		)
	if action == "delete":
		_reject({"source": source, "position": position, "imports": imports}, action)
		if not name:
			raise ToolInputError('action="delete" needs `name`')
		return await symbol_tools._safe_delete(
			name, file_path, prune_imports, force, apply=apply, max_new_errors=max_new_errors, ctx=ctx
		)
	raise ToolInputError(
		f"unknown action {action!r}: use replace, replace_body, insert (insert_after / insert_before) or delete"
	)


def _reject(unused: dict[str, Any], action: str, what: str | None = None) -> None:
	"""A parameter that the chosen action ignores is a mistake to report, not to swallow."""
	extra = [key for key, value in unused.items() if value is not None]
	if extra and what:
		raise ToolInputError(f"{what} {', '.join(f'`{key}`' for key in extra)}: put them inside the items of `edits`")
	if extra:
		raise ToolInputError(f'`{"`, `".join(extra)}` do not apply to action="{action}"')


async def move(
	to_file: str,
	name: str | None = None,
	file_path: str | None = None,
	keep_reexport: bool = False,
	apply: bool = True,
	max_new_errors: int | None = 0,
	allow_large: bool = False,
	allow_import_cycle: bool = False,
	ctx: Context | None = None,
) -> str:
	"""Move a function, class or module and rewrite imports across files. Example: `move(name="parse", to_file="src/parser.py")`."""
	try:
		if name:
			return await refactor_tools._move_symbol(
				name,
				to_file,
				file_path,
				keep_reexport,
				apply=apply,
				max_new_errors=max_new_errors,
				allow_large=allow_large,
				allow_import_cycle=allow_import_cycle,
				ctx=ctx,
			)
		if not file_path:
			raise ToolInputError("pass `name` (a symbol to move) or `file_path` (the module file to move)")
		if keep_reexport:
			raise ToolInputError("`keep_reexport` only applies when moving a symbol (`name`)")
		if allow_import_cycle:
			raise ToolInputError("`allow_import_cycle` only applies when moving a symbol (`name`)")
		return await refactor_tools._move_module(
			file_path, to_file, apply=apply, max_new_errors=max_new_errors, allow_large=allow_large, ctx=ctx
		)
	except ToolInputError as exc:
		return format_tool_error(exc)


TOOLS: list[tuple[Any, Any]] = [
	(edit_symbol, tool_base.WRITES),
	(move, tool_base.WRITES),
]
