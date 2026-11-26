"""Listing a symbol's references without the shared formatter's fixed per-file cap.

`mcp_nav_shared.format.format_references` lists at most 25 files in its compact form whatever `limit` says, so a
symbol used in 30 files showed "… and 5 more file(s)" with no way to see them. Here `limit` (hits) is the only cap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp_nav_shared.format import (
	DEFAULT_REFERENCES_LIMIT,
	DEFAULT_REFERENCES_SNIPPET_LIMIT,
	_is_test_path,  # noqa: PLC2701  (the shared ordering: production files before tests)
	format_location,
	format_references_grouped,
	uri_to_relative,
)


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
