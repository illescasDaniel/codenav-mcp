"""Name-first navigation over the same type-resolved data as the v1 read tools."""

from __future__ import annotations

import ast
import asyncio
import re
import subprocess  # noqa: S404
import tomllib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import Context
from mcp_nav_shared.diagnostics_delta import entries_from_lsp
from mcp_nav_shared.edits import position_to_offset, read_source
from mcp_nav_shared.errors import TOOL_ERRORS, FailedText, ToolInputError, format_tool_error
from mcp_nav_shared.format import (
	_path_filter_matches,
	filter_symbols_by_kind_and_path,
	filter_workspace_symbols,
	outside_workspace_note,
	parse_kind_filter,
	rank_workspace_symbols,
	split_inside_workspace,
	symbol_kind_label,
	uri_to_path,
	uri_to_relative,
	workspace_symbol_position,
)
from mcp_nav_shared.resolve import ResolvedSymbol, SymbolNotFoundError, resolve_symbol, symbol_name_problem

from codenav_mcp import tool_base, write_tools
from codenav_mcp.ambiguity import Candidate, parse_candidates
from codenav_mcp.args import example_call
from codenav_mcp.deps import module_names_for
from codenav_mcp.linked import locate_member_class
from codenav_mcp.mentions import Mention, find_mentions, types_as_never
from codenav_mcp.pysource import Definition, definition_name_position, find_definition, node_position, source_lines
from codenav_mcp.reference_format import _hit_key, constructor_keyword_locations
from codenav_mcp.rename import _is_unlinked_attr, _split_unlinked_attrs
from codenav_mcp.reply import Reply, call, paginate
from codenav_mcp.tool_base import Session
from codenav_mcp.ty_command import resolve_ty_command, ty_coverage
from codenav_mcp.writes import NOT_CHECKED


_SECTIONS = frozenset(
	{"signature", "doc", "source", "references", "callers", "implementations", "subclasses", "overrides", "mentions"}
)
_TEXT_MENTION_KINDS = frozenset({"string", "comment", "text"})
_HOVER_CODE = re.compile(r"\A```[^\n]*\n(.*?)\n```\s*", re.DOTALL)


@dataclass(frozen=True)
class _Position:
	file_path: str
	line: int
	column: int

	def args(self) -> dict[str, Any]:
		return {"file_path": self.file_path, "line": self.line, "column": self.column}


@dataclass(frozen=True)
class _Target:
	position: _Position  # original declaration or input position for a library symbol
	name: str
	label: str
	resolved: ResolvedSymbol | None
	locations: list[dict[str, Any]]
	text: str = ""
	definition: Definition | None = None
	owner: Definition | None = None

	@property
	def protocol(self) -> bool:
		owner = self.owner or self.definition
		return bool(
			owner
			and isinstance(owner.node, ast.ClassDef)
			and any(tool_base.server()._is_protocol_base(base) for base in owner.node.bases)
		)


@dataclass(frozen=True)
class _Row:
	location: str
	context: str

	def render(self) -> str:
		return f"{self.location}  {self.context}"


def _refusal(exc: Exception, tool: Callable[..., Any]) -> str:
	message = format_tool_error(exc)
	return FailedText(Reply(f"{message}\nExample: {example_call(tool)}").render())


def _ambiguity(
	exc: Exception, name: str | None, file_path: str | None, show: list[str] | None, limit: int, offset: int
) -> str | None:
	if name is None:
		return None
	suffix = name
	parsed = parse_candidates(exc, suffix)
	while parsed is None and suffix and "." in suffix:
		suffix = suffix.split(".", 1)[1]
		parsed = parse_candidates(exc, suffix)
	if parsed is None:
		return None
	total, matched, candidates = parsed
	is_container = matched != tool_base.server()._nfkc(suffix)
	reply = Reply(f"{total} symbols match {name!r}.")

	def render(candidate: Candidate) -> str:
		selected_name = name if is_container else candidate.display
		# A position also distinguishes repeated declarations in one file.
		sections = show
		if sum(c.display == candidate.display and c.path == candidate.path for c in candidates) > 1:
			args = _Position(candidate.path, candidate.line, candidate.column).args()
			if is_container:
				# The candidate identifies the containing class. Show its body to locate the requested member.
				sections = ["source"]
		else:
			args = {"name": selected_name, "file_path": candidate.path}
		retry = call("lookup", **args, show=sections)
		return f"{candidate.path}:{candidate.line}  {candidate.display} [{candidate.kind}]  {retry}"

	if total > len(candidates):
		reply.notes.append(f"Resolver supplied {len(candidates)} candidates. Use search to narrow the name or path.")
	if file_path is not None:
		file_path = tool_base.server()._workspace_display_path(file_path)
	reply.add_page(
		paginate(candidates, limit, offset), render, "lookup", name=name, file_path=file_path, show=show, limit=limit
	)
	return reply.render()


def _position(loc: dict[str, Any], workspace: Path) -> _Position:
	srv = tool_base.server()
	line, column = srv._location_start(
		{"targetSelectionRange": loc["selectionRange"]} if "selectionRange" in loc else loc
	)
	return _Position(srv._display_path(srv._location_uri(loc), workspace), line + 1, column + 1)


def _selected_name(text: str, loc: dict[str, Any]) -> str:
	rng = loc.get("targetSelectionRange") or loc.get("range") or {}
	start, end = rng.get("start"), rng.get("end")
	if start is None or end is None or start["line"] != end["line"]:
		return ""
	i = position_to_offset(text, start["line"], start["character"])
	j = position_to_offset(text, end["line"], end["character"])
	name = text[i:j]
	return name if name.isidentifier() else ""


async def _target(
	session: Session, name: str | None, file_path: str | None, line: int | None, column: int | None
) -> _Target:
	srv = tool_base.server()
	resolved = None
	if name is not None:
		query = name
		if file_path and "." in name and not symbol_name_problem(name):
			# The shared resolver tries module-qualified names only without a file constraint.
			modules = module_names_for(session.workspace / file_path, session.roots)
			for module in sorted(modules, key=len, reverse=True):
				if name.startswith(module + "."):
					query = name.removeprefix(module + ".")
					break
		resolved = await srv._resolve_read_symbol(session.client, query, file_path)
		position = _Position(uri_to_relative(resolved.uri, session.workspace), resolved.line + 1, resolved.column + 1)
	else:
		if file_path is None or line is None or column is None:
			raise ToolInputError("Give name, or file_path with 1-based line and column.")
		position = _Position(srv._workspace_display_path(file_path), line, column)
	locations = await session.client.definition(**position.args())
	origin = locations[0] if locations else {}
	if origin and resolved is None and len(locations) == 1:
		uri = srv._location_uri(origin)
		if uri.startswith("file:") and uri_to_path(uri).resolve().is_relative_to(session.workspace.resolve()):
			position = _position(origin, session.workspace)
	text = ""
	definition = owner = None
	if resolved is not None or (origin and _position(origin, session.workspace) == position):
		try:
			text = read_source(session.workspace / position.file_path)[0]
			definition = find_definition(text, name_line=position.line, filename=position.file_path)
		except TOOL_ERRORS:
			pass
	leaf = resolved.name if resolved is not None else (_selected_name(text, origin) if text else "")
	if definition is not None and definition.name != leaf:
		definition = None
	if text and leaf:
		try:
			owner = locate_member_class(text, position.line, leaf)
		except TOOL_ERRORS:
			pass
	label = name or (f"{owner.qualname}.{leaf}" if owner else leaf) or "Symbol"
	if origin and (stub := await srv._stub_twin_location(session.client, origin)):
		locations = [stub, *locations]
	return _Target(position, leaf, label, resolved, locations, text, definition, owner)


def _signature_and_doc(hover: str, definition: Definition | None, *, brief: bool) -> tuple[str, str]:
	blocks = []
	while match := _HOVER_CODE.match(hover):
		blocks.append(match[1])
		hover = hover[match.end() :]
	if not blocks:
		signature, *tail = re.split(r"\n(?:-{3,}\n|\s*\n)", hover, maxsplit=1)
		hover = tail[0] if tail else ""
	else:
		signature = "\n".join(blocks)
		hover = re.sub(r"^[-]{3,}\n\s*", "", hover)
	doc = ast.get_docstring(definition.node) if definition else None
	doc = (doc or hover).strip()
	if brief:
		paragraph = re.split(r"\n\s*\n", doc, maxsplit=1)[0]
		doc = " ".join(line.strip() for line in paragraph.splitlines())
	return signature, doc


def _source(target: _Target) -> str | None:
	if not target.text:
		return None
	if target.definition is not None:
		start, end = target.definition.first_line, target.definition.last_line
	else:
		start = end = target.position.line
		try:
			tree = ast.parse(target.text)
		except SyntaxError:
			return None
		# Fields and variables can have multi-line assignment statements.
		for node in ast.walk(tree):
			if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.lineno == start:
				end = node.end_lineno or start
				break
	return "".join(source_lines(target.text)[start - 1 : end]).rstrip("\r\n")


def _note_outside(reply: Reply, count: int) -> None:
	if note := outside_workspace_note(count):
		reply.notes.append(note)


def _add_section(
	reply: Reply, title: str, rows: list[_Row], section: str, args: dict[str, Any], limit: int, offset: int
) -> None:
	reply.items.append(f"{title} ({len(rows)})")
	reply.add_page(paginate(rows, limit, offset), _Row.render, "lookup", **args, show=[section], limit=limit)


def _keyword_positions(path: Path) -> set[tuple[int, int]]:
	try:
		text = read_source(path)[0]
		tree = ast.parse(text)
	except (*TOOL_ERRORS, SyntaxError):
		return set()
	lines = source_lines(text)
	return {node_position(lines, node) for node in ast.walk(tree) if isinstance(node, ast.keyword) and node.arg}


def _reference_renderer(target: _Target, workspace: Path) -> Callable[[dict[str, Any]], str]:
	keywords: dict[str, set[tuple[int, int]]] = {}

	def render(loc: dict[str, Any]) -> str:
		pos = _position(loc, workspace)
		if pos.file_path not in keywords:
			keywords[pos.file_path] = _keyword_positions(workspace / pos.file_path)
		context = f"column {pos.column}"
		if pos == target.position:
			context += "  declaration"
		elif target.name and (pos.line, pos.column) in keywords[pos.file_path]:
			context += f"  keyword {target.name}="
		return _Row(f"{pos.file_path}:{pos.line}", context).render()

	return render


async def _call_rows(session: Session, position: _Position) -> tuple[list[_Row], int]:
	srv = tool_base.server()
	incoming, outside = await srv._incoming_calls(session.client, **position.args())
	sites = [
		(
			{"uri": item["from"]["uri"], "range": rng},
			item["from"]["name"],
		)
		for item in incoming or []
		for rng in item.get("fromRanges") or [item["from"]["selectionRange"]]
	]
	sites.sort(key=lambda item: _hit_key(item[0], session.workspace))
	rows = []
	for loc, name in sites:
		pos = _position(loc, session.workspace)
		rows.append(_Row(f"{pos.file_path}:{pos.line}", f"{name} (column {pos.column})"))
	return rows, outside


def _owner_position(target: _Target) -> _Position | None:
	cls = target.owner or target.definition
	if cls is None or not cls.is_class:
		return None
	line, col = definition_name_position(source_lines(target.text), cls)
	return _Position(target.position.file_path, line, col)


def _init_disabled(expr: ast.expr) -> bool:
	return isinstance(expr, ast.Call) and any(
		keyword.arg == "init" and isinstance(keyword.value, ast.Constant) and keyword.value.value is False
		for keyword in expr.keywords
	)


def _name_tail(expr: ast.expr) -> str:
	if isinstance(expr, ast.Name):
		return expr.id
	return expr.attr if isinstance(expr, ast.Attribute) else ""


def _is_dataclass_init_field(target: _Target) -> bool:
	if target.owner is None or not isinstance(cls := target.owner.node, ast.ClassDef):
		return False
	if not any(
		_name_tail(d.func if isinstance(d, ast.Call) else d) == "dataclass" and not _init_disabled(d)
		for d in cls.decorator_list
	):
		return False
	for member in cls.body:
		if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name == "__init__":
			return False
	field = next(
		(node for node in cls.body if isinstance(node, ast.AnnAssign) and node.lineno == target.position.line), None
	)
	if field is None or not isinstance(field.target, ast.Name) or field.target.id != target.name:
		return False
	annotation = field.annotation.value if isinstance(field.annotation, ast.Subscript) else field.annotation
	if _name_tail(annotation) == "ClassVar":
		return False
	return field.value is None or not _init_disabled(field.value)


async def _dataclass_keywords(
	session: Session, target: _Target, locations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
	if not _is_dataclass_init_field(target) or (owner := _owner_position(target)) is None:
		return []
	refs = await session.client.references(**owner.args(), include_declaration=False)
	refs, _ = split_inside_workspace(refs, session.workspace, tool_base.server()._location_uri)
	known = {_position(loc, session.workspace) for loc in locations}
	return [
		loc
		for loc in constructor_keyword_locations(refs, target.name)
		if _position(loc, session.workspace) not in known
	]


async def _subclasses(session: Session, target: _Target) -> list[tuple[_Position, str]]:
	owner = _owner_position(target)
	if owner is None:
		return []
	queue = deque(await session.client.prepare_type_hierarchy(**owner.args()))
	seen = {owner}
	found = []
	while queue:
		for subtype in await session.client.subtypes(queue.popleft()):
			uri = subtype.get("uri", "")
			if not uri.startswith("file:") or not uri_to_path(uri).resolve().is_relative_to(
				session.workspace.resolve()
			):
				continue
			site = _position(subtype, session.workspace)
			if site in seen:
				continue
			seen.add(site)
			queue.append(subtype)
			found.append((site, subtype["name"]))
	return sorted(found, key=lambda item: (item[0].file_path, item[0].line, item[0].column))


async def _member(
	session: Session, cls_name: str, position: _Position, name: str, *, inherited: bool
) -> _Position | None:
	try:
		member = await resolve_symbol(
			session.client, session.workspace, f"{cls_name}.{name}", file_path=position.file_path
		)
	except SymbolNotFoundError:
		return None
	site = _Position(uri_to_relative(member.uri, session.workspace), member.line + 1, member.column + 1)
	if inherited:
		return site
	if site.file_path == position.file_path:
		text = read_source(session.workspace / site.file_path)[0]
		owner = locate_member_class(text, site.line, name)
		if owner is not None and owner.def_line == position.line:
			return site
	return None


async def _related_rows(
	session: Session, classes: list[tuple[_Position, str]], target: _Target, *, inherited: bool
) -> list[tuple[_Position, str]]:
	if target.owner is None:
		return classes
	found = []
	for site, cls_name in classes:
		member = await _member(session, cls_name, site, target.name, inherited=inherited)
		if member is not None:
			found.append((member, f"{cls_name}.{target.name}"))
	return found


async def _protocol_implementations(session: Session, target: _Target, reply: Reply) -> list[tuple[_Position, str]]:
	srv = tool_base.server()
	owner = target.owner or target.definition
	if owner is None:
		return []
	port = ResolvedSymbol(
		owner.name,
		5,
		(session.workspace / target.position.file_path).as_uri(),
		owner.def_line - 1,
		0,
		owner.first_line - 1,
		owner.last_line - 1,
	)
	matches = await srv._protocol_matches(session.client, port)
	if note := srv._unscanned_note():
		reply.notes.append(note.strip())
	if matches.unreadable:
		reply.notes.append(f"{matches.unreadable} source files could not be checked for implementations.")
	if matches.not_probed:
		reply.notes.append(f"{matches.not_probed} implementation candidates exceeded the probe limit.")
	if matches.rejected:
		reply.notes.append(f"{len(matches.rejected)} candidates did not pass the type check.")
	classes = [
		(_Position(uri_to_relative(candidate.path.as_uri(), session.workspace), candidate.line, 1), candidate.name)
		for candidate in matches.verified
	]
	rows = await _related_rows(session, classes, target, inherited=True)
	return [(position, label + (" [stub]" if position.file_path.endswith(".pyi") else "")) for position, label in rows]


def _mention_row(mention: Mention, exact_strings: set[str] | None = None) -> _Row:
	kind = mention.kind
	if kind == "attr" and mention.column is not None:
		context = f"unlinked attribute (column {mention.column + 1})"
	elif mention.value:
		label = "exact dotted string" if exact_strings and mention.value in exact_strings else kind
		context = f"{label}  {mention.value}"
	else:
		context = kind
	return _Row(f"{mention.path}:{mention.line}", context)


async def _symbol_mentions(
	session: Session, target: _Target, locations: list[dict[str, Any]], reply: Reply
) -> list[_Row]:
	if not target.name:
		return []
	covered: dict[Path, set[tuple[int, int]]] = {}
	srv = tool_base.server()
	for loc in locations:
		uri = srv._location_uri(loc)
		if uri.startswith("file:"):
			covered.setdefault(uri_to_path(uri).resolve(), set()).add(srv._location_start(loc))
	mentions = find_mentions(session.workspace, target.name, covered, include_text=False)
	# No definition or a resolution to a Mock stub is an unlinked attribute. A definition of another real symbol
	# disqualifies it, even if its spelling matches (e.g. sqlite3.Connection.execute).
	attrs, _, unchecked = await _split_unlinked_attrs(
		session.client, session.workspace, [m for m in mentions if _is_unlinked_attr(m)], {}, limit=None
	)
	if unchecked:
		reply.notes.append(f"{unchecked} attribute accesses could not be checked.")
	exact_strings = set()
	if cls_or_def := target.owner or target.definition:
		qualname = target.definition.qualname if target.definition else f"{cls_or_def.qualname}.{target.name}"
	else:
		qualname = target.name
	for module in module_names_for(session.workspace / target.position.file_path, session.roots):
		exact_strings.add(f"{module}.{qualname}")
	return [_mention_row(m, exact_strings) for m in [*attrs, *(m for m in mentions if m.kind in {"string", "comment"})]]


async def _expand_callers(
	session: Session,
	target: _Target,
	direct: list[_Row],
	reply: Reply,
	args: dict[str, Any],
	limit: int,
	offset: int,
	implementations: list[tuple[_Position, str]] | None,
) -> None:
	if implementations is None or target.owner is None:
		_add_section(reply, "Callers", direct, "callers", args, limit, offset)
		resolved = target.resolved
		if resolved is not None and resolved.kind == 6:
			note = await tool_base.server()._static_callers_note(session.client, target.position.file_path, resolved)
			if note:
				reply.notes.append(note)
		return

	groups = [(f"Through {target.owner.qualname}.{target.name}", direct)]
	outside = 0
	for position, label in implementations:
		rows, omitted = await _call_rows(session, position)
		groups.append((f"To {label}", rows))
		outside += omitted
	_note_outside(reply, outside)

	# A whole section shares one offset. Repeat each group heading when a page starts inside it.
	grouped = [(label, len(rows), row) for label, rows in groups for row in rows]
	continued = ""

	def render(item: tuple[str, int, _Row]) -> str:
		nonlocal continued
		label, total, row = item
		heading = "" if label == continued else f"{label} ({total})\n"
		continued = label
		return heading + row.render()

	reply.items.append(f"Callers ({len(grouped)})")
	reply.items.extend(f"{label} (0)" for label, rows in groups if not rows and label.startswith("Through "))
	reply.add_page(paginate(grouped, limit, offset), render, "lookup", **args, show=["callers"], limit=limit)
	reply.items.extend(f"{label} (0)" for label, rows in groups[1:] if not rows)


def _definition_line(target: _Target, workspace: Path) -> str:
	places = [_position(loc, workspace) for loc in target.locations] or [target.position]
	return f"{target.label}  " + ", ".join(f"{p.file_path}:{p.line}:{p.column}" for p in places)


def _counts(reply: Reply, args: dict[str, Any], refs: int, callers: int, implementations: int | None) -> None:
	def expansion(section: str) -> str:
		return "next: " + call("lookup", **args, show=[section])

	caller_label = f"{callers} direct callers" if implementations is None else f"{callers} callers"
	impl_label = "implementations" if implementations is None else f"{implementations} implementations"
	reply.items.append(
		f"{refs} references ({expansion('references')}) · {caller_label} ({expansion('callers')}) · "
		f"{impl_label} ({expansion('implementations')})"
	)


async def _unverified_references(
	session: Session, target: _Target, locations: list[dict[str, Any]], reply: Reply
) -> None:
	coverage = ty_coverage(session.workspace)
	if coverage.include or coverage.exclude:
		reply.notes.append("Typed references omit files excluded by the ty configuration.")
	if not target.owner or not target.name:
		return
	known = {_position(loc, session.workspace) for loc in locations}
	candidates = [
		m
		for m in find_mentions(session.workspace, target.name, {}, include_text=False)
		if m.column is not None
		and _is_unlinked_attr(m)
		and _Position(m.path, m.line, m.column + 1) not in known
		and not coverage.excludes(session.workspace / m.path, session.workspace)
	]
	for m in candidates[:50]:
		path = session.workspace / m.path
		if await types_as_never(session.client, path, m.line, (m.column or 0) + 1):
			reply.notes.append(f"Unverified same-name use in code ty types as Never: {m.path}:{m.line}.")
	if len(candidates) > 50:
		reply.notes.append("Checked the first 50 unlinked same-name attributes for Never.")


async def _lookup(
	session: Session,
	name: str | None,
	file_path: str | None,
	line: int | None,
	column: int | None,
	show: list[str] | None,
	limit: int,
	offset: int,
) -> str:
	srv = tool_base.server()
	target = await _target(session, name, file_path, line, column)
	position = target.position.args()
	hover = srv._format_hover_contents((await session.client.hover(**position)).get("contents"))
	if not hover and not target.locations:
		if srv._on_definition_keyword(**position):
			raise ToolInputError("Put the cursor on the symbol name, not the 'def' or 'class' keyword.")
		raise ToolInputError("No symbol at that position. Put the cursor on the symbol name (1-based UTF-16 column).")
	default = show is None
	signature, doc = _signature_and_doc(hover, target.definition, brief=default)
	reply = Reply(_definition_line(target, session.workspace), [signature or "Signature unavailable."])
	sections = set(show or ())
	args = (
		{"name": name, "file_path": srv._workspace_display_path(file_path)}
		if name
		else {"file_path": srv._workspace_display_path(file_path), "line": line, "column": column}
	)
	if doc and (default or "doc" in sections):
		reply.items.append(doc)
	if not name and default and hover:
		enriched = await srv._enrich_bare_type(hover, session.client, **position)
		reply.notes.extend(line for line in enriched.splitlines()[1:] if line.startswith("Type defined at "))
	if not name and target.locations and await srv._is_unresolved_import(session.client, target.locations[0]):
		reply.notes.append("The import does not resolve.")
	if "source" in sections:
		if body := _source(target):
			reply.items.append(body)
		else:
			reply.notes.append("Source is unavailable for this symbol.")
	locations = []
	if default or sections & {"references", "mentions"}:
		locations = await session.client.references(**position, include_declaration=True)
		locations, outside = split_inside_workspace(locations, session.workspace, srv._location_uri)
		locations.extend(await _dataclass_keywords(session, target, locations))
		_note_outside(reply, outside)
	direct = []
	if default or "callers" in sections:
		direct, outside = await _call_rows(session, target.position)
		_note_outside(reply, outside)
	protocol = target.protocol
	nominal_implementations = not protocol and (default or "implementations" in sections)
	subclasses = []
	if sections & {"subclasses", "overrides"} or nominal_implementations:
		subclasses = await _subclasses(session, target)
	related = []
	if nominal_implementations or ("overrides" in sections and target.owner):
		related = await _related_rows(session, subclasses, target, inherited=False)
	if default:
		_counts(reply, args, len(locations), len(direct), None if protocol else len(related))
		return reply.render()
	if "references" in sections:
		await _unverified_references(session, target, locations, reply)
		locations.sort(key=lambda loc: _hit_key(loc, session.workspace))
		reply.items.append(f"References ({len(locations)})")
		reply.add_page(
			paginate(locations, limit, offset),
			_reference_renderer(target, session.workspace),
			"lookup",
			**args,
			show=["references"],
			limit=limit,
		)
	structural = None
	if protocol and ("implementations" in sections or ("callers" in sections and target.owner)):
		structural = await _protocol_implementations(session, target, reply)
	if "callers" in sections:
		await _expand_callers(session, target, direct, reply, args, limit, offset, structural)
		if not direct and structural is None and target.resolved and target.resolved.kind in {6, 12}:
			uses = await session.client.references(**position, include_declaration=False)
			if split_inside_workspace(uses, session.workspace, srv._location_uri)[0]:
				reply.notes.append(f"{target.label} has references but no static callers.")
				reply.next_calls.append(call("lookup", **args, show=["references"]))
	if "subclasses" in sections:
		rows = [_Row(f"{p.file_path}:{p.line}", label) for p, label in subclasses]
		_add_section(reply, "Subclasses", rows, "subclasses", args, limit, offset)
	if "overrides" in sections:
		rows = [_Row(f"{p.file_path}:{p.line}", label) for p, label in related] if target.owner else []
		_add_section(reply, "Overrides", rows, "overrides", args, limit, offset)
	if "implementations" in sections:
		rows = [_Row(f"{p.file_path}:{p.line}", label) for p, label in (related if structural is None else structural)]
		_add_section(reply, "Implementations", rows, "implementations", args, limit, offset)
	if "mentions" in sections:
		rows = await _symbol_mentions(session, target, locations, reply)
		_add_section(reply, "Mentions", rows, "mentions", args, limit, offset)
	return reply.render()


async def lookup(
	name: str | None = None,
	file_path: str | None = None,
	line: int | None = None,
	column: int | None = None,
	show: list[str] | None = None,
	limit: int = 20,
	offset: int = 0,
	ctx: Context | None = None,
) -> str:
	"""Use over grep for typed definitions and usages; name or file_path+line+column. show=[signature, doc, source, references, callers, implementations, subclasses, overrides, mentions]. Example: `lookup(name="Cart.total")`."""
	try:
		if name is not None:
			if problem := symbol_name_problem(name):
				raise ToolInputError(problem)
			if line is not None or column is not None:
				raise ToolInputError("Choose name with optional file_path, or file_path with line and column.")
		elif not file_path or line is None or column is None:
			raise ToolInputError("Give name, or file_path with 1-based line and column.")
		if limit < 1 or offset < 0:
			raise ToolInputError("limit must be at least 1 and offset at least 0.")
		if unknown := set(show or ()) - _SECTIONS:
			raise ToolInputError(
				f"Unknown show section: {', '.join(sorted(unknown))}. Valid: {', '.join(sorted(_SECTIONS))}."
			)
		session = await tool_base.open_session(ctx)
		if file_path is not None:
			tool_base.server()._check_python_file(file_path)
			if ty_coverage(session.workspace).excludes(session.workspace / file_path, session.workspace):
				raise ToolInputError(f"{file_path} is excluded by the ty configuration.")
		return await _lookup(session, name, file_path, line, column, show, limit, offset)
	except TOOL_ERRORS as exc:
		return _ambiguity(exc, name, file_path, show, limit, offset) or _refusal(exc, lookup)


async def search(
	query: str,
	kind: str | None = None,
	path: str | None = None,
	scope: Literal["symbols", "mentions"] = "symbols",
	limit: int = 30,
	offset: int = 0,
	ctx: Context | None = None,
) -> str:
	"""Use over grep for Python symbols (scope="symbols", kind=class|function|...) or grouped text mentions (scope="mentions", kind=string|comment|text). Example: `search(query="Cart.total")`."""
	srv = tool_base.server()
	try:
		if not query.strip():
			raise ToolInputError("query must be nonempty.")
		if limit < 1 or offset < 0:
			raise ToolInputError("limit must be at least 1 and offset at least 0.")
		session = await tool_base.open_session(ctx)
		path = srv._workspace_relative(path) if path else path
		if path:
			srv._check_search_path(path)
		args = {"query": query, "kind": kind, "path": path, "scope": scope, "limit": limit}
		if scope == "mentions":
			kinds = {part.strip().lower() for part in kind.split(",")} if kind else _TEXT_MENTION_KINDS
			if kinds - _TEXT_MENTION_KINDS:
				raise ToolInputError("For mention search, kind is string, comment, or text.")
			mentions = find_mentions(
				session.workspace,
				query,
				{},
				include_text="text" in kinds,
				text_file_limit=None,
				path_matches=(lambda rel: _path_filter_matches(rel, path)) if path else None,
			)
			found = [mention for mention in mentions if mention.kind in kinds]
			reply = Reply(f"{len(found)} mentions of {query!r}.")
			group = ""

			def render_mention(mention: Mention) -> str:
				nonlocal group
				heading = f"{mention.kind.title()}\n" if mention.kind != group else ""
				group = mention.kind
				return heading + _mention_row(mention).render()

			reply.add_page(paginate(found, limit, offset), render_mention, "search", **args)
		elif scope == "symbols":
			kinds = parse_kind_filter(kind)
			qualifier, leaf = srv._split_dotted(srv._nfkc(query))
			symbols, outside, skipped = await srv._search_symbols(session.client, qualifier, leaf)
			symbols = filter_symbols_by_kind_and_path(symbols, session.workspace, kinds=kinds, path=path)
			matching = [sym for sym in symbols if leaf.casefold() in str(sym.get("name") or "").casefold()]
			matching = filter_workspace_symbols(rank_workspace_symbols(matching, leaf))
			reply = Reply(f"{len(matching)} symbols match {query!r}.", notes=srv._search_notes(outside, skipped))
			if not matching and symbols:
				reply.notes.append("No exact match among ty's results. Try a shorter query or broaden the filters.")

			def render(sym: dict[str, Any]) -> str:
				uri, line, col = workspace_symbol_position(sym)
				rel = srv._display_path(uri, session.workspace)
				return f"{rel}:{line + 1}  {sym['name']} [{symbol_kind_label(sym.get('kind'))}], column {col + 1}"

			reply.add_page(paginate(matching, limit, offset), render, "search", **args)
		else:
			raise ToolInputError("scope must be symbols or mentions.")
		return reply.render()
	except TOOL_ERRORS as exc:
		return _refusal(exc, search)


def _read_toml(path: Path) -> dict[str, Any]:
	try:
		return tomllib.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, tomllib.TOMLDecodeError):
		return {}


def _python_summary(workspace: Path) -> str:
	project = _read_toml(workspace / "pyproject.toml")
	config = next((workspace / name for name in ("ty.toml", ".ty.toml") if (workspace / name).is_file()), None)
	tools = project.get("tool")
	settings = _read_toml(config) if config else (tools.get("ty") if isinstance(tools, dict) else None)
	environment = settings.get("environment", {}) if isinstance(settings, dict) else {}
	if isinstance(environment, dict) and isinstance(version := environment.get("python-version"), str):
		return f"Python: {version} (ty configuration)"
	metadata = project.get("project")
	if isinstance(metadata, dict) and isinstance(requires := metadata.get("requires-python"), str):
		return f"Python: {requires} (project requirement)"
	return "Python: inferred by ty"


def _run_ty(workspace: Path, command: list[str], *args: str) -> subprocess.CompletedProcess[str]:
	try:
		return subprocess.run(  # noqa: S603
			[*command, *args], cwd=workspace, capture_output=True, text=True, timeout=60, check=False
		)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise ToolInputError(f"ty could not run: {exc}") from exc


async def _check_workspace(limit: int, offset: int) -> str:
	srv = tool_base.server()
	workspace = srv.WORKSPACE_ROOT
	command = resolve_ty_command(workspace)[:-1]  # replace the server subcommand
	metadata = srv._workspace_details()
	metadata.append(_python_summary(workspace))
	metadata.extend(f"Extra source root: {root.relative_to(workspace).as_posix()}" for root in srv.EXTRA_SOURCE_ROOTS)
	result = await asyncio.to_thread(
		_run_ty, workspace, command, "check", "--output-format", "concise", "--color", "never", "--no-progress"
	)
	if result.returncode not in {0, 1}:
		reason = srv._scrub_text((result.stderr.strip() or result.stdout.strip())[:600])
		reply = Reply(f"Workspace type check failed: {reason}", notes=metadata)
		reply.items.append(f"Example: {call('check', file_path='src/app.py')}")
		return FailedText(reply.render())
	version = await asyncio.to_thread(_run_ty, workspace, command, "--version")
	if version.returncode == 0:
		metadata.append(f"ty version: {version.stdout.strip().removeprefix('ty ')}")
	lines = srv._scrub_text(result.stdout).splitlines()
	diagnostics = [line for line in lines if re.match(r"^(?:.+?: )?(?:error|fatal|warning|info)(?:\[|:)", line)]
	error_count = sum(bool(re.match(r"^(?:.+?: )?(?:error|fatal)(?:\[|:)", line)) for line in diagnostics)
	reply = Reply(
		f"Workspace type check: {error_count} error(s), {len(diagnostics)} diagnostic(s) - {NOT_CHECKED}",
		notes=metadata,
	)
	reply.add_page(paginate(diagnostics, limit, offset), str, "check", limit=limit)
	return reply.render()


async def check(
	file_path: str | None = None,
	since: str | None = None,
	limit: int = 30,
	offset: int = 0,
	include_dependents: bool = True,
	ctx: Context | None = None,
) -> str:
	"""Check ty diagnostics for a file, workspace, or changes since git. Example: `check(since="HEAD")`."""
	srv = tool_base.server()
	try:
		paginate([], limit, offset)  # validate before either checker runs
		if file_path is not None and since is not None:
			raise ToolInputError("give either file_path or since")
		if since is not None:
			result = await write_tools._check_changes(
				since, include_dependents=include_dependents, limit=limit, offset=offset, ctx=ctx
			)
			if isinstance(result, FailedText) and "\nExample: " not in result:
				return FailedText(f"{result}\nExample: {call('check', since='HEAD')}")
			return result
		await srv._use_workspace(ctx)
		if file_path is None:
			return await _check_workspace(limit, offset)
		srv._check_python_file(file_path)
		rel = srv._workspace_display_path(file_path)
		workspace = srv.WORKSPACE_ROOT
		if ty_coverage(workspace).excludes(workspace / file_path, workspace):
			return Reply(f"{rel} was excluded by the ty configuration.").render()
		client = await srv.get_client()
		entries = entries_from_lsp(rel, "", await client.diagnostics(file_path))
		error_count = sum(entry.is_error for entry in entries)
		reply = Reply(
			f"{rel}: {error_count} error(s), {len(entries)} diagnostic(s) - {NOT_CHECKED}"
			if entries
			else f"No diagnostics in {rel} - {NOT_CHECKED}."
		)
		reply.add_page(
			paginate(entries, limit, offset),
			lambda entry: srv._scrub_text(entry.render()),
			"check",
			file_path=rel,
			limit=limit,
		)
		return reply.render()
	except TOOL_ERRORS + (ValueError,) as exc:
		return _refusal(exc, check)


TOOLS = [(lookup, tool_base.READS), (search, tool_base.READS), (check, tool_base.READS)]
