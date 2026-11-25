"""Python-source helpers for the write tools: where a definition starts and
ends, re-indenting a snippet to fit a file, and syntax checks.

Everything works on text and `ast`, never on the language server, so it is
deterministic and cheap. Line numbers are 1-based like the tool inputs.
"""

from __future__ import annotations

import ast
import re
import sys
import textwrap
import tokenize
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from mcp_nav_shared.edits import convert_eol, detect_eol, split_lines
from mcp_nav_shared.errors import ToolInputError


_DefNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class PythonSyntaxError(ToolInputError):
	"""The text isn't valid Python, so it must not be written."""


_BOM = "\ufeff"
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")
_VERSION_NOTE_HINTS = ("unsupported", "not supported", "requires python", "only supported")


def reject_nul(text: str, filename: str = "<source>") -> None:
	"""Raise `PythonSyntaxError` when `text` holds a NUL byte (not a text file)."""
	if "\x00" in text:
		raise PythonSyntaxError(f"{filename}: contains a NUL byte; not a text file")


def _syntax_error(exc: SyntaxError, filename: str) -> PythonSyntaxError:
	"""The one short message for a `SyntaxError` on reads: file, line, what the parser said, and a version hint."""
	where = f"line {exc.lineno}" if exc.lineno else "unknown line"
	message = str(exc.msg or "invalid syntax")
	text = f"{filename}: syntax error at {where}: {message}"
	if sys.version_info < (3, 14) or any(hint in message.lower() for hint in _VERSION_NOTE_HINTS):
		v = sys.version_info
		text += f" (the server parses with Python {v.major}.{v.minor}.{v.micro}; syntax newer than that is refused)"
	return PythonSyntaxError(text)


def _write_refusal(exc: SyntaxError, filename: str, *, new_text: bool = False) -> PythonSyntaxError:
	"""The refusal for a write whose text does not parse with the server's own Python (one paragraph, no traceback)."""
	message = str(exc.msg or "invalid syntax")
	if new_text and "U+FEFF" in message:
		return PythonSyntaxError(
			f"{filename}: remove the byte-order mark (U+FEFF) from new_text; the file's own BOM is kept"
		)
	v = sys.version_info
	where = f"line {exc.lineno}" if exc.lineno else "an unknown line"
	return PythonSyntaxError(
		f"{filename}: not written: it does not parse with this server's Python {v.major}.{v.minor}.{v.micro} parser "
		f"({where}: {message}). If the file uses newer syntax (Python 3.13 or 3.14, for example PEP 696 "
		'`class C[T = int]:`, PEP 758 `except A, B:` or a `t"..."` string), this server cannot check it and refuses '
		"the write: run the server under a newer Python (3.14 or later) to edit it, or edit the file with another "
		f"tool. Otherwise fix the syntax error at {where}."
	)


def parse_python(text: str, filename: str = "<source>") -> ast.Module:
	"""`ast.parse` with short, file-named errors (`PythonSyntaxError`); NUL bytes are refused."""
	reject_nul(text, filename)
	try:
		with warnings.catch_warnings():
			warnings.simplefilter("ignore")
			return ast.parse(text, filename=filename)
	except SyntaxError as exc:
		raise _syntax_error(exc, filename) from exc
	except (ValueError, RecursionError, MemoryError) as exc:
		raise PythonSyntaxError(f"{filename}: cannot parse: the text is too deeply nested or not valid source") from exc


def check_syntax(text: str, filename: str) -> None:
	"""Raise `PythonSyntaxError` unless `text` compiles as a module.

	Stricter than `ast.parse`: the full compile also refuses what only the compiler sees
	(`from __future__` after other code, `return`/`yield`/`await` outside a function, ...).
	A leading U+FEFF is ignored: the file's own byte-order mark is kept by the write path.
	A text that does not parse is refused with `_write_refusal`, which names the parser version and the limit.
	"""
	reject_nul(text, filename)
	if text.startswith(_BOM):
		text = text[1:]
	try:
		_compile(text, filename, flags=ast.PyCF_ONLY_AST)
	except SyntaxError as exc:
		raise _write_refusal(exc, filename, new_text=True) from exc
	try:
		_compile(text, filename)
	except SyntaxError as exc:
		raise _write_refusal(exc, filename) from exc


def _compile(text: str, filename: str, *, flags: int = 0) -> None:
	try:
		with warnings.catch_warnings():
			warnings.simplefilter("ignore")
			compile(text, filename, "exec", flags=flags, dont_inherit=True)
	except (ValueError, RecursionError, MemoryError) as exc:
		raise PythonSyntaxError(f"{filename}: cannot parse: the text is too deeply nested or not valid source") from exc


def line_start_offsets(text: str) -> list[int]:
	"""Offset of the first character of every line; lines end at `\\r\\n`, `\\r` or `\\n` only (like `source_lines`)."""
	return [0, *(match.end() for match in _LINE_BREAK_RE.finditer(text))]


def source_lines(text: str) -> list[str]:
	"""Lines with their terminators (`\\n`, `\\r\\n`, `\\r`), like `str.splitlines(True)` but only on real line ends
	(not on form feed, NEL, U+2028 ...)."""
	return split_lines(text, keepends=True)


def leading_whitespace(line: str) -> str:
	return line[: len(line) - len(line.lstrip(" \t"))]


@dataclass(frozen=True)
class Definition:
	node: _DefNode
	name: str
	qualname: str
	kind: str  # "class" | "function" | "method"
	first_line: int  # includes decorators
	def_line: int
	last_line: int
	indent: str
	parents: tuple[_DefNode, ...]

	@property
	def is_class(self) -> bool:
		return isinstance(self.node, ast.ClassDef)

	@property
	def parent(self) -> _DefNode | None:
		return self.parents[-1] if self.parents else None


def iter_definitions(tree: ast.Module, lines: list[str]) -> Iterator[Definition]:
	"""Every class/function in the module, outer ones first."""

	def walk(body: list[ast.stmt], parents: tuple[_DefNode, ...]) -> Iterator[Definition]:
		for stmt in body:
			if isinstance(stmt, _DEF_TYPES):
				first = min([stmt.lineno, *(d.lineno for d in stmt.decorator_list)])
				qual = ".".join([*(p.name for p in parents), stmt.name])
				if isinstance(stmt, ast.ClassDef):
					kind = "class"
				else:
					kind = "method" if parents and isinstance(parents[-1], ast.ClassDef) else "function"
				yield Definition(
					node=stmt,
					name=stmt.name,
					qualname=qual,
					kind=kind,
					first_line=first,
					def_line=stmt.lineno,
					last_line=stmt.end_lineno or stmt.lineno,
					indent=leading_whitespace(lines[stmt.lineno - 1]) if stmt.lineno <= len(lines) else "",
					parents=parents,
				)
				yield from walk(stmt.body, (*parents, stmt))
			else:
				for child_body in _child_bodies(stmt):
					yield from walk(child_body, parents)

	yield from walk(tree.body, ())


def _child_bodies(stmt: ast.stmt) -> Iterator[list[ast.stmt]]:
	"""Statement lists nested in `if`/`try`/`with`/loops, where definitions may also sit."""
	for attr in ("body", "orelse", "finalbody"):
		value = getattr(stmt, attr, None)
		if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
			yield value
	for handler in getattr(stmt, "handlers", []) or []:
		yield handler.body
	for case in getattr(stmt, "cases", []) or []:
		yield case.body


def _is_overload(node: _DefNode, direct: set[str], modules: set[str]) -> bool:
	"""Recognize only decorators imported from typing or typing_extensions."""
	for decorator in node.decorator_list:
		target = decorator.func if isinstance(decorator, ast.Call) else decorator
		if isinstance(target, ast.Name) and target.id in direct:
			return True
		if (
			isinstance(target, ast.Attribute)
			and target.attr == "overload"
			and isinstance(target.value, ast.Name)
			and target.value.id in modules
		):
			return True
	return False


def find_definition(
	text: str, *, name_line: int | None = None, qualname: str | None = None, prefer_implementation: bool = False
) -> Definition | None:
	"""Find a definition; an explicit line stays exact unless implementation preference is requested."""
	lines = source_lines(text)
	tree = parse_python(text)
	direct: set[str] = set()
	modules: set[str] = set()
	for stmt in tree.body:
		if isinstance(stmt, ast.Import):
			modules.update(
				alias.asname or alias.name for alias in stmt.names if alias.name in {"typing", "typing_extensions"}
			)
		elif isinstance(stmt, ast.ImportFrom) and stmt.module in {"typing", "typing_extensions"}:
			direct.update(alias.asname or alias.name for alias in stmt.names if alias.name == "overload")
	definitions = list(iter_definitions(tree, lines))
	for definition in definitions:
		if (name_line is not None and definition.def_line == name_line) or (
			qualname is not None and definition.qualname == qualname
		):
			if (prefer_implementation or (name_line is None and qualname is not None)) and _is_overload(
				definition.node, direct, modules
			):
				for other in definitions:
					if other.qualname == definition.qualname and other.parent is definition.parent:
						if not _is_overload(other.node, direct, modules):
							return other
			return definition
	return None


def definitions_in(text: str) -> list[Definition]:
	return list(iter_definitions(parse_python(text), source_lines(text)))


def leading_comment_start(lines: list[str], first_line: int) -> int:
	"""First line of the comment block sitting directly above `first_line` (no blank line between)."""
	line = first_line
	while line > 1 and lines[line - 2].strip().startswith("#"):
		line -= 1
	return line


def detect_indent_unit(text: str) -> str:
	"""`\\t` or N spaces: whichever the file's first indented line uses."""
	for line in split_lines(text):
		if line.startswith("\t"):
			return "\t"
		stripped = line.lstrip(" ")
		if stripped and stripped != line and not line.lstrip().startswith(("#", '"', "'")):
			width = len(line) - len(stripped)
			return " " * (4 if width % 4 == 0 else width)
	return "\t" if "\n\t" in text else "    "


def _string_continuation_lines(source: str) -> set[int]:
	"""1-based lines that start inside a multi-line string token that is not a docstring."""
	protected: set[int] = set()
	try:
		tree = ast.parse(source)
	except SyntaxError:
		return protected
	docstring_lines: set[int] = set()
	for node in ast.walk(tree):
		if isinstance(node, (ast.Module, *_DEF_TYPES)) and node.body:
			first = node.body[0]
			if (
				isinstance(first, ast.Expr)
				and isinstance(first.value, ast.Constant)
				and isinstance(first.value.value, str)
			):
				docstring_lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
	try:
		lines = iter(source_lines(source))
		for token in tokenize.generate_tokens(lambda: next(lines, "")):
			if token.type == tokenize.STRING and token.end[0] > token.start[0]:
				protected.update(
					line for line in range(token.start[0] + 1, token.end[0] + 1) if line not in docstring_lines
				)
	except (tokenize.TokenError, IndentationError):
		return protected
	return protected


def convert_indent_unit(source: str, unit: str) -> str:
	"""Rewrite the leading whitespace of code lines from the snippet's own indent unit to `unit`."""
	own = detect_indent_unit(source)
	if own == unit:
		return source
	protected = _string_continuation_lines(source)
	out = []
	for number, line in enumerate(source_lines(source), start=1):
		if number in protected or not line.strip():
			out.append(line)
			continue
		lead = leading_whitespace(line)
		if own == "\t":
			depth = lead.count("\t")
			rest = lead.replace("\t", "")
		else:
			width = len(own)
			depth, remainder = divmod(len(lead.replace("\t", own)), width)
			rest = " " * remainder
		out.append(unit * depth + rest + line[len(lead) :])
	return "".join(out)


def indent_block(source: str, indent: str) -> str:
	"""Prefix every code line with `indent`; lines inside non-docstring multi-line strings stay as written."""
	if not indent:
		return source
	protected = _string_continuation_lines(source)
	out = []
	for number, line in enumerate(source_lines(source), start=1):
		out.append(line if number in protected or not line.strip() else indent + line)
	return "".join(out)


def fit_snippet(snippet: str, *, indent: str, file_text: str, match_eol: bool = False) -> str:
	"""A snippet (any indentation, any indent unit, any line endings) ready to be placed at `indent` in a file.

	Uses the file's own indent unit. Always ends with exactly one line ending: `\\n` by default, so callers
	that convert endings themselves keep working; with `match_eol=True` every line ending of the block is
	the file's dominant one (`\\r\\n` or `\\n`, `edits.detect_eol`), so a CRLF file stays CRLF. A caller
	that passes `match_eol=True` must not convert the result again (`.replace("\\n", eol)` would give `\\r\\r\\n`).
	"""
	body = textwrap.dedent(convert_eol(snippet, "\n")).strip("\n")
	body = convert_indent_unit(body, detect_indent_unit(file_text))
	fitted = indent_block(body, indent).rstrip() + "\n"
	return convert_eol(fitted, detect_eol(file_text)) if match_eol else fitted


def top_level_names(tree: ast.Module) -> set[str]:
	"""Names bound by statements directly in the module body (defs, classes, assignments, imports)."""
	names: set[str] = set()
	for stmt in tree.body:
		names |= _bound_by(stmt)
	return names


def _bound_by(stmt: ast.stmt) -> set[str]:
	names: set[str] = set()
	if isinstance(stmt, _DEF_TYPES):
		names.add(stmt.name)
	elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
		for alias in stmt.names:
			names.add((alias.asname or alias.name).split(".")[0])
	elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
		targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
		for target in targets:
			names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
	elif isinstance(stmt, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
		for body in _child_bodies(stmt):
			for inner in body:
				names |= _bound_by(inner)
	return names


def loaded_names(node: ast.AST) -> set[str]:
	"""Names read anywhere inside `node` and not bound inside it (its free variables, roughly).

	Deliberately an over-approximation of what the code needs from its module:
	a local that shadows a module-level name is subtracted, an attribute chain
	contributes its root name (`os` for `os.path.join`).
	"""
	loads: set[str] = set()
	stores: set[str] = set()
	for child in ast.walk(node):
		if isinstance(child, ast.Name):
			(stores if isinstance(child.ctx, (ast.Store, ast.Del)) else loads).add(child.id)
		elif isinstance(child, ast.arg):
			stores.add(child.arg)
		elif isinstance(child, _DEF_TYPES) and child is not node:
			stores.add(child.name)
		elif isinstance(child, (ast.Import, ast.ImportFrom)):
			for alias in child.names:
				stores.add((alias.asname or alias.name).split(".")[0])
		elif isinstance(child, ast.ExceptHandler) and child.name:
			stores.add(child.name)
	return loads - stores


def names_used_outside(tree: ast.Module, first_line: int, last_line: int) -> set[str]:
	"""Names read anywhere in the module except on lines `first_line`..`last_line` and in import
	statements; names inside string annotations and `__all__`-style string lists count too."""
	import_lines: set[int] = set()
	for node in ast.walk(tree):
		if isinstance(node, (ast.Import, ast.ImportFrom)):
			import_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
	used: set[str] = set()
	for node in ast.walk(tree):
		line = getattr(node, "lineno", None)
		if line is None or first_line <= line <= last_line or line in import_lines:
			continue
		if isinstance(node, ast.Name):
			used.add(node.id)
		elif isinstance(node, ast.Constant) and isinstance(node.value, str):
			used |= _names_in_string(node.value)
	return used


def _names_in_string(value: str) -> set[str]:
	if value.isidentifier():
		return {value}  # `__all__ = ["name"]`, a quoted forward reference
	if len(value) >= 80:
		return set()
	try:
		inner = ast.parse(value, mode="eval")
	except SyntaxError:
		return set()
	return {n.id for n in ast.walk(inner) if isinstance(n, ast.Name)}  # "list[Foo]"


def relative_posix(path: Path, root: Path) -> str:
	try:
		return path.resolve().relative_to(root.resolve()).as_posix()
	except ValueError:
		return path.as_posix()


def utf16_column(line: str, index: int) -> int:
	"""UTF-16 code-unit offset of character `index` of `line` (what LSP columns use)."""
	return len(line[:index].encode("utf-16-le")) // 2


def utf16_to_index(line: str, units: int) -> int:
	"""Index into `line` of the character at UTF-16 offset `units` (clamped to the line)."""
	count = 0
	for index, char in enumerate(line):
		if count >= units:
			return index
		count += 2 if ord(char) > 0xFFFF else 1
	return len(line)


def byte_to_utf16_column(line: str, byte_offset: int) -> int:
	"""UTF-16 offset for an `ast` `col_offset` (which counts UTF-8 bytes)."""
	return utf16_column(line, len(line.encode("utf-8")[:byte_offset].decode("utf-8", errors="ignore")))


def node_position(lines: list[str], node: ast.AST) -> tuple[int, int]:
	"""(1-based line, 1-based UTF-16 column) of an `ast` node's start."""
	line = lines[node.lineno - 1] if 0 < node.lineno <= len(lines) else ""  # type: ignore[attr-defined]
	return node.lineno, byte_to_utf16_column(line, node.col_offset) + 1  # type: ignore[attr-defined]


def definition_name_position(lines: list[str], definition: Definition) -> tuple[int, int]:
	"""(line, column), both 1-based, of the identifier in a `def`/`class` statement."""
	line = lines[definition.def_line - 1]
	match = re.search(rf"\b(?:def|class)\s+({re.escape(definition.name)})\b", line)
	index = match.start(1) if match else len(leading_whitespace(line))
	return definition.def_line, utf16_column(line, index) + 1


def parameter_position(lines: list[str], definition: Definition, parameter: str) -> tuple[int, int] | None:
	"""(line, column) of parameter `parameter` in a function definition, if it has one."""
	if isinstance(definition.node, ast.ClassDef):
		return None
	args = definition.node.args
	for arg in [
		*args.posonlyargs,
		*args.args,
		*args.kwonlyargs,
		*([args.vararg] if args.vararg else []),
		*([args.kwarg] if args.kwarg else []),
	]:
		if arg.arg == parameter:
			return node_position(lines, arg)
	return None
