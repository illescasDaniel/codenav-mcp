"""Members that must be renamed together with the one the caller picked.

`ty`'s rename follows references to one declaration. Renaming `Base.run`
leaves `Child.run` (an override) and `super().run()` behind, and renaming a
`Protocol` method leaves its structural implementers behind, which silently
breaks the override or the conformance. This module finds those companions:

- the class hierarchy around the member's class (ty's type hierarchy, both ways),
- `Protocol` ports and the classes that satisfy them (the same type-checker
  probe `implementations` uses),
- `super().name` accesses inside the classes involved.
"""

from __future__ import annotations

import ast
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from mcp_nav_shared.edits import TextEdit, read_source
from mcp_nav_shared.errors import TOOL_ERRORS
from mcp_nav_shared.format import uri_to_path
from mcp_nav_shared.lsp_client import LspClient

from codenav_mcp.deps import iter_python_files
from codenav_mcp.pysource import (
	Definition,
	PythonSyntaxError,
	byte_to_utf16_column,
	definition_name_position,
	iter_definitions,
	node_position,
	parameter_position,
	parse_python,
	source_lines,
)


MAX_CLASSES = 64
MAX_PROBES = 60


@dataclass(frozen=True)
class ClassRef:
	path: Path
	name: str
	def_line: int  # 1-based line of the `class` statement

	def label(self, workspace: Path) -> str:
		try:
			rel = self.path.relative_to(workspace.resolve()).as_posix()
		except ValueError:
			rel = self.path.as_posix()
		return f"{self.name} ({rel}:{self.def_line})"


@dataclass(frozen=True)
class MemberSite:
	"""Where a linked declaration's identifier is (1-based, UTF-16 column)."""

	cls: ClassRef
	line: int
	column: int


@dataclass
class LinkedMembers:
	sites: list[MemberSite] = field(default_factory=list)
	super_edits: dict[Path, list[TextEdit]] = field(default_factory=dict)
	notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _ClassInfo:
	ref: ClassRef
	bases: tuple[str, ...]
	is_protocol: bool
	members: frozenset[str]
	node: ast.ClassDef


def _base_name(base: ast.expr) -> str | None:
	if isinstance(base, ast.Subscript):
		base = base.value
	if isinstance(base, ast.Name):
		return base.id
	if isinstance(base, ast.Attribute):
		return base.attr
	return None


def _own_members(cls: ast.ClassDef) -> frozenset[str]:
	names: set[str] = set()
	for stmt in cls.body:
		if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
			names.add(stmt.name)
			for node in ast.walk(stmt):
				if (
					isinstance(node, ast.Attribute)
					and isinstance(node.ctx, ast.Store)
					and isinstance(node.value, ast.Name)
					and node.value.id == "self"
				):
					names.add(node.attr)
		elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
			names.add(stmt.target.id)
		elif isinstance(stmt, ast.Assign):
			names.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
	return frozenset(names)


_scan_cache: dict[Path, tuple[tuple[int, int], list[_ClassInfo]]] = {}


def classes_in_file(path: Path) -> list[_ClassInfo]:
	try:
		stat = path.stat()
	except OSError:
		return []
	key = (stat.st_mtime_ns, stat.st_size)
	hit = _scan_cache.get(path)
	if hit is not None and hit[0] == key:
		return hit[1]
	try:
		text = read_source(path)[0]
		tree = parse_python(text, str(path))
	except (OSError, UnicodeDecodeError, ValueError, PythonSyntaxError):
		return []  # unreadable or broken files have no classes to offer
	lines = source_lines(text)
	infos = []
	for definition in iter_definitions(tree, lines):
		if not isinstance(definition.node, ast.ClassDef):
			continue
		node = definition.node
		infos.append(
			_ClassInfo(
				ref=ClassRef(path.resolve(), node.name, definition.def_line),
				bases=tuple(n for n in map(_base_name, node.bases) if n),
				is_protocol=any(_base_name(b) == "Protocol" for b in node.bases),
				members=_own_members(node),
				node=node,
			)
		)
	_scan_cache[path] = (key, infos)
	return infos


class _Index:
	"""Every class of the workspace, by name and by location."""

	def __init__(self, workspace: Path) -> None:
		self.infos: list[_ClassInfo] = []
		for path in iter_python_files(workspace):
			self.infos.extend(classes_in_file(path))
		self.by_name: dict[str, list[_ClassInfo]] = {}
		self.by_ref: dict[ClassRef, _ClassInfo] = {}
		for info in self.infos:
			self.by_name.setdefault(info.ref.name, []).append(info)
			self.by_ref[info.ref] = info

	def effective_members(self, info: _ClassInfo) -> set[str]:
		members = set(info.members)
		seen = {info.ref.name}
		pending = list(info.bases)
		while pending:
			base = pending.pop()
			if base in seen:
				continue
			seen.add(base)
			for other in self.by_name.get(base, []):
				members |= other.members
				pending.extend(other.bases)
		return members


def locate_member_class(text: str, line: int, name: str) -> Definition | None:
	"""The class whose member `name` is declared on `line` (a method, a class-level
	field, or a `self.name = ...` assignment in one of its methods)."""
	tree = parse_python(text)
	lines = source_lines(text)
	best: Definition | None = None
	for definition in iter_definitions(tree, lines):
		if not isinstance(definition.node, ast.ClassDef):
			continue
		for stmt in definition.node.body:
			if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
				if stmt.name == name and stmt.lineno == line:
					best = definition
				for inner in ast.walk(stmt):
					if (
						isinstance(inner, ast.Attribute)
						and inner.attr == name
						and inner.lineno == line
						and isinstance(inner.ctx, ast.Store)
						and isinstance(inner.value, ast.Name)
						and inner.value.id == "self"
					):
						best = definition
			elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
				if stmt.target.id == name and stmt.lineno == line:
					best = definition
			elif isinstance(stmt, ast.Assign):
				if any(isinstance(t, ast.Name) and t.id == name for t in stmt.targets) and stmt.lineno == line:
					best = definition
	return best


def _member_site(info: _ClassInfo, text: str, name: str, parameter: str | None) -> MemberSite | None:
	"""Position of `name` (or of its `parameter`) as declared directly in the class."""
	lines = source_lines(text)
	for stmt in info.node.body:
		if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == name:
			definition = next(
				d for d in iter_definitions(parse_python(text), lines) if d.def_line == stmt.lineno and d.name == name
			)
			if parameter is not None:
				pos = parameter_position(lines, definition, parameter)
				return MemberSite(info.ref, *pos) if pos else None
			return MemberSite(info.ref, *definition_name_position(lines, definition))
	if parameter is not None:
		return None
	for stmt in info.node.body:
		if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.target.id == name:
			return MemberSite(info.ref, *node_position(lines, stmt.target))
		if isinstance(stmt, ast.Assign):
			for target in stmt.targets:
				if isinstance(target, ast.Name) and target.id == name:
					return MemberSite(info.ref, *node_position(lines, target))
	for stmt in info.node.body:
		if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
			for inner in ast.walk(stmt):
				if isinstance(inner, ast.Attribute) and inner.attr == name and isinstance(inner.ctx, ast.Store):
					if isinstance(inner.value, ast.Name) and inner.value.id == "self":
						line = lines[inner.lineno - 1]
						end_col = byte_to_utf16_column(line, inner.end_col_offset or 0)
						return MemberSite(info.ref, inner.end_lineno or inner.lineno, end_col - len(name) + 1)
	return None


def _super_edits(info: _ClassInfo, text: str, old: str, new: str) -> list[TextEdit]:
	"""`super().old` inside the class: ty doesn't link it to the base class's member."""
	lines = source_lines(text)
	edits = []
	for node in ast.walk(info.node):
		if (
			isinstance(node, ast.Attribute)
			and node.attr == old
			and isinstance(node.value, ast.Call)
			and isinstance(node.value.func, ast.Name)
			and node.value.func.id == "super"
			and node.end_lineno is not None
			and node.end_col_offset is not None
		):
			line_text = lines[node.end_lineno - 1]
			end = byte_to_utf16_column(line_text, node.end_col_offset)
			start = end - len(old)
			edits.append(TextEdit(node.end_lineno - 1, start, node.end_lineno - 1, end, new))
	return edits


Conforms = Callable[[ClassRef, ClassRef], Awaitable[bool]]


async def _nominal_neighbours(client: LspClient, workspace: Path, info: _ClassInfo, index: _Index) -> list[ClassRef]:
	lines = source_lines(read_source(info.ref.path)[0])
	definition = Definition(
		node=info.node,
		name=info.ref.name,
		qualname=info.ref.name,
		kind="class",
		first_line=info.node.lineno,
		def_line=info.ref.def_line,
		last_line=info.node.end_lineno or info.ref.def_line,
		indent="",
		parents=(),
	)
	line, column = definition_name_position(lines, definition)
	try:
		items = await client.prepare_type_hierarchy(str(info.ref.path), line, column)
		related = []
		for item in items[:1]:
			related += await client.supertypes(item)
			related += await client.subtypes(item)
	except TOOL_ERRORS:
		return []
	refs = []
	for item in related:
		uri = str(item.get("uri") or "")
		if not uri.startswith("file:"):
			continue
		path = uri_to_path(uri).resolve()
		try:
			path.relative_to(workspace.resolve())
		except ValueError:
			continue
		sel = (item.get("selectionRange") or item.get("range") or {}).get("start") or {}
		ref = ClassRef(path, str(item.get("name")), int(sel.get("line", 0)) + 1)
		if ref in index.by_ref:
			refs.append(ref)
	return refs


async def find_linked(
	client: LspClient,
	workspace: Path,
	*,
	origin_path: Path,
	member_line: int,
	member_name: str,
	new_name: str,
	parameter: str | None = None,
	conforms: Conforms | None = None,
) -> LinkedMembers:
	"""Declarations related to the member at `member_line` of `origin_path`, besides the member itself."""
	result = LinkedMembers()
	origin_text = read_source(origin_path)[0]
	container = locate_member_class(origin_text, member_line, member_name)
	if container is None:
		return result  # a module-level function or a local: nothing to link
	index = _Index(workspace)
	start = ClassRef(origin_path.resolve(), container.name, container.def_line)
	if start not in index.by_ref:
		return result
	visited: dict[ClassRef, str] = {start: "origin"}
	queue = [start]
	probes = 0
	while queue and len(visited) < MAX_CLASSES:
		current = queue.pop(0)
		info = index.by_ref[current]
		neighbours: list[tuple[ClassRef, str]] = [
			(r, "hierarchy") for r in await _nominal_neighbours(client, workspace, info, index)
		]
		if conforms is not None:
			if info.is_protocol:
				required = index.effective_members(info)
				for other in index.infos:
					if other.ref in visited or other.is_protocol or member_name not in index.effective_members(other):
						continue
					if not required <= index.effective_members(other) or probes >= MAX_PROBES:
						continue
					probes += 1
					if await conforms(info.ref, other.ref):
						neighbours.append((other.ref, "implements"))
			else:
				mine = index.effective_members(info)
				for other in index.infos:
					if other.ref in visited or not other.is_protocol or member_name not in other.members:
						continue
					if not index.effective_members(other) <= mine or probes >= MAX_PROBES:
						continue
					probes += 1
					if await conforms(other.ref, info.ref):
						neighbours.append((other.ref, "port"))
		for ref, how in neighbours:
			if ref not in visited and ref in index.by_ref:
				visited[ref] = how
				queue.append(ref)
	if len(visited) >= MAX_CLASSES:
		result.notes.append(f"stopped exploring related classes after {MAX_CLASSES}")
	for ref, how in visited.items():
		info = index.by_ref[ref]
		text = read_source(ref.path)[0]
		for edit in _super_edits(info, text, member_name, new_name) if parameter is None else []:
			result.super_edits.setdefault(ref.path, []).append(edit)
		if ref == start:
			continue
		site = _member_site(info, text, member_name, parameter)
		if site is not None:
			result.sites.append(site)
			result.notes.append(
				f"{'parameter ' + parameter + ' of ' if parameter else ''}{ref.name}.{member_name} renamed too ({how}: {ref.path.name}:{site.line})"
			)
	return result
