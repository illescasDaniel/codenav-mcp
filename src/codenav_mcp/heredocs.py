"""Python embedded in shell scripts: `python - <<'PY' ... PY` heredocs.

Projects keep small Python programs inside shell scripts (docs and screenshot generators, packaging). No type checker
sees them, and a plain text search cannot tell `FileSearchResult(lines=...)` from a shell `lines=$(...)`. This module
finds the heredocs whose body is Python, parses each body, and returns the exact places that name a symbol, so a
rename can offer them as optional sites.

Only quoted delimiters (`<<'PY'`, `<<"PY"`) count: the shell does not expand those bodies, so what is on disk is the
Python that runs and the columns are exact. A body that does not parse is skipped.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from codenav_mcp.pysource import utf16_column


SHELL_SUFFIXES = frozenset({".sh", ".bash"})
_HEREDOC = re.compile(r"""<<(-?)[ \t]*(['"])([A-Za-z_][A-Za-z0-9_]*)\2""")
_PYTHON_COMMAND = re.compile(r"(?<![\w.-])python(?:3(?:\.\d+)?)?(?![\w.-])")
_PYTHON_DELIMITERS = frozenset({"PY", "PYTHON", "PYEOF", "EOPY", "END_PYTHON"})

# What a name is at a site, for choosing which sites fit a rename.
Role = str  # "attr" (obj.name) | "keyword" (f(name=...)) | "name" (bare name) | "def" | "import"


@dataclass(frozen=True)
class EmbeddedSite:
	line: int  # 1-based line in the shell script
	column: int  # 0-based UTF-16 column of the name
	role: Role


def python_heredocs(text: str) -> list[tuple[int, list[str]]]:
	"""(0-based index of the first body line, body lines as written) of each quoted heredoc fed to Python."""
	rows = [row.removesuffix("\r") for row in text.split("\n")]
	found: list[tuple[int, list[str]]] = []
	index = 0
	while index < len(rows):
		match = _HEREDOC.search(rows[index])
		if match is None or not (_PYTHON_COMMAND.search(rows[index]) or match.group(3).upper() in _PYTHON_DELIMITERS):
			index += 1
			continue
		strip_tabs, delimiter = match.group(1) == "-", match.group(3)
		end = index + 1
		while end < len(rows) and (rows[end].lstrip("\t") if strip_tabs else rows[end]) != delimiter:
			end += 1
		if end == len(rows):
			break  # unterminated: the shell would read to the end of the file; not worth guessing
		found.append((index + 1, rows[index + 1 : end]))
		index = end + 1
	return found


def _strip(body: list[str], strip_tabs: bool) -> tuple[list[str], list[int]]:
	"""The body as Python sees it after `<<-` tab stripping and a common indent removed, and the characters removed
	from the start of each line (to map columns back)."""
	removed = []
	lines = []
	for row in body:
		cut = len(row) - len(row.lstrip("\t")) if strip_tabs else 0
		lines.append(row[cut:])
		removed.append(cut)
	indents = [len(line) - len(line.lstrip(" \t")) for line in lines if line.strip()]
	common = min(indents, default=0)
	if common and len({line[:common] for line in lines if line.strip()}) == 1:
		lines = [line[common:] if line.strip() else line.lstrip(" \t") for line in lines]
		removed = [cut + common for cut in removed]
	return lines, removed


def _name_spans(tree: ast.Module, lines: list[str], name: str) -> list[tuple[int, int, Role]]:
	"""(0-based line, 0-based character, role) of each place in a parsed body that names `name`."""
	spans: list[tuple[int, int, Role]] = []

	def at(lineno: int, byte_offset: int) -> int:
		return len(lines[lineno - 1].encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore"))

	for node in ast.walk(tree):
		if isinstance(node, ast.Attribute) and node.attr == name and node.end_lineno is not None:
			end = at(node.end_lineno, node.end_col_offset or 0)
			if lines[node.end_lineno - 1][end - len(name) : end] == name:
				spans.append((node.end_lineno - 1, end - len(name), "attr"))
		elif isinstance(node, ast.keyword) and node.arg == name:
			spans.append((node.lineno - 1, at(node.lineno, node.col_offset), "keyword"))
		elif isinstance(node, ast.Name) and node.id == name:
			spans.append((node.lineno - 1, at(node.lineno, node.col_offset), "name"))
		elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
			row = lines[node.lineno - 1]
			match = re.search(rf"\b(?:def|class)\s+({re.escape(name)})\b", row)
			if match:
				spans.append((node.lineno - 1, match.start(1), "def"))
		elif isinstance(node, ast.alias) and node.name.rsplit(".", 1)[-1] == name and hasattr(node, "lineno"):
			start = at(node.lineno, node.col_offset)
			offset = node.name.rfind(name)
			spans.append((node.lineno - 1, start + offset, "import"))
	return spans


def embedded_python_sites(text: str, name: str) -> list[EmbeddedSite]:
	"""Places in the Python heredocs of a shell script that name `name`, with their role, in file order."""
	if name not in text:
		return []
	rows = [row.removesuffix("\r") for row in text.split("\n")]
	sites: list[EmbeddedSite] = []
	for start, body in python_heredocs(text):
		header = _HEREDOC.search(rows[start - 1])
		lines, removed = _strip(body, bool(header and header.group(1) == "-"))
		try:
			tree = ast.parse("\n".join(lines))
		except (SyntaxError, ValueError):
			continue
		for line, char, role in _name_spans(tree, lines, name):
			row = rows[start + line]
			sites.append(EmbeddedSite(start + line + 1, utf16_column(row, char + removed[line]), role))
	return sorted(set(sites), key=lambda s: (s.line, s.column))
