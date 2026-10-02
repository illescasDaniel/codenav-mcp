"""Places that still say the old name after a rename: what the type checker could not link.

A rename follows type-resolved references. What it leaves is exactly what an agent
must look at: attribute accesses on untyped objects, names in strings (`getattr`,
`mock.patch("pkg.mod.name")`, `__all__`), comments, and documentation.
"""

from __future__ import annotations

import io
import os
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

from mcp_nav_shared.edits import TextEdit, relative_name
from mcp_nav_shared.exclude import EXCLUDED_DIR_NAMES

from codenav_mcp.deps import iter_python_files
from codenav_mcp.pysource import utf16_column


_TEXT_SUFFIXES = {".md", ".rst", ".txt", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".json"}
_MAX_TEXT_FILES = 400
_MAX_TEXT_BYTES = 512_000
_FSTRING_MIDDLE = getattr(tokenize, "FSTRING_MIDDLE", None)


@dataclass(frozen=True)
class Mention:
	path: str
	line: int
	kind: str  # "code" | "string" | "comment" | "text"
	snippet: str


def covered_positions(edits_by_path: dict[Path, list[TextEdit]]) -> dict[Path, set[tuple[int, int]]]:
	return {path.resolve(): {(e.start_line, e.start_character) for e in edits} for path, edits in edits_by_path.items()}


def _python_mentions(path: Path, rel: str, name: str, covered: set[tuple[int, int]]) -> list[Mention]:
	try:
		text = path.read_text(encoding="utf-8")
	except (OSError, UnicodeDecodeError):
		return []
	if name not in text:
		return []
	lines = text.splitlines()
	word = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
	found: list[Mention] = []

	def add(row: int, kind: str) -> None:
		snippet = lines[row - 1].strip() if 0 < row <= len(lines) else ""
		found.append(Mention(rel, row, kind, snippet[:140]))

	try:
		for token in tokenize.generate_tokens(io.StringIO(text).readline):
			row, col = token.start
			if token.type == tokenize.NAME and token.string == name:
				line = lines[row - 1] if row <= len(lines) else ""
				if (row - 1, utf16_column(line, col)) not in covered:
					add(row, "code")
			elif token.type == tokenize.COMMENT and word.search(token.string):
				add(row, "comment")
			elif (token.type == tokenize.STRING or token.type == _FSTRING_MIDDLE) and word.search(token.string):
				add(row, "string")
	except (tokenize.TokenError, IndentationError, SyntaxError):
		pass
	return found


def _text_files(workspace: Path) -> list[Path]:
	files: list[Path] = []
	for directory, dirs, names in os.walk(workspace):
		dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIR_NAMES)
		for filename in sorted(names):
			if Path(filename).suffix.lower() in _TEXT_SUFFIXES:
				files.append(Path(directory) / filename)
				if len(files) >= _MAX_TEXT_FILES:
					return files
	return files


def find_mentions(workspace: Path, name: str, covered: dict[Path, set[tuple[int, int]]]) -> list[Mention]:
	"""Occurrences of `name` the rename did not touch, Python code first."""
	found: list[Mention] = []
	for path in iter_python_files(workspace):
		found += _python_mentions(path, relative_name(path, workspace), name, covered.get(path.resolve(), set()))
	word = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
	for path in _text_files(workspace):
		try:
			if path.stat().st_size > _MAX_TEXT_BYTES:
				continue
			lines = path.read_text(encoding="utf-8").splitlines()
		except (OSError, UnicodeDecodeError):
			continue
		rel = relative_name(path, workspace)
		found += [
			Mention(rel, number, "text", line.strip()[:140])
			for number, line in enumerate(lines, 1)
			if word.search(line)
		]
	order = {"code": 0, "string": 1, "comment": 2, "text": 3}
	return sorted(found, key=lambda m: (order[m.kind], m.path, m.line))


def format_mentions(mentions: list[Mention], old_name: str, *, per_kind: int = 6) -> list[str]:
	"""One note per kind (a heading plus a few example lines), for a plan's notes."""
	headings = {
		"code": f"`{old_name}` still appears as code the type checker did not link (untyped or dynamic access; check these)",
		"string": f"`{old_name}` appears in strings (getattr/patch targets, __all__, messages)",
		"comment": f"`{old_name}` appears in comments",
		"text": f"`{old_name}` appears in non-Python files",
	}
	notes = []
	for kind, heading in headings.items():
		group = [m for m in mentions if m.kind == kind]
		if not group:
			continue
		lines = [f"{heading}: {len(group)}"]
		lines += [f"    {m.path}:{m.line}: {m.snippet}" for m in group[:per_kind]]
		if len(group) > per_kind:
			lines.append(f"    ... {len(group) - per_kind} more")
		notes.append("\n".join(lines))
	return notes
