"""Listing a symbol's references without the shared formatter's fixed per-file cap.

`mcp_nav_shared.format.format_references` lists at most 25 files in its compact form whatever `limit` says, so a
symbol used in 30 files showed "… and 5 more file(s)" with no way to see them. Here `limit` (hits) is the only cap.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
from typing import Any

from mcp_nav_shared.edits import read_source
from mcp_nav_shared.errors import TOOL_ERRORS
from mcp_nav_shared.format import (
	DEFAULT_REFERENCES_LIMIT,
	DEFAULT_REFERENCES_SNIPPET_LIMIT,
	_is_test_path,  # noqa: PLC2701  (the shared ordering: production files before tests)
	format_location,
	format_references_grouped,
	uri_to_path,
	uri_to_relative,
)

from codenav_mcp.pysource import byte_to_utf16_column, node_position, source_lines, utf16_column


def _hit_key(loc: dict[str, Any], workspace_root: Path) -> tuple[bool, str, int, int]:
	"""Production files before tests, then path, line and column: a total order, so a capped list is stable."""
	rel = uri_to_relative(loc.get("uri") or loc.get("targetUri", ""), workspace_root)
	start = (loc.get("range") or loc.get("targetSelectionRange") or loc.get("targetRange") or {}).get("start") or {}
	return (_is_test_path(rel), rel, int(start.get("line", 0)), int(start.get("character", 0)))


def format_references_listing(
	locations: list[dict[str, Any]],
	workspace_root: Path,
	*,
	limit: int = DEFAULT_REFERENCES_LIMIT,
	snippet_limit: int = DEFAULT_REFERENCES_SNIPPET_LIMIT,
) -> str:
	"""`N reference(s) in M file(s):`, then the first `limit` hits (in production-before-tests order): full
	snippets for a handful, otherwise `path: L12:4, L40:2` per file with no cap on the number of files.
	Hits beyond `limit` are counted, not listed."""
	if not locations:
		return "No references found at that position."
	ordered = sorted(locations, key=lambda loc: _hit_key(loc, workspace_root))
	shown = ordered[: max(1, limit)]
	files = {uri_to_relative(loc.get("uri") or loc.get("targetUri", ""), workspace_root) for loc in ordered}
	lines = [f"{len(ordered)} reference(s) in {len(files)} file(s):"]
	if len(shown) <= snippet_limit:
		lines.append("\n\n".join(format_location(loc, workspace_root) for loc in shown))
	else:
		grouped = format_references_grouped(shown, workspace_root, file_limit=len(shown), with_columns=True)
		lines += grouped.splitlines()[1:]  # the shared header counts only the shown hits
	omitted = len(ordered) - len(shown)
	if omitted > 0:
		lines.append(f"… {omitted} more omitted; raise limit= to see them")
	return "\n".join(lines)


def format_symbol_references(locations: list[dict[str, Any]], workspace_root: Path, retry_call: str) -> str:
	"""The compact per-file summary for `symbol_info`; when it leaves files out, the last line says how to list
	them all: `retry_call` is the exact `references(...)` call (the position form, so it picks the same symbol)."""
	text = format_references_grouped(locations, workspace_root)
	lines = text.splitlines()
	if lines and lines[-1].startswith("… and ") and lines[-1].endswith("more file(s)"):
		lines[-1] += f"; {retry_call} lists every hit"
	return "\n".join(lines)


def constructor_keyword_locations(class_references: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
	"""Map typed references to a dataclass to keywords in direct constructor calls.

	ty can resolve the callee in unreachable code even when it cannot resolve a constructor keyword there.
	Match the precise callee position so annotations, other constructors and same-named methods stay out.
	"""
	positions: dict[str, set[tuple[int, int]]] = defaultdict(set)
	for loc in class_references:
		uri = loc.get("uri") or loc.get("targetUri", "")
		start = (loc.get("range") or loc.get("targetSelectionRange") or {}).get("start")
		if start and uri.startswith("file:"):
			positions[uri].add((start["line"] + 1, start["character"] + 1))

	locations = []
	for uri, references in positions.items():
		try:
			text = read_source(uri_to_path(uri))[0]
			tree = ast.parse(text)
		except (*TOOL_ERRORS, SyntaxError):
			continue
		lines = source_lines(text)
		for node in ast.walk(tree):
			if not isinstance(node, ast.Call) or not any(keyword.arg == field for keyword in node.keywords):
				continue
			callee = node.func
			if isinstance(callee, ast.Name):
				callee_position = node_position(lines, callee)
			elif (
				isinstance(callee, ast.Attribute)
				and callee.end_lineno is not None
				and callee.end_col_offset is not None
			):
				end = byte_to_utf16_column(lines[callee.end_lineno - 1], callee.end_col_offset)
				callee_position = (callee.end_lineno, end - utf16_column(callee.attr, len(callee.attr)) + 1)
			else:
				continue
			if callee_position in references:
				for keyword in node.keywords:
					if keyword.arg == field:
						line, col = node_position(lines, keyword)
						locations.append(
							{
								"uri": uri,
								"range": {
									"start": {"line": line - 1, "character": col - 1},
									"end": {"line": line - 1, "character": col - 1 + utf16_column(field, len(field))},
								},
							}
						)
	return locations
