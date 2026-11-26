"""Moving code between modules: a top-level symbol, or a whole module file.

Both rewrite imports across the workspace from the syntax tree; the language
server is only used afterwards, to check the result. Anything that is not a
plain `from x import name` / `import x` (a module attribute access, a string
naming the old module) is listed for the caller instead of guessed at.
"""

from __future__ import annotations

import ast
import keyword
import re
from pathlib import Path

from mcp_nav_shared.edits import EditPlan, FileChange, convert_eol, detect_eol, read_source, relative_name
from mcp_nav_shared.errors import ToolInputError
from mcp_nav_shared.transaction import refuse_home_path

from codenav_mcp.all_entries import drop_all_entry as _drop_all_entry
from codenav_mcp.deps import (
	Skipped,
	find_dependents_report,
	format_skipped,
	import_from_targets,
	module_names_for,
	primary_module_name,
)
from codenav_mcp.imports import (
	ImportStmt,
	add_from_import,
	add_import,
	module_imports,
	remove_names,
	render_alias,
	with_future_annotations as _with_future_annotations,
)
from codenav_mcp.mentions import find_mentions, format_mentions
from codenav_mcp.pysource import (
	check_syntax,
	leading_comment_start,
	leading_whitespace,
	loaded_names,
	names_used_outside,
	parse_python,
	source_lines,
	top_level_names,
)
from codenav_mcp.signature import SourceMap, apply_offset_edits
from codenav_mcp.symbol_tools import _delete_span, _locate
from codenav_mcp.tidy import sort_failure, sort_imports
from codenav_mcp.tool_base import Session


# importers the move cannot check or update; the move is held back while any of them exists
def _resolve_dest(session: Session, to_file: str) -> Path:
	refuse_home_path(to_file)
	candidate = Path(to_file)
	dest = (candidate if candidate.is_absolute() else session.workspace / candidate).resolve()
	if dest.suffix.lower() != ".py":
		raise ToolInputError(f"the destination must be a .py file, got {to_file!r}")
	try:
		dest.relative_to(session.workspace.resolve())
	except ValueError:
		raise ToolInputError(f"{to_file} is outside the workspace") from None
	return dest


def _module_or_fail(path: Path, roots: list[Path]) -> str:
	name = primary_module_name(path, roots)
	if name is None:
		raise ToolInputError(
			f"cannot derive an import path for {path}; it is outside the source roots "
			"(set CODENAV_MCP_SOURCE_ROOT if the importable code lives elsewhere)"
		)
	return name


def _check_importable(module: str, to_file: str) -> None:
	if not all(part.isidentifier() and not keyword.iskeyword(part) for part in module.split(".")):
		raise ToolInputError(
			f"{to_file} cannot be imported as {module!r}: every part of its name must be a valid Python identifier"
		)


def _held_back(skipped: Skipped, workspace: Path) -> bool:
	"""Whether a skipped entry stops the move: an importer file that cannot be parsed or read, or the scan limit.
	A directory the scan does not enter (`.git`, `.venv`, `build`, ...) is only reported: its importers are listed
	under "Importers not updated" and the move goes on."""
	if skipped.reason in ("unparsable", "limit"):
		return True
	return skipped.reason == "unreadable" and not (workspace / skipped.path_or_dir.rstrip("/")).is_dir()


def _check_held_back(skipped: list[Skipped], workspace: Path, *, allowed: bool, apply: bool, notes: list[str]) -> None:
	"""Refuse an apply that would leave unreadable importers broken (unless `allowed`); a preview only says so.
	A preview must render, so it never raises: it notes that the move would be held back."""
	held = [s for s in skipped if _held_back(s, workspace)]
	if not held:
		return
	details = "; ".join(s.detail for s in held)
	how = "rerun with apply=true" + ("" if allowed else " and allow_large=true")
	if not apply:
		notes.append(
			f"Held back: this move would not be applied while these importers cannot be checked: {details}. "
			f"Fix or exclude them, or {how} to move anyway."
		)
	elif not allowed:
		raise ToolInputError(
			f"NOT applied: importers that cannot be checked would be left broken ({details}). "
			"Fix or exclude them, or rerun with apply=true and allow_large=true to move anyway."
		)
	else:
		notes.append(f"Moved although these importers cannot be checked (allow_large=true): {details}.")


def _import_requirements(
	tree: ast.Module, needed: set[str], src_module: str, src_names: set[str], is_init: bool
) -> tuple[list[tuple[str, str, str | None]], list[str]]:
	"""How the moved code gets each name it needs, as absolute imports for the destination:
	[("from", module, name, asname) | ("import", module, asname)] and the names defined in the source module itself."""
	requirements: list[tuple[str, str, str | None]] = []
	from_source: list[str] = []
	bound_by_import: dict[str, tuple[str, str, str | None]] = {}
	for stmt in module_imports(tree):
		node = stmt.node
		for (name, asname), bound in zip(stmt.aliases, stmt.bound_names, strict=True):
			if isinstance(node, ast.ImportFrom):
				targets = sorted(import_from_targets(node, src_names, is_package_init=is_init))
				if not targets:
					continue
				bound_by_import[bound] = ("from", targets[0], name if asname is None else f"{name} as {asname}")
			else:
				bound_by_import[bound] = ("import", name, asname)
	for name in sorted(needed):
		if name in bound_by_import:
			requirements.append(bound_by_import[name])
		else:
			from_source.append(name)
	return requirements, from_source


def _annotations(node: ast.AST) -> list[ast.expr]:
	found: list[ast.expr] = []
	for child in ast.walk(node):
		if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
			if child.returns is not None:
				found.append(child.returns)
			args = child.args
			for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
				if arg is not None and arg.annotation is not None:
					found.append(arg.annotation)
		elif isinstance(child, ast.AnnAssign):
			found.append(child.annotation)
	return found


def _string_annotation_names(node: ast.AST) -> set[str]:
	"""Names written inside string (forward-reference) annotations, e.g. `-> "list[Item]"`."""
	names: set[str] = set()
	for annotation in _annotations(node):
		for sub in ast.walk(annotation):
			if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
				if sub.value.isidentifier():
					names.add(sub.value)
					continue
				try:
					inner = ast.parse(sub.value, mode="eval")
				except SyntaxError:
					continue
				names |= {n.id for n in ast.walk(inner) if isinstance(n, ast.Name)}
	return names


def _assigned_globals(node: ast.AST, module_names: set[str]) -> list[str]:
	"""Module-level names that the code declares `global` and then assigns."""
	declared = {name for child in ast.walk(node) if isinstance(child, ast.Global) for name in child.names}
	stored = {
		child.id for child in ast.walk(node) if isinstance(child, ast.Name) and not isinstance(child.ctx, ast.Load)
	}
	return sorted(declared & stored & module_names)


def _has_future_annotations(tree: ast.Module) -> bool:
	return any(
		isinstance(stmt, ast.ImportFrom)
		and stmt.module == "__future__"
		and any(alias.name == "annotations" for alias in stmt.names)
		for stmt in tree.body
	)


def _all_literal(tree: ast.Module) -> ast.List | ast.Tuple | None:
	"""The literal value of a module-level `__all__ = [...]` (or tuple), if there is one."""
	found: ast.List | ast.Tuple | None = None
	for stmt in tree.body:
		if (
			isinstance(stmt, ast.Assign)
			and any(isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets)
			and isinstance(stmt.value, (ast.List, ast.Tuple))
		):
			found = stmt.value
	return found


def _all_names(tree: ast.Module) -> set[str]:
	literal = _all_literal(tree)
	if literal is None:
		return set()
	return {e.value for e in literal.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}


def _add_all_entry(text: str, name: str) -> str:
	literal = _all_literal(parse_python(text))
	if literal is None or (isinstance(literal, ast.Tuple) and not literal.elts):
		return text
	smap = SourceMap(text)
	if literal.elts:
		return apply_offset_edits(text, [(smap.end(literal.elts[-1]), smap.end(literal.elts[-1]), f', "{name}"')])
	return apply_offset_edits(text, [(smap.start(literal) + 1, smap.start(literal) + 1, f'"{name}"')])


async def plan_move_symbol(
	session: Session,
	name: str,
	to_file: str,
	file_path: str | None,
	keep_reexport: bool,
	*,
	allow_import_cycle: bool = False,
	allow_held_back: bool = False,
	apply: bool = False,
) -> EditPlan:
	src, src_text, definition = await _locate(session, name, file_path)
	if definition.parent is not None:
		if isinstance(definition.parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
			raise ToolInputError("nested functions cannot be moved; move the enclosing top-level function or class")
		raise ToolInputError(
			f"{definition.qualname} is nested inside {definition.parent.name}; only module-level symbols can be moved. "
			"For a method, change the code with edit_symbol instead; move cannot take a method with it."
		)
	dest = _resolve_dest(session, to_file)
	if dest == src:
		raise ToolInputError("the destination is the file the symbol is already in")
	workspace = session.workspace
	src_module = _module_or_fail(src, session.roots)
	dest_module = _module_or_fail(dest, session.roots)
	_check_importable(dest_module, to_file)
	dest_text, dest_bom = read_source(dest) if dest.exists() else ("", False)
	src_bom = read_source(src)[1]
	src_tree = parse_python(src_text, relative_name(src, workspace))
	dest_tree = parse_python(dest_text, relative_name(dest, workspace)) if dest_text else ast.parse("")
	top_defs = {
		node.lineno for node in src_tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
	}
	if definition.parent is None and definition.def_line not in top_defs:
		raise ToolInputError(
			f"{definition.qualname} is inside an if/try/with block; only top-level functions and classes can be moved"
		)
	if definition.name in top_level_names(dest_tree):
		raise ToolInputError(f"{relative_name(dest, workspace)} already defines {definition.name!r}")
	source_names = top_level_names(src_tree) - {definition.name}
	mutated = _assigned_globals(definition.node, source_names)
	if mutated:
		raise ToolInputError(
			f"{definition.qualname} assigns the module global {mutated[0]} of {src_module}; moving it would raise "
			f"NameError. Move {mutated[0]} too or pass it as a parameter."
		)
	lines = source_lines(src_text)
	first = leading_comment_start(lines, definition.first_line)
	moved_source = "".join(lines[first - 1 : definition.last_line])
	if not moved_source.endswith("\n"):
		moved_source += "\n"
	src_names = module_names_for(src, session.roots)
	needed = (loaded_names(definition.node) | _string_annotation_names(definition.node)) & source_names
	requirements, from_source = _import_requirements(src_tree, needed, src_module, src_names, src.name == "__init__.py")
	dest_bound = top_level_names(dest_tree)
	notes: list[str] = []
	if not dest.exists():
		notes.append(f"{relative_name(dest, workspace)} was created by this move")

	# destination: the code, then the imports it needs
	body = moved_source if not dest_text.strip() else dest_text.rstrip("\r\n") + "\n\n\n" + moved_source
	for requirement in requirements:
		if requirement[0] == "from":
			imported = requirement[2] or ""
			name_part, _, alias = imported.partition(" as ")
			if (alias or name_part) in dest_bound:
				continue
			body = add_from_import(body, requirement[1], [(name_part, alias or None)])
		else:
			bound = (requirement[2] or requirement[1]).split(".")[0]
			if bound not in dest_bound:
				body = add_import(body, requirement[1], requirement[2])
	back = [n for n in from_source if n not in dest_bound]
	if back:
		body = add_from_import(body, src_module, [*back])
		notes.append(
			f"{relative_name(dest, workspace)} now imports {', '.join(back)} from {src_module}: if {src_module} "
			f"imports {dest_module} back this is a circular import (move those helpers too, or import lazily)"
		)

	# source: the code removed, imports it alone needed dropped, a way back if it still uses the symbol
	used_outside = names_used_outside(src_tree, first, definition.last_line)
	# an `__all__` entry alone is not a use: with keep_reexport=false it leaves with the code (below)
	all_literal = _all_literal(src_tree)
	all_lines = frozenset(
		line
		for element in (all_literal.elts if all_literal else [])
		if isinstance(element, ast.Constant) and element.value == definition.name
		for line in range(element.lineno, (element.end_lineno or element.lineno) + 1)
	)
	used_in_code = names_used_outside(src_tree, first, definition.last_line, ignore_lines=all_lines)
	reexport = definition.name in used_in_code or keep_reexport
	# The re-export joins the import block. Only when the destination imports names back from this module does it
	# take the moved definition's place instead: a module-level import earlier in the file would run before the
	# names the destination needs back (a constant, say) are defined.
	dest_imports_src = bool(from_source) or _imports_other_names_from(
		dest_tree, module_names_for(dest, session.roots), src_names, definition.name, dest.name == "__init__.py"
	)
	in_place = reexport and dest_imports_src
	reexport_line = f"from {dest_module} import {definition.name}" if in_place else ""
	new_src = _delete_span(src_text, definition, comments=True, replacement=reexport_line)
	module_stmts = module_imports(src_tree)
	candidates = {n for n in needed if n not in used_outside} & {b for s in module_stmts for b in s.bound_names}
	kept = {b for s in module_stmts if _is_side_effect_import(s, lines) for b in s.bound_names}
	only_for_moved = candidates - kept
	if only_for_moved:
		new_src = remove_names(new_src, only_for_moved)
		notes.append(
			f"dropped from {relative_name(src, workspace)}: imports only the moved code used "
			f"({', '.join(sorted(only_for_moved))})"
		)
	if candidates & kept:
		notes.append(
			f"kept in {relative_name(src, workspace)}: side-effect import ({', '.join(sorted(candidates & kept))}); "
			"execution order unchanged"
		)
	if reexport:
		reason = "still uses it" if definition.name in used_in_code else "keep_reexport"
		notes.append(f"{relative_name(src, workspace)} imports {definition.name} from {dest_module} ({reason})")
	if reexport and not in_place:
		new_src = add_from_import(new_src, dest_module, [definition.name])
	# the old module still binds the name when it re-exports it, so its `__all__` keeps the entry (a star import of
	# the old module keeps working); otherwise the entry leaves with the code
	moves_all_entry = definition.name in _all_names(src_tree)
	if moves_all_entry and not reexport:
		new_src = _drop_all_entry(new_src, definition.name)

	# importers elsewhere (the destination itself is one of them when it imported the symbol from the source)
	report = find_dependents_report(workspace, [src], session.roots)
	_check_held_back(report.skipped, session.workspace, allowed=allow_held_back, apply=apply, notes=notes)
	notes += format_skipped(report.skipped)
	star_importers: list[str] = []
	changed: dict[Path, tuple[str, str, bool]] = {}
	for importer in report.files:
		if importer == dest:
			text_before, bom = body, dest_bom
		else:
			text_before, bom = read_source(importer)
		own = module_names_for(importer, session.roots)
		is_init = importer.name == "__init__.py"
		tree = parse_python(text_before, relative_name(importer, workspace))
		new_text = text_before
		pending: list[tuple[str, list[str | tuple[str, str | None]]]] = []
		for stmt in sorted(module_imports(tree, include_nested=True), key=lambda s: -s.first_line):
			node = stmt.node
			if (
				not isinstance(node, ast.ImportFrom)
				or not import_from_targets(node, own, is_package_init=is_init) & src_names
			):
				continue
			if any(a.name == "*" for a in node.names):
				if importer != dest:
					star_importers.append(relative_name(importer, workspace))
				continue
			alias = next((a for a in node.names if a.name == definition.name), None)
			if alias is None:
				continue
			bound = alias.asname or alias.name
			if stmt.scope == "module" and not stmt.conditional:
				new_text = remove_names(new_text, {bound}, only_line=stmt.first_line)
				if importer != dest:
					pending.append((dest_module, [(definition.name, alias.asname)]))
			else:
				# inside a function or an if/try block: rewrite the statement in place, so no block is left empty
				new_text = _retarget_statement(
					new_text, stmt, definition.name, alias.asname, None if importer == dest else dest_module
				)
		for module, names in pending:
			new_text = add_from_import(new_text, module, names)
		if importer == dest:
			body = new_text
		elif new_text != text_before:
			changed[importer] = (text_before, new_text, bom)
	if star_importers:
		notes.append(
			f'Importers using "from {src_module} import *" (not rewritten, names resolve through the module): '
			+ ", ".join(star_importers)
		)
	if moves_all_entry:
		body = _add_all_entry(body, definition.name)
	if _has_future_annotations(src_tree) and _annotations(definition.node):
		body = _with_future_annotations(body)

	# import cycles the move would create at module level (refused unless allowed), or only through functions
	dest_own = module_names_for(dest, session.roots)
	dest_module_level, dest_function_level = _runtime_targets(body, dest_own, dest)
	src_module_level, src_function_level = _runtime_targets(new_src, src_names, src)
	relative_dest = relative_name(dest, workspace)
	if src_module in dest_module_level and dest_module in src_module_level:
		if not allow_import_cycle:
			raise ToolInputError(
				f"moving {definition.qualname} to {relative_dest} creates an import cycle "
				f"({dest_module} -> {src_module} -> {dest_module}); pass allow_import_cycle=true to move anyway"
			)
	else:
		texts = {src: new_src, dest: body, **{path: after for path, (_, after, _) in changed.items()}}
		# a module-level cycle of any length (2 or more modules) is refused, like the direct one above
		module_cycle = _cycle_path(dest_module, dest_own, session.roots, texts, with_function_level=False)
		if module_cycle:
			if not allow_import_cycle:
				raise ToolInputError(
					f"moving {definition.qualname} to {relative_dest} creates an import cycle "
					f"({' -> '.join(module_cycle)}); pass allow_import_cycle=true to move anyway"
				)
		elif (src_module in dest_module_level | dest_function_level) and (
			dest_module in src_module_level | src_function_level
		):
			notes.append(
				f"Note: the import cycle {dest_module} -> {src_module} -> {dest_module} goes only through "
				"function-level imports; it can fail only if those run at import time"
			)
		else:
			longer = _cycle_path(dest_module, dest_own, session.roots, texts, with_function_level=True)
			if longer:
				notes.append(
					f"Note: the import cycle {' -> '.join(longer)} goes only through function-level imports; "
					"it can fail only if those run at import time"
				)

	# the text is written with the line endings of each file it lands in (inserted lines too)
	if new_src.strip() == "":
		notes.append(
			f"{relative_name(src, workspace)} is empty after the move; delete it with trash if you no longer need it"
		)
	if new_src.startswith(("\n", "\r")) and not src_text.startswith(("\n", "\r")):
		new_src = new_src.lstrip("\r\n")  # blank lines the removed code left at the top
	sorted_changes: list[FileChange] = []
	src_eol = detect_eol(src_text)
	for path, original, edited, bom in [
		(src, src_text, new_src, src_bom),
		(dest, dest_text, body, dest_bom),
		*[(p, a, b, c) for p, (a, b, c) in changed.items()],
	]:
		rel = relative_name(path, workspace)
		edited = convert_eol(edited, detect_eol(original) if original else src_eol)
		edited = sort_imports(workspace, original, edited, rel)
		if reason := sort_failure():
			notes.append(f"{rel}: {reason}")
		check_syntax(edited, rel)
		# Preserve source permissions so a move cannot expose previously private code.
		created_mode = src.stat().st_mode if path == dest and not path.exists() else None
		sorted_changes.append(
			FileChange(path, original if path.exists() else None, edited, bom, bom, new_mode=created_mode)
		)
	unseen = [
		m for m in find_mentions(workspace, f"{src_module}.{definition.name}", {}) if m.kind in ("string", "text")
	]
	notes += format_mentions(unseen, f"{src_module}.{definition.name}")
	attribute_users = _module_attribute_users(workspace, src, session.roots, src_names, definition.name)
	if attribute_users:
		notes.append(
			f"accessed through the old module (`{src_module.rsplit('.', 1)[-1]}.{definition.name}`), not rewritten: "
			+ ", ".join(attribute_users[:6])
		)
	title = f"Move {definition.qualname} from {relative_name(src, workspace)} to {relative_dest}"
	return EditPlan(title, sorted_changes, notes)


def _imports_other_names_from(
	tree: ast.Module, own_names: set[str], source_names: set[str], moved: str, is_init: bool
) -> bool:
	"""Whether the module imports (at module level, unconditionally) something other than `moved` from the source module."""
	for stmt in module_imports(tree):
		node = stmt.node
		if stmt.conditional or not isinstance(node, ast.ImportFrom):
			continue
		if import_from_targets(node, own_names, is_package_init=is_init) & source_names and any(
			alias.name != moved for alias in node.names
		):
			return True
	return False


def _is_side_effect_import(stmt: ImportStmt, lines: list[str]) -> bool:
	"""An import kept in the source when only moved code used it: one with an inline comment (`# registers`)
	or a bare dotted import (`import pkg.plugins`), which runs for its effect."""
	if stmt.conditional:
		return False
	commented = "#" in lines[stmt.last_line - 1]
	bare_dotted = isinstance(stmt.node, ast.Import) and any("." in name for name, _ in stmt.aliases)
	return commented or bare_dotted


def _retarget_statement(text: str, stmt: ImportStmt, name: str, asname: str | None, new_module: str | None) -> str:
	"""Rewrite one `from x import ...` statement in place: `name` moves to `new_module` (or is dropped when None),
	the other names stay. Keeps the statement's block, so an `if`/`try` body is never left empty."""
	node = stmt.node
	if not isinstance(node, ast.ImportFrom):
		return text
	lines = source_lines(text)
	indent = leading_whitespace(lines[stmt.first_line - 1])
	kept = [a for a in node.names if a.name != name]
	new_lines: list[str] = []
	if kept:
		module_text = "." * node.level + (node.module or "")
		new_lines.append(f"{indent}from {module_text} import {', '.join(render_alias(a.name, a.asname) for a in kept)}")
	if new_module:
		new_lines.append(f"{indent}from {new_module} import {render_alias(name, asname)}")
	if not new_lines:
		new_lines.append(f"{indent}pass")
	replacement = "".join(line + "\n" for line in new_lines)
	return "".join(lines[: stmt.first_line - 1]) + replacement + "".join(lines[stmt.last_line :])


def _runtime_targets(text: str, own: set[str], path: Path) -> tuple[set[str], set[str]]:
	"""Modules a file imports at import time (module level, not under `if TYPE_CHECKING:`/`try:`) and inside functions."""
	tree = parse_python(text, str(path))
	module_level: set[str] = set()
	function_level: set[str] = set()
	for stmt in module_imports(tree, include_nested=True):
		if stmt.scope == "module" and stmt.conditional:
			continue
		bucket = module_level if stmt.scope == "module" else function_level
		node = stmt.node
		if isinstance(node, ast.ImportFrom):
			targets = import_from_targets(node, own, is_package_init=path.name == "__init__.py")
			bucket |= targets
			bucket |= {f"{t}.{a.name}" for t in targets for a in node.names if a.name != "*"}
		else:
			bucket |= {name for name, _ in stmt.aliases}
	return module_level, function_level


def _module_file(module: str, roots: list[Path]) -> Path | None:
	relative = Path(*module.split("."))
	for root in roots:
		for candidate in (root / f"{relative}.py", root / relative / "__init__.py"):
			if candidate.is_file():
				return candidate.resolve()
	return None


_CYCLE_SEARCH_LIMIT = 400


def _cycle_path(
	dest_module: str,
	dest_own: set[str],
	roots: list[Path],
	texts: dict[Path, str],
	*,
	with_function_level: bool,
) -> list[str]:
	"""A cycle back to `dest_module` in its import closure, as the modules on the way (empty when there is none).

	`with_function_level=False` follows module-level (top-of-file) imports only: the caller refuses such a cycle,
	of any length. `True` also follows function-level imports: the caller only notes those. The search is bounded
	by `_CYCLE_SEARCH_LIMIT` modules; hitting the bound without a cycle returns empty (a note at most, never a refusal).
	`texts` holds the post-move text of the files the move writes; other files are read."""
	targets = {dest_module, *dest_own}
	written = {name: (path, text) for path, text in texts.items() for name in module_names_for(path, roots)}
	edges: dict[str, tuple[set[str], set[str]]] = {}

	def outgoing(module: str) -> tuple[set[str], set[str]]:
		if module not in edges:
			edges[module] = (set(), set())
			path = _module_file(module, roots)
			text: str | None = None
			if module in written:
				path, text = written[module]
			if path is not None:
				try:
					text = text if text is not None else read_source(path)[0]
					edges[module] = _runtime_targets(text, module_names_for(path, roots), path)
				except (OSError, SyntaxError, UnicodeDecodeError, ToolInputError):
					pass
		return edges[module]

	def search(*, with_function_level: bool) -> list[str]:
		parents: dict[str, str | None] = {dest_module: None}
		queue = [dest_module]
		while queue and len(parents) < _CYCLE_SEARCH_LIMIT:
			current = queue.pop(0)
			module_level, function_level = outgoing(current)
			for target in sorted(module_level | function_level if with_function_level else module_level):
				if target in targets and current != dest_module:
					chain = [dest_module]
					walk: str | None = current
					while walk is not None and walk != dest_module:
						chain.insert(1, walk)
						walk = parents[walk]
					return [*chain, dest_module]
				if target not in parents and (target in written or _module_file(target, roots) is not None):
					parents[target] = current
					queue.append(target)
		return []

	return search(with_function_level=with_function_level)


def _module_attribute_users(workspace: Path, src: Path, roots: list[Path], src_names: set[str], name: str) -> list[str]:
	"""Files using the symbol as `module.name` where `module` is the old module."""
	found = []
	leaves = {n.rsplit(".", 1)[-1] for n in src_names}
	for dependent in find_dependents_report(workspace, [src], roots).files:
		try:
			tree = ast.parse(read_source(dependent)[0])
		except (OSError, SyntaxError, UnicodeDecodeError):
			continue
		for node in ast.walk(tree):
			if (
				isinstance(node, ast.Attribute)
				and node.attr == name
				and ast.unparse(node.value).split(".")[-1] in leaves
			):
				found.append(f"{relative_name(dependent, workspace)}:{node.lineno}")
	return found


# -- moving a module ---------------------------------------------------------------


def _relative_module(importer_module: str, level: int, target: str, is_init: bool) -> str | None:
	"""`target` written relative to the importer, or None when absolute reads better."""
	parts = importer_module.split(".") if is_init else importer_module.split(".")[:-1]
	base = parts[: len(parts) - (level - 1)] if level - 1 <= len(parts) else []
	prefix = ".".join(base)
	if prefix and (target == prefix or target.startswith(prefix + ".")):
		return "." * level + target[len(prefix) + 1 :]
	return None


def plan_move_module(
	session: Session, from_file: str, to_file: str, *, allow_held_back: bool = False, apply: bool = False
) -> EditPlan:
	workspace = session.workspace
	src_candidate = Path(from_file)
	src = (src_candidate if src_candidate.is_absolute() else workspace / src_candidate).resolve()
	if not src.is_file() or src.suffix.lower() != ".py":
		raise ToolInputError(f"{from_file} is not an existing .py file")
	if src.name == "__init__.py":
		raise ToolInputError("moving a package's __init__.py is not supported; move the modules inside it")
	dest = _resolve_dest(session, to_file)
	if dest.exists():
		raise ToolInputError(f"{relative_name(dest, workspace)} already exists")
	old_primary = _module_or_fail(src, session.roots)
	new_primary = _module_or_fail(dest, session.roots)
	_check_importable(new_primary, to_file)
	mapping = {}
	for root in session.roots:
		before, after = module_names_for(src, [root]), module_names_for(dest, [root])
		if before and after:
			mapping[next(iter(before))] = next(iter(after))
	if not mapping:
		raise ToolInputError("source and destination are not under a common import root")
	changes: list[FileChange] = []
	notes: list[str] = []

	src_text, src_bom = read_source(src)
	moved_text = _absolutize_relative_imports(src_text, src, session)
	moved_text = _drop_self_imports(
		_rewrite_importer(moved_text, dest, mapping, session, notes), new_primary, dest, session.roots
	)
	check_syntax(moved_text, relative_name(src, workspace))

	report = find_dependents_report(workspace, [src], session.roots)
	_check_held_back(report.skipped, session.workspace, allowed=allow_held_back, apply=apply, notes=notes)
	notes += format_skipped(report.skipped)
	for importer in report.files:
		text, bom = read_source(importer)
		new_text = _rewrite_importer(text, importer, mapping, session, notes)
		if new_text != text:
			changes.append(FileChange(importer, text, convert_eol(new_text, detect_eol(text)), bom, bom))
	changes.append(FileChange(src, src_text, None, src_bom, False))
	# The new file takes the source's permission bits (a +x script stays executable), and undo restores them.
	changes.append(FileChange(dest, None, moved_text, src_bom, src_bom, new_mode=src.stat().st_mode))
	for change in changes:
		if change.new_text is not None:
			check_syntax(change.new_text, relative_name(change.path, workspace))
	if (
		dest.parent != src.parent
		and not (dest.parent / "__init__.py").exists()
		and (src.parent / "__init__.py").exists()
	):
		notes.append(
			f"{relative_name(dest.parent, workspace)} has no __init__.py (the old package does); add one if it must be a regular package"
		)
	unseen = [m for m in find_mentions(workspace, old_primary, {}) if m.kind in ("string", "text", "comment")]
	notes += format_mentions(unseen, old_primary)
	notes = list(dict.fromkeys(notes))
	return EditPlan(f"Move module {old_primary} -> {new_primary}", changes, notes)


def _drop_self_imports(text: str, module: str, path: Path, roots: list[Path]) -> str:
	"""A moved module that imported itself (its old name) must not import itself under the new one, unless it uses it."""
	tree = parse_python(text, str(path))
	own = module_names_for(path, roots)
	used = names_used_outside(tree, 0, 0)
	for stmt in sorted(module_imports(tree), key=lambda s: -s.first_line):
		node = stmt.node
		if not isinstance(node, ast.ImportFrom):
			continue
		targets = import_from_targets(node, own, is_package_init=path.name == "__init__.py")
		if module in targets:
			bounds = set(stmt.bound_names)
		else:
			bounds = {
				alias.asname or alias.name
				for alias in node.names
				if any(f"{t}.{alias.name}" == module for t in targets)
			}
		# A self-import whose name the body still reads (`return mod`) stays: it points at the new name already,
		# and dropping it would leave that name undefined.
		bounds -= used
		if bounds:
			text = remove_names(text, bounds, only_line=stmt.first_line)
	return text


def _absolutize_relative_imports(text: str, path: Path, session: Session) -> str:
	"""A moved file's relative imports would point somewhere else at the new location: spell them absolutely."""
	tree = parse_python(text, str(path))
	own = module_names_for(path, session.roots)
	smap = SourceMap(text)
	edits: list[tuple[int, int, str]] = []
	for node in ast.walk(tree):
		if isinstance(node, ast.ImportFrom) and node.level > 0:
			targets = sorted(import_from_targets(node, own, is_package_init=path.name == "__init__.py"))
			if not targets:
				continue
			segment = smap.segment(node)
			head = re.match(r"(\s*from\s+)(\.+)([\w.]*)(\s+import\b)", segment)
			if head:
				edits.append((smap.start(node) + len(head.group(1)), smap.start(node) + head.end(3), targets[0]))
	return apply_offset_edits(text, edits)


def _parent_binds(parent: str, name: str, roots: list[Path]) -> bool:
	"""Only accept a provable literal binding; ambiguous package bindings need manual review."""
	relative = Path(*parent.split("."))
	for root in roots:
		for candidate in (root / relative / "__init__.py", root / f"{relative}.py"):
			if candidate.is_file():
				text, _ = read_source(candidate)
				tree = parse_python(text, candidate.name)
				# Only a simple module body can establish the final binding without executing arbitrary code.
				# Imports and definitions with decorators can mutate the package while they execute.
				bound = False
				for stmt in tree.body:
					if isinstance(stmt, ast.Pass):
						continue
					if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
						continue
					if (
						isinstance(stmt, ast.Assign)
						and isinstance(stmt.value, ast.Constant)
						and all(isinstance(target, ast.Name) for target in stmt.targets)
					):
						bound = bound or any(
							isinstance(target, ast.Name) and target.id == name for target in stmt.targets
						)
						continue
					raise ToolInputError(
						f"Cannot safely determine whether {parent} binds {name!r}; review its imports and move this module manually."
					)
				if bound:
					raise ToolInputError(
						f"Cannot safely determine whether {parent} binds {name!r}; importing its submodule may change the binding. Review the imports and move this module manually."
					)
				return False
	return False


def _rewrite_importer(text: str, importer: Path, mapping: dict[str, str], session: Session, notes: list[str]) -> str:
	tree = parse_python(text, str(importer))
	smap = SourceMap(text)
	own = module_names_for(importer, session.roots)
	is_init = importer.name == "__init__.py"
	edits: list[tuple[int, int, str]] = []
	rename_names: dict[str, str] = {}
	chain_renames: dict[str, str] = {}
	repackage: list[tuple[int, str, str | None, str, str]] = []
	for old, new in mapping.items():
		parent_old, _, leaf_old = old.rpartition(".")
		parent_new, _, leaf_new = new.rpartition(".")
		for node in ast.walk(tree):
			if isinstance(node, ast.ImportFrom):
				segment = smap.segment(node)
				targets = import_from_targets(node, own, is_package_init=is_init)
				if old in targets and node.level == 0 or (node.level > 0 and old in targets):
					head = re.match(r"(\s*from\s+)([.\w]+)(\s+import\b)", segment)
					if head:
						replacement = new
						if node.level > 0:
							relative = next(
								(
									rel
									for rel in (_relative_module(m, node.level, new, is_init) for m in sorted(own))
									if rel is not None
								),
								None,
							)
							if relative is not None:
								replacement = relative  # keeps the importer's relative form (same package)
							else:
								notes.append(
									f"{relative_name(importer, session.workspace)}: relative import rewritten as absolute"
								)
						edits.append(
							(smap.start(node) + len(head.group(1)), smap.start(node) + head.end(2), replacement)
						)
				elif parent_old in targets and any(a.name == leaf_old for a in node.names):
					_parent_binds(parent_old, leaf_old, session.roots)
					if parent_old == parent_new and (len(node.names) >= 1):
						for alias in node.names:
							if alias.name == leaf_old:
								pattern = re.compile(rf"\b{re.escape(leaf_old)}\b")
								match = pattern.search(segment[segment.index("import") :])
								if match:
									start = smap.start(node) + segment.index("import") + match.start()
									edits.append((start, start + len(leaf_old), leaf_new))
								if alias.asname is None:
									rename_names[leaf_old] = leaf_new
					else:
						for alias in node.names:
							if alias.name == leaf_old:
								repackage.append(
									(node.lineno, alias.asname or leaf_old, alias.asname, parent_new, leaf_new)
								)
								if alias.asname is None and leaf_old != leaf_new:
									rename_names[leaf_old] = leaf_new
			elif isinstance(node, ast.Import):
				for alias in node.names:
					if alias.name == old:
						segment = smap.segment(node)
						match = re.search(rf"\b{re.escape(old)}\b", segment)
						if match:
							start = smap.start(node) + match.start()
							edits.append((start, start + len(old), new))
						if alias.asname is None:
							chain_renames[old] = new
							if "." not in old:
								# `import a` binds `a`: its uses (`a.helper`) follow the new name
								rename_names[old] = new
	if rename_names or chain_renames:
		for node in ast.walk(tree):
			if isinstance(node, ast.Name) and node.id in rename_names and isinstance(node.ctx, ast.Load):
				edits.append((smap.start(node), smap.end(node), rename_names[node.id]))
			elif isinstance(node, ast.Attribute) and ast.unparse(node) in chain_renames:
				edits.append((smap.start(node), smap.end(node), chain_renames[ast.unparse(node)]))
	deduped = list(dict.fromkeys(edits))
	result = apply_offset_edits(text, _drop_nested(deduped))
	for lineno, bound, asname, parent_new, leaf_new in sorted(repackage, reverse=True):
		result = remove_names(result, {bound}, only_line=lineno)
		result = (
			add_from_import(result, parent_new, [(leaf_new, asname)])
			if parent_new
			else add_import(result, leaf_new, asname)
		)
	return result


def _drop_nested(edits: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
	"""When an outer replacement contains an inner one (a dotted chain inside a longer chain), keep the outer."""
	kept = []
	for edit in sorted(edits, key=lambda e: (e[0], -(e[1] - e[0]))):
		if kept and edit[0] < kept[-1][1]:
			continue
		kept.append(edit)
	return kept
