"""Attribute names written as strings: `getattr(obj, "old")`, `monkeypatch.setattr(obj, "old", ...)`, `patch.object(X, "old")`.

ty's rename never sees them. They are sites of the renamed symbol when the object is its owner (the declaring class or
module): written with the rename when ty resolves the object exactly to that owner, offered as optional sites when it
cannot say (untyped objects, a subclass that inherits the member, a module that re-exports the name), left alone when
the object is something else.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from mcp_nav_shared.errors import TOOL_ERRORS
from mcp_nav_shared.format import uri_to_path
from mcp_nav_shared.lsp_client import LspClient

from codenav_mcp.linked import classes_in_file
from codenav_mcp.pysource import PythonSyntaxError, byte_to_utf16_column, parse_python


# Calls that take (object, "attribute name", ...): the builtins by bare name, the mocking helpers by attribute.
_BUILTIN_CALLS = frozenset({"getattr", "hasattr", "setattr", "delattr"})
_ATTRIBUTE_CALLS = frozenset({"setattr", "delattr", "object"})  # monkeypatch.setattr, mock.patch.object, ...
# Where ty reports the type of something it knows nothing about (an untyped parameter).
_UNKNOWN_FILES = ("_internal.pyi",)
MAX_LOOKUPS = 300


@dataclass(frozen=True)
class AttrString:
	"""A string literal naming an attribute: its span in the row (UTF-16, quotes excluded) and where the object is."""

	line: int  # 1-based
	start: int  # 0-based UTF-16 column of the first character after the quote
	end: int
	obj_position: tuple[int, int]  # 1-based (line, column) to ask ty about


@dataclass
class Owner:
	"""What the renamed symbol belongs to: its class(es) (path, 1-based `class` line) or its module(s)."""

	classes: set[tuple[Path, int]] = field(default_factory=set)
	modules: set[Path] = field(default_factory=set)


Verdict = Literal["exact", "maybe", "other"]


def find_attr_strings(text: str, lines: list[str], old: str) -> list[AttrString]:
	"""String literals equal to `old` in the attribute-name slot of a getattr-style call."""
	try:
		tree = parse_python(text)
	except (PythonSyntaxError, ValueError, SystemError):
		return []  # a file that does not parse has no calls to offer
	found: list[AttrString] = []
	for node in ast.walk(tree):
		if not isinstance(node, ast.Call) or len(node.args) < 2:
			continue
		func = node.func
		named = (isinstance(func, ast.Name) and func.id in _BUILTIN_CALLS) or (
			isinstance(func, ast.Attribute) and func.attr in _ATTRIBUTE_CALLS
		)
		obj, name = node.args[0], node.args[1]
		if not named or isinstance(obj, ast.Constant):
			continue  # `monkeypatch.setattr("pkg.mod.name", ...)` is a dotted target, handled elsewhere
		span = isinstance(name, ast.Constant) and literal_span(lines, name, old)
		if span:
			found.append(AttrString(name.lineno, *span, _resolve_point(lines, obj)))
	return found


def literal_span(lines: list[str], node: ast.Constant, old: str) -> tuple[int, int] | None:
	"""(start, end) UTF-16 columns of the text inside the plain one-line string literal `node` when it reads `old`;
	None for any other literal (prefixed, escaped, implicitly joined, another value)."""
	if node.value != old or node.end_lineno != node.lineno:
		return None
	row = lines[node.lineno - 1].rstrip("\r\n")
	raw = row.encode("utf-8")[node.col_offset : node.end_col_offset].decode("utf-8", errors="ignore")
	if raw[:1] not in {"'", '"'} or raw != f"{raw[:1]}{old}{raw[:1]}":
		return None
	return byte_to_utf16_column(row, node.col_offset) + 1, byte_to_utf16_column(
		row, node.end_col_offset or node.col_offset
	) - 1


def _resolve_point(lines: list[str], obj: ast.expr) -> tuple[int, int]:
	"""The identifier ty can resolve for `obj`: the name itself, or the last attribute of a dotted expression."""
	if isinstance(obj, ast.Attribute):
		row = lines[(obj.end_lineno or obj.lineno) - 1]
		offset = (obj.end_col_offset or obj.col_offset) - len(obj.attr.encode("utf-8"))
		return obj.end_lineno or obj.lineno, byte_to_utf16_column(row, offset) + 1
	row = lines[obj.lineno - 1]
	return obj.lineno, byte_to_utf16_column(row, obj.col_offset) + 1


def _locations(items: list[dict[str, Any]]) -> list[tuple[Path, int, int]]:
	found = []
	for item in items:
		uri = str(item.get("uri") or item.get("targetUri") or "")
		start = (item.get("range") or item.get("targetSelectionRange") or {}).get("start", {})
		if uri.startswith("file:"):
			found.append((uri_to_path(uri).resolve(), int(start.get("line", 0)) + 1, int(start.get("character", 0))))
	return found


async def classify(
	client: LspClient, workspace: Path, path: Path, obj_position: tuple[int, int], old: str, owner: Owner
) -> Verdict:
	"""Is the object at `obj_position` the owner of the renamed symbol (`exact`), possibly it (`maybe`), or not (`other`)?"""
	try:
		typed = _locations(await client.type_definition(str(path), *obj_position))
		named = _locations(await client.definition(str(path), *obj_position))
	except TOOL_ERRORS:
		return "maybe"
	# The object *is* the owner when ty's definition (a module, a class name) or its type (an instance) is the owner.
	if any(_is_owner(loc, owner) for loc in [*typed, *named]):
		return "exact"
	known = [loc for loc in typed if not loc[0].as_posix().endswith(_UNKNOWN_FILES)]
	if not known:
		return "maybe"  # untyped: nothing says it is another symbol
	root = workspace.resolve()
	for loc_path, line, column in known:
		if (line, column) == (1, 0):
			if _imports_name(loc_path, old):
				return "maybe"  # a module that imports or re-exports the name
		elif loc_path.is_relative_to(root) and _inherits(loc_path, line, old):
			return "maybe"  # a subclass that does not define the member itself: it inherits the renamed one
	return "other"


def _is_owner(location: tuple[Path, int, int], owner: Owner) -> bool:
	path, line, column = location
	if (line, column) == (1, 0):  # a module object (a class or def is located at its name)
		return path in owner.modules
	return (path, line) in owner.classes


def _imports_name(path: Path, name: str) -> bool:
	"""A same-named import can re-export the target. A local declaration belongs to this other module."""
	try:
		tree = parse_python(path.read_text(encoding="utf-8", errors="replace"), str(path))
	except (OSError, PythonSyntaxError, ValueError, SystemError):
		return False
	return any(
		isinstance(stmt, ast.ImportFrom) and any((alias.asname or alias.name) in {name, "*"} for alias in stmt.names)
		for stmt in tree.body
	)


def _inherits(path: Path, class_line: int, name: str) -> bool:
	"""The class at `class_line` has bases and does not define `name` itself."""
	info = next((i for i in classes_in_file(path) if i.ref.def_line == class_line), None)
	return info is not None and bool(info.bases) and name not in info.members
