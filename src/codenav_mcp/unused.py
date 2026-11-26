"""Find workspace definitions with no linked production uses."""

from __future__ import annotations

import ast
import asyncio
import io
import tokenize
import tomllib
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from mcp_nav_shared.errors import TOOL_ERRORS
from mcp_nav_shared.format import _is_test_path, uri_to_path

from codenav_mcp.deps import import_from_targets, iter_python_files, module_names_for, primary_module_name
from codenav_mcp.linked import ClassRef, Conforms, _base_name, _declared_members, _own_members
from codenav_mcp.pysource import (
	_child_bodies,
	byte_to_utf16_column,
	iter_definitions,
	node_position,
	source_lines,
	utf16_column,
)
from codenav_mcp.tool_base import Session
from codenav_mcp.write_tools import conforms_probe


KINDS = frozenset({"class", "constant", "function", "method"})
_REGISTRATION = frozenset({
	"abstractmethod", "abstractclassmethod", "abstractstaticmethod", "fixture", "yield_fixture",
	"route", "api_route", "websocket", "tool", "resource", "prompt", "register", "command",
	"group", "callback", "event", "listener", "receiver", "hookimpl", "Slot", "pyqtSlot", "Property",
	"pyqtProperty", "work", "on", "setter", "deleter", "overload", "override", "atexit",
})  # fmt: skip
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "options", "head", "trace"})
_REFLECTION = frozenset({"getattr", "setattr", "hasattr", "delattr", "attrgetter", "methodcaller"})
Position = tuple[int, int]  # 1-based UTF-16 coordinates
Binding = tuple[str, str]  # (module, name)


@dataclass(frozen=True)
class Candidate:
	path: str
	name: str
	qualname: str
	kind: str
	position: Position
	start: Position
	end: Position
	owner: ClassRef | None = None

	@property
	def line(self) -> int:
		return self.position[0]


@dataclass(frozen=True)
class Finding:
	candidate: Candidate
	confidence: str

	def render(self) -> str:
		c = self.candidate
		label = "exported, unused inside the project" if self.confidence == "exported" else self.confidence
		return f"{c.path}:{c.line}  {c.qualname} ({c.kind}, {label})"


@dataclass
class Scan:
	path: str
	counts: Counter[str] = field(default_factory=Counter)
	candidates: list[Candidate] = field(default_factory=list)
	declarations: dict[tuple[str, str], list[Candidate]] = field(default_factory=lambda: defaultdict(list))
	classes: dict[ClassRef, ClassInfo] = field(default_factory=dict)
	imports: list[tuple[ast.ImportFrom, list[tuple[Position, Position]]]] = field(default_factory=list)
	exports: set[str] = field(default_factory=set)
	dotted_strings: set[str] = field(default_factory=set)
	reflective_strings: set[str] = field(default_factory=set)
	textual_actions: set[str] = field(default_factory=set)

	def add(self, candidate: Candidate, *, eligible: bool = True) -> None:
		self.declarations[(candidate.kind, candidate.qualname)].append(candidate)
		if eligible:
			self.candidates.append(candidate)


@dataclass(frozen=True)
class ClassInfo:
	ref: ClassRef
	position: Position
	bases: tuple[str, ...]
	protocol: bool
	textual: bool
	members: frozenset[str]


def _statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
	"""Bindings in this scope, including conditional definitions and imports."""
	for stmt in body:
		yield stmt
		if not isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
			for branch in _child_bodies(stmt):
				yield from _statements(branch)


def _dotted(node: ast.expr) -> str:
	if isinstance(node, ast.Name):
		return node.id
	if isinstance(node, ast.Attribute):
		parent = _dotted(node.value)
		return f"{parent}.{node.attr}" if parent else node.attr
	return ""


def _bindings(tree: ast.Module) -> dict[str, str]:
	bindings = {}
	for node in _statements(tree.body):
		if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
			bindings.update({alias.asname or alias.name: f"{node.module}.{alias.name}" for alias in node.names})
		elif isinstance(node, ast.Import):
			bindings.update(
				{
					(alias.asname or alias.name.split(".")[0]): (
						alias.name if alias.asname else alias.name.split(".")[0]
					)
					for alias in node.names
				}
			)
	return bindings


def _resolve_dotted(name: str, bindings: dict[str, str]) -> str:
	root, dot, tail = name.partition(".")
	return bindings.get(root, root) + dot + tail


def _end_position(lines: list[str], node: ast.stmt | ast.expr | ast.alias) -> Position:
	line = node.end_lineno or node.lineno
	column = byte_to_utf16_column(lines[line - 1], node.end_col_offset or 0)
	return line, column + 1


def _registered(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, bindings: dict[str, str]) -> bool:
	for decorator in node.decorator_list:
		target = decorator.func if isinstance(decorator, ast.Call) else decorator
		dotted = _resolve_dotted(_dotted(target), bindings)
		name = dotted.rsplit(".", 1)[-1]
		if name in _REGISTRATION or dotted == "atexit.register":
			return True
		if isinstance(target, ast.Attribute) and isinstance(decorator, ast.Call) and name in _HTTP_METHODS:
			return True
	return False


def _test_hook(path: str, name: str, *, test_class: bool, pytest_plugin: bool) -> bool:
	if pytest_plugin and name.startswith(("pytest_", "pytest_xdist_")):
		return True
	return _is_test_path(path) and (
		name.startswith("test_")
		or test_class
		or name
		in {
			"setUp",
			"tearDown",
			"setUpClass",
			"tearDownClass",
			"setUpModule",
			"tearDownModule",
			"setup_method",
			"teardown_method",
			"setup_class",
			"teardown_class",
			"setup_module",
			"teardown_module",
			"setup_function",
			"teardown_function",
		}
	)


def _literal_names(node: ast.expr) -> Iterator[str]:
	if isinstance(node, ast.Constant) and isinstance(node.value, str):
		yield node.value
	elif isinstance(node, (ast.List, ast.Tuple)):
		for item in node.elts:
			yield from _literal_names(item)
	elif isinstance(node, ast.Starred):
		yield from _literal_names(node.value)
	elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
		yield from _literal_names(node.left)
		yield from _literal_names(node.right)


def _metadata(scan: Scan, tree: ast.Module, lines: list[str], bindings: dict[str, str]) -> None:
	for stmt in _statements(tree.body):
		if isinstance(stmt, ast.ImportFrom):
			scan.imports.append((stmt, [(node_position(lines, a), _end_position(lines, a)) for a in stmt.names]))
		if isinstance(stmt, ast.Assign):
			targets, value = stmt.targets, stmt.value
		elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
			targets, value = [stmt.target], stmt.value
		else:
			targets, value = [], None
		for target in targets:
			if not isinstance(target, ast.Name):
				continue
			if target.id == "__all__" and value is not None:
				scan.exports.update(_literal_names(value))
			elif target.id.isupper() and not (target.id.startswith("__") and target.id.endswith("__")):
				pos = node_position(lines, target)
				scan.add(Candidate(scan.path, target.id, target.id, "constant", pos, pos, pos))
		if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
			if _dotted(stmt.value.func) in {"__all__.append", "__all__.extend"}:
				for arg in stmt.value.args:
					scan.exports.update(_literal_names(arg))

	for node in ast.walk(tree):
		if isinstance(node, ast.Constant) and isinstance(node.value, str):
			parts = node.value.split(".")
			if len(parts) > 1 and all(part.isidentifier() for part in parts):
				scan.dotted_strings.add(node.value)
		elif isinstance(node, ast.Call):
			name = _resolve_dotted(_dotted(node.func), bindings)
			leaf = name.rsplit(".", 1)[-1]
			index = 0 if leaf in {"attrgetter", "methodcaller"} else 1
			if (leaf in _REFLECTION or leaf == "object" and "patch" in name) and len(node.args) > index:
				argument = node.args[index]
				if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
					scan.reflective_strings.add(argument.value)
			if name == "textual.binding.Binding":
				action = (
					node.args[1]
					if len(node.args) > 1
					else next(
						(kw.value for kw in node.keywords if kw.arg == "action"),
						None,
					)
				)
				if isinstance(action, ast.Constant) and isinstance(action.value, str):
					action_name = action.value.partition("(")[0].rsplit(".", 1)[-1].strip()
					scan.textual_actions.add(f"action_{action_name}")
		elif isinstance(node, ast.FormattedValue):
			# 3.11's tokenizer emits an entire f-string as one STRING token.
			for child in ast.walk(node.value):
				if isinstance(child, ast.Name):
					scan.counts[child.id] += 1
				elif isinstance(child, ast.Attribute):
					scan.counts[child.attr] += 1


def _stamp(path: Path) -> tuple[int, int, int]:
	stat = path.stat()
	return stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size


@lru_cache(maxsize=2048)
def _scan_file(path: Path, rel: str, stamp: tuple[int, int, int]) -> Scan:
	with tokenize.open(path) as source:
		text = source.read()
	tree = ast.parse(text, filename=rel)
	lines = source_lines(text)
	tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
	scan = Scan(rel)
	scan.counts.update(unicodedata.normalize("NFKC", t.string) for t in tokens if t.type == tokenize.NAME)
	# AST positions point to 'class', 'def' or 'async', not its name. Using
	# the token also handles escaped newlines and normalized Unicode names.
	positions: dict[Position, Position] = {}
	for index, token in enumerate(tokens):
		if token.string not in {"class", "def", "async"} or token.type != tokenize.NAME:
			continue
		name_index = index + (2 if token.string == "async" else 1)
		if name_index >= len(tokens) or tokens[name_index].type != tokenize.NAME:
			continue
		row, col = token.start
		name_row, name_col = tokens[name_index].start
		positions[(row, utf16_column(lines[row - 1], col) + 1)] = (
			name_row,
			utf16_column(lines[name_row - 1], name_col) + 1,
		)

	bindings = _bindings(tree)
	_metadata(scan, tree, lines, bindings)
	pytest_plugin = Path(rel).name == "conftest.py" or any(name.split(".")[0] == "pytest" for name in bindings.values())
	for definition in iter_definitions(tree, lines):
		node, name = definition.node, definition.name
		start = node_position(lines, node)
		if start not in positions:
			continue
		pos = positions[start]
		if isinstance(node, ast.ClassDef) and not definition.parents:
			bases = tuple(b for b in map(_base_name, node.bases) if b)
			dotted_bases = {
				_resolve_dotted(_dotted(base.value if isinstance(base, ast.Subscript) else base), bindings)
				for base in node.bases
			}
			protocol = bool(dotted_bases & {"typing.Protocol", "typing_extensions.Protocol"})
			ref = ClassRef(path, name, definition.def_line)
			textual = any(base.startswith("textual.") for base in dotted_bases)
			scan.classes[ref] = ClassInfo(ref, pos, bases, protocol, textual, _own_members(node))
		if definition.parents and (
			len(definition.parents) != 1 or not isinstance(definition.parent, ast.ClassDef) or definition.is_class
		):
			continue
		excluded = (
			(name.startswith("__") and name.endswith("__"))
			or _registered(node, bindings)
			or _test_hook(
				rel,
				name,
				test_class=definition.is_class and name.startswith("Test"),
				pytest_plugin=pytest_plugin and not definition.parents,
			)
		)
		owner = ClassRef(path, definition.parent.name, definition.parent.lineno) if definition.parent else None
		scan.add(
			Candidate(rel, name, definition.qualname, definition.kind, pos, start, _end_position(lines, node), owner),
			eligible=not excluded,
		)
	scan.candidates.sort(key=lambda c: c.position)
	return scan


def _scan_workspace(workspace: Path) -> tuple[list[Scan], list[str]]:
	scans, skipped = [], []
	for path in sorted(iter_python_files(workspace)):
		rel = path.relative_to(workspace).as_posix()
		if path.is_symlink() or any(part.startswith(".") for part in path.relative_to(workspace).parts):
			continue
		try:
			scans.append(_scan_file(path, rel, _stamp(path)))
		except (OSError, UnicodeError, SyntaxError, LookupError, tokenize.TokenError, RecursionError, ValueError):
			skipped.append(rel)
	return scans, skipped


def inventory(workspace: Path) -> tuple[list[Candidate], Counter[str]]:
	"""Collect candidate declarations and name counts."""
	scans, _ = _scan_workspace(workspace.resolve())
	return [c for scan in scans for c in scan.candidates], _counts(scans)


def _counts(scans: list[Scan]) -> Counter[str]:
	counts: Counter[str] = Counter()
	for scan in scans:
		counts.update(scan.counts)
	return counts


def _entry_points(workspace: Path) -> tuple[set[Binding], str | None]:
	path = workspace / "pyproject.toml"
	try:
		with path.open("rb") as handle:
			project = tomllib.load(handle).get("project", {})
	except FileNotFoundError:
		return set(), None
	except (OSError, ValueError):
		return set(), "Could not read entry points from pyproject.toml."
	if not isinstance(project, dict):
		return set(), None
	tables = [project.get("scripts"), project.get("gui-scripts")]
	groups = project.get("entry-points")
	if isinstance(groups, dict):
		tables.extend(groups.values())
	entries = set()
	for table in tables:
		if isinstance(table, dict):
			for value in table.values():
				if isinstance(value, str) and ":" in value:
					module, attr = value.split(":", 1)
					entries.add((module.strip(), attr.split("[", 1)[0].strip()))
	return entries, None


@dataclass
class Exports:
	bindings: set[Binding] = field(default_factory=set)
	reexports: dict[str, list[tuple[Position, Position]]] = field(default_factory=lambda: defaultdict(list))


def _exports(scans: list[Scan], session: Session) -> Exports:
	"""Follow explicit re-exports through absolute/relative imports, including aliases."""
	result = Exports()
	origins: dict[Binding, list[tuple[Binding, str, tuple[Position, Position]]]] = defaultdict(list)
	for scan in scans:
		path = session.workspace / scan.path
		module = primary_module_name(path, session.roots)
		if module is None:
			continue
		result.bindings.update((module, name) for name in scan.exports)
		for stmt, spans in scan.imports:
			targets = import_from_targets(
				stmt,
				{module},
				is_package_init=path.stem == "__init__",
			)
			for alias, span in zip(stmt.names, spans, strict=True):
				for target in targets:
					origins[(module, alias.asname or alias.name)].append(
						((target, alias.name), scan.path, span),
					)
	queue = list(result.bindings)
	while queue:
		for source, path, span in origins.get(queue.pop(), []):
			result.reexports[path].append(span)
			if source not in result.bindings:
				result.bindings.add(source)
				queue.append(source)
	return result


class ClassUses:
	"""Share hierarchy walks and Protocol conformance checks among methods of the same class."""

	def __init__(self, session: Session, scans: list[Scan]) -> None:
		self.session = session
		self.classes = {ref: info for scan in scans for ref, info in scan.classes.items()}
		self.by_location = {(ref.path, ref.def_line): info for ref, info in self.classes.items()}
		self.classes_by_name: dict[str, list[ClassInfo]] = defaultdict(list)
		for info in self.classes.values():
			self.classes_by_name[info.ref.name].append(info)
		self.protocols = [info for info in self.classes.values() if info.protocol]
		self.conforms: Conforms = conforms_probe(session)
		self._ancestors: dict[ClassRef, tuple[set[str], set[ClassRef]]] = {}
		self._conformance: dict[tuple[ClassRef, ClassRef], bool] = {}
		self._external_members: dict[tuple[Path, int], frozenset[str] | None] = {}
		self._used_test_classes: dict[ClassRef, bool] = {}

	async def is_used_test_class(self, owner: ClassRef) -> bool:
		"""A test class with linked uses may supply methods through injection or monkeypatching."""
		if owner not in self._used_test_classes:
			client = self.session.client
			locations = await client.references(
				str(owner.path), *self.classes[owner].position, include_declaration=False
			)
			self._used_test_classes[owner] = any(
				_is_test_path(path.relative_to(self.session.workspace).as_posix())
				for loc in locations
				if (uri := loc.get("uri", "")).startswith("file:")
				and (path := uri_to_path(uri)).is_relative_to(self.session.workspace)
			)
		return self._used_test_classes[owner]

	def _effective_members(self, info: ClassInfo) -> tuple[set[str], set[ClassRef]]:
		"""Cheap nominal name prefilter. ty verifies the actual structural match."""
		members = set(info.members)
		pending = list(info.bases)
		parents: set[ClassRef] = set()
		seen = {info.ref.name}
		while pending:
			base = pending.pop()
			if base in seen:
				continue
			seen.add(base)
			for parent in self.classes_by_name[base]:
				parents.add(parent.ref)
				members.update(parent.members)
				pending.extend(parent.bases)
		return members, parents

	async def _base_members(self, info: ClassInfo) -> tuple[set[str], set[ClassRef]]:
		if info.ref in self._ancestors:
			return self._ancestors[info.ref]
		members: set[str] = set()
		parents: set[ClassRef] = set()
		if info.bases:
			client = self.session.client
			items = await client.prepare_type_hierarchy(str(info.ref.path), *info.position)
			pending = list(items)
			seen: set[tuple[Path, int]] = set()
			while pending:
				for base in await client.supertypes(pending.pop()):
					uri = base.get("uri", "")
					if not uri.startswith("file:"):
						continue
					path = uri_to_path(uri)
					line = (base.get("selectionRange") or base["range"])["start"]["line"] + 1
					key = (path, line)
					if key in seen:
						continue
					seen.add(key)
					known = self.by_location.get(key)
					if known:
						parents.add(known.ref)
						declared = known.members
					else:
						if key not in self._external_members:
							self._external_members[key] = _declared_members(path, line)
						declared = self._external_members[key]
					members.update(declared or ())
					pending.append(base)
		self._ancestors[info.ref] = members, parents
		return members, parents

	async def _conforms_to(self, protocol: ClassInfo, concrete: ClassInfo) -> bool:
		key = (protocol.ref, concrete.ref)
		if key not in self._conformance:
			self._conformance[key] = await self.conforms(*key)
		return self._conformance[key]

	async def keeps_method(self, owner: ClassRef, name: str) -> bool:
		info = self.classes[owner]
		if info.protocol:
			return True
		inherited, _ = await self._base_members(info)
		if name in inherited:
			return True
		for protocol in self.protocols:
			if name not in protocol.members:
				continue
			required, _ = self._effective_members(protocol)
			if required <= info.members | inherited and await self._conforms_to(protocol, info):
				return True
			for concrete in self.classes.values():
				if concrete.protocol or name in concrete.members:
					continue
				maybe_members, maybe_parents = self._effective_members(concrete)
				if owner not in maybe_parents or not required <= maybe_members:
					continue
				_, parents = await self._base_members(concrete)
				if owner not in parents:
					continue
				# An intermediate class can supply a different implementation.
				redefined = any(name in self.classes[parent].members for parent in parents - {owner})
				if not redefined and await self._conforms_to(protocol, concrete):
					return True
		return False


def _confidence(
	locations: list[dict[str, Any]],
	candidate: Candidate,
	twins: list[Candidate],
	workspace: Path,
	scanned: dict[str, Scan],
	exports: Exports,
	exported: bool,
) -> str | None:
	test_refs = False
	for location in locations:
		uri = location.get("uri") or location.get("targetUri", "")
		if not uri.startswith("file:"):
			continue
		path = uri_to_path(uri)
		if not path.is_relative_to(workspace):
			continue
		rel = path.relative_to(workspace).as_posix()
		if rel not in scanned:
			continue
		start = (location.get("targetSelectionRange") or location.get("range") or location["targetRange"])["start"]
		pos = start["line"] + 1, start["character"] + 1
		if any(c.path == rel and c.start <= pos <= c.end for c in (candidate, *twins)):
			continue
		if exported and any(start <= pos < end for start, end in exports.reexports.get(rel, [])):
			continue
		if not _is_test_path(rel):
			return None
		test_refs = True
	if exported:
		return "exported"
	if test_refs:
		return None if _is_test_path(candidate.path) else "only in tests"
	return "unused"


async def find_unused(
	session: Session,
	*,
	scope: Path,
	kind: str | None,
	include_private: bool,
) -> tuple[list[Finding], list[str]]:
	"""Return all matching candidates, ordered by confidence and source location."""
	scans, skipped = await asyncio.to_thread(_scan_workspace, session.workspace)
	counts = _counts(scans)
	scanned = {scan.path: scan for scan in scans}
	exports = _exports(scans, session)
	entries, entry_error = _entry_points(session.workspace)
	entries |= {(module, name.split(".")[0]) for module, name in entries if "." in name}
	dotted = {value for scan in scans for value in scan.dotted_strings}
	reflective = {value for scan in scans for value in scan.reflective_strings}
	actions = {value for scan in scans for value in scan.textual_actions}
	classes = ClassUses(session, scans)
	failures: list[Candidate] = []
	findings: list[Finding] = []
	possible_test_doubles = 0
	for scan in scans:
		path = session.workspace / scan.path
		if not path.is_relative_to(scope):
			continue
		modules = module_names_for(path, session.roots)
		primary = primary_module_name(path, session.roots)
		twin_suffix = ".py" if path.suffix == ".pyi" else ".pyi"
		twin_scan = scanned.get(Path(scan.path).with_suffix(twin_suffix).as_posix())
		for candidate in scan.candidates:
			if kind and candidate.kind != kind:
				continue
			if not include_private and any(part.startswith("_") for part in candidate.qualname.split(".")):
				continue
			if any((module, candidate.qualname) in entries for module in modules):
				continue
			textual = candidate.owner is not None and classes.classes[candidate.owner].textual
			if textual and candidate.name.startswith("on_"):
				continue
			exported = not candidate.owner and primary is not None and (primary, candidate.name) in exports.bindings
			twins = twin_scan.declarations.get((candidate.kind, candidate.qualname), []) if twin_scan else []
			if path.suffix == ".pyi" and twins and (session.workspace / twins[0].path).is_relative_to(scope):
				continue  # report the implementation when both versions are in scope
			has_string_ref = (
				candidate.name in reflective
				or (textual and candidate.name in actions)
				or any(f"{module}.{candidate.qualname}" in dotted for module in modules)
			)
			try:
				locations = []
				if counts[candidate.name] > 1:
					locations = await session.client.references(scan.path, *candidate.position)
					if twins:
						twin = twins[0]
						locations.extend(await session.client.references(twin.path, *twin.position))
				confidence = _confidence(locations, candidate, twins, session.workspace, scanned, exports, exported)
				if (
					confidence is None
					or candidate.owner
					and await classes.keeps_method(candidate.owner, candidate.name)
				):
					continue
				if (
					confidence == "unused"
					and not has_string_ref
					and candidate.owner is not None
					and _is_test_path(candidate.path)
					and await classes.is_used_test_class(candidate.owner)
				):
					confidence = "only in tests"
					possible_test_doubles += 1
			except TOOL_ERRORS:
				failures.append(candidate)
				continue
			if confidence == "unused" and has_string_ref:
				confidence = "only in strings"
			elif confidence == "only in tests" and textual and candidate.name in actions:
				# A known runtime binding is stronger evidence than a bare reflection string.
				continue
			findings.append(Finding(candidate, confidence))
	order = {"unused": 0, "only in tests": 1, "only in strings": 2, "exported": 3}
	findings.sort(key=lambda row: (order[row.confidence], row.candidate.path, row.candidate.position))
	notes = []
	if possible_test_doubles:
		notes.append(
			"Methods on test classes used by tests are marked only in tests when dynamic dispatch may use them."
		)
	if skipped:
		notes.append(f"Could not scan {len(skipped)} Python file(s), including {', '.join(skipped[:3])}.")
	if failures:
		sites = ", ".join(f"{candidate.path}:{candidate.line}" for candidate in failures[:3])
		notes.append(f"ty could not check {len(failures)} candidate(s), including {sites}.")
	if entry_error:
		notes.append(entry_error)
	return findings, notes
