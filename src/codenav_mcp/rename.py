"""rename_symbol: ty's rename, widened to the members that must change with it and checked afterwards."""

from __future__ import annotations

import ast
import keyword
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_nav_shared.edits import (
	EditPlan,
	TextEdit,
	changes_from_edits,
	parse_workspace_edit,
	position_to_offset,
	read_source,
	relative_name,
)
from mcp_nav_shared.errors import TOOL_ERRORS, ToolInputError
from mcp_nav_shared.exclude import excluded_names, is_inside_nested_repo
from mcp_nav_shared.format import uri_to_path
from mcp_nav_shared.lsp_client import LspClient
from mcp_nav_shared.resolve import resolve_symbol
from mcp_nav_shared.transaction import WriteGuard

from codenav_mcp.deps import Skipped, SkipReason, find_dependents, format_skipped, module_names_for
from codenav_mcp.linked import Conforms, classes_in_file, external_declaration, find_linked, locate_member_class
from codenav_mcp.mentions import Mention, covered_positions, find_mentions, format_mentions
from codenav_mcp.plan_sites import OptionalSite
from codenav_mcp.pysource import (
	Definition,
	PythonSyntaxError,
	byte_to_utf16_column,
	definition_name_position,
	find_definition,
	iter_definitions,
	node_position,
	parameter_position,
	parse_python,
	source_lines,
	top_level_names,
	utf16_to_index,
)
from codenav_mcp.rename_sites import Declaration, plan_v2_sites, select_sites
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
		# A given file_path is a hard scope: it must be inside the workspace, and the symbol must be declared in
		# that file (or its .py/.pyi twin), never in a same-named file reached by suffix matching (RC-092).
		scope = require_workspace_file(workspace, file_path) if file_path else None
		resolved = await resolve_symbol(client, workspace, name, file_path=file_path)
		path = uri_to_path(resolved.uri).resolve()
		if scope is not None and not _same_module_file(path, scope):
			raise ToolInputError(f"{name} is not declared in {file_path}; drop file_path to search the workspace.")
		if scope is not None and line is not None and column is not None:
			await _ensure_position_agrees(client, scope, line, column, path, resolved.line + 1)
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


def _nfkc(name: str) -> str:
	"""Python compares identifiers after NFKC normalisation: `ﬁx` and `fix` are the same name."""
	return unicodedata.normalize("NFKC", name)


def _is_dunder(name: str) -> bool:
	return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _same_module_file(path: Path, scope: Path) -> bool:
	"""`path` is `scope` itself or its .py/.pyi twin."""
	return path == scope or (
		path.with_suffix("") == scope.with_suffix("") and {path.suffix, scope.suffix} <= {".py", ".pyi"}
	)


def _twin_of(path: Path) -> Path | None:
	"""The stub (.pyi) next to a source file, or the source next to a stub, when it exists."""
	twin = path.with_suffix(".pyi" if path.suffix == ".py" else ".py") if path.suffix in {".py", ".pyi"} else None
	return twin if twin is not None and twin.is_file() else None


def _folded_match(new_name: str, names: Iterable[str]) -> str | None:
	"""A name in `names` equal to `new_name`, exactly or after NFKC normalisation (exact matches first)."""
	candidates = list(names)
	if new_name in candidates:
		return new_name
	folded = _nfkc(new_name)
	return next((n for n in sorted(candidates) if _nfkc(n) == folded), None)


def _ensure_free(new_name: str, names: Iterable[str], exact_message: str) -> None:
	match = _folded_match(new_name, names)
	if match is None:
		return
	if match == new_name:
		raise ToolInputError(exact_message)
	raise ToolInputError(f"new name {new_name} collides with {match} after Unicode normalisation")


def validate_new_name(old_name: str, new_name: str) -> None:
	if not new_name.isidentifier() or keyword.iskeyword(new_name):
		raise ToolInputError(f"{new_name!r} is not a valid Python identifier.")
	if _nfkc(new_name) == _nfkc(old_name):
		raise ToolInputError(f"new_name is the same as the current name ({old_name!r}).")


def check_collision(text: str, target: Target, new_name: str) -> None:
	"""Refuse a rename onto a name already defined in the same scope (exactly or after NFKC normalisation)."""
	if target.parameter:
		definition = find_definition(text, name_line=target.member_line)
		if definition is not None and not isinstance(definition.node, ast.ClassDef):
			args = definition.node.args
			names = {
				a.arg
				for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, *filter(None, [args.vararg, args.kwarg])]
			}
			_ensure_free(new_name, names, f"{definition.qualname} already has a parameter named {new_name!r}.")
		return
	container = locate_member_class(text, target.line, _identifier_at(text, target.line, target.column))
	if container is not None:
		info = next((i for i in classes_in_file(target.path) if i.ref.def_line == container.def_line), None)
		if info is not None:
			_ensure_free(new_name, info.members, f"class {container.qualname} already defines {new_name!r}.")
		return
	definition = find_definition(text, name_line=target.line)
	if definition is not None and definition.parent is None:
		_ensure_free(
			new_name,
			top_level_names(parse_python(text)),
			f"{target.path.name} already defines {new_name!r} at module level.",
		)


def _binding_line(tree: ast.Module, name: str) -> int | None:
	"""Line of the first module-level statement that binds `name` (after NFKC normalisation)."""
	folded = _nfkc(name)
	for stmt in tree.body:
		bound: list[str] = []
		if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
			bound = [stmt.name]
		elif isinstance(stmt, ast.Import):
			bound = [(alias.asname or alias.name).split(".")[0] for alias in stmt.names]
		elif isinstance(stmt, ast.ImportFrom):
			bound = [alias.asname or alias.name for alias in stmt.names]
		elif isinstance(stmt, ast.Assign):
			bound = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
		elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)) and isinstance(stmt.target, ast.Name):
			bound = [stmt.target.id]
		if any(_nfkc(b) == folded for b in bound):
			return stmt.lineno
	return None


def check_importers(workspace: Path, roots: Sequence[Path], target: Target, old_name: str, new_name: str) -> None:
	"""Refuse when a file that imports `old_name` from the target module already binds `new_name` at module level:
	after the rename its import would collide with that binding (and Python would silently use the wrong one)."""
	search_roots = list(roots) or [workspace]
	modules = module_names_for(target.path, search_roots)
	for importer in find_dependents(workspace, [target.path], search_roots):
		try:
			tree = parse_python(read_source(importer)[0], str(importer))
		except (OSError, UnicodeDecodeError, ValueError, PythonSyntaxError):
			continue
		imports_old = any(
			isinstance(stmt, ast.ImportFrom)
			and (stmt.level > 0 or stmt.module in modules)
			and any(alias.name == old_name and alias.asname is None for alias in stmt.names)
			for stmt in tree.body
		)
		if not imports_old:
			continue
		line = _binding_line(tree, new_name)
		if line is not None:
			rel = relative_name(importer, workspace)
			raise ToolInputError(f"{new_name} is already bound in {rel} (line {line}).")


def _identifier_at(text: str, line: int, column: int) -> str:
	lines = source_lines(text)
	row = lines[line - 1].rstrip("\r\n") if 0 < line <= len(lines) else ""
	match = re.match(r"\w+", row[utf16_to_index(row, column - 1) :])
	return match.group(0) if match else ""


async def _ensure_position_agrees(
	client: LspClient, scope: Path, line: int, column: int, name_path: Path, name_line: int
) -> None:
	"""`name` and `line/column` must point at the same declaration; otherwise the caller gave two symbols."""
	locations = []
	for item in await client.definition(str(scope), line, column):
		uri = str(item.get("uri") or item.get("targetUri") or "")
		rng = item.get("range") or item.get("targetSelectionRange") or {}
		if uri.startswith("file:"):
			locations.append((uri_to_path(uri).resolve(), int(rng.get("start", {}).get("line", 0)) + 1))
	if locations and (name_path, name_line) not in locations:
		raise ToolInputError("name and line/column point to different symbols; give one of them")


def dunder_all_edits(text: str, old: str, new: str) -> list[TextEdit]:
	"""Edits renaming the string entries equal to `old` in a literal module-level `__all__` list or tuple (D36):
	the export list follows the rename instead of going stale."""
	try:
		tree = parse_python(text)
	except PythonSyntaxError:
		return []
	lines = source_lines(text)
	edits: list[TextEdit] = []
	for stmt in tree.body:
		if isinstance(stmt, ast.Assign):
			targets, value = stmt.targets, stmt.value
		elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)) and stmt.value is not None:
			targets, value = [stmt.target], stmt.value
		else:
			continue
		if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
			continue
		if not isinstance(value, (ast.List, ast.Tuple)):
			continue
		for element in value.elts:
			if not (isinstance(element, ast.Constant) and element.value == old):
				continue
			if element.end_lineno != element.lineno:
				continue
			row = lines[element.lineno - 1]
			raw = row.encode("utf-8")[element.col_offset : element.end_col_offset].decode("utf-8")
			quote = raw[:1]
			if quote not in {"'", '"'} or raw != f"{quote}{old}{quote}":
				continue  # prefixed, escaped or implicitly joined literal: leave it alone
			start = byte_to_utf16_column(row, element.col_offset)
			end = byte_to_utf16_column(row, element.end_col_offset or element.col_offset)
			edits.append(TextEdit(element.lineno - 1, start + 1, element.lineno - 1, end - 1, new))
	return edits


def _merge(into: dict[Path, list[TextEdit]], more: dict[Path, list[TextEdit]]) -> None:
	for path, edits in more.items():
		bucket = into.setdefault(path.resolve(), [])
		for edit in edits:
			if edit not in bucket:
				bucket.append(edit)


def _rename_bom_convention(path: Path, edits: list[TextEdit], old_name: str) -> bool | None:
	"""Infer only when the exact old identifier uniquely identifies the coordinate convention."""
	text, bom = read_source(path)
	first_line_edits = [e for e in edits if e.start_line == 0 or e.end_line == 0]
	if not bom or not first_line_edits:
		return None
	possible = []
	for includes_bom in (False, True):
		matches = True
		for edit in first_line_edits:
			start_col = edit.start_character - int(includes_bom and edit.start_line == 0)
			end_col = edit.end_character - int(includes_bom and edit.end_line == 0)
			if start_col < 0 or end_col < 0:
				matches = False
				break
			start = position_to_offset(text, edit.start_line, start_col)
			end = position_to_offset(text, edit.end_line, end_col)
			if text[start:end] != old_name:
				matches = False
				break
		if matches:
			possible.append(includes_bom)
	return possible[0] if len(possible) == 1 else None


async def _refuse_external_contract(client: LspClient, workspace: Path, target: Target, text: str, old: str) -> None:
	"""Renaming a special method, or an override of a base class outside the workspace, breaks a contract the
	workspace does not own (RC-024, RC-065): refuse rather than apply it silently."""
	if _is_dunder(old):
		raise ToolInputError(
			f"{old} is a special method name that Python calls by protocol; renaming it would break that contract."
		)
	if locate_member_class(text, target.line, old) is None:
		return
	base = await external_declaration(
		client, workspace, origin_path=target.path, member_line=target.line, member_name=old
	)
	if base is not None:
		raise ToolInputError(
			f"{old} overrides {base.name}.{old} from {base.module} (outside the workspace); "
			f"renaming it would break the contract. Rename the base method only if you own it."
		)


def _declaration_at(text: str, target: Target) -> Definition | None:
	"""The function or class declared on the target's line (None for parameters, variables and library code)."""
	definition = find_definition(text, name_line=target.line)
	return definition if definition is not None and target.parameter is None else None


# Ends the note about unlinked attribute accesses (stored in the plan, so it reads right for a preview as it is);
# `rename_symbol` swaps it for the undo-then-rerun wording when the rename was written.
ATTR_HINT = "to rename these too, rerun with include_unlinked_attrs=true"


def _is_unlinked_attr(mention: Mention) -> bool:
	"""An attribute access after a dot (`mock.old`) on a code line; never an import (`from .old import x`)."""
	return (
		mention.kind == "attr" and mention.column is not None and not mention.snippet.startswith(("from ", "import "))
	)


# Most attribute accesses one opt-in rename asks the type checker about (sequential requests; the rest stay as they are).
_MAX_ATTR_LOOKUPS = 300
# Where ty resolves an attribute on a mock: that is still an "unlinked" use of the renamed member, not another symbol.
_MOCK_STUBS = ("unittest/mock.pyi", "unittest/mock.py")


async def _split_unlinked_attrs(
	client: LspClient,
	workspace: Path,
	candidates: list[Mention],
	renamed_at: dict[Path, set[tuple[int, int]]],
	*,
	limit: int | None = _MAX_ATTR_LOOKUPS,
) -> tuple[list[Mention], list[Mention], int]:
	"""(accesses to rename, accesses that resolve to another symbol, accesses not checked).

	An access is renamed only when the type checker has no definition for it (an untyped object, a mock) or the
	definition is the renamed symbol itself: `connection.execute` on a typed `sqlite3.Connection` is a different
	method and must stay. A lookup that fails or runs past the cap leaves the access alone (counted, not renamed)."""
	rename, other, unchecked = [], [], 0
	for index, mention in enumerate(candidates):
		if limit is not None and index >= limit:
			unchecked += len(candidates) - index
			break
		try:
			locations = await client.definition(str(workspace / mention.path), mention.line, (mention.column or 0) + 1)
		except TOOL_ERRORS:
			unchecked += 1
			continue
		if all(_is_renamed_or_mock(item, renamed_at) for item in locations):
			rename.append(mention)
		else:
			other.append(mention)
	return rename, other, unchecked


def _is_renamed_or_mock(item: dict[str, Any], renamed_at: dict[Path, set[tuple[int, int]]]) -> bool:
	uri = str(item.get("uri") or item.get("targetUri") or "")
	if not uri.startswith("file:"):
		return False
	path = uri_to_path(uri)
	if path.as_posix().endswith(_MOCK_STUBS):
		return True
	start = (item.get("range") or item.get("targetSelectionRange") or {}).get("start", {})
	return (int(start.get("line", -1)), int(start.get("character", -1))) in renamed_at.get(path.resolve(), set())


@dataclass
class RenamePlan(EditPlan):
	"""A rename plan that also knows which importers it did not rewrite (excluded directories, nested repositories)."""

	left_out: list[str] = field(default_factory=list)
	# v2 `rename`: uncertain sites the agent can opt into (`sites=`), the ones this plan already includes,
	# and how many certain extras (patch-target strings, keyword arguments) it writes.
	optional_sites: list[OptionalSite] = field(default_factory=list)
	chosen_sites: list[OptionalSite] = field(default_factory=list)
	certain_extras: int = 0


def _declaration_for(path: Path, line: int, name: str) -> Declaration:
	"""A renamed declaration with the name it has in a dotted path (`Class.method`, `func`, `Class`)."""
	text = read_source(path)[0]
	definition = find_definition(text, name_line=line)
	if definition is not None and definition.name == name:
		return Declaration(path.resolve(), line, definition.qualname)
	container = locate_member_class(text, line, name)
	return Declaration(path.resolve(), line, f"{container.qualname}.{name}" if container else name)


async def plan_rename(
	client: LspClient,
	workspace: Path,
	guard: WriteGuard,
	target: Target,
	new_name: str,
	*,
	linked: bool,
	conforms: Conforms | None,
	roots: Sequence[Path] = (),
	include_unlinked_attrs: bool = False,
	v2_sites: bool = False,
	sites: Sequence[str] | None = None,
) -> RenamePlan:
	"""Plan a rename. With `v2_sites` (the `rename` tool) the sites ty cannot link are split into certain ones,
	written with the plan, and optional ones (ids u1..uN, added by `sites=`) instead of notes."""
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
	if target.parameter is None:
		await _refuse_external_contract(client, workspace, target, text, old_name)
	check_collision(text, target, new_name)
	declaration = _declaration_at(text, target)
	module_level = declaration is not None and declaration.parent is None and declaration.name == old_name
	if module_level:
		check_importers(workspace, roots, target, old_name, new_name)

	edits: dict[Path, list[TextEdit]] = {}
	conventions: dict[Path, bool | None] = {}

	left_out: dict[Path, Skipped] = {}

	def merge_server_edits(raw: dict[str, Any] | None) -> None:
		parsed = parse_workspace_edit(raw)
		for path in list(parsed):
			# An importer in an excluded directory or a nested git repository is not rewritten: it is listed
			# below, like `move` lists it, instead of refusing the whole rename (RC-040 policy, docs/tools.md).
			group = _unwritable_importer_group(path.resolve(), workspace, guard.exclude_prefix)
			if group is not None and path.resolve() != target.path.resolve():
				left_out[path.resolve()] = group
				del parsed[path]
		for path in parsed:
			resolved = path.resolve()
			guard.check_path(resolved)
			# Client open state can change while the server computes the response.
			# Only accept a convention proven by the exact identifier being renamed.
			convention = _rename_bom_convention(resolved, parsed[path], old_name)
			if resolved in conventions and conventions[resolved] != convention:
				conventions[resolved] = None
			else:
				conventions[resolved] = convention
		_merge(edits, parsed)

	def merge_bom_free(path: Path, more: list[TextEdit]) -> None:
		"""Add edits measured in the BOM-less text (as `read_source` gives it), like the super-class edits are:
		a first-line one in a BOM file is applied as it is, unless server edits on that line prove another convention."""
		resolved = path.resolve()
		server_first_line = any(e.start_line == 0 or e.end_line == 0 for e in edits.get(resolved, []))
		if server_first_line and conventions.get(resolved) is not False:
			conventions[resolved] = None
		else:
			conventions[resolved] = False
		_merge(edits, {path: more})

	main_edit = await client.rename(str(target.path), target.line, target.column, new_name)
	merge_server_edits(main_edit)
	notes: list[str] = []
	related = None
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
			merge_server_edits(more)
		for path in related.super_edits:
			resolved = path.resolve()
			if resolved in conventions and conventions[resolved] is not False:
				conventions[resolved] = None
			else:
				conventions[resolved] = False
		_merge(edits, related.super_edits)
		notes += related.notes
	twin = _twin_of(target.path)
	if twin is not None and declaration is not None:
		# ty links a `.py` function to its stub only one way: rename the stub's declaration too (RC-003).
		twin_text = read_source(twin)[0]
		twin_declaration = find_definition(twin_text, qualname=declaration.qualname)
		if twin_declaration is None:
			notes.append(f"{twin.name} declares no {declaration.qualname}; the stub is unchanged")
		else:
			twin_line, twin_column = definition_name_position(source_lines(twin_text), twin_declaration)
			try:
				more = await client.rename(str(twin), twin_line, twin_column, new_name)
			except TOOL_ERRORS as exc:
				notes.append(f"could not rename {declaration.qualname} in {twin.name}: {exc}")
			else:
				merge_server_edits(more)
	synced_exports: set[tuple[str, int]] = set()
	if target.parameter is None and locate_member_class(text, target.line, old_name) is None:
		# A literal `__all__` that exports the old name follows the rename, in this module and in any
		# re-exporting `__init__.py` the rename touches (D36).
		for export_file in sorted({target.path.resolve(), *(p for p in edits if p.name == "__init__.py")}):
			more_all = dunder_all_edits(read_source(export_file)[0], old_name, new_name)
			if more_all:
				merge_bom_free(export_file, more_all)
				synced_exports.update((relative_name(export_file, workspace), e.start_line + 1) for e in more_all)
	is_member = None if target.parameter else locate_member_class(text, target.line, old_name) is not None
	# The `__all__` lines renamed above are not mentions the caller still has to review.
	kept = (
		[]
		if v2_sites
		else [
			m
			for m in find_mentions(workspace, old_name, covered_positions(edits))
			if not _synced_export(m, synced_exports)
		]
	)
	attr_hint = None
	v2_plan = None
	chosen: list[OptionalSite] = []
	if v2_sites:
		declarations = [_declaration_for(target.path, target.line, old_name)] if target.parameter is None else []
		if related is not None and target.parameter is None:
			declarations += [_declaration_for(site.cls.path, site.line, old_name) for site in related.sites]
		v2_plan = await plan_v2_sites(
			client,
			workspace,
			roots or [workspace],
			old_name=old_name,
			new_name=new_name,
			declarations=declarations,
			is_member=bool(is_member),
			parameter=target.parameter is not None,
			link_keywords=target.parameter is not None or declaration is None,
			edits=edits,
			split_attrs=_split_unlinked_attrs,
			is_unlinked_attr=_is_unlinked_attr,
		)
		chosen = select_sites(v2_plan.optional, sites)
		for path, extra in v2_plan.certain.items():
			guard.check_path(path)
			merge_bom_free(path, extra)
		by_site: dict[Path, list[TextEdit]] = {}
		for site in chosen:
			by_site.setdefault((workspace / site.path).resolve(), []).append(site.edit)
		for path, more_sites in by_site.items():
			guard.check_path(path)
			merge_bom_free(path, more_sites)
		notes += v2_plan.notes
	elif include_unlinked_attrs and not is_member:
		notes.append(
			"include_unlinked_attrs was ignored: it only applies to renaming a method or a field of a class "
			"(a bare name, string or comment is never renamed this way)."
		)
	elif include_unlinked_attrs:
		by_file: dict[Path, list[TextEdit]] = {}
		renamed_attrs, other_attrs, unchecked = await _split_unlinked_attrs(
			client, workspace, [m for m in kept if _is_unlinked_attr(m)], covered_positions(edits)
		)
		width = len(old_name.encode("utf-16-le")) // 2
		for mention in renamed_attrs:
			column = mention.column or 0
			edit = TextEdit(mention.line - 1, column, mention.line - 1, column + width, new_name)
			by_file.setdefault((workspace / mention.path).resolve(), []).append(edit)
		for path, more_attrs in by_file.items():
			guard.check_path(path)
			merge_bom_free(path, more_attrs)
		# What resolves to another symbol is neither renamed nor listed as "probably the renamed member".
		gone = {id(m) for m in (*renamed_attrs, *other_attrs)}
		kept = [m for m in kept if id(m) not in gone]
		if other_attrs:
			notes.append(
				f"include_unlinked_attrs: left {len(other_attrs)} attribute access(es) alone: the type checker "
				f"resolves them to another symbol (e.g. {other_attrs[0].path}:{other_attrs[0].line})."
			)
		if unchecked:
			notes.append(
				f"include_unlinked_attrs: {unchecked} attribute access(es) could not be checked and were not renamed."
			)
		if renamed_attrs:
			files = sorted({m.path for m in renamed_attrs})
			shown = ", ".join(files[:5]) + (f" and {len(files) - 5} more" if len(files) > 5 else "")
			notes.append(
				f"include_unlinked_attrs: also renamed {len(renamed_attrs)} attribute access(es) the type checker "
				f"could not link (mocks, untyped objects) in {shown}; review them in the diff."
			)
	elif is_member and any(m.kind == "attr" for m in kept):
		attr_hint = ATTR_HINT
	for path in edits:
		guard.check_path(path)
	changes = changes_from_edits(
		{path: sorted(set(es), key=lambda e: (e.start_line, e.start_character)) for path, es in edits.items()},
		bom_in_positions=conventions,
	)
	notes += format_mentions(kept, old_name, member=is_member, attr_hint=attr_hint)
	left: list[str] = []
	if left_out:
		counts: dict[tuple[str, SkipReason], int] = {}
		for group in left_out.values():
			key = (group.path_or_dir, group.reason)
			counts[key] = counts.get(key, 0) + 1
		skipped = [Skipped(path, reason, "", count) for (path, reason), count in sorted(counts.items())]
		notes = [*format_skipped(skipped), *notes]
		left = [f"{path} ({count} file{'s' if count != 1 else ''})" for (path, _), count in sorted(counts.items())]
	total = sum(len(es) for es in edits.values())
	title = f"Rename {old_name} -> {new_name} ({target.display}): {total} edit(s) in {len(changes)} file(s)"
	plan = RenamePlan(title, changes, notes, left_out=left)
	if v2_plan is not None:
		plan.optional_sites = v2_plan.optional
		plan.chosen_sites = chosen
		plan.certain_extras = v2_plan.certain_count
	return plan


def _unwritable_importer_group(path: Path, workspace: Path, exclude_prefix: str) -> Skipped | None:
	"""The excluded directory or nested git repository a file sits in (as a `Skipped` group), else None."""
	try:
		rel = path.relative_to(workspace.resolve())
	except ValueError:
		return None
	excluded = excluded_names(exclude_prefix)
	for index, part in enumerate(rel.parts[:-1]):
		if part in excluded:
			return Skipped("/".join(rel.parts[: index + 1]) + "/", "excluded dir", "")
	nested = is_inside_nested_repo(path, workspace.resolve())
	if nested is not None:
		return Skipped(nested.relative_to(workspace.resolve()).as_posix() + "/", "nested repo", "")
	return None


def _synced_export(mention: Mention, synced: set[tuple[str, int]]) -> bool:
	"""True for a string mention that is a `__all__` entry this rename has already renamed (D36)."""
	return mention.kind == "string" and mention.snippet.startswith("__all__") and (mention.path, mention.line) in synced


def _function_name(text: str, def_line: int) -> str:
	definition = find_definition(text, name_line=def_line)
	return definition.name if definition else ""
