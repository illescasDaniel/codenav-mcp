"""MCP tools for structural refactorings: change a signature, move a symbol, move a module."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context
from mcp_nav_shared.edits import EditPlan, FileChange, read_source, relative_name
from mcp_nav_shared.errors import TOOL_ERRORS, ToolInputError, format_tool_error
from mcp_nav_shared.format import uri_to_path

from codenav_mcp import tool_base
from codenav_mcp.deps import import_from_targets, iter_python_files, module_names_for
from codenav_mcp.linked import find_linked
from codenav_mcp.moves import plan_move_module, plan_move_symbol
from codenav_mcp.pysource import (
	Definition,
	NulByteError,
	PythonSyntaxError,
	check_syntax,
	definition_name_position,
	definitions_in,
	detect_indent_unit,
	find_definition,
	overload_stubs,
	parse_python,
)
from codenav_mcp.signature import (
	Change,
	CommentedCallError,
	ManualCallError,
	SourceMap,
	apply_offset_edits,
	call_edit,
	find_call_at,
	new_params,
	read_call_args,
	read_signature,
	rebind_call,
	render_params,
	tokenize_source,
)
from codenav_mcp.symbol_tools import _locate
from codenav_mcp.tool_base import Session, open_session
from codenav_mcp.write_tools import conforms_probe
from codenav_mcp.writes import finish_plan


@dataclass
class FileCtx:
	path: Path
	text: str
	smap: SourceMap
	tokens: list
	tree: ast.Module
	edits: list[tuple[int, int, str]] = field(default_factory=list)
	seen_calls: set[int] = field(default_factory=set)

	@classmethod
	def load(cls, path: Path) -> FileCtx:
		text = read_source(path)[0]
		return cls(path, text, SourceMap(text), tokenize_source(text, path.name), parse_python(text, path.name))


@dataclass
class Declaration:
	ctx: FileCtx
	definition: Definition
	ref_line: int  # where to ask for references (the function, or its class for constructors)
	ref_column: int
	is_constructor: bool
	receiver_classes: frozenset[str]


def _validate_params(params_text: str, name: str) -> None:
	try:
		ast.parse(f"def _({params_text}):\n\tpass\n")
	except (SyntaxError, ValueError) as exc:
		detail = exc.msg if isinstance(exc, SyntaxError) else "source contains a null character"
		raise ToolInputError(
			f"the new parameter list is not valid Python: {detail}; give a `default` or adjust `position`"
		) from exc


def _decorators(definition: Definition) -> set[str]:
	node = definition.node
	return {ast.unparse(d) for d in getattr(node, "decorator_list", [])}


def _super_calls(ctx: FileCtx, container: ast.AST | None, method: str) -> list[ast.Call]:
	if container is None:
		return []
	node = next(
		(n for n in ast.walk(ctx.tree) if isinstance(n, ast.ClassDef) and n.lineno == getattr(container, "lineno", -1)),
		None,
	)
	if node is None:
		return []
	return [
		c
		for c in ast.walk(node)
		if isinstance(c, ast.Call)
		and isinstance(c.func, ast.Attribute)
		and c.func.attr == method
		and isinstance(c.func.value, ast.Call)
		and isinstance(c.func.value.func, ast.Name)
		and c.func.value.func.id == "super"
	]


async def change_signature(
	name: str,
	add: list[dict[str, Any]] | None = None,
	remove: list[str] | None = None,
	reorder: list[str] | None = None,
	file_path: str | None = None,
	include_overrides: bool = True,
	apply: bool = True,
	max_new_errors: int | None = 0,
	allow_large: bool = False,
	ctx: Context | None = None,
) -> str:
	"""Add, remove or reorder a function's parameters and update every call site. Example:
	`change_signature(name="send_mail", add=[{"name": "retries", "annotation": "int", "default": "3"}])`.

	`name` is a function, a method (`Class.method`) or a class (its `__init__`).
	- `add`: list of `{"name", "annotation"?, "default"?, "value"?, "position"?, "keyword_only"?}`.
	`default` is written in the definition and callers need not change; `value` is
	inserted at every call site as `name=value` (give both for a required-at-call
	parameter with a default). `position` is a 0-based index among the parameters
	(self/cls not counted); the default is the end. One of `default`/`value` is required.
	- `remove`: parameter names; the matching argument is dropped at each call.
	- `reorder`: the complete new order of the named parameters. Positional arguments
	are rearranged, and turned into keywords where a position would no longer line up.

	Call sites come from the type checker's references, so calls through injected
	dependencies and typed attributes are found, not just textual matches. Overriding
	methods get the same change (`include_overrides`). Calls using `*args`/`**kwargs`
	and places where the function is passed around as a value are listed as manual
	work rather than guessed at. Unresolved alias calls, calls with comments, and
	effectful argument reorderings are also listed for manual review. Decorators
	and docstrings mentioning removed parameters produce notes. Writes when new errors stay within `max_new_errors`
	(default 0), else previews with an id for `apply_edit`; `apply=false` always previews.
	"""
	try:
		change = Change(add=list(add or []), remove=list(remove or []), reorder=list(reorder or []))
		if not (change.add or change.remove or change.reorder):
			raise ToolInputError("nothing to change: pass add, remove or reorder")
		session = await open_session(ctx)
		plan = await _plan_signature(session, name, file_path, change, include_overrides)
		outcome = await finish_plan(
			session.client,
			workspace=session.workspace,
			roots=session.roots,
			plan=plan,
			title=plan.description,
			apply=apply,
			max_new_errors=max_new_errors,
			allow_large=allow_large,
		)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return outcome.text


async def _plan_signature(
	session: Session, name: str, file_path: str | None, change: Change, include_overrides: bool
) -> EditPlan:
	path, text, definition = await _locate(session, name, file_path)
	rel_path = relative_name(path, session.workspace)
	stubs = overload_stubs(text, definition, filename=rel_path)
	if stubs:
		where = ", ".join(f"{relative_name(path, session.workspace)}:{stub.def_line}" for stub in stubs)
		raise ToolInputError(
			f"{definition.qualname} has {len(stubs)} @overload stub(s) ({where}); changing only the implementation "
			"would leave them with the old signature. Change the stubs and the implementation with edit_symbol "
			"and update the call sites by hand."
		)
	files: dict[Path, FileCtx] = {}

	def load(p: Path) -> FileCtx:
		resolved = p.resolve()
		if resolved not in files:
			files[resolved] = FileCtx.load(resolved)
		return files[resolved]

	origin_ctx = load(path)
	is_constructor = isinstance(definition.node, ast.ClassDef) or (
		definition.name == "__init__" and bool(definition.parents) and isinstance(definition.parents[-1], ast.ClassDef)
	)
	ref_definition = definition
	if is_constructor and not isinstance(definition.node, ast.ClassDef):
		ref_definition = next(
			(
				d
				for d in definitions_in(text, filename=rel_path)
				if isinstance(d.node, ast.ClassDef) and d.node.lineno == definition.parents[-1].lineno
			),
			None,
		)
		if ref_definition is None:
			raise ToolInputError("could not resolve the constructor's class; retry with the class name")
	ref_line, ref_col = definition_name_position(origin_ctx.smap.lines, ref_definition)
	if isinstance(definition.node, ast.ClassDef):
		init = next(
			(d for d in definitions_in(text, filename=rel_path) if d.qualname == f"{definition.qualname}.__init__"),
			None,
		)
		if init is None:
			raise ToolInputError(
				f"class {definition.qualname} defines no __init__ of its own (dataclass and inherited constructors "
				"are not supported here); change the fields with edit_symbol(action=replace), or the base class's __init__."
			)
		target_def = init
	else:
		target_def = definition
	declarations = [
		Declaration(origin_ctx, target_def, ref_line, ref_col, is_constructor, frozenset({ref_definition.name}))
	]
	notes: list[str] = []
	if target_def.kind == "method" and target_def.name not in ("__init__", "__new__"):
		related = await find_linked(
			session.client,
			session.workspace,
			origin_path=path,
			member_line=target_def.def_line,
			member_name=target_def.name,
			new_name=target_def.name,
			conforms=conforms_probe(session),
		)
	else:
		related = None
	if related is not None and not include_overrides and related.sites:
		left = ", ".join(
			f"{site.cls.name}.{target_def.name} ({relative_name(site.cls.path, session.workspace)}:{site.line})"
			for site in related.sites
		)
		notes.append(
			f"overrides left unchanged (include_overrides=false): {left}; "
			"pass include_overrides=true to change them with the method"
		)
	if include_overrides and related is not None:
		names = {target_def.parents[-1].name} if target_def.parents else set()
		names |= {site.cls.name for site in related.sites}
		declarations[0] = Declaration(origin_ctx, target_def, ref_line, ref_col, False, frozenset(names))
		for site in related.sites:
			site_ctx = load(site.cls.path)
			other = find_definition(
				site_ctx.text, name_line=site.line, filename=relative_name(site.cls.path, session.workspace)
			)
			if other is None:
				continue
			line, col = definition_name_position(site_ctx.smap.lines, other)
			declarations.append(Declaration(site_ctx, other, line, col, False, frozenset(names)))
			notes.append(
				f"also changing the override {site.cls.name}.{other.name} ({relative_name(site.cls.path, session.workspace)}:{site.line})"
			)

	updated_calls = 0
	manual: list[str] = []
	commented: list[str] = []
	summary_bits: list[str] = []
	for declaration in declarations:
		ctx = declaration.ctx
		signature = read_signature(ctx.smap, ctx.tokens, declaration.definition)
		try:
			params, values, removed = new_params(signature, change, declaration.definition.qualname)
			indent = declaration.definition.indent
			rendered = render_params(
				params, multiline=signature.multiline, indent=indent, unit=detect_indent_unit(ctx.text)
			)
			_validate_params(
				rendered if not signature.multiline else rendered.replace("\n", " "), declaration.definition.qualname
			)
		except ToolInputError as exc:
			if declaration is declarations[0]:
				raise
			notes.append(f"skipped override {declaration.definition.qualname}: {exc}")
			continue
		if rendered != ctx.text[signature.span[0] : signature.span[1]]:
			ctx.edits.append((signature.span[0], signature.span[1], rendered))
		if declaration is declarations[0]:
			summary_bits = [
				*(f"+{spec['name']}" for spec in change.add),
				*(f"-{n}" for n in change.remove),
				*(["reordered"] if change.reorder else []),
			]
		old_callable = signature.callable_params
		new_callable = params[1:] if signature.has_self else params
		decorators = _decorators(declaration.definition)
		for decorator in sorted(decorators):
			notes.append(
				f"Note: {declaration.definition.qualname} is decorated with @{decorator}; callers of the decorated object may have a different signature."
			)
		doc = ast.get_docstring(declaration.definition.node) or ""
		mentioned = sorted(n for n in removed if re.search(r":param\s+" + re.escape(n) + r"\s*:", doc))
		if mentioned:
			notes.append(
				f"Note: docstring of {declaration.definition.qualname} still mentions parameter(s) {', '.join(repr(n) for n in mentioned)}; update it manually."
			)
		call_sites: list[tuple[FileCtx, ast.Call, int]] = []
		locations = await session.client.references(
			str(ctx.path), declaration.ref_line, declaration.ref_column, include_declaration=False
		)
		for location in locations:
			target_ctx = load(uri_to_path(location["uri"]))
			end = (location["range"]["end"]["line"], location["range"]["end"]["character"])
			call = find_call_at(target_ctx.tree, target_ctx.smap, end)
			rel = f"{relative_name(target_ctx.path, session.workspace)}:{location['range']['start']['line'] + 1}"
			if call is None:
				line_text = target_ctx.smap.lines[location["range"]["start"]["line"]].lstrip()
				if not declaration.is_constructor and not line_text.startswith(("import ", "from ")):
					manual.append(f"{rel}: used as a value, not called ({line_text.strip()[:70]})")
				continue
			unbound = (
				signature.has_self
				and "classmethod" not in decorators
				and isinstance(call.func, ast.Attribute)
				and isinstance(call.func.value, ast.Name)
				and call.func.value.id in declaration.receiver_classes
			)
			call_sites.append((target_ctx, call, 1 if unbound else 0))
		# ty references may stop at an import alias. Report calls through such aliases
		# rather than silently claiming all calls were updated.
		if declaration is declarations[0] and (is_constructor or not declaration.definition.parents):
			target_modules = module_names_for(ctx.path, session.roots)
			for candidate in iter_python_files(session.workspace):
				if candidate.resolve() == ctx.path.resolve():
					continue
				try:
					candidate_ctx = load(candidate)
				except NulByteError:
					raise  # a NUL-text file in the workspace refuses the whole change, like the target file does
				except (OSError, UnicodeError, SyntaxError, ValueError, PythonSyntaxError):
					continue
				own_names = module_names_for(candidate, session.roots)
				aliases = {
					a.asname
					for node in ast.walk(candidate_ctx.tree)
					if isinstance(node, ast.ImportFrom)
					and target_modules
					& import_from_targets(node, own_names, is_package_init=candidate.name == "__init__.py")
					for a in node.names
					if a.name == ref_definition.name and a.asname
				}
				for node in ast.walk(candidate_ctx.tree):
					if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in aliases:
						if not any(
							c.path == candidate_ctx.path and c.smap.start(call) == candidate_ctx.smap.start(node)
							for c, call, _ in call_sites
						):
							manual.append(
								f"{relative_name(candidate, session.workspace)}:{node.lineno}: call left as is, imported alias {node.func.id!r} requires review"
							)
		if signature.has_self and declaration.definition.parents:
			for call in _super_calls(ctx, declaration.definition.parents[-1], declaration.definition.name):
				call_sites.append((ctx, call, 0))
		for target_ctx, call, skip in call_sites:
			key = target_ctx.smap.start(call)
			if key in target_ctx.seen_calls:
				continue
			target_ctx.seen_calls.add(key)
			rel = f"{relative_name(target_ctx.path, session.workspace)}:{call.lineno}"
			try:
				old_args = read_call_args(target_ctx.smap, call)
				new_args = rebind_call(old_args, old_callable, new_callable, values, removed, skip_leading=skip)
				edit = call_edit(target_ctx.smap, target_ctx.tokens, call, old_args, new_args)
			except CommentedCallError:
				commented.append(rel)
				continue
			except ManualCallError as exc:
				manual.append(f"{rel}: call left as is, {exc}")
				continue
			if edit is not None:
				target_ctx.edits.append(edit)
				updated_calls += 1
	if commented:
		# Refused outright (no preview id): a rewrite would move or drop the comments, and leaving the call as it
		# is would break it once the parameter goes.
		raise ToolInputError(
			f"{len(commented)} call site(s) have comments inside their argument list, so the change cannot be "
			"written safely: "
			+ ", ".join(commented[:10])
			+ (f" (and {len(commented) - 10} more)" if len(commented) > 10 else "")
			+ ". Move those comments out of the calls (or edit the calls by hand), then rerun change_signature."
		)
	changes = []
	for ctx in files.values():
		if not ctx.edits:
			continue
		new_text = apply_offset_edits(ctx.text, ctx.edits)
		check_syntax(new_text, relative_name(ctx.path, session.workspace))
		changes.append(FileChange(ctx.path, ctx.text, new_text))
	changes.sort(key=lambda c: str(c.path))
	if updated_calls:
		notes.insert(0, f"updated {updated_calls} call site(s) in {len([c for c in changes])} file(s)")
	if manual:
		shown = manual[:10]
		notes.append(
			"needs manual attention:\n"
			+ "\n".join(f"    {m}" for m in shown)
			+ (f"\n    ... {len(manual) - 10} more" if len(manual) > 10 else "")
		)
	title = f"Change signature of {declarations[0].definition.qualname}: {' '.join(summary_bits)}"
	return EditPlan(title, changes, notes)


async def _move_symbol(
	name: str,
	to_file: str,
	file_path: str | None = None,
	keep_reexport: bool = False,
	apply: bool = False,
	max_new_errors: int | None = 0,
	allow_large: bool = False,
	allow_import_cycle: bool = False,
	ctx: Context | None = None,
) -> str:
	"""Move a module-level function or class to another file and fix the imports. Example:
	`move_symbol(name="parse_date", to_file="src/app/dates.py")`.

	The destination is created if it does not exist. The code travels with the imports it
	needs (copied from the old module, relative ones made absolute), imports that only the
	moved code used are dropped from the old file, and if the old file still uses the symbol
	it imports it from the new place (`keep_reexport=true` leaves such an import
	regardless, so old `from old import name` keeps working). Every `from old import name`
	elsewhere is rewritten to the new module. Uses through the old module (`old.name`) or
	strings naming it are listed for you to handle, and importers the move cannot update are
	reported by name. A move that would create a module-level import cycle is refused unless
	`allow_import_cycle=true`.
	Preview by default; `apply=true` writes only if the new errors stay within `max_new_errors`.
	`undo_edit` reverts it.
	"""
	try:
		session = await open_session(ctx)
		plan = await plan_move_symbol(
			session,
			name,
			to_file,
			file_path,
			keep_reexport,
			allow_import_cycle=allow_import_cycle,
			allow_held_back=allow_large,
			apply=apply,
		)
		outcome = await finish_plan(
			session.client,
			workspace=session.workspace,
			roots=session.roots,
			plan=plan,
			title=plan.description,
			apply=apply,
			max_new_errors=max_new_errors,
			allow_large=allow_large,
		)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return outcome.text


async def _move_module(
	from_file: str,
	to_file: str,
	apply: bool = False,
	max_new_errors: int | None = 0,
	allow_large: bool = False,
	ctx: Context | None = None,
) -> str:
	"""Move or rename a Python module file and update every import of it. Example:
	`move_module(from_file="src/app/util.py", to_file="src/app/common/helpers.py")`.

	Rewrites `from old import x`, `import old [as y]` (and uses of `old.attr`), and
	`from package import old`, in every file that imports it; relative imports of the moved
	module are rewritten as absolute, and the moved file's own relative imports are made
	absolute so they keep pointing at the same modules. Strings and documents naming the
	old module path are listed, not changed. The old file is deleted and the new one
	created as one undoable edit; it is previewed unless `apply=true`.
	"""
	try:
		session = await open_session(ctx)
		plan = plan_move_module(session, from_file, to_file, allow_held_back=allow_large, apply=apply)
		outcome = await finish_plan(
			session.client,
			workspace=session.workspace,
			roots=session.roots,
			plan=plan,
			title=plan.description,
			apply=apply,
			max_new_errors=max_new_errors,
			allow_large=allow_large,
		)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return outcome.text


TOOLS: list[tuple[Any, Any]] = [
	(change_signature, tool_base.WRITES),
]
