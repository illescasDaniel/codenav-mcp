"""Python-source helpers for the write tools: where a definition starts and
ends, re-indenting a snippet to fit a file, and syntax checks.

Everything works on text and `ast`, never on the language server, so it is
deterministic and cheap. Line numbers are 1-based like the tool inputs.
"""

from __future__ import annotations

import ast
import io
import re
import textwrap
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from mcp_nav_shared.errors import ToolInputError


_DefNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class PythonSyntaxError(ToolInputError):
	"""The text isn't valid Python, so it must not be written."""


def parse_python(text: str, filename: str = "<source>") -> ast.Module:
	try:
		return ast.parse(text, filename=filename)
	except SyntaxError as exc:
		where = f"line {exc.lineno}" if exc.lineno else "unknown line"
		raise PythonSyntaxError(f"{filename}: syntax error at {where}: {exc.msg}") from exc
	except ValueError as exc:  # e.g. null bytes
		raise PythonSyntaxError(f"{filename}: cannot parse: {exc}") from exc


def check_syntax(text: str, filename: str) -> None:
	parse_python(text, filename)


def line_start_offsets(text: str) -> list[int]:
	offsets = [0]
	for index, char in enumerate(text):
		if char == "\n":
			offsets.append(index + 1)
	return offsets


def source_lines(text: str) -> list[str]:
	"""Lines with their terminators (`\\n`, `\\r\\n`), like `str.splitlines(True)` but only on real line ends."""
	return text.splitlines(keepends=True)


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


def find_definition(text: str, *, name_line: int | None = None, qualname: str | None = None) -> Definition | None:
	"""The definition whose identifier is on `name_line`, or named `qualname` (`Class.method`)."""
	lines = source_lines(text)
	tree = parse_python(text)
	for definition in iter_definitions(tree, lines):
		if name_line is not None and definition.def_line == name_line:
			return definition
		if qualname is not None and definition.qualname == qualname:
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
	for line in text.splitlines():
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
		for token in tokenize.generate_tokens(io.StringIO(source).readline):
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


def fit_snippet(snippet: str, *, indent: str, file_text: str) -> str:
	"""A snippet (any indentation, any indent unit) ready to be placed at `indent` in a file.

	Always ends with exactly one newline.
	"""
	body = textwrap.dedent(snippet).strip("\n")
	body = convert_indent_unit(body, detect_indent_unit(file_text))
	return indent_block(body, indent).rstrip() + "\n"


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
