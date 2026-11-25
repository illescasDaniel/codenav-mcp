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

import argparse
import ast
import asyncio
import functools
import importlib.metadata
import inspect
import itertools
import json
import logging
import os
import re
import sys
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args, get_type_hints
from urllib.parse import urlparse

import mcp_nav_shared
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent
from mcp_nav_shared.edits import split_lines
from mcp_nav_shared.errors import TOOL_ERRORS, FailedText, ToolInputError, format_tool_error
from mcp_nav_shared.exclude import is_excluded, is_inside_nested_repo
from mcp_nav_shared.format import (
	STATIC_CALLERS_NOTE,
	filter_symbols_by_kind_and_path,
	format_callers,
	format_diagnostics,
	format_outline,
	format_references,
	format_references_grouped,
	format_workspace_symbols,
	outside_workspace_note,
	parse_kind_filter,
	snippet,
	split_inside_workspace,
	symbol_kind_label,
	to_symbol_tree,
	uri_to_path,
	uri_to_relative,
	workspace_symbol_position,
)
from mcp_nav_shared.lsp_client import LspClient, LspRequestError
from mcp_nav_shared.notices import NoticeBoard, package_source_dirs
from mcp_nav_shared.output import cap_output
from mcp_nav_shared.params import resolve_name_query
from mcp_nav_shared.resolve import ResolvedSymbol, SymbolNotFoundError, resolve_symbol
from mcp_nav_shared.transaction import read_only_from_env, refuse_home_path
from mcp_nav_shared.workspace import (
	WorkspaceConfigError,
	WorkspaceSelector,
	ignored_extra_source_roots,
	resolve_extra_source_roots,
	resolve_source_root,
)

import codenav_mcp
from codenav_mcp import tool_base
from codenav_mcp.pysource import reject_nul
from codenav_mcp.ty_command import resolve_ty_command, ty_config_note
from codenav_mcp.writes import READ_ONLY_ENV


logger = logging.getLogger(__name__)

# Which checkout/worktree to navigate is decided per request (see
# `WorkspaceSelector`): the host starts this process once, usually from the
# main checkout, even when the session works in a linked worktree.
try:
	_selector = WorkspaceSelector("CODENAV_MCP_WORKSPACE")
	WORKSPACE_ROOT = _selector.base
	_workspace_source = _selector.base_source
	# Root for import-path derivation and implementations' workspace-wide class
	# scan. Defaults to the workspace's `src/` layout when there is one, else the
	# whole workspace; set CODENAV_MCP_SOURCE_ROOT (e.g. to "src") in a project's
	# MCP config to pick another scope.
	SOURCE_ROOT = resolve_source_root("CODENAV_MCP_SOURCE_ROOT", WORKSPACE_ROOT, autodetect_src=True)
	# Extra directories `implementations` also scans (e.g. "tests", for test
	# doubles of a port); their matches are listed under a separate heading.
	EXTRA_SOURCE_ROOTS = resolve_extra_source_roots("CODENAV_MCP_EXTRA_SOURCE_ROOTS", WORKSPACE_ROOT)
except WorkspaceConfigError as exc:
	# The console script imports this module before `main` runs, so the check has to happen here. The
	# refusal is one line for the user: no traceback, non-zero exit.
	print(f"codenav-mcp: {exc}", file=sys.stderr)
	raise SystemExit(2) from None

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
		"references) once you have a specific line to inspect. To change code, prefer "
		"the write tools over hand edits where one fits: edit (text), edit_symbol (replace/"
		"insert/delete a definition by name), rename_symbol, change_signature, move (a "
		"symbol or a module), quick_fix. They type-check the edit before writing and write it "
		"when it adds no errors (max_new_errors=0; apply=false only previews); verify_changes "
		"shows what the working tree broke, undo_edit reverts. To read or change several files or "
		"symbols, send them as one batch(calls=[{tool, args}, ...]) instead of many separate calls. " + _POSITION_NOTE
	),
)

_PYTHON_EXTENSIONS = {".py", ".pyi"}
# ty reads these once at startup; a change restarts it (see `LspClient.config_names`).
_TY_CONFIG_NAMES = frozenset({"pyproject.toml", "ty.toml", ".ty.toml"})


class _CodenavNoticeBoard(NoticeBoard):
	def tool(self, fn: Any) -> Any:
		decorated = super().tool(fn)

		@functools.wraps(decorated)
		async def wrapper(*args: Any, **kwargs: Any) -> str | CallToolResult:
			result = await decorated(*args, **kwargs)
			if isinstance(result, FailedText):
				return CallToolResult(content=[TextContent(type="text", text=str(result))], is_error=True)
			return result

		return wrapper


_notices = _CodenavNoticeBoard("codenav", package_source_dirs(mcp_nav_shared, codenav_mcp))


def _post_ignored_extra_roots(root: Path) -> None:
	"""Extra source roots that are not folders inside `root` are skipped; say so once."""
	for entry in ignored_extra_source_roots("CODENAV_MCP_EXTRA_SOURCE_ROOTS", root):
		_notices.post(f"Ignored extra source root: {entry} (not an existing folder inside the workspace)")


_post_ignored_extra_roots(WORKSPACE_ROOT)

_client: LspClient | None = None
_client_lock = asyncio.Lock()


_workspace_lock = asyncio.Lock()


async def _configure_workspace(root: Path, source: str) -> None:
	"""Re-target the server at `root`: the language server, probe verdicts and
	every path derived from the old root belong to the previous tree."""
	global WORKSPACE_ROOT, SOURCE_ROOT, EXTRA_SOURCE_ROOTS, _client, _workspace_source, _probe_cache_signature
	# Resolve the new paths first: a config error must not leave a half-switched server.
	source_root = resolve_source_root("CODENAV_MCP_SOURCE_ROOT", root, autodetect_src=True)
	extra_roots = resolve_extra_source_roots("CODENAV_MCP_EXTRA_SOURCE_ROOTS", root)
	if _client is not None:
		try:
			await _client.stop()
		except Exception:
			logger.debug("failed to stop ty server on workspace switch", exc_info=True)
		_client = None
	WORKSPACE_ROOT = root
	SOURCE_ROOT = source_root
	EXTRA_SOURCE_ROOTS = extra_roots
	_post_ignored_extra_roots(root)
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
	refuse_home_path(file_path)  # WRX-020: `~/x.py` is not expanded, so it is refused before it is read
	suffix = Path(file_path).suffix.lower()
	if suffix not in _PYTHON_EXTENSIONS:
		raise ToolInputError(f"codenav only supports Python files (.py/.pyi), got {file_path!r}")
	# Refused before anything is read: a read tool must not open files outside the workspace.
	try:
		(WORKSPACE_ROOT / file_path).resolve().relative_to(WORKSPACE_ROOT.resolve())
	except ValueError:
		raise ToolInputError(f"{file_path} is outside the workspace; give a path inside it") from None


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
	described = [f"Type defined at {_display_path(uri, workspace_root)}:{line0 + 1}:{start_col + 1}"]
	try:
		source = uri_to_path(uri).read_text(encoding="utf-8")
		header = split_lines(source)[line0].strip()
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
		text = contents.get("value", str(contents)).strip()
	elif isinstance(contents, list):
		text = "\n".join(c.get("value", str(c)) if isinstance(c, dict) else str(c) for c in contents).strip()
	else:
		text = str(contents).strip()
	return _scrub_text(text)


# -- paths shown to the agent ----------------------------------------------------
#
# Workspace files show relative to the workspace; library files show a label
# (`<stdlib>/json/__init__.py`, `<site-packages>/pkg/mod.py`), never the
# absolute path of the machine. Passthrough text from ty is scrubbed the same way.

_LIBRARY_LABELS = (
	(re.compile(r"/[^\s'\"()\[\]]*?/site-packages/"), "<site-packages>/"),
	(re.compile(r"/[^\s'\"()\[\]]*?/typeshed/[^/\s'\"()\[\]]+/stdlib/"), "<stdlib>/"),
	(re.compile(r"/(?:usr/)?lib(?:64)?/python3\.\d+/"), "<stdlib>/"),
)


def _scrub_library(text: str) -> str:
	for pattern, label in _LIBRARY_LABELS:
		text = pattern.sub(label, text)
	return text


def _scrub_text(text: str) -> str:
	"""Replace the workspace root and library install paths in text from ty (hover, `Searched in` lines)."""
	for root in {str(WORKSPACE_ROOT), str(WORKSPACE_ROOT.resolve())}:
		text = text.replace(root + "/", "")
		# The root itself (a `Searched in` line for the workspace): no trailing slash, so it needs its own rule.
		text = re.sub(re.escape(root) + r"(?=[\s)\]'\",:]|$)", "<workspace root>", text, flags=re.MULTILINE)
	# Other `Searched in` entries (extra search paths from PYTHONPATH or config, outside the workspace): label them.
	text = re.sub(r"(?m)^(\s*info:\s+\d+\.\s+)/\S+(?=\s+\()", r"\1<outside workspace>", text)
	return _scrub_library(text)


def _display_path(uri: str, workspace_root: Path | None = None) -> str:
	parsed = urlparse(uri)
	if parsed.scheme == "vendored":  # ty's bundled typeshed: vendored://stdlib/builtins.pyi
		return f"<{parsed.netloc}>{parsed.path}"
	path = uri_to_path(uri)
	try:
		return path.relative_to(workspace_root or WORKSPACE_ROOT).as_posix()
	except ValueError:
		pass
	label = _scrub_library(path.as_posix())
	return label if label != path.as_posix() else f"<outside workspace>/{path.name}"


def _format_definition(loc: dict[str, Any]) -> str:
	"""`file:line:column` plus a short excerpt, with the path shown as `_display_path` gives it."""
	uri = loc.get("uri") or loc.get("targetUri", "")
	rng = loc.get("targetSelectionRange") or loc.get("range") or loc.get("targetRange") or {}
	start = rng.get("start", {})
	start_line = int(start.get("line", 0))
	start_col = int(start.get("character", 0))
	end_line = int((rng.get("end") or {}).get("line", start_line))
	header = f"{_display_path(uri)}:{start_line + 1}:{start_col + 1}"
	body = snippet(uri, start_line, end_line, context=2)
	return f"{header}\n{body}" if body else header


def _location_start(loc: dict[str, Any]) -> tuple[int, int]:
	rng = loc.get("targetSelectionRange") or loc.get("range") or loc.get("targetRange") or {}
	start = rng.get("start", {})
	return int(start.get("line", 0)), int(start.get("character", 0))


def _find_symbol_node(nodes: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
	"""First symbol called `name` in a document-symbol result, outer ones before nested ones."""
	queue = list(nodes)
	while queue:
		node = queue.pop(0)
		if str(node.get("name")) == name:
			return node
		queue.extend(node.get("children") or [])
	return None


async def _stub_twin_location(client: LspClient, loc: dict[str, Any]) -> dict[str, Any] | None:
	"""For a `.py` definition with a `.pyi` twin: the stub's declaration of the same name (ty answers with the `.py`)."""
	uri = loc.get("uri") or loc.get("targetUri", "")
	path = uri_to_path(uri)
	stub = path.with_suffix(".pyi")
	if path.suffix != ".py" or not stub.is_file():
		return None
	line, column = _location_start(loc)
	try:
		source_line = split_lines(path.read_text(encoding="utf-8"))[line]
	except (OSError, UnicodeDecodeError, IndexError):
		return None
	match = re.match(r"[A-Za-z_]\w*", source_line[column:])
	if match is None:
		return None
	try:
		members = await client.document_symbol(uri_to_relative(stub.as_uri(), WORKSPACE_ROOT))
	except TOOL_ERRORS:
		return None
	node = _find_symbol_node(members, match.group(0))
	if node is None:
		return None
	rng = node.get("selectionRange") or node.get("range") or (node.get("location") or {}).get("range") or {}
	return {"uri": stub.as_uri(), "range": rng}


_IMPORT_LINE_RE = re.compile(r"\s*(?:from\s+\S+\s+import\b|import\s+\S)")


async def _is_unresolved_import(client: LspClient, loc: dict[str, Any]) -> bool:
	"""Whether the definition is a line of a workspace file that imports a module ty cannot resolve."""
	uri = loc.get("uri") or loc.get("targetUri", "")
	path = uri_to_path(uri)
	try:
		rel = path.relative_to(WORKSPACE_ROOT).as_posix()
	except ValueError:
		return False
	line, _ = _location_start(loc)
	try:
		source_line = split_lines(path.read_text(encoding="utf-8"))[line]
	except (OSError, UnicodeDecodeError, IndexError):
		return False
	if _IMPORT_LINE_RE.match(source_line) is None:
		return False
	try:
		items = await client.diagnostics(rel)
	except TOOL_ERRORS:
		return False
	return any(
		item.get("code") == "unresolved-import"
		and int(((item.get("range") or {}).get("start") or {}).get("line", -1)) == line
		for item in items
	)


async def _definition_block(client: LspClient, loc: dict[str, Any]) -> str:
	text = _format_definition(loc)
	if await _is_unresolved_import(client, loc):
		text += "\nNote: the import does not resolve."
	return text


async def _definition_text(client: LspClient, locations: list[dict[str, Any]]) -> str:
	"""Definitions for display: a `.pyi` stub comes first, with its `.py` implementation as an `Also:` line."""
	blocks: list[str] = []
	for loc in locations:
		stub = await _stub_twin_location(client, loc)
		if stub is None:
			blocks.append(await _definition_block(client, loc))
			continue
		blocks.append(await _definition_block(client, stub))
		line, column = _location_start(loc)
		uri = loc.get("uri") or loc.get("targetUri", "")
		blocks.append(f"Also: {_display_path(uri)}:{line + 1}:{column + 1} (implementation)")
	return "\n\n".join(blocks)


_DEF_KEYWORD_RE = re.compile(r"(\s*)((?:async\s+)?(?:def|class))\b")


def _on_definition_keyword(file_path: str, line: int, column: int) -> bool:
	"""Whether the 1-indexed position is on the `def`/`class` keyword of a line (not on the name)."""
	if line < 1:
		return False
	try:
		source_line = split_lines((WORKSPACE_ROOT / file_path).read_text(encoding="utf-8"))[line - 1]
	except (OSError, UnicodeDecodeError, IndexError):
		return False
	match = _DEF_KEYWORD_RE.match(source_line)
	return match is not None and match.start(2) <= column - 1 < match.end(2)


def _function_bound_names(tree: ast.AST) -> set[str]:
	"""Names bound inside a function or lambda (locals and parameters): the module's symbol index never holds them."""
	names: set[str] = set()
	for node in ast.walk(tree):
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
			for inner in ast.walk(node):
				if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store):
					names.add(inner.id)
				elif isinstance(inner, ast.arg):
					names.add(inner.arg)
	return names


# Bounds for the scan that runs only after a name lookup has failed.
_LOCAL_SCAN_FILE_LIMIT = 2000
_LOCAL_SCAN_BYTE_LIMIT = 1_000_000


def _bound_in_function(name: str, file_path: str | None) -> bool:
	if not name.isidentifier():
		return False
	paths: Iterable[Path]
	if file_path is not None:
		paths = [WORKSPACE_ROOT / file_path]
	else:
		paths = itertools.islice(
			(p for p in WORKSPACE_ROOT.rglob("*.py") if not is_excluded(p, WORKSPACE_ROOT)), _LOCAL_SCAN_FILE_LIMIT
		)
	for path in paths:
		try:
			if path.stat().st_size > _LOCAL_SCAN_BYTE_LIMIT:
				continue
			source = path.read_text(encoding="utf-8")
		except (OSError, UnicodeDecodeError):
			continue
		if name not in source:
			continue
		try:
			tree = ast.parse(source)
		except (SyntaxError, ValueError):
			continue
		if name in _function_bound_names(tree):
			return True
	return False


def _local_hint(name: str) -> str:
	return f"{name} is a local variable/parameter; locals are only visible through hover/definition at their position."


def _local_only_in_file(name: str, file_path: str | None) -> bool:
	"""True when `file_path` binds `name` only inside functions: a local the symbol index never holds."""
	if file_path is None or not name.isidentifier():
		return False
	root = WORKSPACE_ROOT.resolve()
	target = (WORKSPACE_ROOT / file_path).resolve()
	if not target.is_relative_to(root):
		return False  # never read a file outside the workspace, not even for a hint
	try:
		tree = ast.parse(target.read_text(encoding="utf-8"))
	except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
		return False
	return name in _function_bound_names(tree) and name not in _outer_bound_names(tree)


def _outer_bound_names(tree: ast.AST) -> set[str]:
	"""Names bound at module or class level (outside every function body): what the symbol index can hold."""
	names: set[str] = set()
	pending: list[ast.AST] = [tree]
	while pending:
		node = pending.pop()
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
			names.add(node.name)
		elif isinstance(node, (ast.Import, ast.ImportFrom)):
			names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
		elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
			names.add(node.id)
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
			continue  # the body and the parameters are locals
		pending.extend(ast.iter_child_nodes(node))
	return names


def _ty_label(command: str) -> str:
	"""The ty program for the workspace() reply: workspace-relative when it lies in the workspace, else its name only (NAVN-034/035)."""
	path = Path(command)
	if not path.is_absolute():
		return command
	for candidate in (path, path.resolve()):
		for root in (WORKSPACE_ROOT, WORKSPACE_ROOT.resolve()):
			try:
				return candidate.relative_to(root).as_posix()
			except ValueError:
				continue
	return f"{path.name} (outside the workspace; full path not shown)"


def _source_root_line() -> str:
	if not os.environ.get("CODENAV_MCP_SOURCE_ROOT") and SOURCE_ROOT == WORKSPACE_ROOT / "src":
		return "Source root: src (auto-detected)"
	return f"Source root: {SOURCE_ROOT}"


def _pyproject_warning(root: Path) -> str | None:
	"""ty resolves imports from the folder it runs in, so a folder without a
	pyproject.toml gives wrong answers when its parent or its sub-packages have one."""
	if (root / "pyproject.toml").is_file():
		return None
	if root.parent != root and (root.parent / "pyproject.toml").is_file():
		return (
			f"Warning: no pyproject.toml here but {root.parent.name}/ has one; set CODENAV_MCP_WORKSPACE to that "
			"folder for cross-package navigation (results outside this workspace are not shown)."
		)
	packages = sorted(p.parent.name for p in root.glob("*/pyproject.toml"))
	if not packages:
		return None
	shown = ", ".join(f"{name}/" for name in packages[:3])
	more = f" and {len(packages) - 3} more" if len(packages) > 3 else ""
	return (
		f"Warning: no pyproject.toml here, but these sub-folders have one: {shown}{more}. ty resolves imports from "
		"this folder only, so cross-package navigation can fail. Set CODENAV_MCP_WORKSPACE to one package folder, "
		"or add a pyproject.toml here."
	)


@mcp.tool(annotations=tool_base.READS)
@_notices.tool
async def workspace(ctx: Context | None = None) -> str:
	"""Which directory is codenav navigating, and why?

	Also shows the source root, the write mode, the ty command and any configuration
	warnings. Use when results look like they come from the wrong checkout/worktree.
	"""
	await _use_workspace(ctx)
	writes = "disabled (CODENAV_MCP_READ_ONLY)" if read_only_from_env(READ_ONLY_ENV) else "enabled"
	ty_command = _ty_label(resolve_ty_command(WORKSPACE_ROOT)[0])  # the trailing "server" is not shown
	lines = [
		str(WORKSPACE_ROOT),
		f"chosen because: {_selector.explain(_workspace_source)}",
		_source_root_line(),
		f"Writes: {writes}",
		f"ty: {ty_command}",
	]
	lines += [note for note in (ty_config_note(WORKSPACE_ROOT), _pyproject_warning(WORKSPACE_ROOT)) if note]
	return "\n".join(lines)


@mcp.tool(annotations=tool_base.READS)
@_notices.tool
async def hover(file_path: str, line: int, column: int, ctx: Context | None = None) -> str:
	"""Get type/documentation info for the symbol at a position.

	`line` and `column` are 1-indexed. `column` is a UTF-16 character offset
	on the line (not a visual/display column): a leading tab counts as one
	character. Put the position on the symbol's name: on a `def`/`class`
	keyword there is no hover. Paths outside the workspace are refused
	before the file is read.
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
	if text:
		return text
	if _on_definition_keyword(file_path, line, column):
		return "No hover information at that position; put the cursor on the name, not the 'def' keyword."
	return "No hover information at that position."


@mcp.tool(annotations=tool_base.READS)
@_notices.tool
async def definition(file_path: str, line: int, column: int, ctx: Context | None = None) -> str:
	"""Go to the definition of the symbol at a position.

	`line` and `column` are 1-indexed. `column` is a UTF-16 character offset
	on the line (not a visual/display column): a leading tab counts as one
	character.

	Resolves through ty's type inference, so this works even when the call
	site only has a typed parameter/attribute (e.g. `services.some_method()`
	where `services: AppServices` is a constructor argument), not just
	direct references to a name in scope. A module with a `.pyi` stub gives
	the stub first and the `.py` implementation as an `Also:` line. Library
	locations show as `<stdlib>/...` or `<site-packages>/...`.
	"""
	await _use_workspace(ctx)
	try:
		_check_python_file(file_path)
		client = await get_client()
		locations = await client.definition(file_path, line, column)
		if not locations:
			return "No definition found at that position."
		return await _definition_text(client, locations)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)


def _is_under(path: Path, root: Path) -> bool:
	return path.resolve().is_relative_to(root.resolve())


def _workspace_relative(path: str) -> str:
	"""An absolute path inside the workspace as workspace-relative (a trailing slash is kept); other paths unchanged."""
	if not os.path.isabs(path):
		return path
	try:
		rel = Path(path).resolve().relative_to(WORKSPACE_ROOT.resolve())
	except ValueError:
		return path
	text = "" if rel == Path() else rel.as_posix()
	return f"{text}/" if text and path.endswith("/") else text


def _workspace_display_path(file_path: str) -> str:
	"""The file name as it is shown in replies: workspace-relative, forward slashes."""
	return Path(_workspace_relative(file_path)).as_posix() if file_path else file_path


def _check_at_least_one(value: int | None, name: str) -> None:
	if value is not None and value < 1:
		raise ToolInputError(f"{name} must be 1 or more")


def _location_uri(loc: dict[str, Any]) -> str:
	return loc.get("uri") or loc.get("targetUri", "")


def _symbol_uri(sym: dict[str, Any]) -> str:
	return (sym.get("location") or {}).get("uri", "")


def _caller_uri(call: dict[str, Any]) -> str:
	return (call.get("from") or {}).get("uri", "")


def _with_outside_note(text: str, outside: int) -> str:
	note = outside_workspace_note(outside)
	return f"{text}\n{note}" if note else text


def _owning_class_name(nodes: list[dict[str, Any]], name: str, line: int) -> str | None:
	"""The class (from a `to_symbol_tree` tree) that directly holds the member `name` at 0-based `line`."""
	for node in nodes:
		for child in node["children"]:
			if child["name"] == name and child["start_line"] <= line <= child["end_line"] and node["kind"] == 5:
				return str(node["name"])
		found = _owning_class_name(node["children"], name, line)
		if found is not None:
			return found
	return None


async def _method_owner_has_subclasses(client: LspClient, rel_path: str, resolved: ResolvedSymbol) -> bool:
	"""True when the method `resolved` belongs to a class that another class in the source roots inherits from."""
	owner = _owning_class_name(to_symbol_tree(await client.document_symbol(rel_path)), resolved.name, resolved.line)
	if owner is None:
		return False
	for root in (SOURCE_ROOT, *EXTRA_SOURCE_ROOTS):
		for path in _candidate_files(root):
			try:
				infos = _cached_class_infos(path)
			except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
				continue
			if any(owner in info.bases and info.name != owner for info in infos):
				return True
	return False


def _outline_source(file_path: str) -> str | None:
	"""The file text for the NUL and syntax checks; None when it is not readable as UTF-8."""
	try:
		return (WORKSPACE_ROOT / file_path).read_text(encoding="utf-8")
	except (OSError, UnicodeDecodeError):
		return None


async def _syntax_warning(client: LspClient, file_path: str) -> str:
	"""ty's first `invalid-syntax` line of the file, as a warning that the outline may be incomplete; '' when none."""
	lines = [
		int(((item.get("range") or {}).get("start") or {}).get("line", 0)) + 1
		for item in await client.diagnostics(file_path)
		if item.get("code") == "invalid-syntax"
	]
	if not lines:
		return ""
	return f"Warning: {_workspace_display_path(file_path)} has a syntax error at line {min(lines)}; the outline may be incomplete."


@mcp.tool(annotations=tool_base.READS)
@_notices.tool
async def references(
	file_path: str,
	line: int,
	column: int,
	include_declaration: bool = True,
	limit: int = 200,
	ctx: Context | None = None,
) -> str:
	"""Find all usages of the symbol at a position across the workspace.

	`line` and `column` are 1-indexed. `column` is a UTF-16 character offset
	on the line (not a visual/display column): a leading tab counts as one
	character.

	The first line is the exact total (`N reference(s) in M file(s):`). `limit`
	(default 200) caps how many hits are listed; production code is listed before
	tests. Hits in files outside the workspace are not shown.
	"""
	await _use_workspace(ctx)
	try:
		_check_python_file(file_path)
		_check_at_least_one(limit, "limit")
		client = await get_client()
		locations = await client.references(file_path, line, column, include_declaration=include_declaration)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	inside, outside = split_inside_workspace(locations, WORKSPACE_ROOT, _location_uri)
	return _with_outside_note(format_references(inside, WORKSPACE_ROOT, limit=limit), outside)


@mcp.tool(annotations=tool_base.READS)
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
	other tools. Results include a SymbolKind label and are capped; hits in
	files outside the workspace, and in nested git checkouts below it (a
	vendored repo, a linked worktree), are not shown.
	`name` is accepted as an alias for `query`. Narrow broad queries with
	`kind` (SymbolKind labels, comma-separated: `class`, `function,method`,
	`interface`, ...) and `path`. `path` is a workspace-relative prefix matched
	against the start of each file's path (`src/` matches `src/...`, but `p` also
	matches `plugins/...`); a value with `*`, `?` or `[` is a glob matched against the
	whole relative path (`src/**/*.py`; `*` also matches `/`). An absolute path inside
	the workspace is accepted. Production code ranks before tests.
	Loose fuzzy hits whose names don't contain the query are summarised as a
	count; pass `fuzzy=true` to list them with a fuzzy marker.
	A dotted query (`Cart.total`, `pkg.mod.func`) finds the name after the last dot
	whose owner is the part before it (its class, or its module path).
	"""
	await _use_workspace(ctx)
	try:
		kinds = parse_kind_filter(kind)
		query = resolve_name_query(preferred="query", example="create_user", query=query, name=name)
		path = _workspace_relative(path) if path else path
		if path:
			_check_search_path(path)
		client = await get_client()
		# ty matches names literally, so the query is NFKC-normalised first (`ﬁ` finds `fi`).
		qualifier, leaf = _split_dotted(_nfkc(query))
		symbols = await client.workspace_symbol(leaf)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	if not symbols:
		return f"No symbols matching {query!r}."
	inside, outside = split_inside_workspace(symbols, WORKSPACE_ROOT, _symbol_uri)
	# SRCH-028 / ENV-018: a nested git checkout (vendored repo, linked worktree) is another project, not part of this scan.
	kept = [sym for sym in inside if not _in_nested_repo(sym)]
	outside += len(inside) - len(kept)
	inside = kept
	if qualifier:
		inside = await _owned_by(client, inside, qualifier)
	if not inside:
		return _with_outside_note(f"No symbols matching {query!r}.", outside)
	matching = filter_symbols_by_kind_and_path(inside, WORKSPACE_ROOT, kinds=kinds, path=path)
	if not matching:
		filters = ", ".join(f"{k}={v!r}" for k, v in (("kind", kind), ("path", path)) if v)
		return _with_outside_note(
			f"No symbols matching {query!r} with {filters} ({len(inside)} without the filters).", outside
		)
	return _with_outside_note(format_workspace_symbols(matching, WORKSPACE_ROOT, query=leaf, fuzzy=fuzzy), outside)


def _check_search_path(path: str) -> None:
	"""SRCH-007: a path outside the workspace is refused by name, not answered as an empty result."""
	root = os.path.normpath(str(WORKSPACE_ROOT))
	target = os.path.normpath(os.path.join(root, path))
	if target != root and not target.startswith(root.rstrip(os.sep) + os.sep):
		raise ToolInputError(f"{path} is outside the workspace; give a path inside it")


def _in_nested_repo(sym: dict[str, Any]) -> bool:
	"""True when the symbol's file sits in a nested git checkout below the workspace (SRCH-028 / ENV-018)."""
	uri = _symbol_uri(sym)
	return bool(uri) and is_inside_nested_repo(uri_to_path(uri), WORKSPACE_ROOT) is not None


def _nfkc(text: str) -> str:
	"""The parser NFKC-normalises identifiers, so `ﬁ` and `fi` name the same symbol."""
	return unicodedata.normalize("NFKC", text)


def _split_dotted(query: str) -> tuple[str, str]:
	"""(owner, name) of a dotted query (`Cart.total` -> `Cart`, `total`); the owner is "" for a plain name."""
	owner, dot, leaf = query.rpartition(".")
	if not dot or not leaf:
		return "", query
	return owner, leaf


async def _owned_by(client: LspClient, symbols: list[dict[str, Any]], owner: str) -> list[dict[str, Any]]:
	"""The symbols whose owner is `owner`: the class that directly holds them, or a module path ending in it."""
	trees: dict[str, list[dict[str, Any]]] = {}
	kept: list[dict[str, Any]] = []
	for sym in symbols:
		uri, line, _column = workspace_symbol_position(sym)
		rel = uri_to_relative(uri, WORKSPACE_ROOT)
		if rel not in trees:
			try:
				trees[rel] = to_symbol_tree(await client.document_symbol(rel))
			except TOOL_ERRORS:
				trees[rel] = []
		module = re.sub(r"\.pyi?$", "", rel).replace("/", ".")
		owned_by_class = _owning_class_name(trees[rel], str(sym.get("name") or ""), line) == owner
		if owned_by_class or module == owner or module.endswith("." + owner):
			kept.append(sym)
	return kept


@mcp.tool(annotations=tool_base.READS)
@_notices.tool
async def diagnostics(file_path: str, ctx: Context | None = None) -> str:
	"""Get ty's type-check diagnostics (errors/warnings) for a single file.

	Each line starts with the file's workspace-relative path, then line:column.
	"""
	await _use_workspace(ctx)
	try:
		_check_python_file(file_path)
		client = await get_client()
		items = await client.diagnostics(file_path)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	# ty's info lines (`Searched in the following paths`) carry absolute paths: scrub them like hover does.
	return _scrub_text(format_diagnostics(items, path=_workspace_display_path(file_path)))


@mcp.tool(annotations=tool_base.READS)
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
	method when the plain name is ambiguous. A dotted module path
	(`pkg.mod.func`) resolves to the function in that module. Pass `file_path`
	(relative to the workspace root) to disambiguate when several symbols share
	a name elsewhere in the workspace; if it's still ambiguous, the candidates
	are listed back so you can retry with a narrower name or file_path. Local
	variables and parameters are not indexed, so they are not found by name.
	`query` is accepted as an alias for `name`; passing both with different
	values is refused.
	"""
	await _use_workspace(ctx)
	try:
		name = resolve_name_query(preferred="name", example="UserService.create_user", name=name, query=query)
		if _local_only_in_file(name, file_path):
			# Checked before the lookup: a same-named module-level symbol elsewhere must not hide the local.
			raise ToolInputError(_local_hint(name))
		client = await get_client()
		try:
			resolved = await resolve_symbol(client, WORKSPACE_ROOT, name, file_path=file_path)
		except SymbolNotFoundError as exc:
			if _bound_in_function(name, file_path):
				raise ToolInputError(_local_hint(name)) from exc
			raise
		rel_path = uri_to_relative(resolved.uri, WORKSPACE_ROOT)
		line, column = resolved.line + 1, resolved.column + 1
		hover_result = await client.hover(rel_path, line, column)
		definition_locations = await client.definition(rel_path, line, column)
		reference_locations = await client.references(rel_path, line, column) if include_references else []
		definition_text = (
			await _definition_text(client, definition_locations) if definition_locations else "No definition found."
		)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	header = f"{resolved.name}  [{symbol_kind_label(resolved.kind)}]  ({_display_path(resolved.uri)}:{line}:{column})"
	hover_text = _format_hover_contents(hover_result.get("contents")) or "No hover information."
	parts = [header, "", hover_text, "", "Definition:", definition_text]
	if include_references:
		parts += ["", "References:", format_references_grouped(reference_locations, WORKSPACE_ROOT)]
	return "\n".join(parts)


@mcp.tool(annotations=tool_base.READS)
@_notices.tool
async def outline(file_path: str, depth: int | None = None, ctx: Context | None = None) -> str:
	"""What's in this file? Example: `outline(file_path="src/app/services.py")`.

	Indented outline (classes, methods, functions, with line numbers) of a
	Python file, so you can navigate a large file without reading it in full.
	`depth=1` lists only the top-level definitions. A file with a syntax error
	gets a warning line first (the outline may be incomplete); a file that
	contains a NUL byte is refused.
	Follow up with hover/definition/references at a listed line, or
	symbol_info by name.
	"""
	await _use_workspace(ctx)
	try:
		_check_python_file(file_path)
		_check_at_least_one(depth, "depth")
		source = _outline_source(file_path)
		if source is not None:
			reject_nul(source, _workspace_display_path(file_path))
		client = await get_client()
		symbols = await client.document_symbol(file_path)
		warning = await _syntax_warning(client, file_path) if source is not None else ""
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	body = format_outline(symbols, max_depth=depth)
	return f"{warning}\n{body}" if warning else body


@mcp.tool(annotations=tool_base.READS)
@_notices.tool
async def callers(
	name: str | None = None,
	query: str | None = None,
	file_path: str | None = None,
	limit: int = 200,
	ctx: Context | None = None,
) -> str:
	"""Who calls this function? Example: `callers(name="create_user")`.

	Narrower than references, since it
	leaves out imports and type-only usages and only lists actual call sites.

	`name` resolves the same way as symbol_info (dotted Class.method accepted;
	pass file_path to disambiguate a common name). `query` is accepted as an
	alias for `name`. The first line is the exact count of callers; `limit`
	(default 200) caps how many are listed. A function that is only used as a
	value (passed or stored, never called) gets a note saying so. For a method of
	a class that has subclasses the reply says overriding methods are not included.
	Callers in files outside the workspace are not shown.
	"""
	await _use_workspace(ctx)
	try:
		name = resolve_name_query(preferred="name", example="create_user", name=name, query=query)
		_check_at_least_one(limit, "limit")
		client = await get_client()
		resolved = await resolve_symbol(client, WORKSPACE_ROOT, name, file_path=file_path)
		rel_path = uri_to_relative(resolved.uri, WORKSPACE_ROOT)
		items = await client.prepare_call_hierarchy(rel_path, resolved.line + 1, resolved.column + 1)
		if not items:
			return f"{resolved.name} has no call hierarchy entry at that position (it may not be a callable)."
		incoming = await client.incoming_calls(items[0])
		inside, outside = split_inside_workspace(incoming, WORKSPACE_ROOT, _caller_uri)
		value_only = False
		if not inside:
			uses = await client.references(rel_path, resolved.line + 1, resolved.column + 1, include_declaration=False)
			value_only = bool(split_inside_workspace(uses, WORKSPACE_ROOT, _location_uri)[0])
		static_note = resolved.kind == 6 and await _method_owner_has_subclasses(client, rel_path, resolved)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	if value_only:
		text = f"{resolved.name} is used as a value only (not called); see references for those uses."
	else:
		text = format_callers(inside, WORKSPACE_ROOT, limit=limit)
	if static_note:
		text = f"{text}\n{STATIC_CALLERS_NOTE}"
	return _with_outside_note(text, outside)


def _shown(path: Path) -> str:
	"""A path relative to the workspace root (never absolute, RC-032 family); `the workspace` for the root itself."""
	rel = os.path.relpath(path.resolve(), WORKSPACE_ROOT.resolve()).replace(os.sep, "/")
	return "the workspace" if rel == "." else rel


def _module_path(abs_path: Path, base: Path | None = None) -> str:
	"""Dotted import path for a file under `base` (default `SOURCE_ROOT`)."""
	base = SOURCE_ROOT if base is None else base
	try:
		rel = abs_path.relative_to(base)
	except ValueError as exc:
		raise ToolInputError(
			f"{_shown(abs_path)} is outside the source root ({_shown(base)}); "
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

	def add_target(target: ast.expr, *, in_method: bool = False) -> None:
		# Inside a method body a bare name is a local variable, not a member: only `self.<attr>` counts.
		if isinstance(target, ast.Name):
			if not in_method:
				names.add(target.id)
		elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
			names.add(target.attr)
		elif isinstance(target, (ast.Tuple, ast.List)):
			for element in target.elts:
				add_target(element, in_method=in_method)

	for stmt in cls.body:
		if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
			names.add(stmt.name)
			for node in ast.walk(stmt):
				if isinstance(node, ast.Assign):
					for target in node.targets:
						add_target(target, in_method=True)
				elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
					add_target(node.target, in_method=True)
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
	"""The in-memory probe: `_p` is assignable only if the candidate class conforms to the port. Both names are
	aliased, so a candidate with the port's name (or in the port's module) cannot shadow the port."""
	return (
		f"from {port_module} import {port_name} as _CodenavPort\n"
		f"from {candidate_module} import {candidate_name} as _CodenavCandidate\n"
		"\n\ndef _p(x: _CodenavCandidate) -> _CodenavPort:\n\treturn x\n"
	)


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


def _candidate_files(root: Path) -> list[Path]:
	"""Python sources under `root` that `implementations` may probe: every `.py`, plus each `.pyi` whose `.py`
	twin does not exist (a stub-only class is a declaration of code elsewhere; its matches are marked `(stub)`)."""
	files = [p for p in root.rglob("*.py") if not is_excluded(p, root)]
	files += [p for p in root.rglob("*.pyi") if not is_excluded(p, root) and not p.with_suffix(".py").exists()]
	return sorted(files)


@mcp.tool(annotations=tool_base.READS)
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
		return FailedText(
			f"{port.name!r} is not a Protocol; implementations only finds structural "
			"implementers of Protocol ports (use symbol_info/references for explicit subclasses)."
		)
	port_class = next(
		(n for n in to_symbol_tree(port_symbols) if n["kind"] == 5 and n["name"] == port.name),  # Class
		None,
	)
	if port_class is None:
		return FailedText(f"{port_name!r} did not resolve to a class in {port_rel_path}.")
	port_own = _symbol_members(port_class)
	for info in _class_infos(port_source):
		if info.name == port.name:
			port_own |= info.members
	if not port_own:
		return FailedText(f"{port.name} has no members to match candidates against.")

	port_abs = WORKSPACE_ROOT / port_rel_path
	# A port in an extra root (e.g. tests/) is imported relative to the workspace, as its candidates are.
	port_in_extra = not _is_under(port_abs, SOURCE_ROOT) and any(_is_under(port_abs, r) for r in EXTRA_SOURCE_ROOTS)
	try:
		port_module = _module_path(port_abs, WORKSPACE_ROOT if port_in_extra else None)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)

	# Vendored/generated/virtualenv trees are never this project's own source
	# and can be enormous (a `.venv` alone can dwarf the real codebase) — skip
	# them outright rather than walking (and failing to decode) thousands of
	# irrelevant files.
	primary_files = _candidate_files(SOURCE_ROOT)
	primary_set = set(primary_files)
	# Extra roots (tests, ...): scanned too, reported separately, imported relative to the workspace.
	extra_files = sorted({p for root in EXTRA_SOURCE_ROOTS for p in _candidate_files(root) if p not in primary_set})
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
		except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
			# One unparsable or unreadable candidate is skipped (and counted), never aborts the scan.
			unreadable.append(path)
			continue
		infos_by_path[path] = infos
		for info in infos:
			infos_by_name.setdefault(info.name, []).append(info)
	port_required = _effective_members(port.name, port_own, infos_by_name)

	name_matches: list[tuple[Path, str, str, bool]] = []  # (path, class name, module path, from extra root)
	not_probed = 0  # matches whose module has no import path (outside the source root): cannot be verified
	for path, symbols in zip(candidate_files, symbol_lists, strict=True):
		if path not in infos_by_path:
			continue
		other_protocols = {info.name for info in infos_by_path[path] if info.is_protocol}
		for cls_node in (n for n in to_symbol_tree(symbols) if n["kind"] == 5):
			if path.resolve() == port_abs_path and cls_node["name"] == port.name:
				continue  # the port never "implements" itself
			if cls_node["name"] in other_protocols:
				continue  # another Protocol, not a concrete implementer
			own = _symbol_members(cls_node)
			for info in infos_by_path[path]:
				if info.name == cls_node["name"]:
					own |= info.members
			if port_required <= _effective_members(cls_node["name"], own, infos_by_name):
				try:
					candidate_module = _module_path(path, WORKSPACE_ROOT if path in extra_set else None)
				except ToolInputError:
					not_probed += 1  # outside SOURCE_ROOT's import-path derivation; can't probe it
					continue
				name_matches.append((path, cls_node["name"], candidate_module, path in extra_set))

	skipped_note = f" ({len(unreadable)} file(s) under {_shown(SOURCE_ROOT)} skipped: unreadable)" if unreadable else ""
	if not name_matches:
		return (
			f"No classes under {_shown(SOURCE_ROOT)} cover {port.name}'s methods: "
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
			if path.suffix == ".pyi":
				rel = f"{rel}, stub"  # declarations only: the class is not type-checked here
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
	not_verified = len(unverified) + len(unverified_extra) + not_probed
	if not_verified:
		lines += ["", f"Method-name matches not verified as the port ({not_verified}):"]
		lines += [*unverified, *unverified_extra]
		if not_probed:
			lines.append(
				f"{not_probed} more could not be probed: their module has no import path under the source root."
			)
	return "\n".join(lines)


def _register_write_tools() -> None:
	"""Add the tools that change code (see `write_tools`, `symbol_tools`, `refactor_tools`)."""
	from mcp_nav_shared.errors import set_display_root

	from codenav_mcp import batch_tools, edit_tools, refactor_tools, symbol_tools, tool_base, write_tools

	tool_base.bind(sys.modules[__name__])
	set_display_root(_workspace_root_for_errors)
	for read_tool in (
		workspace,
		hover,
		definition,
		references,
		search_symbol,
		diagnostics,
		symbol_info,
		outline,
		callers,
		implementations,
	):
		batch_tools.register(read_tool.__name__, read_tool, writes=False)
	for tool_module in (write_tools, symbol_tools, refactor_tools, edit_tools):
		for fn, tool_annotations in tool_module.TOOLS:
			mcp.tool(annotations=tool_annotations)(_notices.tool(fn))
			batch_tools.register(fn.__name__, fn, writes=tool_annotations is tool_base.WRITES)
	# Last: the batch can reach every tool registered above and carries the strictest hints of them.
	batch_fn = batch_tools.make_batch_tool()
	batch_tools.register_signature("batch", batch_fn)
	mcp.tool(annotations=tool_base.WRITES)(_notices.tool(batch_fn))
	# Direct calls get the same argument checks as batch calls, before the SDK validates them.
	mcp.call_tool = _checked_call_tool(mcp.call_tool)
	mcp.middleware.append(_require_initialize)


def _workspace_root_for_errors() -> Path | None:
	"""Where file-not-found messages show paths relative to (see `mcp_nav_shared.errors.set_display_root`)."""
	return WORKSPACE_ROOT


def _refusal(text: str) -> Any:
	"""A tool error reply. It bypasses the notices wrapper, so the output cap is applied here."""
	from mcp.types import CallToolResult, TextContent

	return CallToolResult(content=[TextContent(type="text", text=cap_output(text))], is_error=True)


def _needs_literal_string_dispatch(fn: Any, arguments: dict[str, Any]) -> bool:
	"""Whether SDK JSON pre-parsing would alter a valid union-typed string argument."""
	hints = get_type_hints(fn)
	for key, value in arguments.items():
		annotation = hints.get(key)
		if not isinstance(value, str) or annotation is str or str not in get_args(annotation):
			continue
		try:
			parsed = json.loads(value)
		except (ValueError, RecursionError):
			continue
		if parsed is None or isinstance(parsed, (list, dict)):
			return True
	return False


def _checked_call_tool(original: Any) -> Any:
	"""Wrap `MCPServer.call_tool`: refuse unknown or mistyped arguments by name, in the shared short words.

	The SDK ignores unknown arguments and answers a wrong type with its own validation dump. This
	wrapper runs `batch_tools.argument_problem` (the batch's own check) first; a ValidationError the
	SDK still raises is turned into the same refusal. An unknown tool name is refused here too, so
	the SDK's own 'Unknown tool' reply (which echoes the name unbounded) is never sent.
	"""

	async def call_tool(name: str, arguments: dict[str, Any], context: Any = None) -> Any:
		from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
		from pydantic import ValidationError

		from codenav_mcp import batch_tools

		fn = batch_tools.signature_of(name)
		if fn is None:
			return _refusal(batch_tools.unknown_tool_message(name))
		problem = batch_tools.argument_problem(name, fn, arguments or {})
		if problem is not None:
			return _refusal(problem)
		if _needs_literal_string_dispatch(fn, arguments or {}):
			# The SDK pre-parses JSON-looking text for optional string parameters.
			# Preserve the caller's text after the same strict validation above. The
			# normal notice/cap/error wrapper still handles the tool result.
			from mcp.shared.exceptions import MCPError

			kwargs = dict(arguments)
			if "ctx" in inspect.signature(fn).parameters:
				kwargs["ctx"] = context if context is not None else Context(mcp_server=mcp)
			try:
				result = await _notices.tool(fn)(**kwargs)
			except MCPError:
				raise
			except Exception as exc:
				raise UnexpectedToolError(f"Error executing tool {name}") from exc
			if isinstance(result, CallToolResult):
				return result
			if isinstance(result, FailedText):
				return _refusal(str(result))
			return CallToolResult(
				content=[TextContent(type="text", text=result)], structured_content={"result": result}
			)
		try:
			return await original(name, arguments, context)
		except ToolError as exc:
			if isinstance(exc, UnexpectedToolError) or not isinstance(exc.__cause__, ValidationError):
				raise
			problem = batch_tools.argument_problem(name, fn, arguments or {})
			raise ToolError(problem or f"{name}: one or more arguments have the wrong type.") from exc

	return call_tool


async def _require_initialize(ctx: Any, call_next: Any) -> Any:
	"""Answer a request sent before `initialize` with a message that names the handshake (RC-108)."""
	from mcp.shared.exceptions import MCPError
	from mcp.types import INVALID_PARAMS

	try:
		return await call_next(ctx)
	except MCPError as exc:
		# The SDK's pre-initialize gate is the only INVALID_PARAMS with empty data reached before initialize.
		gate = exc.error.code == INVALID_PARAMS and exc.error.data == ""
		if ctx.request_id is None or ctx.session.client_params is not None or not gate:
			raise
		raise MCPError(INVALID_PARAMS, "the server is not initialized yet; send initialize first", "") from exc


_register_write_tools()


def _parse_args(argv: list[str]) -> None:
	"""Handle `--help` / `--version`; the server itself takes no arguments (see the README's Environment table)."""
	parser = argparse.ArgumentParser(
		prog="codenav-mcp",
		description=(
			"MCP server (stdio) for Python code navigation and editing, backed by ty. "
			"Started by an MCP client, it waits for JSON-RPC on stdin. Configure it through "
			"CODENAV_MCP_* environment variables."
		),
	)
	parser.add_argument("--version", action="version", version=f"%(prog)s {importlib.metadata.version('codenav-mcp')}")
	parser.parse_args(argv)


async def _serve_stdio() -> None:
	"""Serve over stdio. SIGINT or SIGTERM stops the language server and ends the process with status 0 (RC-016)."""
	import signal

	loop = asyncio.get_running_loop()
	shutdown: list[asyncio.Task[None]] = []

	def on_signal() -> None:
		if not shutdown:
			shutdown.append(loop.create_task(_shutdown_and_exit()))

	for signum in (signal.SIGINT, signal.SIGTERM):
		try:
			loop.add_signal_handler(signum, on_signal)
		except (NotImplementedError, RuntimeError):  # Windows, or not the main thread
			pass
	await mcp.run_stdio_async()
	await _stop_language_server()


async def _shutdown_and_exit() -> None:
	"""End the process after the language server is stopped.

	The SDK reads stdin in a thread that cannot be cancelled while stdin stays open, so `run_stdio_async`
	may never return after a signal; exiting here is the only reliable end.
	"""
	import os

	await _stop_language_server()
	for stream in (sys.stdout, sys.stderr):
		stream.flush()
	os._exit(0)


async def _stop_language_server() -> None:
	import anyio

	client = _client
	if client is None:
		return
	with anyio.move_on_after(3):  # seconds; the SDK's stdio read cannot be cancelled, so do not wait for it
		try:
			await client.stop()
		except Exception:  # noqa: BLE001 - shutting down: there is no client left to report to
			logger.debug("language server did not stop cleanly", exc_info=True)


# The SDK's own DEBUG records for inbound lines it drops (it sends no reply for them, see RC-046).
_DROPPED_INBOUND = {
	"transport yielded exception: %r": "a line that is not a JSON-RPC message",
	"dropping response for unknown/late request id %r": "a response to no request of this server",
}


class _DroppedInboundHandler(logging.Handler):
	def emit(self, record: logging.LogRecord) -> None:
		reason = _DROPPED_INBOUND.get(record.msg) if isinstance(record.msg, str) else None
		if reason is not None:
			sys.stderr.write(f"codenav-mcp: ignored a non-request message ({reason}); no reply is sent\n")


def _report_dropped_inbound() -> None:
	"""Write one stderr line per inbound message the SDK ignores, so the drop is visible (the SDK offers no hook)."""
	dispatcher_log = logging.getLogger("mcp.shared.jsonrpc_dispatcher")
	dispatcher_log.setLevel(logging.DEBUG)
	dispatcher_log.propagate = False  # keep the SDK's own traceback out of stderr; our one line replaces it
	dispatcher_log.addHandler(_DroppedInboundHandler(logging.DEBUG))


def main(argv: list[str] | None = None) -> None:
	"""Console entry point: serve over stdio until the client disconnects or the process is interrupted."""
	import anyio

	_parse_args(sys.argv[1:] if argv is None else argv)
	_report_dropped_inbound()
	try:
		anyio.run(_serve_stdio)
	except KeyboardInterrupt:  # Ctrl-C before the serving loop installed its own handler
		return


if __name__ == "__main__":
	main()
