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
import sysconfig
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args, get_type_hints
from urllib.parse import urlparse

import mcp_nav_shared
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, Tool
from mcp_nav_shared.edits import split_lines
from mcp_nav_shared.errors import TOOL_ERRORS, FailedText, ToolInputError, format_tool_error
from mcp_nav_shared.exclude import excluded_names, is_excluded, is_inside_nested_repo
from mcp_nav_shared.format import (
	STATIC_CALLERS_NOTE,
	format_outline,
	outside_workspace_note,
	split_inside_workspace,
	to_symbol_tree,
	uri_to_path,
	uri_to_relative,
	workspace_symbol_position,
)
from mcp_nav_shared.lsp_client import LspClient, LspRequestError
from mcp_nav_shared.notices import NoticeBoard, package_source_dirs
from mcp_nav_shared.output import cap_output
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
from codenav_mcp.deps import ENV_PREFIX
from codenav_mcp.pysource import reject_nul
from codenav_mcp.scan_cap import workspace_too_large
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
	# Before anything reads the tree: a huge workspace (a whole home directory) would exhaust memory in the
	# language server and the file watcher. The refusal is the same one-line startup exit as the checks below.
	if (_too_large := workspace_too_large(WORKSPACE_ROOT)) is not None:
		raise WorkspaceConfigError(_too_large)
	# Root for import-path derivation and implementations' workspace-wide class
	# scan. Defaults to the workspace's `src/` layout when there is one, else the
	# whole workspace; set CODENAV_MCP_SOURCE_ROOT (e.g. to "src") in a project's
	# MCP config to pick another scope.
	SOURCE_ROOT = resolve_source_root("CODENAV_MCP_SOURCE_ROOT", WORKSPACE_ROOT, autodetect_src=True)
	# Extra directories to scan for implementations, such as tests/ for Protocol doubles.
	EXTRA_SOURCE_ROOTS = resolve_extra_source_roots("CODENAV_MCP_EXTRA_SOURCE_ROOTS", WORKSPACE_ROOT)
except WorkspaceConfigError as exc:
	# The console script imports this module before `main` runs, so the check has to happen here. The
	# refusal is one line for the user: no traceback, non-zero exit.
	print(f"codenav-mcp: {exc}", file=sys.stderr)
	raise SystemExit(2) from None


def _compact_schema(value: Any) -> Any:
	"""Trim discovery metadata and express simple unions more compactly."""
	if isinstance(value, list):
		return [_compact_schema(item) for item in value]
	if not isinstance(value, dict):
		return value
	schema = {
		key: _compact_schema(item)
		for key, item in value.items()
		if key not in {"title", "default"} and (key, item) != ("additionalProperties", True)
	}
	variants = schema.get("anyOf", [])
	if len(variants) == 2 and variants[1] == {"type": "null"}:
		variant = variants[0]
		if "enum" in variant:
			return {"enum": [*variant["enum"], None]}
		if "type" in variant:
			return {**variant, "type": [variant["type"], "null"]}
	if variants and all(isinstance(variant, dict) and variant.keys() == {"type"} for variant in variants):
		schema["type"] = [variant["type"] for variant in schema.pop("anyOf")]
	return schema


class _CodenavMCPServer(MCPServer):
	async def list_tools(self) -> list[Tool]:
		tools = await super().list_tools()
		for tool in tools:
			tool.input_schema = _compact_schema(tool.input_schema)
		return tools


mcp = _CodenavMCPServer(
	name="codenav",
	instructions=(
		'Python code navigation and refactors, type-resolved by ty. Name-first: lookup(name="Class.method") gives '
		"signature, location, typed references, callers and Protocol implementations; add file_path to narrow a "
		"name (or give file_path+line+column, 1-based UTF-16). Use grep for exact-name text and Read for known "
		"source. search finds symbols or text mentions, outline maps a file, check reports ty diagnostics (a "
		"file, the workspace, or since a git baseline), unused finds dead code, impact shows what breaks before "
		"you change an API. Refactor with rename, change_signature, move, edit_symbol (one definition, or "
		"edits=[...] for several at once) and fix. Writes apply by default (apply=true) when the type check adds "
		"no new errors; otherwise they are held as a preview, and apply=false always previews. edits lists, "
		"shows and applies previews, and undoes applied edits. The gate checks types only: run tests and docs checks yourself."
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

		# Python 3.14's lazy annotations are not copied by functools.wraps.
		wrapper.__annotations__ = get_type_hints(decorated)
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
	if (too_large := await asyncio.to_thread(workspace_too_large, root)) is not None:
		raise ToolInputError(too_large)  # refused before the old server is stopped or a new one started
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
	# Windows installs the standard library under Lib rather than lib/pythonX.Y.
	stdlib = sysconfig.get_path("stdlib")
	if stdlib:
		try:
			relative = path.resolve().relative_to(Path(stdlib).resolve())
			if "site-packages" in relative.parts:
				index = relative.parts.index("site-packages")
				return "<site-packages>/" + "/".join(relative.parts[index + 1 :])
			return f"<stdlib>/{relative.as_posix()}"
		except ValueError:
			pass
	label = _scrub_library(path.as_posix())
	return label if label != path.as_posix() else f"<outside workspace>/{path.name}"


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


_DEF_KEYWORD_RE = re.compile(r"(\s*)((?:async\s+)?(?:def|class))\b")


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
			(p for p in WORKSPACE_ROOT.rglob("*.py") if not is_excluded(p, WORKSPACE_ROOT, prefix=ENV_PREFIX)),
			_LOCAL_SCAN_FILE_LIMIT,
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
	return f"{name} is a local variable/parameter; locals are only visible through lookup at their position."


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
	"""The ty program for the check() reply: workspace-relative when it lies in the workspace, else its name only (NAVN-034/035)."""
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
	return f"Source root: {SOURCE_ROOT.relative_to(WORKSPACE_ROOT).as_posix()}"


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


def _workspace_details() -> list[str]:
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
	return lines


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


def _subclassed(owner: str) -> bool:
	"""True when another class in the source roots lists `owner` as a base."""
	for root in (SOURCE_ROOT, *EXTRA_SOURCE_ROOTS):
		for path in _candidate_files(root):
			try:
				infos = _cached_class_infos(path)
			except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
				continue
			if any(owner in info.bases and info.name != owner for info in infos):
				return True
	return False


async def _static_callers_note(client: LspClient, rel_path: str, resolved: ResolvedSymbol) -> str | None:
	"""The caveat for `callers` of a method whose class has other implementations, or None.

	Overrides in subclasses are found by name in the source roots; a `Protocol` member is satisfied
	structurally (no subclass), so it is recognised from the class declaration itself.
	"""
	owner = _owning_class_name(to_symbol_tree(await client.document_symbol(rel_path)), resolved.name, resolved.line)
	if owner is None:
		return None
	try:
		is_protocol = any(i.name == owner and i.is_protocol for i in _cached_class_infos(WORKSPACE_ROOT / rel_path))
	except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
		is_protocol = False
	if is_protocol:
		return (
			f"Static callers only; implementations of this Protocol member are not included: "
			f'lookup(name="{owner}", show=["implementations"]) lists them.'
		)
	return STATIC_CALLERS_NOTE if _subclassed(owner) else None


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


async def _search_symbols(client: LspClient, qualifier: str, leaf: str) -> tuple[list[dict[str, Any]], int, list[str]]:
	symbols = await client.workspace_symbol(leaf)
	inside, outside = split_inside_workspace(symbols, WORKSPACE_ROOT, _symbol_uri)
	# SRCH-028 / ENV-018: a nested git checkout (vendored repo, linked worktree) is another project, not part of this scan.
	kept = [sym for sym in inside if not _in_nested_repo(sym)]
	outside += len(inside) - len(kept)
	inside = kept
	# B28 / RC-002: excluded directories (build, env, dist, ... and CODENAV_MCP_EXCLUDE_DIRS / INCLUDE_DIRS) are skipped
	# here too, as for importer discovery and writes.
	excluded_dirs = [_excluded_dir_of(sym) for sym in inside]
	inside = [sym for sym, excluded_dir in zip(inside, excluded_dirs, strict=True) if excluded_dir is None]
	skipped = [d for d in excluded_dirs if d is not None]
	if qualifier:
		inside = await _owned_by(client, inside, qualifier)
	return inside, outside, skipped


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


def _excluded_dir_of(sym: dict[str, Any]) -> str | None:
	"""The workspace-relative excluded directory (`build/`, `app/env/`) holding the symbol's file, or None.
	Uses the same names as importer discovery and the write guard (B28 / RC-002)."""
	uri = _symbol_uri(sym)
	if not uri:
		return None
	try:
		parts = uri_to_path(uri).relative_to(WORKSPACE_ROOT).parts
	except ValueError:
		return None
	names = excluded_names(ENV_PREFIX)
	for index, part in enumerate(parts[:-1]):
		if part in names:
			return "/".join(parts[: index + 1]) + "/"
	return None


def _search_notes(outside: int, skipped_dirs: list[str]) -> list[str]:
	"""Notes for results dropped as outside the workspace or in excluded directories."""
	notes = [outside_workspace_note(outside)] if outside > 0 else []
	if skipped_dirs:
		counts: dict[str, int] = {}
		for directory in skipped_dirs:
			counts[directory] = counts.get(directory, 0) + 1
		groups = ", ".join(f"{directory} ({count})" for directory, count in counts.items())
		names = ",".join(dict.fromkeys(d.rstrip("/").rsplit("/", 1)[-1] for d in counts))
		notes.append(
			f"Symbols in excluded directories were not shown: {groups}. "
			f"Pass {ENV_PREFIX}_INCLUDE_DIRS={names} to include them."
		)
	return notes


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


async def _resolve_read_symbol(client: LspClient, name: str, file_path: str | None) -> ResolvedSymbol:
	# A same-named module-level symbol elsewhere must not hide a local.
	if _local_only_in_file(name, file_path):
		raise ToolInputError(_local_hint(name))
	try:
		return await resolve_symbol(client, WORKSPACE_ROOT, name, file_path=file_path)
	except SymbolNotFoundError as exc:
		if _bound_in_function(name, file_path):
			raise ToolInputError(_local_hint(name)) from exc
		raise


@mcp.tool(annotations=tool_base.READS, structured_output=False)
@_notices.tool
async def outline(file_path: str, depth: int | None = None, ctx: Context | None = None) -> str:
	"""Map a Python file before reading it; depth=1 shows top-level definitions. Example: `outline(file_path="src/app.py")`."""
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


async def _incoming_calls(
	client: LspClient, file_path: str, line: int, column: int
) -> tuple[list[dict[str, Any]] | None, int]:
	items = await client.prepare_call_hierarchy(file_path, line, column)
	if not items:
		return None, 0
	incoming = await client.incoming_calls(items[0])
	return split_inside_workspace(incoming, WORKSPACE_ROOT, _caller_uri)


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
# (port module, port name, candidate module, class name) -> rejection reason (None means verified).
_probe_cache: dict[tuple[str, str, str, str], str | None] = {}
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
	files = [p for p in root.rglob("*.py") if not is_excluded(p, root, prefix=ENV_PREFIX)]
	files += [
		p
		for p in root.rglob("*.pyi")
		if not is_excluded(p, root, prefix=ENV_PREFIX) and not p.with_suffix(".py").exists()
	]
	return sorted(files)


@dataclass(frozen=True)
class ProtocolImplementation:
	path: Path
	name: str
	line: int
	extra: bool

	def listing(self) -> str:
		rel = uri_to_relative(self.path.as_uri(), WORKSPACE_ROOT)
		stub = ", stub" if self.path.suffix == ".pyi" else ""
		return f"{self.name}  ({rel}{stub})"


@dataclass
class _ProtocolMatches:
	verified: list[ProtocolImplementation]
	rejected: list[tuple[ProtocolImplementation, str]]
	not_probed: int
	unreadable: int
	no_match_members: set[str] | None = None


async def _protocol_matches(client: LspClient, port: ResolvedSymbol) -> _ProtocolMatches:
	port_rel_path = uri_to_relative(port.uri, WORKSPACE_ROOT)
	port_symbols = await client.document_symbol(port_rel_path)
	port_source = (WORKSPACE_ROOT / port_rel_path).read_text(encoding="utf-8")
	if port.name not in _protocol_class_names(port_source):
		raise ToolInputError(
			f"{port.name!r} is not a Protocol; implementations only finds structural "
			'implementers of Protocol ports (use lookup with show=["subclasses"] for explicit subclasses).'
		)
	port_class = next(
		(n for n in to_symbol_tree(port_symbols) if n["kind"] == 5 and n["name"] == port.name),  # Class
		None,
	)
	if port_class is None:
		raise ToolInputError(f"{port.name!r} did not resolve to a class in {port_rel_path}.")
	port_own = _symbol_members(port_class)
	for info in _class_infos(port_source):
		if info.name == port.name:
			port_own |= info.members
	if not port_own:
		raise ToolInputError(f"{port.name} has no members to match candidates against.")

	port_abs = WORKSPACE_ROOT / port_rel_path
	# A port in an extra root (e.g. tests/) is imported relative to the workspace, as its candidates are.
	port_in_extra = not _is_under(port_abs, SOURCE_ROOT) and any(_is_under(port_abs, r) for r in EXTRA_SOURCE_ROOTS)
	port_module = _module_path(port_abs, WORKSPACE_ROOT if port_in_extra else None)

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

	name_matches: list[tuple[ProtocolImplementation, str]] = []
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
				candidate = ProtocolImplementation(
					path, cls_node["name"], cls_node["start_line"] + 1, path in extra_set
				)
				name_matches.append((candidate, candidate_module))

	matches = _ProtocolMatches([], [], not_probed, len(unreadable))
	if not name_matches:
		matches.no_match_members = port_required
		return matches

	global _probe_cache_signature
	probe_uri = (WORKSPACE_ROOT / _PROBE_RELATIVE_PATH).as_uri()
	opened = False
	# All calls share one scratch document, so concurrent runs would overwrite
	# each other's probe (and close it under one another): serialize them.
	await _probe_lock.acquire()
	try:
		signature = _source_signature(all_candidate_files)
		if signature != _probe_cache_signature:
			_probe_cache.clear()
			_probe_cache_signature = signature
		for candidate, candidate_module in name_matches:
			cache_key = (port_module, port.name, candidate_module, candidate.name)
			if cache_key in _probe_cache:
				reason = _probe_cache[cache_key]
				if reason is None:
					matches.verified.append(candidate)
				else:
					matches.rejected.append((candidate, reason))
				continue
			code = _probe_source(port_module, port.name, candidate_module, candidate.name)
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
				reason = None
				matches.verified.append(candidate)
			elif any(item.get("code") == "invalid-return-type" for item in items):
				reason = "method names match but ty rejects the assignment"
				matches.rejected.append((candidate, reason))
			else:
				reasons = "; ".join(str(item.get("message", "")).splitlines()[0] for item in items[:3])
				reason = f"could not verify: {reasons}"
				matches.rejected.append((candidate, reason))
			_probe_cache[cache_key] = reason
	finally:
		try:
			if opened:
				await client.close_scratch_document(probe_uri)
		finally:
			_probe_lock.release()

	return matches


def _unscanned_note() -> str:
	"""One line saying which root `implementations` scanned and how to include tests/, when a tests directory was left out."""
	if EXTRA_SOURCE_ROOTS:
		return ""
	left_out = [
		d.name
		for d in sorted(WORKSPACE_ROOT.iterdir() if WORKSPACE_ROOT.is_dir() else [])
		if d.is_dir()
		and d.name in ("tests", "test")
		and not _is_under(d, SOURCE_ROOT)
		and not is_excluded(d, WORKSPACE_ROOT, prefix=ENV_PREFIX)
	]
	if not left_out:
		return ""
	return (
		f"\nScanned only {_shown(SOURCE_ROOT)}; {', '.join(left_out)}/ not scanned (test doubles live there): "
		f"set CODENAV_MCP_EXTRA_SOURCE_ROOTS={','.join(left_out)} to include {'them' if len(left_out) > 1 else 'it'}."
	)


def _register_tools() -> None:
	"""Register the v2 tool modules and the checked MCP dispatch."""
	from mcp_nav_shared.errors import set_display_root

	from codenav_mcp import (
		analysis_tools,
		args,
		edit_tools,
		edits_tools,
		read_tools,
		refactor_tools,
		symbol_tools,
		tool_base,
		write_tools,
	)

	tool_base.bind(sys.modules[__name__])
	set_display_root(_workspace_root_for_errors)
	args.register("outline", outline)
	for tool_module in (write_tools, symbol_tools, refactor_tools, edit_tools, read_tools, analysis_tools, edits_tools):
		for fn, tool_annotations in tool_module.TOOLS:
			mcp.tool(annotations=tool_annotations, structured_output=False)(_notices.tool(fn))
			args.register(fn.__name__, fn)
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
	"""Wrap `MCPServer.call_tool`: rename guessed argument names, then refuse unknown or mistyped ones by name.

	The SDK ignores unknown arguments and answers a wrong type with its own validation dump. This
	wrapper runs `args.argument_problem` first; a ValidationError the
	SDK still raises is turned into the same refusal. An unknown tool name is refused here too, so
	the SDK's own 'Unknown tool' reply (which echoes the name unbounded) is never sent.
	"""

	async def call_tool(name: str, arguments: dict[str, Any], context: Any = None) -> Any:
		from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
		from pydantic import ValidationError

		from codenav_mcp import args

		fn = args.signature_of(name)
		if fn is None:
			return _refusal(args.unknown_tool_message(name))
		# Guessed argument names (`symbol`, `file`, ...) become the real ones before anything is checked or called.
		arguments, problem = args.apply_aliases(name, fn, arguments or {})
		problem = problem or args.argument_problem(name, fn, arguments)
		if problem is not None:
			return _refusal(problem)
		if _needs_literal_string_dispatch(fn, arguments):
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
			return CallToolResult(content=[TextContent(type="text", text=result)])
		try:
			return await original(name, arguments, context)
		except ToolError as exc:
			if isinstance(exc, UnexpectedToolError) or not isinstance(exc.__cause__, ValidationError):
				raise
			problem = args.argument_problem(name, fn, arguments)
			raise ToolError(problem or f"{name}: one or more arguments have the wrong type.") from exc

	async def call_tool_recording_the_call(name: str, arguments: dict[str, Any], context: Any = None) -> Any:
		# A preview stored while serving this call remembers it, so a stale one can say how to redo it.
		from codenav_mcp.edit_state import call_text, current_call

		token = current_call.set(call_text(name, dict(arguments or {})))
		try:
			return await call_tool(name, arguments, context)
		finally:
			current_call.reset(token)

	return call_tool_recording_the_call


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


_register_tools()


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
