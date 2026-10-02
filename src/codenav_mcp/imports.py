"""Reading and editing a module's top-level import statements as text."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from codenav_mcp.pysource import detect_indent_unit, leading_whitespace, parse_python, source_lines


_LINE_WIDTH = 120


@dataclass(frozen=True)
class ImportStmt:
	node: ast.Import | ast.ImportFrom
	first_line: int
	last_line: int
	module: str | None  # None for a plain `import x`
	level: int
	aliases: tuple[tuple[str, str | None], ...]  # (name, asname)
	conditional: bool  # inside `if TYPE_CHECKING:` / `try:` at module level

	@property
	def is_from(self) -> bool:
		return isinstance(self.node, ast.ImportFrom)

	@property
	def bound_names(self) -> list[str]:
		return [(asname or name).split(".")[0] for name, asname in self.aliases]


def module_imports(tree: ast.Module) -> list[ImportStmt]:
	"""Import statements at module level, including those nested in module-level `if`/`try`."""
	found: list[ImportStmt] = []

	def walk(body: list[ast.stmt], conditional: bool) -> None:
		for stmt in body:
			if isinstance(stmt, ast.Import):
				found.append(_stmt(stmt, None, 0, conditional))
			elif isinstance(stmt, ast.ImportFrom):
				found.append(_stmt(stmt, stmt.module, stmt.level, conditional))
			elif isinstance(stmt, ast.If):
				walk(stmt.body, True)
				walk(stmt.orelse, True)
			elif isinstance(stmt, ast.Try):
				walk(stmt.body, True)
				for handler in stmt.handlers:
					walk(handler.body, True)
				walk(stmt.orelse, True)
				walk(stmt.finalbody, True)

	walk(tree.body, False)
	return found


def _stmt(node: ast.Import | ast.ImportFrom, module: str | None, level: int, conditional: bool) -> ImportStmt:
	return ImportStmt(
		node=node,
		first_line=node.lineno,
		last_line=node.end_lineno or node.lineno,
		module=module,
		level=level,
		aliases=tuple((alias.name, alias.asname) for alias in node.names),
		conditional=conditional,
	)


def render_alias(name: str, asname: str | None) -> str:
	return f"{name} as {asname}" if asname else name


def render_import(
	module: str | None,
	aliases: list[tuple[str, str | None]],
	*,
	level: int = 0,
	indent_unit: str = "\t",
	indent: str = "",
) -> str:
	"""A `from module import ...` (or `import ...` when `module` is None) statement, wrapped if long."""
	rendered = [render_alias(name, asname) for name, asname in aliases]
	if module is None:
		return f"{indent}import {', '.join(rendered)}\n"
	head = f"{indent}from {'.' * level}{module or ''} import "
	one_line = head + ", ".join(rendered)
	if len(one_line) <= _LINE_WIDTH:
		return one_line + "\n"
	inner = "".join(f"{indent}{indent_unit}{item},\n" for item in rendered)
	return f"{head}(\n{inner}{indent})\n"


def _replace_stmt_lines(text: str, stmt: ImportStmt, replacement: str) -> str:
	lines = source_lines(text)
	return "".join(lines[: stmt.first_line - 1]) + replacement + "".join(lines[stmt.last_line :])


def remove_names(text: str, names: set[str]) -> str:
	"""Drop `names` (as bound in the module: the alias or last component) from the module-level imports."""
	tree = parse_python(text)
	lines = source_lines(text)
	unit = detect_indent_unit(text)
	edits: list[tuple[ImportStmt, str]] = []
	for stmt in module_imports(tree):
		kept = [alias for alias, bound in zip(stmt.aliases, stmt.bound_names, strict=True) if bound not in names]
		if len(kept) == len(stmt.aliases):
			continue
		indent = leading_whitespace(lines[stmt.first_line - 1])
		edits.append(
			(
				stmt,
				render_import(stmt.module, list(kept), level=stmt.level, indent_unit=unit, indent=indent)
				if kept
				else "",
			)
		)
	for stmt, replacement in sorted(edits, key=lambda item: item[0].first_line, reverse=True):
		text = _replace_stmt_lines(text, stmt, replacement)
	return text


def _insertion_point(tree: ast.Module) -> tuple[int, bool]:
	"""(line after which a new import goes, whether the file already has imports): the end of the
	leading docstring / import block, or 0 for a file that starts with code."""
	last = 0
	saw_import = False
	for index, stmt in enumerate(tree.body):
		is_docstring = (
			index == 0
			and isinstance(stmt, ast.Expr)
			and isinstance(stmt.value, ast.Constant)
			and isinstance(stmt.value.value, str)
		)
		if isinstance(stmt, (ast.Import, ast.ImportFrom)):
			saw_import = True
			last = stmt.end_lineno or stmt.lineno
		elif is_docstring:
			last = stmt.end_lineno or stmt.lineno
		elif isinstance(stmt, ast.If) and "TYPE_CHECKING" in ast.unparse(stmt.test):
			saw_import = True
			last = stmt.end_lineno or stmt.lineno
		else:
			break
	return last, saw_import


def add_from_import(text: str, module: str, names: list[str], *, level: int = 0) -> str:
	"""Make `from <module> import <names>` true at module level, merging into an existing
	import from the same module when there is one. Names already imported are left alone."""
	tree = parse_python(text)
	unit = detect_indent_unit(text)
	existing = [
		s for s in module_imports(tree) if s.is_from and not s.conditional and s.module == module and s.level == level
	]
	already = {bound for s in module_imports(tree) for bound in s.bound_names}
	wanted = [name for name in names if name not in already]
	if not wanted:
		return text
	if existing and all(alias != "*" for s in existing for alias, _ in s.aliases):
		stmt = existing[0]
		merged = [*stmt.aliases, *((name, None) for name in wanted)]
		return _replace_stmt_lines(text, stmt, render_import(module, merged, level=level, indent_unit=unit))
	return _insert_statement(
		text, tree, render_import(module, [(name, None) for name in wanted], level=level, indent_unit=unit)
	)


def add_import(text: str, module: str) -> str:
	"""Make `import <module>` true at module level."""
	tree = parse_python(text)
	if any(a == module for s in module_imports(tree) if not s.is_from for a, asname in s.aliases if not asname):
		return text
	return _insert_statement(text, tree, f"import {module}\n")


def _insert_statement(text: str, tree: ast.Module, statement: str) -> str:
	lines = source_lines(text)
	after, saw_import = _insertion_point(tree)
	eol = "\r\n" if "\r\n" in text else "\n"
	statement = statement.replace("\n", eol)
	head = "".join(lines[:after])
	if head and not head.endswith(("\n", "\r")):
		head += eol
	tail = "".join(lines[after:])
	if saw_import or not tail.strip():
		return head + statement + tail
	# First import of the file: separate it from the docstring above and from the code below.
	next_stmt = tree.body[_count_leading(tree, after)] if _count_leading(tree, after) < len(tree.body) else None
	gap_after = 2 if isinstance(next_stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else 1
	gap_before = 1 if head else 0
	return head + eol * gap_before + statement + eol * gap_after + tail.lstrip("\r\n")


def _count_leading(tree: ast.Module, line: int) -> int:
	return sum(1 for stmt in tree.body if (stmt.end_lineno or stmt.lineno) <= line)
