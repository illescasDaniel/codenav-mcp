"""Places that still say the old name after a rename: what the type checker could not link.

A rename follows type-resolved references. What it leaves is exactly what an agent
must look at: attribute accesses on untyped objects, names in strings (`getattr`,
`mock.patch("pkg.mod.name")`, `__all__`), comments, and documentation.
"""

from __future__ import annotations

import ast
import io
import os
import re
import tokenize
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mcp_nav_shared.edits import TextEdit, decode_source, relative_name
from mcp_nav_shared.exclude import excluded_names

from codenav_mcp.deps import ENV_PREFIX, iter_python_files
from codenav_mcp.pysource import reject_nul, utf16_column


_TEXT_SUFFIXES = {".md", ".rst", ".txt", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".json"}
_MAX_TEXT_FILES = 400
_MAX_TEXT_BYTES = 512_000
_FSTRING_MIDDLE = getattr(tokenize, "FSTRING_MIDDLE", None)


@dataclass(frozen=True)
class Mention:
	path: str
	line: int
	kind: str  # "attr" | "code" | "string" | "comment" | "text"
	snippet: str
	column: int | None = None  # 0-based UTF-16 column of an "attr" / "code" name; None for the other kinds
	value: str | None = None  # a dotted string literal, when present


def _lines(text: str) -> list[str]:
	"""Lines as the tokenizer numbers them (only `\\n` ends a line; a trailing `\\r` is dropped)."""
	return [line.removesuffix("\r") for line in text.split("\n")]


def covered_positions(edits_by_path: dict[Path, list[TextEdit]]) -> dict[Path, set[tuple[int, int]]]:
	return {path.resolve(): {(e.start_line, e.start_character) for e in edits} for path, edits in edits_by_path.items()}


def _python_mentions(path: Path, rel: str, name: str, covered: set[tuple[int, int]]) -> list[Mention]:
	try:
		text = decode_source(path.read_bytes())[0]  # a BOM is not part of the source (tokenize would see U+FEFF)
	except (OSError, UnicodeDecodeError):
		return []
	reject_nul(text, rel)  # the tokenizer can escape with SystemError on a NUL: refuse, don't guess
	if name not in text:
		return []
	lines = _lines(text)
	word = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
	found: list[Mention] = []

	def add(row: int, kind: str, column: int | None = None, value: str | None = None) -> None:
		snippet = lines[row - 1].strip() if 0 < row <= len(lines) else ""
		found.append(Mention(rel, row, kind, snippet[:140], column, value))

	try:
		previous: tokenize.TokenInfo | None = None
		for token in tokenize.generate_tokens(io.StringIO(text).readline):
			row, col = token.start
			if token.type == tokenize.NAME and token.string == name:
				line = lines[row - 1] if row <= len(lines) else ""
				if (row - 1, utf16_column(line, col)) not in covered:
					after_dot = previous is not None and previous.type == tokenize.OP and previous.string == "."
					add(row, "attr" if after_dot else "code", utf16_column(line, col))
			elif token.type == tokenize.COMMENT and word.search(token.string):
				add(row, "comment")
			elif token.type == tokenize.STRING or token.type == _FSTRING_MIDDLE:
				rows = {row + token.string.count("\n", 0, match.start()) for match in word.finditer(token.string)}
				if rows:
					value = _dotted_literal(token)
					for match_row in sorted(rows):
						add(match_row, "string", value=value)
			if token.type not in (tokenize.NL, tokenize.COMMENT):
				previous = token
	except (tokenize.TokenError, IndentationError, SyntaxError):
		pass
	return found


def _dotted_literal(token: tokenize.TokenInfo) -> str | None:
	if token.type == tokenize.STRING:
		try:
			value = ast.literal_eval(token.string)
		except (SyntaxError, ValueError):
			return None
		if isinstance(value, str) and "." in value and all(part.isidentifier() for part in value.split(".")):
			return value
	return None


def _text_files(workspace: Path, limit: int | None) -> list[Path]:
	files: list[Path] = []
	skip = excluded_names(ENV_PREFIX)
	for directory, dirs, names in os.walk(workspace):
		if Path(directory) != workspace and ".git" in dirs + names:
			dirs[:] = []  # another checkout or linked worktree (e.g. .claude/worktrees/*), not this workspace
			continue
		dirs[:] = sorted(d for d in dirs if d not in skip)
		for filename in sorted(names):
			if Path(filename).suffix.lower() in _TEXT_SUFFIXES:
				files.append(Path(directory) / filename)
				if limit is not None and len(files) >= limit:
					return files
	return files


def _in_hidden_dir(rel: str) -> bool:
	"""True for a path below a dot-directory (`.agents`, `.cursor`, `.github`): tooling, not the project's code."""
	return any(part.startswith(".") for part in rel.split("/")[:-1])


def find_mentions(
	workspace: Path,
	name: str,
	covered: dict[Path, set[tuple[int, int]]],
	*,
	path_matches: Callable[[str], bool] | None = None,
	include_text: bool = True,
	text_file_limit: int | None = _MAX_TEXT_FILES,
) -> list[Mention]:
	"""Occurrences of `name` the rename did not touch, Python code first. Dot-directories are not scanned."""
	found: list[Mention] = []
	for path in iter_python_files(workspace):
		rel = relative_name(path, workspace)
		if _in_hidden_dir(rel) or (path_matches is not None and not path_matches(rel)):
			continue
		if path.is_symlink() or not path.is_file():
			continue
		found += _python_mentions(path, rel, name, covered.get(path.resolve(), set()))
	word = re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])")
	for path in _text_files(workspace, text_file_limit) if include_text else ():
		rel = relative_name(path, workspace)
		if _in_hidden_dir(rel) or (path_matches is not None and not path_matches(rel)):
			continue
		if path.is_symlink() or not path.is_file():
			continue
		try:
			if path.stat().st_size > _MAX_TEXT_BYTES:
				continue
			lines = _lines(decode_source(path.read_bytes())[0])
		except (OSError, UnicodeDecodeError):
			continue
		found += [
			Mention(rel, number, "text", line.strip()[:140])
			for number, line in enumerate(lines, 1)
			if word.search(line)
		]
	order = {"attr": 0, "code": 1, "string": 2, "comment": 3, "text": 4}
	return sorted(found, key=lambda m: (order[m.kind], m.path, m.line))


def format_mentions(
	mentions: list[Mention],
	old_name: str,
	*,
	per_kind: int = 6,
	attr_cap: int = 50,
	member: bool | None = None,
	attr_hint: str | None = None,
) -> list[str]:
	"""One note per kind (a heading plus a few example lines), for a plan's notes.

	An unlinked attribute access (`mock.old`) is the likeliest real use of a renamed member, so it comes first and
	is listed almost in full; a bare name is usually another symbol with the same name, so it keeps the short cap.
	`member` is True when the renamed symbol is a method or field, False for a module-level function or class
	(where `module.old` is common and less telling), None when unknown. `attr_hint` ends the attribute note
	(how to have them renamed too).
	"""
	if member:
		attr = f"`{old_name}` still accessed as an attribute the type checker could not link (mocks, untyped objects). Probably the renamed member; check each"
	elif member is False:
		attr = f"`{old_name}` accessed as an attribute the type checker could not link (`module.{old_name}` or an untyped object; may or may not be this symbol)"
	else:
		attr = f"`{old_name}` accessed as an attribute the type checker could not link (untyped objects, mocks; check these)"
	headings = {
		"attr": attr,
		"code": f"`{old_name}` also appears as a bare name (often a different symbol with the same name)",
		"string": f"`{old_name}` appears in strings (getattr/patch targets, __all__, messages)",
		"comment": f"`{old_name}` appears in comments",
		"text": f"`{old_name}` appears in non-Python files",
	}
	notes = []
	for kind, heading in headings.items():
		group = [m for m in mentions if m.kind == kind]
		if not group:
			continue
		cap = attr_cap if kind == "attr" else per_kind
		lines = [f"{heading}: {len(group)}"]
		lines += [f"    {m.path}:{m.line}: {m.snippet}" for m in group[:cap]]
		if len(group) > cap:
			lines.append(f"    ... {len(group) - cap} more")
		if kind == "attr" and attr_hint:
			lines.append(f"    {attr_hint}")
		notes.append("\n".join(lines))
	return notes
