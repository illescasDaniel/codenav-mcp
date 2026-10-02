"""codenav: MCP server exposing ty's language-server features (hover,
definition, references, workspace symbol search, diagnostics) as MCP tools.

Named "codenav" (not "ty") since ty is Astral's name for the underlying
type checker/language server this wraps — the MCP server itself is a
thin tool built on top of it.

Built specifically for ty rather than as a generic LSP bridge: see
docs/agent-tooling.md for why (mcp-language-server's name-based
definition/references tools don't resolve symbols against ty, even though
ty's own workspace/symbol implementation answers those same queries
correctly when asked directly over LSP).

Run standalone for manual testing:
    uv run python -m codenav_mcp.server
"""

from __future__ import annotations

import ast
import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mcp_nav_shared
from mcp.server.mcpserver import Context, MCPServer
from mcp_nav_shared.errors import TOOL_ERRORS, ToolInputError, format_tool_error
from mcp_nav_shared.exclude import is_excluded
from mcp_nav_shared.format import (
	filter_symbols_by_kind_and_path,
	format_callers,
	format_diagnostics,
	format_location,
	format_outline,
	format_references,
	format_references_grouped,
	format_workspace_symbols,
	parse_kind_filter,
	symbol_kind_label,
	to_symbol_tree,
	uri_to_path,
	uri_to_relative,
)
from mcp_nav_shared.lsp_client import LspClient, LspRequestError
from mcp_nav_shared.notices import NoticeBoard, package_source_dirs
from mcp_nav_shared.params import resolve_name_query
from mcp_nav_shared.resolve import resolve_symbol
from mcp_nav_shared.workspace import WorkspaceSelector, resolve_extra_source_roots, resolve_source_root

import codenav_mcp
from codenav_mcp.ty_command import resolve_ty_command


logger = logging.getLogger(__name__)

# Which checkout/worktree to navigate is decided per request (see
# `WorkspaceSelector`): the host starts this process once, usually from the
# main checkout, even when the session works in a linked worktree.
_selector = WorkspaceSelector("CODENAV_MCP_WORKSPACE")
WORKSPACE_ROOT = _selector.base
_workspace_source = _selector.base_source
# Root for import-path derivation and implementations' workspace-wide class
# scan. Defaults to the whole workspace; set CODENAV_MCP_SOURCE_ROOT (e.g. to
# "src") in a project's MCP config to scope/speed up the scan.
SOURCE_ROOT = resolve_source_root("CODENAV_MCP_SOURCE_ROOT", WORKSPACE_ROOT)
# Extra directories `implementations` also scans (e.g. "tests", for test
# doubles of a port); their matches are listed under a separate heading.
EXTRA_SOURCE_ROOTS = resolve_extra_source_roots("CODENAV_MCP_EXTRA_SOURCE_ROOTS", WORKSPACE_ROOT)

_POSITION_NOTE = (
	"Positions are 1-indexed. `column` is a UTF-16 character offset on the "
	"line (not a visual/display column): a leading tab counts as one "
	"character, so after a single tab the next character starts at column 2."
)

mcp = MCPServer(
	name="codenav",
	instructions=(
		"Code navigation for this Python codebase, backed by ty (Astral's type "
		"checker/language server). Prefer this over grepping for symbol "
		"definitions/usages: it resolves through type inference (imports, "
		"dependency-injected parameters, dataclass fields, etc.), not just text "
		"matching. Python files only (.py/.pyi) — other extensions are rejected. "
		"Start with symbol_info (what is X, where is it used) or outline (what's "
		"in this file) rather than chaining search_symbol → hover → definition → "
		"references by hand; drop to the position tools (hover/definition/"
		"references) once you have a specific line to inspect. " + _POSITION_NOTE
	),
)

_PYTHON_EXTENSIONS = {".py", ".pyi"}
# ty reads these once at startup; a change restarts it (see `LspClient.config_names`).
_TY_CONFIG_NAMES = frozenset({"pyproject.toml", "ty.toml", ".ty.toml"})

_notices = NoticeBoard("codenav", package_source_dirs(mcp_nav_shared, codenav_mcp))

_client: LspClient | None = None
_client_lock = asyncio.Lock()


_workspace_lock = asyncio.Lock()


async def _configure_workspace(root: Path, source: str) -> None:
	"""Re-target the server at `root`: the language server, probe verdicts and
	every path derived from the old root belong to the previous tree."""
	global WORKSPACE_ROOT, SOURCE_ROOT, EXTRA_SOURCE_ROOTS, _client, _workspace_source, _probe_cache_signature
	if _client is not None:
		try:
			await _client.stop()
		except Exception:
			logger.debug("failed to stop ty server on workspace switch", exc_info=True)
		_client = None
	WORKSPACE_ROOT = root
	SOURCE_ROOT = resolve_source_root("CODENAV_MCP_SOURCE_ROOT", root)
	EXTRA_SOURCE_ROOTS = resolve_extra_source_roots("CODENAV_MCP_EXTRA_SOURCE_ROOTS", root)
	_workspace_source = source
	_probe_cache.clear()
	_probe_cache_signature = ()
	logger.info("workspace: %s (%s)", root, source)


async def _use_workspace(ctx: Context | None) -> None:
	"""Called first by every tool. `ctx` is None only when a tool is invoked
	directly (tests), in which case the current workspace stays as is."""
	if ctx is None:
		return
	async with _workspace_lock:
		selection = await _selector.select(ctx.session)
		if selection.root != WORKSPACE_ROOT:
			await _configure_workspace(selection.root, selection.source)


def _check_python_file(file_path: str) -> None:
	"""ty only understands Python; without this, asking it to type-check or
	navigate a non-Python file (e.g. diagnostics on a README) silently
	mis-parses the file as Python instead of failing clearly."""
	suffix = Path(file_path).suffix.lower()
	if suffix not in _PYTHON_EXTENSIONS:
		raise ToolInputError(f"codenav only supports Python files (.py/.pyi), got {file_path!r}")


async def _after_ty_restart(_client: LspClient) -> None:
	"""Probe verdicts were computed under the old project config."""
	global _probe_cache_signature
	_probe_cache.clear()
	_probe_cache_signature = ()


async def get_client() -> LspClient:
	global _client
	async with _client_lock:
		if _client is None or not _client.is_alive:
			if _client is not None:
				try:
					await _client.stop()  # reap the dead server instead of leaking it
				except Exception:
					logger.debug("failed to stop stale ty server", exc_info=True)
			_client = LspClient(
				workspace_root=WORKSPACE_ROOT,
				command=resolve_ty_command(WORKSPACE_ROOT),
				language_id="python",
				watch_suffixes=frozenset(_PYTHON_EXTENSIONS),
				config_names=_TY_CONFIG_NAMES,
				on_restart=_after_ty_restart,
				on_notice=_notices.post,
			)
			await _client.start()
			await _after_ty_restart(_client)  # verdicts from a previous server are suspect too
		# Tell ty about anything created/edited/deleted on disk since the last call.
		await _client.refresh()
		return _client


# ty's hover on a variable/parameter is just its type ("AppServices", "list[str] | None").
# Two words in a row ("Return the value") is prose, not a type expression.
_BARE_TYPE_RE = re.compile(r"(?!.*\w\s+\w)[A-Za-z_][\w.\[\], |]*")
_MAX_TYPE_LOCATIONS = 3


def _describe_type_definition(loc: dict[str, Any], workspace_root: Path) -> str:
	"""Where a type is defined, its header line, and the first line of its docstring."""
	uri = loc.get("uri") or loc.get("targetUri", "")
	rng = loc.get("range") or loc.get("targetSelectionRange") or {}
	line0 = rng.get("start", {}).get("line", 0)
	start_col = rng.get("start", {}).get("character", 0)
	described = [f"Type defined at {uri_to_relative(uri, workspace_root)}:{line0 + 1}:{start_col + 1}"]
	try:
		source = uri_to_path(uri).read_text(encoding="utf-8")
		header = source.splitlines()[line0].strip()
		tree = ast.parse(source)
	except (OSError, UnicodeDecodeError, SyntaxError, IndexError):
		return "\n".join(described)
	described.append(f"  {header}")
	for node in ast.walk(tree):
		if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.lineno == line0 + 1:
			doc = ast.get_docstring(node)
			if doc:
				described.append(f'  """{doc.splitlines()[0]}"""')
			break
	return "\n".join(described)


async def _enrich_bare_type(text: str, client: LspClient, file_path: str, line: int, column: int) -> str:
	"""A bare type name says nothing about where the type lives; add its definition."""
	if "\n" in text or len(text) > 120 or not _BARE_TYPE_RE.fullmatch(text):
		return text
	try:
		locations = await client.type_definition(file_path, line, column)
	except LspRequestError:
		return text  # the hover itself is still good; the extra is optional
	described = [
		_describe_type_definition(loc, WORKSPACE_ROOT)
		for loc in locations[:_MAX_TYPE_LOCATIONS]
		if not _is_builtins_stub(loc)
	]
	if not described:
		return text
	return "\n".join([text, *described])


def _is_builtins_stub(loc: dict[str, Any]) -> bool:
	"""`str`/`int`/`list` point into typeshed; that is noise on every plain variable."""
	return uri_to_path(loc.get("uri") or loc.get("targetUri", "")).name == "builtins.pyi"


def _format_hover_contents(contents: Any) -> str:
	if not contents:
		return ""
	if isinstance(contents, dict):
		return contents.get("value", str(contents)).strip()
	if isinstance(contents, list):
		return "\n".join(c.get("value", str(c)) if isinstance(c, dict) else str(c) for c in contents).strip()
	return str(contents).strip()


@mcp.tool()
@_notices.tool
async def workspace(ctx: Context | None = None) -> str:
	"""Which directory is codenav navigating, and why? Use when results look like they come from the wrong checkout/worktree."""
	await _use_workspace(ctx)
	return f"{WORKSPACE_ROOT}\nchosen because: {_selector.explain(_workspace_source)}\nsource root: {SOURCE_ROOT}"


@mcp.tool()
@_notices.tool
async def hover(file_path: str, line: int, column: int, ctx: Context | None = None) -> str:
	"""Get type/documentation info for the symbol at a position.

	`line` and `column` are 1-indexed. `column` is a UTF-16 character offset
	on the line (not a visual/display column): a leading tab counts as one
	character.
	"""
	await _use_workspace(ctx)
	try:
		_check_python_file(file_path)
		client = await get_client()
		result = await client.hover(file_path, line, column)
		text = _format_hover_contents(result.get("contents"))
		if text:
			text = await _enrich_bare_type(text, client, file_path, line, column)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return text or "No hover information at that position."


@mcp.tool()
@_notices.tool
async def definition(file_path: str, line: int, column: int, ctx: Context | None = None) -> str:
	"""Go to the definition of the symbol at a position.

	`line` and `column` are 1-indexed. `column` is a UTF-16 character offset
	on the line (not a visual/display column): a leading tab counts as one
	character.

	Resolves through ty's type inference, so this works even when the call
	site only has a typed parameter/attribute (e.g. `services.some_method()`
	where `services: AppServices` is a constructor argument), not just
	direct references to a name in scope.
	"""
	await _use_workspace(ctx)
	try:
		_check_python_file(file_path)
		client = await get_client()
		locations = await client.definition(file_path, line, column)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	if not locations:
		return "No definition found at that position."
	return "\n\n".join(format_location(loc, WORKSPACE_ROOT) for loc in locations)


@mcp.tool()
@_notices.tool
async def references(
	file_path: str, line: int, column: int, include_declaration: bool = True, ctx: Context | None = None
) -> str:
	"""Find all usages of the symbol at a position across the workspace.

	`line` and `column` are 1-indexed. `column` is a UTF-16 character offset
	on the line (not a visual/display column): a leading tab counts as one
	character.
	"""
	await _use_workspace(ctx)
	try:
		_check_python_file(file_path)
		client = await get_client()
		locations = await client.references(file_path, line, column, include_declaration=include_declaration)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return format_references(locations, WORKSPACE_ROOT)


@mcp.tool()
@_notices.tool
async def search_symbol(
	query: str | None = None,
	name: str | None = None,
	kind: str | None = None,
	path: str | None = None,
	fuzzy: bool = False,
	ctx: Context | None = None,
) -> str:
	"""Search the whole workspace for a symbol by name (class, function, method, etc.).

	Use this to find a symbol's file/position first, then pass that position
	to definition/references/hover for precise, type-resolved navigation.
	Returned positions point at the identifier name (not the `class`/`def`
	keyword) and use the same character-offset column convention as the
	other tools. Results include a SymbolKind label and are capped.
	`name` is accepted as an alias for `query`. Narrow broad queries with
	`kind` (SymbolKind labels, comma-separated: `class`, `function,method`,
	`interface`, ...) and `path` (workspace-relative prefix such as `src/`,
	or a glob such as `src/**/*.py`). Production code ranks before tests.
	Loose fuzzy hits whose names don't contain the query are summarised as a
	count when real matches exist; pass `fuzzy=true` to list them too.
	"""
	await _use_workspace(ctx)
	try:
		kinds = parse_kind_filter(kind)
		query = resolve_name_query(preferred="query", example="create_user", query=query, name=name)
		client = await get_client()
		symbols = await client.workspace_symbol(query)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	if not symbols:
		return f"No symbols matching {query!r}."
	matching = filter_symbols_by_kind_and_path(symbols, WORKSPACE_ROOT, kinds=kinds, path=path)
	if not matching:
		filters = ", ".join(f"{k}={v!r}" for k, v in (("kind", kind), ("path", path)) if v)
		return f"No symbols matching {query!r} with {filters} ({len(symbols)} without the filters)."
	return format_workspace_symbols(matching, WORKSPACE_ROOT, query=query, fuzzy=fuzzy)


@mcp.tool()
@_notices.tool
async def diagnostics(file_path: str, ctx: Context | None = None) -> str:
	"""Get ty's type-check diagnostics (errors/warnings) for a single file."""
	await _use_workspace(ctx)
	try:
		_check_python_file(file_path)
		client = await get_client()
		items = await client.diagnostics(file_path)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return format_diagnostics(items)


@mcp.tool()
@_notices.tool
async def symbol_info(
	name: str | None = None,
	query: str | None = None,
	file_path: str | None = None,
	include_references: bool = True,
	ctx: Context | None = None,
) -> str:
	"""What is X and where is it used? Example: `symbol_info(name="UserService.create_user")`.

	One-call summary for a name: header, hover text, definition, and
	references grouped by file — the usual first lookup instead of chaining search_symbol → hover →
	definition → references by hand.

	`name` is a symbol name, or a dotted `Class.method` to resolve a specific
	method when the plain name is ambiguous. Pass `file_path` (relative to the
	workspace root) to disambiguate when several symbols share a name
	elsewhere in the workspace; if it's still ambiguous, the candidates are
	listed back so you can retry with a narrower name or file_path.
	`query` is accepted as an alias for `name`.
	"""
	await _use_workspace(ctx)
	try:
		name = resolve_name_query(preferred="name", example="UserService.create_user", name=name, query=query)
		client = await get_client()
		resolved = await resolve_symbol(client, WORKSPACE_ROOT, name, file_path=file_path)
		rel_path = uri_to_relative(resolved.uri, WORKSPACE_ROOT)
		line, column = resolved.line + 1, resolved.column + 1
		hover_result = await client.hover(rel_path, line, column)
		definition_locations = await client.definition(rel_path, line, column)
		reference_locations = await client.references(rel_path, line, column) if include_references else []
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	header = f"{resolved.name}  [{symbol_kind_label(resolved.kind)}]  ({rel_path}:{line}:{column})"
	hover_text = _format_hover_contents(hover_result.get("contents")) or "No hover information."
	definition_text = (
		"\n\n".join(format_location(loc, WORKSPACE_ROOT) for loc in definition_locations)
		if definition_locations
		else "No definition found."
	)
	parts = [header, "", hover_text, "", "Definition:", definition_text]
	if include_references:
		parts += ["", "References:", format_references_grouped(reference_locations, WORKSPACE_ROOT)]
	return "\n".join(parts)


@mcp.tool()
@_notices.tool
async def outline(file_path: str, ctx: Context | None = None) -> str:
	"""What's in this file? Example: `outline(file_path="src/app/services.py")`.

	Indented outline (classes, methods, functions, with line numbers) of a
	Python file, so you can navigate a large file without reading it in full.
	Follow up with hover/definition/references at a listed line, or
	symbol_info by name.
	"""
	await _use_workspace(ctx)
	try:
		_check_python_file(file_path)
		client = await get_client()
		symbols = await client.document_symbol(file_path)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return format_outline(symbols)


@mcp.tool()
@_notices.tool
async def callers(
	name: str | None = None,
	query: str | None = None,
	file_path: str | None = None,
	ctx: Context | None = None,
) -> str:
	"""Who calls this function? Example: `callers(name="create_user")`.

	Narrower than references, since it
	leaves out imports and type-only usages and only lists actual call sites.

	`name` resolves the same way as symbol_info (dotted Class.method accepted;
	pass file_path to disambiguate a common name). `query` is accepted as an
	alias for `name`.
	"""
	await _use_workspace(ctx)
	try:
		name = resolve_name_query(preferred="name", example="create_user", name=name, query=query)
		client = await get_client()
		resolved = await resolve_symbol(client, WORKSPACE_ROOT, name, file_path=file_path)
		rel_path = uri_to_relative(resolved.uri, WORKSPACE_ROOT)
		items = await client.prepare_call_hierarchy(rel_path, resolved.line + 1, resolved.column + 1)
		if not items:
			return f"{resolved.name} has no call hierarchy entry at that position (it may not be a callable)."
		incoming = await client.incoming_calls(items[0])
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return format_callers(incoming, WORKSPACE_ROOT)


def _module_path(abs_path: Path, base: Path | None = None) -> str:
	"""Dotted import path for a file under `base` (default `SOURCE_ROOT`)."""
	base = SOURCE_ROOT if base is None else base
	try:
		rel = abs_path.relative_to(base)
	except ValueError as exc:
		raise ToolInputError(
			f"{abs_path} is outside the source root ({base}); "
			"set CODENAV_MCP_SOURCE_ROOT if this project's importable code "
			"lives under a different directory."
		) from exc
	parts = rel.with_suffix("").parts
	if parts and parts[-1] == "__init__":
		parts = parts[:-1]
	if not parts:
		raise ToolInputError(f"cannot derive an import path for {abs_path}")
	return ".".join(parts)


def _is_protocol_base(base: ast.expr) -> bool:
	"""`Protocol`, `typing.Protocol`/`typing_extensions.Protocol`, or a
	subscripted `Protocol[T]` — `documentSymbol` doesn't expose base classes,
	so `implementations` parses the source directly to tell a Protocol port
	apart from an ordinary class."""
	if isinstance(base, ast.Subscript):
		base = base.value
	if isinstance(base, ast.Name):
		return base.id == "Protocol"
	if isinstance(base, ast.Attribute):
		return base.attr == "Protocol"
	return False


@dataclass(frozen=True)
class _ClassInfo:
	"""What `implementations` needs from a class's source: `documentSymbol`
	exposes neither base classes nor `self.x = ...` attributes."""

	name: str
	bases: tuple[str, ...]  # simple names of the base expressions
	is_protocol: bool
	members: frozenset[str]  # methods, properties, class-level and `self.` attributes (no dunders)


def _base_simple_name(base: ast.expr) -> str | None:
	if isinstance(base, ast.Subscript):
		base = base.value
	if isinstance(base, ast.Name):
		return base.id
	if isinstance(base, ast.Attribute):
		return base.attr
	return None


def _is_dunder(name: str) -> bool:
	return name.startswith("__") and name.endswith("__")


def _own_members(cls: ast.ClassDef) -> frozenset[str]:
	names: set[str] = set()

	def add_target(target: ast.expr) -> None:
		if isinstance(target, ast.Name):
			names.add(target.id)
		elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
			names.add(target.attr)
		elif isinstance(target, (ast.Tuple, ast.List)):
			for element in target.elts:
				add_target(element)

	for stmt in cls.body:
		if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
			names.add(stmt.name)
			for node in ast.walk(stmt):
				if isinstance(node, ast.Assign):
					for target in node.targets:
						add_target(target)
				elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
					add_target(node.target)
		elif isinstance(stmt, ast.AnnAssign):
			add_target(stmt.target)
		elif isinstance(stmt, ast.Assign):
			for target in stmt.targets:
				add_target(target)
	return frozenset(n for n in names if not _is_dunder(n))


def _class_infos(source: str) -> list[_ClassInfo]:
	"""Every class in `source` (at any nesting level)."""
	try:
		tree = ast.parse(source)
	except SyntaxError:
		return []
	return [
		_ClassInfo(
			name=node.name,
			bases=tuple(n for n in map(_base_simple_name, node.bases) if n),
			is_protocol=any(_is_protocol_base(base) for base in node.bases),
			members=_own_members(node),
		)
		for node in ast.walk(tree)
		if isinstance(node, ast.ClassDef)
	]


def _protocol_class_names(source: str) -> set[str]:
	"""Names of every class in `source` (at any nesting level) that subclasses
	`Protocol`."""
	return {info.name for info in _class_infos(source) if info.is_protocol}


# path -> ((mtime_ns, size), class infos): a pure function of the file's own
# text, so the stat pair is a sufficient key. Parsing every file's AST
# dominated `implementations`' warm cost before this.
_class_infos_cache: dict[Path, tuple[tuple[int, int], list[_ClassInfo]]] = {}


def _cached_class_infos(path: Path) -> list[_ClassInfo]:
	stat = path.stat()
	key = (stat.st_mtime_ns, stat.st_size)
	hit = _class_infos_cache.get(path)
	if hit is not None and hit[0] == key:
		return hit[1]
	infos = _class_infos(path.read_text(encoding="utf-8"))
	_class_infos_cache[path] = (key, infos)
	return infos


def _cached_protocol_class_names(path: Path) -> set[str]:
	return {info.name for info in _cached_class_infos(path) if info.is_protocol}


def _symbol_members(cls_node: dict[str, Any]) -> set[str]:
	"""Method/property/field names (no dunders) directly under a `to_symbol_tree` class node."""
	names: set[str] = set()
	for child in cls_node["children"]:
		if child["kind"] not in (6, 7, 8, 13):  # Method, Property, Field, Variable
			continue
		if not _is_dunder(child["name"]):
			names.add(child["name"])
	return names


def _effective_members(
	name: str,
	own: set[str],
	infos_by_name: dict[str, list[_ClassInfo]],
) -> set[str]:
	"""`own` plus everything inherited through same-workspace base classes
	(matched by simple name; a cycle or an unknown base just stops the walk).
	A class with the right method via a mixin or parent still satisfies a
	Protocol, so the name pre-filter has to see inherited members too."""
	members = set(own)
	seen = {name}
	pending = [b for info in infos_by_name.get(name, []) for b in info.bases]
	while pending:
		base = pending.pop()
		if base in seen:
			continue
		seen.add(base)
		for info in infos_by_name.get(base, []):
			members |= info.members
			pending.extend(info.bases)
	return members


# In-memory only (never written to disk). Its imports are absolute, so its
# directory doesn't affect resolution; the workspace root keeps it clear of
# path-scoped ty overrides (e.g. rules relaxed for `tests/**`) and of any
# project-specific layout.
_PROBE_RELATIVE_PATH = Path(".codenav_probe.py")


def _probe_source(port_module: str, port_name: str, candidate_module: str, candidate_name: str) -> str:
	imports = f"from {port_module} import {port_name}\n"
	if candidate_module != port_module:
		imports += f"from {candidate_module} import {candidate_name}\n"
	return f"{imports}\n\ndef _p(x: {candidate_name}) -> {port_name}:\n\treturn x\n"


# A probe verdict depends on the port and candidate files *and* whatever they
# import, so it can only be reused while nothing under SOURCE_ROOT has changed:
# the whole cache is keyed to one (path, mtime_ns, size) signature of every
# candidate file plus the dependency environment (see `_environment_paths`)
# and dropped wholesale when it differs. Verdicts are keyed by
# (port module, port name, candidate module, class name) -> (verified?, line).
_probe_cache: dict[tuple[str, str, str, str], tuple[bool, str]] = {}
_probe_cache_signature: tuple[tuple[str, int, int], ...] = ()
_probe_lock = asyncio.Lock()


def _environment_paths() -> list[Path]:
	"""Files/dirs whose change can alter a probe verdict without touching any
	source file: dependency manifests and the venv's `site-packages` (its own
	mtime moves whenever a package is installed, upgraded or removed — e.g. by
	`uv sync`)."""
	paths = [WORKSPACE_ROOT / "pyproject.toml", WORKSPACE_ROOT / "uv.lock"]
	for pattern in ("lib/python*/site-packages", "Lib/site-packages"):
		paths.extend((WORKSPACE_ROOT / ".venv").glob(pattern))
	return paths


def _source_signature(files: list[Path]) -> tuple[tuple[str, int, int], ...]:
	entries: list[tuple[str, int, int]] = []
	for path in [*files, *_environment_paths()]:
		try:
			stat = path.stat()
		except OSError:
			continue
		entries.append((str(path), stat.st_mtime_ns, stat.st_size))
	return tuple(entries)


@mcp.tool()
@_notices.tool
async def implementations(
	port_name: str | None = None,
	name: str | None = None,
	query: str | None = None,
	file_path: str | None = None,
	ctx: Context | None = None,
) -> str:
	"""Find concrete classes that structurally satisfy a `Protocol` port.

	Many hexagonal codebases define ports as `Protocol`s that adapters never
	subclass explicitly, so ty's own `implementation`/`typeHierarchy` return
	nothing for them. This scans classes under SOURCE_ROOT (the whole
	workspace by default; see CODENAV_MCP_SOURCE_ROOT) whose method names
	cover the protocol's, then verifies each candidate with ty's real type
	checker via an in-memory probe file (never written to disk) — so a
	result means "assignable", not just "same method names". `port_name`
	must itself resolve to a `Protocol` class; other classes' subclasses are
	better found with `references`/`symbol_info`. `name` and `query` are
	accepted as aliases for `port_name`. `file_path` narrows the port lookup
	to one file when the name exists in several. Members inherited from
	same-workspace base classes, and fields/properties declared by the port,
	count when matching names. Directories listed in
	CODENAV_MCP_EXTRA_SOURCE_ROOTS (e.g. `tests`) are scanned too; their
	matches (test doubles) are listed under a separate heading.
	"""
	await _use_workspace(ctx)
	try:
		port_name = resolve_name_query(
			preferred="port_name",
			example="FileSystemPort",
			port_name=port_name,
			name=name,
			query=query,
		)
		client = await get_client()
		port = await resolve_symbol(client, WORKSPACE_ROOT, port_name, file_path=file_path)
		port_rel_path = uri_to_relative(port.uri, WORKSPACE_ROOT)
		port_symbols = await client.document_symbol(port_rel_path)
		port_source = (WORKSPACE_ROOT / port_rel_path).read_text(encoding="utf-8")
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	if port.name not in _protocol_class_names(port_source):
		return (
			f"{port.name!r} is not a Protocol; implementations only finds structural "
			"implementers of Protocol ports (use symbol_info/references for explicit subclasses)."
		)
	port_class = next(
		(n for n in to_symbol_tree(port_symbols) if n["kind"] == 5 and n["name"] == port.name),  # Class
		None,
	)
	if port_class is None:
		return f"{port_name!r} did not resolve to a class in {port_rel_path}."
	port_own = _symbol_members(port_class)
	for info in _class_infos(port_source):
		if info.name == port.name:
			port_own |= info.members
	if not port_own:
		return f"{port.name} has no members to match candidates against."

	try:
		port_module = _module_path(WORKSPACE_ROOT / port_rel_path)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)

	# Vendored/generated/virtualenv trees are never this project's own source
	# and can be enormous (a `.venv` alone can dwarf the real codebase) — skip
	# them outright rather than walking (and failing to decode) thousands of
	# irrelevant files.
	primary_files = sorted(p for p in SOURCE_ROOT.rglob("*.py") if not is_excluded(p, SOURCE_ROOT))
	primary_set = set(primary_files)
	# Extra roots (tests, ...): scanned too, reported separately, imported relative to the workspace.
	extra_files = sorted(
		{
			p
			for root in EXTRA_SOURCE_ROOTS
			for p in root.rglob("*.py")
			if not is_excluded(p, root) and p not in primary_set
		}
	)
	extra_set = set(extra_files)
	all_candidate_files = [*primary_files, *extra_files]
	port_abs_path = (WORKSPACE_ROOT / port_rel_path).resolve()
	# Sequential, not `asyncio.gather`: firing ~90 concurrent documentSymbol
	# requests at ty made it respond with a "content modified" LSP error;
	# sequential stays fast (under a second for this codebase's size). Each
	# file is fetched independently (not one list comprehension) so a single
	# unreadable/undecodable candidate (binary file mis-suffixed `.py`,
	# permission error, …) is skipped rather than aborting the whole scan.
	candidate_files: list[Path] = []
	symbol_lists: list[list[dict[str, Any]]] = []
	unreadable: list[Path] = []
	for path in all_candidate_files:
		try:
			symbols = await client.document_symbol(str(path))
		except TOOL_ERRORS:
			unreadable.append(path)
			continue
		candidate_files.append(path)
		symbol_lists.append(symbols)

	infos_by_path: dict[Path, list[_ClassInfo]] = {}
	infos_by_name: dict[str, list[_ClassInfo]] = {}
	for path in candidate_files:
		try:
			infos = _cached_class_infos(path)
		except (OSError, UnicodeDecodeError):
			unreadable.append(path)
			continue
		infos_by_path[path] = infos
		for info in infos:
			infos_by_name.setdefault(info.name, []).append(info)
	port_required = _effective_members(port.name, port_own, infos_by_name)

	name_matches: list[tuple[Path, str, str, bool]] = []  # (path, class name, module path, from extra root)
	for path, symbols in zip(candidate_files, symbol_lists, strict=True):
		if path not in infos_by_path:
			continue
		other_protocols = {info.name for info in infos_by_path[path] if info.is_protocol}
		for cls_node in (n for n in to_symbol_tree(symbols) if n["kind"] == 5):
			if path.resolve() == port_abs_path and cls_node["name"] == port.name:
				continue  # the port never "implements" itself
			if cls_node["name"] in other_protocols:
				continue  # another Protocol, not a concrete implementer
			try:
				candidate_module = _module_path(path, WORKSPACE_ROOT if path in extra_set else None)
			except ToolInputError:
				continue  # outside SOURCE_ROOT's import-path derivation; can't probe it
			own = _symbol_members(cls_node)
			for info in infos_by_path[path]:
				if info.name == cls_node["name"]:
					own |= info.members
			if port_required <= _effective_members(cls_node["name"], own, infos_by_name):
				name_matches.append((path, cls_node["name"], candidate_module, path in extra_set))

	skipped_note = f" ({len(unreadable)} file(s) under {SOURCE_ROOT} skipped: unreadable)" if unreadable else ""
	if not name_matches:
		return (
			f"No classes under {SOURCE_ROOT} cover {port.name}'s methods: "
			f"{', '.join(sorted(port_required))}.{skipped_note}"
		)

	global _probe_cache_signature
	probe_uri = (WORKSPACE_ROOT / _PROBE_RELATIVE_PATH).as_uri()
	verified: list[str] = []
	unverified: list[str] = []
	verified_extra: list[str] = []
	unverified_extra: list[str] = []
	opened = False
	# All calls share one scratch document, so concurrent runs would overwrite
	# each other's probe (and close it under one another): serialize them.
	await _probe_lock.acquire()
	try:
		signature = _source_signature(all_candidate_files)
		if signature != _probe_cache_signature:
			_probe_cache.clear()
			_probe_cache_signature = signature
		for path, cls_name, candidate_module, is_extra in name_matches:
			cache_key = (port_module, port.name, candidate_module, cls_name)
			rel = uri_to_relative(path.as_uri(), WORKSPACE_ROOT)
			ok_bucket, bad_bucket = (verified_extra, unverified_extra) if is_extra else (verified, unverified)
			cached = _probe_cache.get(cache_key)
			if cached is not None:
				(ok_bucket if cached[0] else bad_bucket).append(cached[1])
				continue
			code = _probe_source(port_module, port.name, candidate_module, cls_name)
			if not opened:
				await client.open_scratch_document(probe_uri, code)
				opened = True
			else:
				await client.change_scratch_document(probe_uri, code)
			items = await client.pull_diagnostics(probe_uri)
			# Verified means the probe document has *no* diagnostics at all —
			# not just none tagged `invalid-return-type`. A candidate module
			# that fails to import (e.g. it lives outside where its own
			# import path resolves, a common miss when SOURCE_ROOT doesn't
			# match the project's real package layout) produces an
			# `unresolved-import` diagnostic instead, and with only the
			# narrower check that case was silently counted as verified even
			# though ty never actually checked the assignment.
			if not items:
				line, is_verified = f"{cls_name}  ({rel})", True
			elif any(item.get("code") == "invalid-return-type" for item in items):
				line = f"{cls_name}  ({rel})  — method names match but ty rejects the assignment"
				is_verified = False
			else:
				reasons = "; ".join(str(item.get("message", "")).splitlines()[0] for item in items[:3])
				line = f"{cls_name}  ({rel})  — could not verify: {reasons}"
				is_verified = False
			_probe_cache[cache_key] = (is_verified, line)
			(ok_bucket if is_verified else bad_bucket).append(line)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	finally:
		try:
			if opened:
				await client.close_scratch_document(probe_uri)
		finally:
			_probe_lock.release()

	lines = [f"{len(verified)} class(es) implement {port.name} (type-verified):{skipped_note}"]
	lines += verified or ["(none)"]
	if EXTRA_SOURCE_ROOTS:
		roots = ", ".join(
			str(r.relative_to(WORKSPACE_ROOT)) if r.is_relative_to(WORKSPACE_ROOT) else str(r)
			for r in EXTRA_SOURCE_ROOTS
		)
		lines += ["", f"{len(verified_extra)} more in extra roots ({roots}), e.g. test doubles (type-verified):"]
		lines += verified_extra or ["(none)"]
	if unverified or unverified_extra:
		lines += ["", "Method-name matches that don't type-check as the port:"]
		lines += [*unverified, *unverified_extra]
	return "\n".join(lines)


def _register_write_tools() -> None:
	"""Add the tools that change code (see `write_tools`, `symbol_tools`, `refactor_tools`)."""
	from codenav_mcp import tool_base, write_tools

	tool_base.bind(sys.modules[__name__])
	for tool_module in (write_tools,):
		for fn, tool_annotations in tool_module.TOOLS:
			mcp.tool(annotations=tool_annotations)(_notices.tool(fn))


_register_write_tools()


def main() -> None:
	"""Console entry point: serve over stdio."""
	mcp.run(transport="stdio")


if __name__ == "__main__":
	main()
