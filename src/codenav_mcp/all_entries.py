"""Dropping a name from a module's `__all__ = [...]` / `(...)` literal as text, keeping its type and layout."""

from __future__ import annotations

import ast

from codenav_mcp.pysource import parse_python
from codenav_mcp.signature import SourceMap, apply_offset_edits


def _all_assignments(tree: ast.Module) -> list[ast.Assign]:
	return [
		stmt
		for stmt in tree.body
		if isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets)
	]


def _literal_index(tree: ast.Module) -> int | None:
	"""Which `__all__` assignment (counting from 0) holds the list or tuple literal that `__all__` ends up as."""
	found = None
	for index, stmt in enumerate(_all_assignments(tree)):
		if isinstance(stmt.value, (ast.List, ast.Tuple)):
			found = index
	return found


def _dropped_element(text: str, smap: SourceMap, element: ast.expr) -> tuple[int, int, str]:
	start, end = smap.start(element), smap.end(element)
	after = end
	while after < len(text) and text[after] in " \t\r\n":
		after += 1
	if after < len(text) and text[after] == ",":
		stop = after + 1
		while stop < len(text) and text[stop] == " ":
			stop += 1
		line_start = text.rfind("\n", 0, start) + 1
		line_end = text.find("\n", stop)
		line_end = len(text) if line_end == -1 else line_end
		rest = text[stop:line_end].strip()
		if not text[line_start:start].strip() and (not rest or rest.startswith("#")):
			# the entry is alone on its line (a trailing comment belongs to it): drop the whole line, so no
			# whitespace-only line is left behind
			return line_start, min(line_end + 1, len(text)), ""
		return start, stop, ""
	before = start - 1
	while before >= 0 and text[before] in " \t\r\n":
		before -= 1
	return (before if before >= 0 and text[before] == "," else start), end, ""


def drop_all_entry(text: str, name: str) -> str:
	"""`text` without `name` in its `__all__` literal (unchanged when there is no such literal or entry).

	A tuple stays a tuple: a one-entry tuple keeps its trailing comma, and a bare tuple that loses its only entry
	becomes `()`."""
	tree = parse_python(text)
	index = _literal_index(tree)
	if index is None:
		return text
	literal = _all_assignments(tree)[index].value
	assert isinstance(literal, (ast.List, ast.Tuple))  # noqa: S101, _literal_index only returns those
	dropped = [e for e in literal.elts if isinstance(e, ast.Constant) and e.value == name]
	if not dropped:
		return text
	smap = SourceMap(text)
	if isinstance(literal, ast.Tuple) and len(dropped) == len(literal.elts) and text[smap.start(literal)] != "(":
		return apply_offset_edits(text, [(smap.start(literal), smap.end(literal), "()")])
	result = apply_offset_edits(text, [_dropped_element(text, smap, element) for element in dropped])
	if isinstance(literal, ast.Tuple):
		result = _keep_tuple(result, index)
	return result


def _keep_tuple(text: str, index: int) -> str:
	"""Put back the comma that makes one entry a tuple (`("b")` is a string, `("b",)` is not)."""
	statement = _all_assignments(parse_python(text))[index]
	value = statement.value
	if isinstance(value, ast.Tuple):
		if len(value.elts) != 1:
			return text
		value = value.elts[0]
	smap = SourceMap(text)
	end = smap.end(value)
	following = text[end:].lstrip(" \t\r\n")
	if following.startswith(","):
		return text
	return text[:end] + "," + text[end:]
