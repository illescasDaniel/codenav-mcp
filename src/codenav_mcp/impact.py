"""`impact`: what breaks if a symbol changes, grouped by what the agent has to do about each kind of use.

One `references` request (type-resolved, so injected dependencies and typed attributes are found) is
classified in the syntax tree into call sites (with how each call passes its arguments), imports,
subclasses, annotations and uses as a value. Overrides/implementations of a method come from
`linked.find_linked`; strings, patch targets, comments and non-Python text from `mentions.find_mentions`.
`classify_sites` and `render_report` are pure (no language server) so tests can feed them plain data.
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_nav_shared.edits import read_source, relative_name
from mcp_nav_shared.errors import TOOL_ERRORS, ToolInputError, format_tool_error
from mcp_nav_shared.format import uri_to_path
from mcp_nav_shared.resolve import resolve_symbol

from codenav_mcp import tool_base
from codenav_mcp.ambiguity import ambiguity_listing
from codenav_mcp.deps import module_names_for, not_scanned_line
from codenav_mcp.linked import find_linked
from codenav_mcp.mentions import Mention, find_mentions
from codenav_mcp.pysource import (
	Definition,
	PythonSyntaxError,
	definition_name_position,
	definitions_in,
	find_definition,
	parse_python,
)
from codenav_mcp.reply import Reply, call, paginate
from codenav_mcp.signature import SourceMap, read_call_args
from codenav_mcp.tool_base import Session
from codenav_mcp.write_tools import conforms_probe
from codenav_mcp.writes import require_workspace_file


CHANGES = ("any", "signature", "rename", "delete", "type")
MAX_DEPTH = 3
MAX_TRANSITIVE = 80
MAX_REQUESTS = 60
GATE = "type check only; tests and docs not checked"


# -- classification ----------------------------------------------------------------


@dataclass(frozen=True)
class Args:
	"""How one call passes its arguments."""

	positional: int
	keywords: tuple[str, ...]
	star: bool  # `*xs`
	kwstar: bool  # `**kw`

	def label(self) -> str:
		parts = []
		if self.positional:
			parts.append(f"{self.positional} positional")
		if self.keywords:
			shown = ", ".join(self.keywords[:4])
			parts.append(f"kw: {shown}" + (f" +{len(self.keywords) - 4} more" if len(self.keywords) > 4 else ""))
		if self.star:
			parts.append("*args")
		if self.kwstar:
			parts.append("**kwargs")
		return ", ".join(parts) or "no args"

	@property
	def spread(self) -> bool:
		return self.star or self.kwstar


@dataclass(frozen=True)
class Site:
	path: str  # workspace-relative
	line: int  # 1-based
	column: int  # 0-based UTF-16
	kind: str  # "call" | "import" | "subclass" | "annotation" | "value"
	text: str  # the source line, stripped
	args: Args | None = None
	via: str | None = None  # the implementing/overriding class when the site belongs to one of those


def is_test_path(rel: str) -> bool:
	parts = rel.split("/")
	name = parts[-1]
	return (
		any(part in ("tests", "test") for part in parts[:-1])
		or name.startswith("test_")
		or name.endswith(("_test.py", "_tests.py"))
		or name == "conftest.py"
	)


class _File:
	"""A parsed file with the index `classify_sites` needs: which Name/Attribute ends where, and parents."""

	def __init__(self, rel: str, text: str) -> None:
		self.rel = rel
		self.smap = SourceMap(text)
		self.tree = parse_python(text, rel)
		self.parent: dict[int, ast.AST] = {}
		self.at_end: dict[tuple[int, int], ast.AST] = {}
		self.imports: list[tuple[int, int]] = []
		for node in ast.walk(self.tree):
			for child in ast.iter_child_nodes(node):
				self.parent[id(child)] = node
			if isinstance(node, (ast.Name, ast.Attribute)):
				self.at_end[self.smap.lsp_end(node)] = node
			elif isinstance(node, (ast.Import, ast.ImportFrom)):
				self.imports.append((node.lineno, node.end_lineno or node.lineno))

	def in_import(self, line: int) -> bool:
		return any(first <= line <= last for first, last in self.imports)

	def line_text(self, line: int) -> str:
		lines = self.smap.lines
		return lines[line - 1].strip()[:140] if 0 < line <= len(lines) else ""

	def args_of(self, call: ast.Call) -> Args:
		parsed = read_call_args(self.smap, call)
		return Args(
			positional=sum(1 for a in parsed if a.keyword is None and not a.starred),
			keywords=tuple(a.keyword for a in parsed if a.keyword),
			star=any(a.starred and not a.text.startswith("**") for a in parsed),
			kwstar=any(a.starred and a.keyword is None and a.text.startswith("**") for a in parsed),
		)


def _classify(file: _File, loc: dict[str, Any]) -> tuple[str, Args | None]:
	start = loc["range"]["start"]
	end = loc["range"]["end"]
	line = start["line"] + 1
	if file.in_import(line):
		return "import", None
	node = file.at_end.get((end["line"], end["character"]))
	if node is None:
		return "value", None
	parent = file.parent.get(id(node))
	if isinstance(parent, ast.Call) and parent.func is node:
		return "call", file.args_of(parent)
	child, ancestor = node, parent
	while ancestor is not None:
		if isinstance(ancestor, ast.ClassDef) and any(child is base for base in ancestor.bases):
			return "subclass", None
		if isinstance(ancestor, ast.arg) and ancestor.annotation is child:
			return "annotation", None
		if isinstance(ancestor, ast.AnnAssign) and ancestor.annotation is child:
			return "annotation", None
		if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)) and ancestor.returns is child:
			return "annotation", None
		if isinstance(ancestor, (ast.stmt, ast.Module)):
			break
		child, ancestor = ancestor, file.parent.get(id(ancestor))
	return "value", None


def classify_sites(
	locations: Iterable[dict[str, Any]],
	workspace: Path,
	read: Callable[[Path], str | None],
	*,
	via: str | None = None,
	skip: set[tuple[str, int, int]] | None = None,
) -> list[Site]:
	"""Classify LSP reference `locations` (inside the workspace) into sites. A file that cannot be read or
	parsed turns its locations into plain `value` sites so nothing is dropped. One import statement is one site
	(`from m import x as x` references the name twice)."""
	files: dict[Path, _File | None] = {}
	sites: list[Site] = []
	seen: set[tuple[str, int, int]] = set(skip or ())
	imports_seen: set[tuple[str, int]] = set()
	for loc in locations:
		path = uri_to_path(loc["uri"]).resolve()
		try:
			rel = path.relative_to(workspace.resolve()).as_posix()
		except ValueError:
			continue
		start = loc["range"]["start"]
		key = (rel, start["line"], start["character"])
		if key in seen:
			continue
		seen.add(key)
		if path not in files:
			text = read(path)
			try:
				files[path] = _File(rel, text) if text is not None else None
			except (PythonSyntaxError, ValueError, SyntaxError, RecursionError):
				files[path] = None
		file = files[path]
		if file is None:
			sites.append(Site(rel, start["line"] + 1, start["character"], "value", "", via=via))
			continue
		kind, args = _classify(file, loc)
		line = start["line"] + 1
		if kind == "import":
			if (rel, line) in imports_seen:
				continue
			imports_seen.add((rel, line))
		sites.append(Site(rel, line, start["character"], kind, file.line_text(line), args, via))
	return sites


# -- string / text mentions ------------------------------------------------------------


@dataclass
class Mentions:
	all_exports: list[Mention] = field(default_factory=list)  # `__all__ = [..., "name"]` lines
	patch_targets: list[tuple[Mention, bool]] = field(default_factory=list)  # (mention, names this very symbol)
	strings: list[Mention] = field(default_factory=list)
	comments: list[Mention] = field(default_factory=list)
	text: list[Mention] = field(default_factory=list)  # non-Python files (docs, config)
	unlinked: list[Mention] = field(default_factory=list)  # attr / bare name the type checker did not link


def _all_lines(text: str, name: str) -> set[int]:
	"""Lines of the string constants equal to `name` inside an `__all__` assignment."""
	try:
		tree = ast.parse(text)
	except (SyntaxError, ValueError, RecursionError):
		return set()
	found: set[int] = set()
	for node in ast.walk(tree):
		targets: list[ast.expr] = []
		if isinstance(node, ast.Assign):
			targets = node.targets
		elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
			targets = [node.target]
		if node_value := getattr(node, "value", None):
			if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
				found |= {c.lineno for c in ast.walk(node_value) if isinstance(c, ast.Constant) and c.value == name}
	return found


def group_mentions(
	mentions: list[Mention],
	name: str,
	qualified: set[str],
	read: Callable[[str], str | None],
) -> Mentions:
	"""Sort `find_mentions` results into the groups of the report (one entry per file and line)."""
	grouped = Mentions()
	dotted = re.compile(rf"""["']([A-Za-z_][\w.]*\.{re.escape(name)})["']""")
	all_cache: dict[str, set[int]] = {}
	done: set[tuple[str, int, str]] = set()
	for mention in mentions:
		key = (mention.path, mention.line, mention.kind)
		if key in done:
			continue
		done.add(key)
		if mention.kind == "string":
			if mention.path not in all_cache:
				text = read(mention.path)
				all_cache[mention.path] = _all_lines(text, name) if text else set()
			if mention.line in all_cache[mention.path]:
				grouped.all_exports.append(mention)
				continue
			target = dotted.search(mention.snippet)
			if target:
				grouped.patch_targets.append((mention, target.group(1) in qualified))
			else:
				grouped.strings.append(mention)
		elif mention.kind == "comment":
			grouped.comments.append(mention)
		elif mention.kind == "text":
			grouped.text.append(mention)
		else:
			grouped.unlinked.append(mention)
	return grouped


# -- the report ----------------------------------------------------------------------


@dataclass
class Subject:
	"""What impact was asked about."""

	name: str  # as the agent will type it back (`Class.method`)
	file_path: str | None  # only when the name alone would be ambiguous
	kind: str  # "function" | "method" | "class" | "variable"
	where: str  # `path:line`
	protocol: bool = False
	file: str = ""  # workspace-relative path of the definition


@dataclass
class Overrides:
	label: str  # `Class.method`
	where: str  # `path:line`


@dataclass
class Data:
	subject: Subject
	sites: list[Site] = field(default_factory=list)
	via_sites: list[Site] = field(default_factory=list)  # uses of overrides/implementations
	overrides: list[Overrides] = field(default_factory=list)
	mentions: Mentions = field(default_factory=Mentions)
	transitive: list[tuple[int, str, str]] = field(default_factory=list)  # (depth, `path:line name`, calls what)
	notes: list[str] = field(default_factory=list)
	not_scanned: str = ""  # one line: the directories no text scan looked into


def write_calls(subject: Subject, change: str) -> list[str]:
	"""The matching write call(s), as previews."""
	args: dict[str, Any] = {"name": subject.name, "file_path": subject.file_path}
	callable_ = subject.kind in ("function", "method", "class")
	rename = call("rename", **args, new_name="<new_name>", apply=False)
	signature = call("change_signature", **args, add=[{"name": "<param>", "default": "None"}], apply=False)
	delete = call("edit_symbol", name=subject.name, file_path=subject.file_path, action="delete", apply=False)
	if change == "rename":
		return [rename]
	if change == "delete":
		return [delete]
	if change == "type":
		return [
			call(
				"edit_symbol",
				name=subject.name,
				file_path=subject.file_path,
				action="replace",
				source="<new definition>",
				apply=False,
			)
		]
	if not callable_ or (subject.kind == "class" and subject.protocol):
		return [rename]
	return [signature] + ([rename] if change == "any" else [])


def _arg_summary(sites: list[Site]) -> str:
	shapes = Counter()
	keywords: Counter[str] = Counter()
	for site in sites:
		args = site.args
		if args is None:
			continue
		if args.spread:
			shapes["with *args/**kwargs (cannot be rewritten automatically)"] += 1
		elif args.keywords and args.positional:
			shapes["mixed positional + keyword"] += 1
		elif args.keywords:
			shapes["keywords only"] += 1
		elif args.positional:
			shapes["positional only"] += 1
		else:
			shapes["no arguments"] += 1
		keywords.update(args.keywords)
	text = ", ".join(f"{count} {label}" for label, count in shapes.most_common())
	if keywords:
		text += "; keywords used: " + ", ".join(f"{k} x{n}" for k, n in keywords.most_common(6))
	return text


def _by_file(mentions: list[Mention]) -> list[str]:
	"""One row per file: `path:3,9,14 (3)`, for groups whose lines need no excerpt (docs, comments)."""
	lines: dict[str, list[int]] = {}
	for m in mentions:
		lines.setdefault(m.path, []).append(m.line)
	rows = []
	for path, numbers in lines.items():
		shown = ",".join(str(n) for n in numbers[:6]) + (f",+{len(numbers) - 6}" if len(numbers) > 6 else "")
		rows.append(f"{path}:{shown}" + (f" ({len(numbers)})" if len(numbers) > 1 else ""))
	return rows


def _tests_lines(sites: list[Site]) -> list[tuple[str, str]]:
	"""One (file, description) per test file: its calls, imports and other uses, most first."""
	by_file: dict[str, Counter[str]] = {}
	for site in sites:
		by_file.setdefault(site.path, Counter())[site.kind] += 1
	order = sorted(by_file.items(), key=lambda item: (-sum(item[1].values()), item[0]))
	lines = []
	for path, counts in order:
		parts = [f"{n} {kind}{'' if n == 1 or kind == 'value' else 's'}" for kind, n in counts.most_common()]
		lines.append((path, ", ".join(parts).replace("calls", "calls")))
	return lines


def render_report(data: Data, change: str, depth: int, limit: int) -> Reply:
	"""The reply: answer line, groups (each cut at `limit`), notes, the matching write call."""
	subject = data.subject
	everything = [*data.sites, *data.via_sites]
	prod = [s for s in data.sites if not is_test_path(s.path)]
	tests = [s for s in data.sites if is_test_path(s.path)]
	calls_ = [s for s in data.sites if s.kind == "call"]
	values = [s for s in data.sites if s.kind == "value"]
	imports_prod = [s for s in prod if s.kind == "import"]
	subclasses = [s for s in data.sites if s.kind == "subclass"]
	annotations = [s for s in prod if s.kind == "annotation"]
	files = {s.path for s in everything}
	# test files that use it, call it through an override, or hold a fake/implementation that must change with it
	test_files = {s.path for s in everything if is_test_path(s.path)}
	test_files |= {path for o in data.overrides if is_test_path(path := o.where.rsplit(":", 1)[0])}
	counts = [
		f"{len(calls_)} call{'s' if len(calls_) != 1 else ''}",
		f"{len(values)} used as value",
		f"{sum(1 for s in data.sites if s.kind == 'import')} import{'s' if sum(1 for s in data.sites if s.kind == 'import') != 1 else ''}",
	]
	annotated = [s for s in data.sites if s.kind == "annotation"]
	if annotated:
		counts.append(f"{len(annotated)} type annotation{'s' if len(annotated) != 1 else ''}")
	if subclasses:
		counts.append(f"{len(subclasses)} subclass{'es' if len(subclasses) != 1 else ''}")
	if data.overrides:
		counts.append(f"{len(data.overrides)} implementation(s)/override(s)")
	mention = data.mentions
	text_n = len(mention.text)
	strings_n = len(mention.strings) + len(mention.patch_targets) + len(mention.all_exports)
	if (strings_n or text_n) and change != "signature":
		counts.append(f"{strings_n} string(s), {text_n} text mention(s)")
	elif text_n:  # a signature change leaves strings alone, but docs and examples show the old signature
		counts.append(f"{text_n} text mention(s)")
	unlinked_n = len({(m.path, m.line) for m in mention.unlinked})
	if unlinked_n and change == "signature":  # callers in files the type checker does not check (scripts/, examples/)
		counts.append(f"{unlinked_n} unresolved same-name use(s)")
	if not everything and not data.overrides and not data.transitive:
		extra = f"; {strings_n} string(s) and {text_n} text mention(s) name it" if strings_n or text_n else ""
		head = f"Nothing in the project references {subject.name} ({subject.kind}, {subject.where}) besides its definition{extra}."
	else:
		head = (
			f"Changing {subject.name} ({subject.kind}, {subject.where}) touches {', '.join(counts)} in {len(files)} file(s)."
			f" Tests: {len(test_files)} file(s)."
		)
	reply = Reply(head)
	notes = reply.notes
	cut: list[int] = []

	def add(title: str, rows: list[str]) -> None:
		if not rows:
			return
		page = paginate(rows, limit)
		reply.items.append(f"{title} ({len(rows)}):")
		reply.items.extend(f"  {row}" for row in page.items)
		if page.more:
			reply.items.append(f"  ... {page.more} more")
			cut.append(len(rows))

	show_calls = change != "rename"
	if show_calls and calls_:
		summary = _arg_summary(calls_)
		prod_calls = [s for s in prod if s.kind == "call"]
		test_calls = len(calls_) - len(prod_calls)
		head = f"{len(calls_)} call{'s' if len(calls_) != 1 else ''} ({len(prod_calls)} in code, {test_calls} in tests)"
		reply.items.append(f"Call sites: {head}. Arguments: {summary}")
		rows = [f"{s.path}:{s.line}  ({s.args.label() if s.args else '?'})" for s in prod_calls]
		if rows:
			page = paginate(rows, limit)
			reply.items.extend(f"  {row}" for row in page.items)
			if page.more:
				reply.items.append(f"  ... {page.more} more")
				cut.append(len(rows))
	if data.via_sites and show_calls:
		by_via: dict[str, list[Site]] = {}
		for site in data.via_sites:
			by_via.setdefault(site.via or "?", []).append(site)
		rows = []
		for label, group in by_via.items():
			kinds = Counter(s.kind for s in group)
			rows.append(
				f"{label}: {', '.join(f'{n} {k}' for k, n in kinds.most_common())} (e.g. {group[0].path}:{group[0].line})"
			)
		add("Uses of implementations/overrides (change with the call above)", rows)
	add("Implementations/overrides that must change together", [f"{o.label}  {o.where}" for o in data.overrides])
	add("Subclasses", [f"{s.path}:{s.line}  {s.text}" for s in subclasses])
	add(
		"Used as a value (passed or stored, not called"
		+ ("; a signature change cannot rewrite these)" if change in ("any", "signature") else ")"),
		[f"{s.path}:{s.line}  {s.text}" for s in values],
	)
	if change in ("any", "type", "delete"):
		add("Type annotations", [f"{s.path}:{s.line}  {s.text}" for s in annotations])
	if change != "signature":
		add("Re-exported in __all__", [f"{m.path}:{m.line}  {m.snippet}" for m in mention.all_exports])
		add("Imported by", [f"{s.path}:{s.line}  {s.text}" for s in imports_prod])
	add(
		"Tests that reference it (by file)",
		[f"{path}  {desc}" for path, desc in _tests_lines([s for s in tests if s.kind != "value"])],
	)
	if change == "signature":
		add("Docs and other text files that may show the old signature (file:lines)", _by_file(mention.text))
	if change != "signature":
		targets = [
			f"{m.path}:{m.line}  {m.snippet}  [{'exact patch target' if exact else 'patch target?'}]"
			for m, exact in mention.patch_targets
		]
		add("Patch targets / dotted strings", targets)
		add("Other strings (getattr, registries)", [f"{m.path}:{m.line}  {m.snippet}" for m in mention.strings])
		add("Docs and other text files (file:lines)", _by_file(mention.text))
		add("Comments (file:lines)", _by_file(mention.comments))
	if mention.unlinked and change in ("any", "rename", "delete", "signature"):
		add(
			"Same name, not resolved by the type checker (files it does not check, mocks, untyped objects, or "
			"another symbol; file:lines)",
			_by_file(mention.unlinked),
		)
	if data.transitive:
		code = [(d, where, callee) for d, where, callee in data.transitive if not is_test_path(where.split(":")[0])]
		in_tests = len(data.transitive) - len(code)
		add(
			f"Transitive callers (depth 2-{depth})" if depth > 2 else "Transitive callers (depth 2)",
			[f"{where}  (calls {callee})" + (f" [depth {d}]" if depth > 2 else "") for d, where, callee in code],
		)
		if in_tests:
			reply.items.append(f"  + {in_tests} more function(s) in tests")
	notes.extend(data.notes)
	if data.not_scanned:
		notes.append(data.not_scanned)
	if subject.protocol:
		notes.append(
			f'{subject.name} is a Protocol: classes that satisfy it structurally are listed by lookup(name="{subject.name}", show=["implementations"])'
		)
	if cut:
		reply.next_calls.append(
			call(
				"impact",
				name=subject.name,
				file_path=subject.file_path,
				change=None if change == "any" else change,
				depth=depth if depth > 1 else None,
				limit=max(cut),
			)
		)
	for write in write_calls(subject, change):
		reply.next_calls.append(f"{write}  # preview; {GATE}")
	return reply


# -- gathering (language server) ---------------------------------------------------


def _reader(workspace: Path) -> Callable[[Path], str | None]:
	def read(path: Path) -> str | None:
		try:
			return read_source(path)[0]
		except (OSError, UnicodeDecodeError, ValueError):
			return None

	return read


def _location_start(loc: dict[str, Any]) -> tuple[Path, int, int]:
	start = loc["range"]["start"]
	return uri_to_path(loc["uri"]).resolve(), start["line"], start["character"]


async def _transitive(
	session: Session, rel: str, line: int, column: int, depth: int, workspace: Path
) -> tuple[list[tuple[int, str, str]], bool]:
	"""Callers of the callers (levels 2..depth) as (level, `path:line name`, callee), and whether a cap stopped the walk.

	Sequential requests (ty answers "content modified" to concurrent ones). A test function is a leaf: it is
	listed but never expanded, so a widely tested function costs one request per production caller, not one
	per test. At most MAX_REQUESTS expansions and MAX_TRANSITIVE listed functions.
	"""
	client = session.client
	items = await client.prepare_call_hierarchy(rel, line, column)
	if not items:
		return [], False
	found: list[tuple[int, str, str]] = []
	seen: set[tuple[str, int]] = set()
	requests = 0
	frontier = [(items[0], str(items[0].get("name", "?")))]
	for level in range(1, depth + 1):
		next_frontier: list[tuple[dict[str, Any], str]] = []
		for item, label in frontier:
			if requests >= MAX_REQUESTS or len(found) >= MAX_TRANSITIVE:
				return found, True
			requests += 1
			for incoming in await client.incoming_calls(item):
				caller = incoming.get("from") or {}
				uri = str(caller.get("uri", ""))
				if not uri.startswith("file:"):
					continue
				try:
					path = uri_to_path(uri).resolve().relative_to(workspace.resolve()).as_posix()
				except ValueError:
					continue
				start = (caller.get("selectionRange") or caller.get("range") or {}).get("start") or {}
				row = int(start.get("line", 0)) + 1
				if (path, row) in seen:
					continue
				seen.add((path, row))
				name = str(caller.get("name", "?"))
				if level > 1:
					found.append((level, f"{path}:{row} {name}", label))
				if not is_test_path(path):
					next_frontier.append((caller, name))
		frontier = next_frontier
	return found, False


def _class_for_init(text: str, rel: str, definition: Definition) -> Definition | None:
	parent = definition.parents[-1] if definition.parents else None
	return next(
		(
			d
			for d in definitions_in(text, filename=rel)
			if isinstance(d.node, ast.ClassDef) and parent is not None and d.node.lineno == parent.lineno
		),
		None,
	)


async def gather(session: Session, name: str, file_path: str | None, change: str, depth: int) -> Data:
	srv = tool_base.server()
	client, workspace = session.client, session.workspace
	if file_path:
		require_workspace_file(workspace, file_path)
	resolved = await resolve_symbol(client, workspace, name, file_path=file_path)
	path = uri_to_path(resolved.uri).resolve()
	rel = relative_name(path, workspace)
	text = read_source(path)[0]
	definition = find_definition(text, name_line=resolved.line + 1, prefer_implementation=True, filename=rel)
	lines = text.splitlines()
	ref_line, ref_col = resolved.line + 1, resolved.column + 1
	mention_name = resolved.name
	kind = "variable"
	protocol = False
	if definition is not None:
		kind = "class" if definition.is_class else definition.kind
		anchor = definition
		if definition.name == "__init__" and definition.parents:  # constructor: its calls are the class's
			anchor = _class_for_init(text, rel, definition) or definition
			if anchor is not definition:
				mention_name = anchor.name
		ref_line, ref_col = (
			definition_name_position(lines if lines else [""], anchor)
			if anchor is not definition
			else (ref_line, ref_col)
		)
		if definition.is_class:
			try:
				protocol = definition.name in srv._cached_protocol_class_names(path)
			except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
				protocol = False
	subject = Subject(
		name, file_path, kind, f"{rel}:{definition.def_line if definition else resolved.line + 1}", protocol, rel
	)
	data = Data(subject)
	read = _reader(workspace)
	locations = await client.references(rel, ref_line, ref_col, include_declaration=True)
	own = {(path, ref_line - 1, ref_col - 1), (path, resolved.line, resolved.column)}
	locations = [
		loc for loc in locations if str(loc.get("uri", "")).startswith("file:") and _location_start(loc) not in own
	]
	data.sites = classify_sites(locations, workspace, read)

	covered_by_path: dict[Path, set[tuple[int, int]]] = {}

	def cover(loc_path: Path, line0: int, char: int) -> None:
		covered_by_path.setdefault(loc_path.resolve(), set()).add((line0, char))

	for loc in locations:
		loc_path, line0, char = _location_start(loc)
		cover(loc_path, line0, char)
	cover(path, ref_line - 1, ref_col - 1)
	if definition is not None:
		cover(path, resolved.line, resolved.column)

	if kind == "method" and definition is not None and definition.name not in ("__init__", "__new__"):
		linked = await find_linked(
			client,
			workspace,
			origin_path=path,
			member_line=definition.def_line,
			member_name=definition.name,
			new_name=definition.name,
			conforms=conforms_probe(session),
		)
		data.notes.extend(n for n in linked.notes if n.startswith("stopped"))
		for site in linked.sites:
			label = f"{site.cls.name}.{definition.name}"
			cls_rel = relative_name(site.cls.path, workspace)
			data.overrides.append(Overrides(label, f"{cls_rel}:{site.line}"))
			cover(site.cls.path, site.line - 1, site.column - 1)
			uses = await client.references(cls_rel, site.line, site.column, include_declaration=False)
			uses = [u for u in uses if str(u.get("uri", "")).startswith("file:")]
			known = {
				(relative_name(_location_start(u)[0], workspace), _location_start(u)[1], _location_start(u)[2])
				for u in locations
			}
			data.via_sites += classify_sites(uses, workspace, read, via=label, skip=known)
			for u in uses:
				u_path, u_line, u_char = _location_start(u)
				cover(u_path, u_line, u_char)

	# strings, comments, docs and unlinked names
	covered = {p: set(v) for p, v in covered_by_path.items()}
	raw = find_mentions(workspace, mention_name, covered)
	qualified: set[str] = set()
	if definition is not None:
		qual = definition.qualname if not (definition.name == "__init__" and definition.parents) else mention_name
		qualified = {f"{module}.{qual}" for module in module_names_for(path, session.roots)}
	else:
		qualified = {f"{module}.{mention_name}" for module in module_names_for(path, session.roots)}

	def read_rel(rel_path: str) -> str | None:
		return read(workspace / rel_path)

	data.mentions = group_mentions(raw, mention_name, qualified, read_rel)
	data.not_scanned = not_scanned_line(workspace)
	if depth > 1 and kind in ("function", "method", "class"):
		data.transitive, stopped = await _transitive(session, rel, ref_line, ref_col, depth, workspace)
		if stopped:
			data.notes.append(
				f"transitive callers incomplete: stopped after {MAX_REQUESTS} lookups or {MAX_TRANSITIVE} functions"
			)
		elif not data.transitive:
			data.notes.append(f"the callers of {name} have no callers of their own in the project")
	data.notes.append(
		"static analysis: calls through getattr/dynamic dispatch and non-Python consumers are not visible"
	)
	return data


async def impact_text(session: Session, name: str, file_path: str | None, change: str, depth: int, limit: int) -> str:
	if change not in CHANGES:
		raise ToolInputError(
			f'change must be one of {", ".join(CHANGES)}. Example: impact(name="{name}", change="signature")'
		)
	if not 1 <= depth <= MAX_DEPTH:
		raise ToolInputError(f'depth must be 1 to {MAX_DEPTH}. Example: impact(name="{name}", depth=2)')
	if limit < 1:
		raise ToolInputError("limit must be at least 1")
	data = await gather(session, name, file_path, change, depth)
	return render_report(data, change, depth, limit).render()


async def impact_reply(ctx: Any, name: str, file_path: str | None, change: str, depth: int, limit: int) -> Any:
	"""The tool body: an ambiguous name is answered with the exact retry calls, any other error is formatted."""
	try:
		session = await tool_base.open_session(ctx)
		return await impact_text(session, name, file_path, change, depth, limit)
	except TOOL_ERRORS as exc:
		# The retry keeps the arguments that shape the answer (the read tool has no "other arguments unchanged").
		kept = [
			f"{key}={json.dumps(value)}"
			for key, value, default in (("change", change, "any"), ("depth", depth, 1), ("limit", limit, 30))
			if value != default
		]
		return ambiguity_listing(exc, "impact", name, fixed="".join(f"{part}, " for part in kept)) or format_tool_error(
			exc
		)
