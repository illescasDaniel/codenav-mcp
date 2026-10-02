"""rename_symbol: ty's rename, widened to the members that must change with it and checked afterwards."""

from __future__ import annotations

import ast
import keyword
import re
from pathlib import Path

from mcp_nav_shared.edits import (
	EditPlan,
	TextEdit,
	changes_from_edits,
	parse_workspace_edit,
	position_to_offset,
	read_source,
)
from mcp_nav_shared.errors import TOOL_ERRORS, ToolInputError
from mcp_nav_shared.format import uri_to_path
from mcp_nav_shared.lsp_client import LspClient
from mcp_nav_shared.resolve import resolve_symbol
from mcp_nav_shared.transaction import WriteGuard

from codenav_mcp.linked import Conforms, classes_in_file, find_linked, locate_member_class
from codenav_mcp.mentions import covered_positions, find_mentions, format_mentions
from codenav_mcp.pysource import (
	Definition,
	find_definition,
	iter_definitions,
	node_position,
	parameter_position,
	parse_python,
	source_lines,
	top_level_names,
	utf16_to_index,
)
from codenav_mcp.writes import require_workspace_file


class Target:
	def __init__(
		self, path: Path, line: int, column: int, display: str, member_line: int, parameter: str | None
	) -> None:
		self.path = path
		self.line = line
		self.column = column
		self.display = display
		# Line of the member (method/field) whose linked declarations matter; for a parameter, its function.
		self.member_line = member_line
		self.parameter = parameter


def _parameter_at(text: str, line: int, column: int) -> tuple[Definition, str] | None:
	"""The function and parameter name when (line, column) points at a parameter."""
	lines = source_lines(text)
	for definition in iter_definitions(parse_python(text), lines):
		if isinstance(definition.node, ast.ClassDef):
			continue
		args = definition.node.args
		for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, *filter(None, [args.vararg, args.kwarg])]:
			if node_position(lines, arg) == (line, column):
				return definition, arg.arg
	return None


async def resolve_target(
	client: LspClient,
	workspace: Path,
	*,
	name: str | None,
	file_path: str | None,
	line: int | None,
	column: int | None,
	parameter: str | None,
) -> Target:
	if name:
		resolved = await resolve_symbol(client, workspace, name, file_path=file_path)
		path = uri_to_path(resolved.uri).resolve()
		t_line, t_col, display = resolved.line + 1, resolved.column + 1, name
	elif file_path and line and column:
		path = require_workspace_file(workspace, file_path)
		t_line, t_col, display = line, column, f"{file_path}:{line}:{column}"
	else:
		raise ToolInputError(
			"give either `name` (e.g. 'UserService.create_user') or `file_path` with `line` and `column`."
		)
	text = read_source(path)[0]
	if parameter:
		definition = find_definition(text, name_line=t_line)
		if definition is None or isinstance(definition.node, ast.ClassDef):
			raise ToolInputError(f"{display} is not a function, so it has no parameter {parameter!r}.")
		pos = parameter_position(source_lines(text), definition, parameter)
		if pos is None:
			names = [
				a.arg
				for a in [
					*definition.node.args.posonlyargs,
					*definition.node.args.args,
					*definition.node.args.kwonlyargs,
				]
			]
			raise ToolInputError(
				f"{display} has no parameter {parameter!r} (parameters: {', '.join(names) or 'none'})."
			)
		return Target(path, pos[0], pos[1], f"{display}({parameter})", t_line, parameter)
	found = _parameter_at(text, t_line, t_col)
	if found is not None:
		definition, arg_name = found
		return Target(path, t_line, t_col, f"{definition.qualname}({arg_name})", definition.def_line, arg_name)
	return Target(path, t_line, t_col, display, t_line, None)


def validate_new_name(old_name: str, new_name: str) -> None:
	if not new_name.isidentifier() or keyword.iskeyword(new_name):
		raise ToolInputError(f"{new_name!r} is not a valid Python identifier.")
	if new_name == old_name:
		raise ToolInputError(f"new_name is the same as the current name ({old_name!r}).")


def check_collision(text: str, target: Target, new_name: str) -> None:
	"""Refuse a rename onto a name already defined in the same scope."""
	if target.parameter:
		definition = find_definition(text, name_line=target.member_line)
		if definition is not None and not isinstance(definition.node, ast.ClassDef):
			args = definition.node.args
			names = {
				a.arg
				for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, *filter(None, [args.vararg, args.kwarg])]
			}
			if new_name in names:
				raise ToolInputError(f"{definition.qualname} already has a parameter named {new_name!r}.")
		return
	container = locate_member_class(text, target.line, _identifier_at(text, target.line, target.column))
	if container is not None:
		info = next((i for i in classes_in_file(target.path) if i.ref.def_line == container.def_line), None)
		if info is not None and new_name in info.members:
			raise ToolInputError(f"class {container.qualname} already defines {new_name!r}.")
		return
	definition = find_definition(text, name_line=target.line)
	if definition is not None and definition.parent is None and new_name in top_level_names(parse_python(text)):
		raise ToolInputError(f"{target.path.name} already defines {new_name!r} at module level.")


def _identifier_at(text: str, line: int, column: int) -> str:
	lines = text.splitlines()
	row = lines[line - 1] if 0 < line <= len(lines) else ""
	match = re.match(r"\w+", row[utf16_to_index(row, column - 1) :])
	return match.group(0) if match else ""


def _merge(into: dict[Path, list[TextEdit]], more: dict[Path, list[TextEdit]]) -> None:
	for path, edits in more.items():
		bucket = into.setdefault(path.resolve(), [])
		for edit in edits:
			if edit not in bucket:
				bucket.append(edit)


async def plan_rename(
	client: LspClient,
	workspace: Path,
	guard: WriteGuard,
	target: Target,
	new_name: str,
	*,
	linked: bool,
	conforms: Conforms | None,
) -> EditPlan:
	text = read_source(target.path)[0]
	prep = await client.prepare_rename(str(target.path), target.line, target.column)
	if prep is None:
		raise ToolInputError(
			f"{target.display} cannot be renamed here: it is not a user-defined symbol "
			"(builtin, keyword, or code from a library)."
		)
	rng = prep["range"]
	start = position_to_offset(text, rng["start"]["line"], rng["start"]["character"])
	end = position_to_offset(text, rng["end"]["line"], rng["end"]["character"])
	old_name = text[start:end]
	validate_new_name(old_name, new_name)
	check_collision(text, target, new_name)

	edits: dict[Path, list[TextEdit]] = {}
	main_edit = await client.rename(str(target.path), target.line, target.column, new_name)
	_merge(edits, parse_workspace_edit(main_edit))
	notes: list[str] = []
	if linked:
		member_name = _function_name(text, target.member_line) if target.parameter else old_name
		related = await find_linked(
			client,
			workspace,
			origin_path=target.path,
			member_line=target.member_line,
			member_name=member_name,
			new_name=new_name,
			parameter=target.parameter,
			conforms=conforms,
		)
		for site in related.sites:
			try:
				more = await client.rename(str(site.cls.path), site.line, site.column, new_name)
			except TOOL_ERRORS as exc:
				notes.append(f"could not rename {site.cls.name}.{old_name}: {exc}")
				continue
			_merge(edits, parse_workspace_edit(more))
		_merge(edits, related.super_edits)
		notes += related.notes
	for path in edits:
		guard.check_path(path)
	changes = changes_from_edits(
		{path: sorted(set(es), key=lambda e: (e.start_line, e.start_character)) for path, es in edits.items()}
	)
	notes += format_mentions(find_mentions(workspace, old_name, covered_positions(edits)), old_name)
	total = sum(len(es) for es in edits.values())
	title = f"Rename {old_name} -> {new_name} ({target.display}): {total} edit(s) in {len(changes)} file(s)"
	return EditPlan(title, changes, notes)


def _function_name(text: str, def_line: int) -> str:
	definition = find_definition(text, name_line=def_line)
	return definition.name if definition else ""
