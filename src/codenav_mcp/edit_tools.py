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

from codenav_mcp import multi_edit, refactor_tools, symbol_tools, tool_base, write_tools


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
	"""Like the built-in Edit (str_replace) for .py files, plus a type check. Example: `edit(file_path="src/app.py", old_string="x = 1", new_string="x = 2")`.

	One replacement per call; several edits or files go in one `batch`. Use it for a few lines, an
	import or a module-level statement, or to rewrite a whole file (`new_text`); to replace, add
	or delete a function/method/class by name use `edit_symbol`.

	Give either `old_string` -> `new_string` (`old_string` must match exactly once unless
	`replace_all=true`; the reply gives the matching lines and the replacement count) or the
	complete `new_text` (also creates a new file), never both. Each text is at most 1,000,000
	characters. The text must be valid Python; new lines take the file's line endings (CRLF
	stays CRLF). Writes when the new errors in the file and in the files that import it
	stay within `max_new_errors` (default 0, null = no limit); otherwise it refuses to write and
	lists the errors plus an id for `apply_edit`. `apply=false` previews. `undo_edit` reverts.
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
	"""Replace, add or delete a whole function, method or class by name (not for a few lines: use `edit`). Examples:
	`edit_symbol(action="replace", name="Cart.total", source="def total(self): ...")`,
	`edit_symbol(action="insert", file_path="src/utils.py", name="parse", source="def helper(): ...")`,
	`edit_symbol(action="delete", name="legacy_parse")`.

	- `action="replace"` (the default): `name` (the symbol; dotted `Class.method` ok) and `source` (the complete
	new definition, decorators included: anything not given is dropped and the result says
	so). Re-indented to the old one's place, file style kept. A changed signature shows up
	as errors at call sites; to change a signature and its callers together use
	`change_signature`.
	- `action="replace_body"`: `name` (a function or method) and `source` (only the new body; the signature,
	decorators and comments above the first statement stay).
	- `action="insert"`: `source` (the new definition) and `file_path`. `position` says where, relative
	to `name`: "after" (default when `name` is given) / "before" a symbol defined in the file
	(a method like `Cart.total` places it inside that class), "into" a class (appended to its
	body), or "end" of the file (default without `name`). PEP 8 blank lines, matching
	indentation. Refuses a name that already exists in that scope (use "replace").
	`insert_after` / `insert_before` are `insert` with that `position`.
	- `action="delete"`: `name` only. Deletes only when nothing uses it, otherwise lists the
	users and writes nothing (`force=true` deletes anyway and shows what breaks). Imports
	that are the only remaining users are removed (`prune_imports`). Uses the type checker
	cannot see (strings, `getattr`, docs) are listed and the deletion is previewed. Comments
	directly above a definition are kept.

	`file_path` narrows `name` when several symbols share it. `imports` (replace/insert) is a
	list of import statements the new code needs, added if missing. Writes when the new errors
	stay within `max_new_errors` (default 0, null = no limit); else previews with an id for
	`apply_edit`; `apply=false` always previews. `undo_edit` reverts.

	`edits=[...]` (instead of the arguments above) applies several edits atomically as ONE plan, with one
	type check and one undo id: items are `{"file_path", "old_string", "new_string"}` (optional
	`replace_all`; or `new_text` for a whole file) or `{"name", "action", "source"}` (the item arguments of
	a single call). All items are matched against the files as they are now; items in one file are merged
	(they must not overlap). If any item fails, nothing is written and the reply says which one and why.
	Example: `edit_symbol(edits=[{"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"},
	{"name": "parse", "action": "delete"}])`.
	"""
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
	"""Move a function/class to another file, or a whole module file, and fix every import. Examples:
	`move(name="parse_date", to_file="src/app/dates.py")` (one symbol),
	`move(file_path="src/app/util.py", to_file="src/app/common/helpers.py")` (the whole module).

	With `name`: moves that module-level function or class (`file_path` narrows which one).
	The destination is created if missing. The code travels with the imports it needs
	(relative ones made absolute), imports only the moved code used are dropped from the old
	file, and if the old file still uses it, it imports it from the new place
	(`keep_reexport=true` always leaves that import so old `from old import name` keeps
	working). Every `from old import name` elsewhere is rewritten. A move that would create a
	module-level import cycle is refused unless `allow_import_cycle=true`. Importers the move
	cannot update (too many, unreadable, unparsable) are reported by name and held back unless
	`apply=true` and `allow_large=true`; uses through the old module (`old.name`) and strings
	are listed for you.

	Without `name`: `file_path` is the module to move or rename (required); every
	`from old import x`, `import old [as y]` and `from package import old` is rewritten
	(a relative import stays relative when the module stays in its package, else it becomes
	absolute). Strings and docs naming the old path are listed.

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
	(edit, tool_base.WRITES),
	(edit_symbol, tool_base.WRITES),
	(move, tool_base.WRITES),
]
