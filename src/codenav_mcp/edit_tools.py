"""The three code-changing tools that take an edit, not a plan: `edit`, `edit_symbol`, `move`.

Each is a thin front over the implementations in `write_tools`, `symbol_tools` and
`refactor_tools`, so the agent picks a tool by *what it is changing* (text, a named
definition, a location) instead of by which refactoring recipe applies. They all follow
the write rule shared by every write tool: the edit is type-checked first and written
when it adds no more than `max_new_errors` (default 0) errors; otherwise you get a
preview id for `apply_edit`. `apply=false` always previews.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import Context
from mcp_nav_shared.errors import ToolInputError, format_tool_error

from codenav_mcp import refactor_tools, symbol_tools, tool_base, write_tools


async def edit(
	file_path: str,
	old_string: str | None = None,
	new_string: str | None = None,
	replace_all: bool = False,
	new_text: str | None = None,
	apply: bool = True,
	max_new_errors: int | None = 0,
	include_dependents: bool = True,
	ctx: Context | None = None,
) -> str:
	"""Edit text in a Python file, type-checked: `edit(file_path="src/app.py", old_string="x = 1", new_string="x = 2")`.

	Use this for changes that are not a whole definition (a few lines, an import, a
	module-level statement) or for rewriting a whole file (`new_text`). To replace,
	add or delete a function/method/class by name use `edit_symbol`.

	Give either `old_string` -> `new_string` (`old_string` must match exactly once unless
	`replace_all=true`) or the complete `new_text` (also creates a new file). The text must
	be valid Python. Writes when the new errors in the file and in the files that import it
	stay within `max_new_errors` (default 0, null = no limit); otherwise nothing is written
	and the result lists the errors plus an id for `apply_edit`. `apply=false` always
	previews. `undo_edit` reverts.
	"""
	return await write_tools._edit_text(
		file_path,
		new_text=new_text,
		old_string=old_string,
		new_string=new_string,
		replace_all=replace_all,
		apply=apply,
		max_new_errors=max_new_errors,
		include_dependents=include_dependents,
		ctx=ctx,
	)


_Action = Literal["replace", "insert", "delete"]
_Position = Literal["after", "before", "into", "end"]


async def edit_symbol(
	action: _Action,
	name: str | None = None,
	source: str | None = None,
	file_path: str | None = None,
	position: _Position | None = None,
	imports: list[str] | None = None,
	force: bool = False,
	prune_imports: bool = True,
	apply: bool = True,
	max_new_errors: int | None = 0,
	ctx: Context | None = None,
) -> str:
	"""Replace, add or delete a function, method or class by name. Examples:
	`edit_symbol(action="replace", name="Cart.total", source="def total(self): ...")`,
	`edit_symbol(action="insert", file_path="src/utils.py", name="parse", source="def helper(): ...")`,
	`edit_symbol(action="delete", name="legacy_parse")`.

	- `action="replace"`: `name` (the symbol; dotted `Class.method` ok) and `source` (the complete
	new definition, decorators included: anything not given is dropped and the result says
	so). Re-indented to the old one's place, file style kept. A changed signature shows up
	as errors at call sites; to change a signature and its callers together use
	`change_signature`.
	- `action="insert"`: `source` (the new definition) and `file_path`. `position` says where, relative
	to `name`: "after" (default when `name` is given) / "before" a symbol defined in the file
	(a method like `Cart.total` places it inside that class), "into" a class (appended to its
	body), or "end" of the file (default without `name`). PEP 8 blank lines, matching
	indentation. Refuses a name that already exists in that scope (use "replace").
	- `action="delete"`: `name` only. Deletes only when nothing uses it, otherwise lists the
	users and writes nothing (`force=true` deletes anyway and shows what breaks). Imports
	that are the only remaining users are removed (`prune_imports`). Uses the type checker
	cannot see (strings, `getattr`, docs) are listed and the deletion is previewed.

	`file_path` narrows `name` when several symbols share it. `imports` (replace/insert) is a
	list of import statements the new code needs, added if missing. Writes when the new errors
	stay within `max_new_errors` (default 0, null = no limit); else previews with an id for
	`apply_edit`; `apply=false` always previews. `undo_edit` reverts.
	"""
	try:
		if action == "replace":
			_reject({"position": position, "force": force or None}, action)
			if not name or source is None:
				raise ToolInputError('action="replace" needs `name` and `source`')
			return await symbol_tools._replace_symbol(
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
		raise ToolInputError(f"unknown action {action!r}: use replace, insert or delete")
	except ToolInputError as exc:
		return format_tool_error(exc)


def _reject(unused: dict[str, Any], action: str) -> None:
	"""A parameter that the chosen action ignores is a mistake to report, not to swallow."""
	extra = [key for key, value in unused.items() if value is not None]
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
	ctx: Context | None = None,
) -> str:
	"""Move a function/class to another file, or a whole module file, and fix every import. Examples:
	`move(name="parse_date", to_file="src/app/dates.py")` (one symbol),
	`move(file_path="src/app/util.py", to_file="src/app/common/helpers.py")` (the whole module).

	With `name`: moves that module-level function or class (`file_path` narrows which one).
	The destination is created if missing. The code travels with the imports it needs
	(relative ones made absolute), imports only the moved code used are dropped from the old
	file, and if the old file still uses it, it imports it from the new place
	(`keep_reexport=true` always leaves that import so old `from old import name` keeps
	working). Every `from old import name` elsewhere is rewritten. Import-cycle risks are
	warned about; uses through the old module (`old.name`) and strings are listed for you.

	Without `name`: `file_path` is the module to move or rename (required); every
	`from old import x`, `import old [as y]` and `from package import old` is rewritten,
	relative imports become absolute. Strings and docs naming the old path are listed.

	Writes when the new errors stay within `max_new_errors` (default 0); else previews with
	an id for `apply_edit`; `apply=false` always previews. `undo_edit` reverts.
	"""
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
				ctx=ctx,
			)
		if not file_path:
			raise ToolInputError("pass `name` (a symbol to move) or `file_path` (the module file to move)")
		if keep_reexport:
			raise ToolInputError("`keep_reexport` only applies when moving a symbol (`name`)")
		return await refactor_tools._move_module(
			file_path, to_file, apply=apply, max_new_errors=max_new_errors, allow_large=allow_large, ctx=ctx
		)
	except ToolInputError as exc:
		return format_tool_error(exc)


TOOLS: list[tuple[Any, Any]] = [
	(edit, tool_base.WRITES),
	(edit_symbol, tool_base.WRITES),
	(move, tool_base.WRITES),
]
