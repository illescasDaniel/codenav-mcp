"""MCP tools that edit by symbol instead of by line: replace, insert, delete, quick-fix.

Anchoring an edit to a named function or class (found with the syntax tree,
not a text match) keeps it right when the file shifts, re-indents the new code
to fit, and refuses anything that is not valid Python before it is written.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context
from mcp_nav_shared.edits import (
	EditPlan,
	FileChange,
	TextEdit,
	changes_from_edits,
	detect_eol,
	parse_workspace_edit,
	read_source,
	relative_name,
	replace_lines,
)
from mcp_nav_shared.errors import TOOL_ERRORS, ToolInputError, format_tool_error
from mcp_nav_shared.format import uri_to_path
from mcp_nav_shared.resolve import resolve_symbol

from codenav_mcp import tool_base
from codenav_mcp.deps import find_dependents, import_from_targets, module_names_for
from codenav_mcp.imports import add_from_import, add_import, remove_names
from codenav_mcp.mentions import find_mentions, format_mentions
from codenav_mcp.pysource import (
	Definition,
	check_syntax,
	definition_name_position,
	definitions_in,
	detect_indent_unit,
	find_definition,
	fit_snippet,
	leading_comment_start,
	leading_whitespace,
	parse_python,
	source_lines,
	top_level_names,
)
from codenav_mcp.tool_base import Session, open_session
from codenav_mcp.writes import finish_plan, require_workspace_file


async def _locate(session: Session, name: str, file_path: str | None) -> tuple[Path, str, Definition]:
	"""The class/function/method called `name` (dotted `Class.method` accepted), with its file's text."""
	resolved = await resolve_symbol(session.client, session.workspace, name, file_path=file_path)
	path = uri_to_path(resolved.uri).resolve()
	text = read_source(path)[0]
	definition = find_definition(text, name_line=resolved.line + 1)
	if definition is None:
		raise ToolInputError(
			f"{name!r} is not a class, function or method (found a variable or attribute); edit it with `edit` instead."
		)
	return path, text, definition


def _parse_snippet(source: str, what: str) -> list[ast.stmt]:
	body = parse_python(textwrap.dedent(source).strip("\n") + "\n", f"the new {what}").body
	if not body:
		raise ToolInputError(f"the new {what} is empty")
	return body


def _signature_text(node: ast.AST) -> str:
	if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
		returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
		return f"({ast.unparse(node.args)}){returns}"
	if isinstance(node, ast.ClassDef):
		return f"({', '.join(ast.unparse(b) for b in node.bases)})"
	return ""


def _apply_imports(text: str, imports: list[str] | None) -> str:
	for statement in imports or []:
		for node in parse_python(statement.strip() + "\n", "imports").body:
			if isinstance(node, ast.ImportFrom):
				text = add_from_import(
					text, node.module or "", [(a.name, a.asname) for a in node.names], level=node.level
				)
			elif isinstance(node, ast.Import):
				for alias in node.names:
					text = add_import(text, alias.name)
			else:
				raise ToolInputError(f"`imports` takes import statements only, got: {statement!r}")
	return text


async def _replace_symbol(
	name: str,
	source: str,
	file_path: str | None = None,
	imports: list[str] | None = None,
	apply: bool = True,
	max_new_errors: int | None = None,
	ctx: Context | None = None,
) -> str:
	"""Replace a whole function, method or class by name. Example: `replace_symbol(name="Cart.total", source="def total(self): ...")`.

	`source` is the complete new definition, decorators included (anything
	not given is dropped, and the result says so). It is re-indented to fit
	where the old one stood and may use spaces or tabs; the file's own style is
	kept. `imports` is a list of import statements the new code needs
	(`["from decimal import Decimal"]`), added if missing. `name` resolves as in
	symbol_info (`file_path` disambiguates).

	The new code must be valid Python. By default the edit is written (undo
	with `undo_edit`) and the response lists the diagnostics it caused in this
	file and its importers: a changed signature shows up as errors at call
	sites. Pass `apply=false` to preview, or `max_new_errors` to refuse an edit
	that adds more errors than that. To change a signature and update the call
	sites in one step, use change_signature.
	"""
	try:
		session = await open_session(ctx)
		path, text, old = await _locate(session, name, file_path)
		body = _parse_snippet(source, "definition")
		if len(body) != 1 or not isinstance(body[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
			raise ToolInputError("`source` must be exactly one function or class definition (plus its decorators).")
		new_node = body[0]
		notes = []
		if new_node.name != old.name:
			notes.append(
				f"the definition is now called {new_node.name!r}; references to {old.name!r} are NOT updated "
				"(use rename_symbol to rename)"
			)
		if type(new_node) is not type(old.node) and isinstance(old.node, ast.ClassDef) != isinstance(
			new_node, ast.ClassDef
		):
			notes.append(f"a {old.kind} became a {'class' if isinstance(new_node, ast.ClassDef) else 'function'}")
		if _signature_text(new_node) != _signature_text(old.node):
			notes.append(
				f"signature changed: {old.name}{_signature_text(old.node)} -> {new_node.name}{_signature_text(new_node)}; "
				"callers may need updating (see diagnostics; change_signature updates call sites)"
			)
		dropped = [
			ast.unparse(d)
			for d in old.node.decorator_list
			if ast.unparse(d) not in [ast.unparse(n) for n in new_node.decorator_list]
		]
		if dropped:
			notes.append(f"decorators not in the new source were removed: {', '.join('@' + d for d in dropped)}")
		fitted = fit_snippet(source, indent=old.indent, file_text=text)
		new_text = replace_lines(text, old.first_line, old.last_line, fitted)
		new_text = _apply_imports(new_text, imports)
		rel = relative_name(path, session.workspace)
		check_syntax(new_text, rel)
		plan = EditPlan(f"Replace {old.qualname} in {rel}", [FileChange(path, text, new_text)], notes)
		outcome = await finish_plan(
			session.client,
			workspace=session.workspace,
			roots=session.roots,
			plan=plan,
			title=plan.description,
			apply=apply,
			max_new_errors=max_new_errors,
		)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return outcome.text


def _blank_lines_for(indent: str) -> int:
	return 2 if not indent else 1


def _insert_block(text: str, anchor_line: int, snippet: str, *, before: bool, indent: str) -> str:
	"""Put `snippet` before/after the statement starting/ending at `anchor_line` (1-based) with PEP 8 spacing:
	two blank lines around top-level code, one inside classes, none right after a block opener."""
	eol = detect_eol(text)
	lines = source_lines(text)
	snippet = snippet.replace("\n", eol)
	if before:
		head = "".join(lines[: anchor_line - 1]).rstrip("\r\n")
		rest = "".join(lines[anchor_line - 1 :])
		opens_block = head.rstrip().endswith(":")
		gap_before = "" if opens_block else eol * _blank_lines_for(indent)
		return (head + eol + gap_before if head else "") + snippet + eol * _blank_lines_for(indent) + rest
	head = "".join(lines[:anchor_line]).rstrip("\r\n")
	rest = "".join(lines[anchor_line:]).lstrip("\r\n")
	if not rest.strip():
		return head + eol + eol * _blank_lines_for(indent) + snippet
	rest_indent = leading_whitespace(rest.splitlines()[0])
	gap_after = eol * _blank_lines_for(rest_indent if len(rest_indent) < len(indent) else indent)
	return head + eol + eol * _blank_lines_for(indent) + snippet + gap_after + rest


async def _insert_symbol(
	source: str,
	file_path: str,
	after: str | None = None,
	before: str | None = None,
	into: str | None = None,
	imports: list[str] | None = None,
	apply: bool = True,
	max_new_errors: int | None = None,
	ctx: Context | None = None,
) -> str:
	"""Add a new function, method or class next to a named symbol. Example: `insert_symbol(source="def helper(): ...", file_path="src/utils.py", after="parse")`.

	Place it with one of `after` / `before` (a symbol defined in `file_path`;
	a method name like `Cart.total` places the new code inside that class) or
	`into` (a class: appended to its body). With none of them it goes at the
	end of the file. Blank lines follow PEP 8 (two between top-level
	definitions, one between methods), indentation matches the file, and
	`imports` adds import statements the new code needs. Refuses a definition
	whose name already exists in that scope (use action="replace").
	"""
	try:
		if sum(x is not None for x in (after, before, into)) > 1:
			raise ToolInputError("give at most one of `after`, `before`, `into`")
		session = await open_session(ctx)
		path = require_workspace_file(session.workspace, file_path)
		text = read_source(path)[0]
		body = _parse_snippet(source, "definition")
		if not all(
			isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))
			for s in body
		):
			raise ToolInputError("`source` must contain function, class or assignment statements only.")
		definitions = {d.qualname: d for d in definitions_in(text)}

		def lookup(qualname: str) -> Definition:
			found = definitions.get(qualname)
			if found is None:
				known = ", ".join(list(definitions)[:12]) or "none"
				raise ToolInputError(f"no definition named {qualname!r} in {file_path} (found: {known})")
			return found

		anchor = after or before
		if anchor is not None:
			target = lookup(anchor)
			indent = target.indent
			scope_parent = target.parents[-1] if target.parents else None
			line = leading_comment_start(source_lines(text), target.first_line) if before else target.last_line
			insert_before = before is not None
		elif into is not None:
			container = lookup(into)
			if not container.is_class:
				raise ToolInputError(f"`into` must name a class; {into!r} is a {container.kind}.")
			indent = container.indent + detect_indent_unit(text)
			scope_parent = container.node
			line = container.last_line
			insert_before = False
		else:
			indent, scope_parent, line, insert_before = "", None, len(source_lines(text)), False
		existing = _names_in_scope(text, scope_parent)
		clashes = [
			s.name
			for s in body
			if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and s.name in existing
		]
		if clashes:
			raise ToolInputError(
				f"{', '.join(clashes)} already defined in that scope; use edit_symbol(action=replace) to change it."
			)
		fitted = fit_snippet(source, indent=indent, file_text=text)
		if into is not None and _is_placeholder_body(container.node):
			first = container.node.body[-1]
			new_text = replace_lines(text, first.lineno, first.end_lineno or first.lineno, fitted)
		elif text.strip() == "":
			new_text = fitted
		else:
			new_text = _insert_block(text, line, fitted, before=insert_before, indent=indent)
		new_text = _apply_imports(new_text, imports)
		rel = relative_name(path, session.workspace)
		check_syntax(new_text, rel)
		plan = EditPlan(
			f"Insert {', '.join(getattr(s, 'name', '?') for s in body)} in {rel}", [FileChange(path, text, new_text)]
		)
		outcome = await finish_plan(
			session.client,
			workspace=session.workspace,
			roots=session.roots,
			plan=plan,
			title=plan.description,
			apply=apply,
			max_new_errors=max_new_errors,
		)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return outcome.text


def _is_placeholder_body(node: ast.ClassDef) -> bool:
	"""A class whose body is only `pass` / `...`: the new member replaces it."""
	if len(node.body) != 1:
		return False
	only = node.body[0]
	return isinstance(only, ast.Pass) or (
		isinstance(only, ast.Expr) and isinstance(only.value, ast.Constant) and only.value.value is Ellipsis
	)


def _names_in_scope(text: str, scope: ast.AST | None) -> set[str]:
	tree = parse_python(text)
	if scope is None:
		return top_level_names(tree)
	for node in ast.walk(tree):
		if (
			isinstance(node, ast.ClassDef)
			and isinstance(scope, ast.ClassDef)
			and node.name == scope.name
			and node.lineno == scope.lineno
		):
			return {
				n
				for stmt in node.body
				for n in (
					[stmt.name]
					if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
					else [t.id for t in getattr(stmt, "targets", []) if isinstance(t, ast.Name)]
					+ (
						[stmt.target.id]
						if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
						else []
					)
				)
			}
	return set()


def _delete_span(text: str, definition: Definition, *, comments: bool = True) -> str:
	"""`text` without the definition (decorators and the comment block right above included),
	spacing repaired, and `pass` left behind when it was the only statement of its block."""
	lines = source_lines(text)
	first = leading_comment_start(lines, definition.first_line) if comments else definition.first_line
	last = definition.last_line
	eol = detect_eol(text)
	parent = definition.parent
	siblings = [s for s in (parent.body if parent is not None else parse_python(text).body)]
	non_doc = [
		s
		for s in siblings
		if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
	]
	if parent is not None and len(non_doc) == 1:
		return replace_lines(text, first, last, definition.indent + "pass" + eol)
	head = "".join(lines[: first - 1]).rstrip("\r\n")
	tail = "".join(lines[last:]).lstrip("\r\n")
	if not tail.strip():
		return (head + eol) if head else ""
	if not head:
		return tail
	gap = eol * _blank_lines_for(definition.indent)
	# keep the original spacing style: methods get one blank line, top level two
	return head + eol + gap + tail


async def _safe_delete(
	name: str,
	file_path: str | None = None,
	prune_imports: bool = True,
	force: bool = False,
	apply: bool = True,
	max_new_errors: int | None = 0,
	ctx: Context | None = None,
) -> str:
	"""Delete a function, method or class only if nothing uses it. Example: `safe_delete(name="legacy_parse")`.

	Finds its references with the type checker. If anything besides its own
	body uses it, nothing is deleted and the users are listed (`force=true`
	deletes anyway and shows what breaks). Imports of the symbol are the one
	exception: when imports are the only remaining users they are removed too
	(`prune_imports`). Usages the type checker cannot see (strings such as
	`getattr(x, "name")`, untyped attribute accesses, docs) are listed, and when
	there are any the deletion is previewed rather than written, so you can
	confirm with `apply_edit`.
	"""
	try:
		session = await open_session(ctx)
		path, text, definition = await _locate(session, name, file_path)
		rel = relative_name(path, session.workspace)
		line, column = definition_name_position(source_lines(text), definition)
		locations = await session.client.references(str(path), line, column, include_declaration=False)
		blockers: list[tuple[Path, int, int]] = []
		import_only: dict[Path, list[int]] = {}
		for loc in locations:
			ref_path = uri_to_path(loc["uri"]).resolve()
			ref_line = loc["range"]["start"]["line"] + 1
			ref_col = loc["range"]["start"]["character"] + 1
			if ref_path == path and definition.first_line <= ref_line <= definition.last_line:
				continue  # the symbol's own body (recursion, self-reference)
			if _is_import_line(ref_path, ref_line):
				import_only.setdefault(ref_path, []).append(ref_line)
			else:
				blockers.append((ref_path, ref_line, ref_col))
		if definition.parent is None:
			for ref_path, lines in _importing_statements(session, path, definition.name).items():
				import_only.setdefault(ref_path, []).extend(lines)
		if blockers and not force:
			shown = "\n".join(
				f"  {relative_name(p, session.workspace)}:{ln}:{col}: {_line_of(p, ln)}" for p, ln, col in blockers[:12]
			)
			more = f"\n  ... {len(blockers) - 12} more" if len(blockers) > 12 else ""
			return (
				f"Not deleted: {definition.qualname} is still used in {len({p for p, _, _ in blockers})} file(s):\n{shown}{more}\n"
				"Remove or change those first, or pass force=true to delete anyway and see what breaks."
			)
		changes = []
		notes = []
		new_text = _delete_span(text, definition)
		if import_only and not prune_imports and not force:
			raise ToolInputError(
				"imports of the symbol remain: "
				+ ", ".join(relative_name(p, session.workspace) for p in import_only)
				+ " (pass prune_imports=true to remove them)"
			)
		edits: dict[Path, str] = {path: new_text}
		for ref_path in import_only:
			if prune_imports and ref_path != path:
				ref_text = read_source(ref_path)[0]
				edits[ref_path] = remove_names(ref_text, {definition.name})
				notes.append(f"removed its import from {relative_name(ref_path, session.workspace)}")
		for file, content in edits.items():
			check_syntax(content, relative_name(file, session.workspace))
			old_text, bom = read_source(file)
			changes.append(FileChange(file, old_text, content, bom, bom))
		mentions = find_mentions(session.workspace, definition.name, {})
		own_lines = set(range(definition.first_line, definition.last_line + 1))
		pruned = (
			{(relative_name(p, session.workspace), ln) for p, lines in import_only.items() for ln in lines}
			if prune_imports
			else set()
		)
		unseen = [
			m
			for m in mentions
			if not (m.path == rel and m.line in own_lines)
			and (m.path, m.line) not in pruned
			and m.kind in ("code", "string")
		]
		notes += format_mentions(unseen, definition.name)
		plan = EditPlan(f"Delete {definition.qualname} from {rel}", changes, notes)
		hold_back = bool(unseen) and apply and not force
		if hold_back:
			plan.notes.insert(
				0,
				"kept as a preview because the name still appears where the type checker could not link it; apply_edit to confirm",
			)
		outcome = await finish_plan(
			session.client,
			workspace=session.workspace,
			roots=session.roots,
			plan=plan,
			title=plan.description,
			apply=apply and not hold_back,
			max_new_errors=None if force else max_new_errors,
		)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return outcome.text


def _importing_statements(session: Session, target: Path, name: str) -> dict[Path, list[int]]:
	"""Files with `from <target module> import name`, and the lines of those statements.

	The type checker reports usages but not an unused import, so importers are found from the syntax."""
	targets = module_names_for(target, session.roots)
	found: dict[Path, list[int]] = {}
	for dependent in find_dependents(session.workspace, [target], session.roots):
		own = module_names_for(dependent, session.roots)
		try:
			tree = ast.parse(read_source(dependent)[0])
		except (OSError, SyntaxError, UnicodeDecodeError):
			continue
		for node in ast.walk(tree):
			if isinstance(node, ast.ImportFrom) and any(a.name == name for a in node.names):
				if import_from_targets(node, own, is_package_init=dependent.name == "__init__.py") & targets:
					found.setdefault(dependent, []).extend(range(node.lineno, (node.end_lineno or node.lineno) + 1))
	return found


def _line_of(path: Path, line: int) -> str:
	try:
		return read_source(path)[0].splitlines()[line - 1].strip()[:120]
	except (OSError, IndexError, UnicodeDecodeError):
		return ""


def _is_import_line(path: Path, line: int) -> bool:
	try:
		tree = ast.parse(read_source(path)[0])
	except (OSError, SyntaxError, UnicodeDecodeError):
		return False
	return any(
		isinstance(n, (ast.Import, ast.ImportFrom)) and n.lineno <= line <= (n.end_lineno or n.lineno)
		for n in ast.walk(tree)
	)


_SIMPLE_IMPORT_RE = re.compile(r"^(?:from (\S+) import (\w+)|import ([\w.]+))\n?$")
_SUPPRESS_RE = re.compile(r"ignore|suppress|noqa", re.IGNORECASE)


def _is_suppression(action: dict[str, Any]) -> bool:
	if _SUPPRESS_RE.search(str(action.get("title", ""))):
		return True
	edits = parse_workspace_edit(action.get("edit"))
	return any("ty: ignore" in e.new_text or "type: ignore" in e.new_text for es in edits.values() for e in es)


def _import_insertion(edits: list[TextEdit]) -> tuple[str, list[str], bool] | None:
	"""(module, names, is_plain_import) when the edits are one bare import statement inserted at the file top."""
	if len(edits) != 1:
		return None
	edit = edits[0]
	if (edit.start_line, edit.start_character, edit.end_line, edit.end_character) != (0, 0, 0, 0):
		return None
	match = _SIMPLE_IMPORT_RE.match(edit.new_text)
	if not match:
		return None
	if match.group(3):
		return match.group(3), [], True
	return match.group(1), [match.group(2)], False


async def quick_fix(
	file_path: str,
	line: int | None = None,
	code: str | None = None,
	choice: str | None = None,
	allow_suppress: bool = False,
	apply: bool = True,
	max_new_errors: int | None = 0,
	ctx: Context | None = None,
) -> str:
	"""Apply ty's own fixes for a file's diagnostics (today: add the missing import). Example: `quick_fix(file_path="src/app.py")`.

	Looks at the file's diagnostics (narrow with `line` or a rule `code` such as
	`unresolved-reference`), asks ty for the quick fixes, and applies them.
	Imports are placed after the docstring and the existing imports, merged into
	an existing `from x import ...` where there is one. When a diagnostic has
	several equally good fixes (two modules that both export `Path`) nothing is
	applied: the options are listed, and you repeat the call with `choice` set to
	part of the title you want (`choice="pathlib"`). Fixes that only silence the
	error (`# ty: ignore`) are skipped unless `allow_suppress=true`.
	"""
	try:
		session = await open_session(ctx)
		path = require_workspace_file(session.workspace, file_path)
		rel = relative_name(path, session.workspace)
		text = read_source(path)[0]
		items = await session.client.diagnostics(str(path))
		wanted = [
			d
			for d in items
			if (line is None or d["range"]["start"]["line"] + 1 <= line <= d["range"]["end"]["line"] + 1)
			and (code is None or str(d.get("code")) == code)
		]
		if not wanted:
			return f"No diagnostics to fix in {rel}" + (f" at line {line}" if line else "") + "."
		chosen: list[tuple[dict[str, Any], dict[str, Any]]] = []
		ambiguous: list[str] = []
		unfixable: list[str] = []
		for diagnostic in wanted[:25]:
			start, end = diagnostic["range"]["start"], diagnostic["range"]["end"]
			actions = await session.client.code_actions(
				str(path),
				(start["line"] + 1, start["character"] + 1),
				(end["line"] + 1, end["character"] + 1),
				diagnostics=[diagnostic],
				only=["quickfix"],
			)
			usable = [a for a in actions if allow_suppress or not _is_suppression(a)]
			if choice:
				usable = [a for a in usable if choice.lower() in str(a.get("title", "")).lower()]
			label = f"line {start['line'] + 1}: {str(diagnostic.get('message', '')).splitlines()[0]}"
			if not usable:
				unfixable.append(label)
			elif len(usable) == 1 or all(_same_edit(usable[0], other) for other in usable[1:]):
				chosen.append((diagnostic, usable[0]))
			else:
				ambiguous.append(f"{label}\n" + "\n".join(f"      - {a.get('title')}" for a in usable[:8]))
		if ambiguous:
			return (
				'Several fixes apply; nothing was changed. Repeat with choice="<part of a title>":\n  '
				+ "\n  ".join(ambiguous)
			)
		if not chosen:
			return "ty offers no quick fix for: " + "; ".join(unfixable[:6]) + "."
		new_text = text
		other_edits: dict[Path, list[TextEdit]] = {}
		titles = []
		for _diagnostic, action in chosen:
			titles.append(str(action.get("title")))
			edits = parse_workspace_edit(action.get("edit"))
			for edit_path, edit_list in edits.items():
				insertion = _import_insertion(edit_list) if edit_path.resolve() == path else None
				if insertion is not None:
					module, names, plain = insertion
					new_text = add_import(new_text, module) if plain else add_from_import(new_text, module, names)
				else:
					other_edits.setdefault(edit_path.resolve(), []).extend(edit_list)
		changes = []
		if other_edits:
			if path in other_edits and new_text != text:
				raise ToolInputError(
					"fixes mix import insertion with other edits to the same file; apply them one at a time with `line`"
				)
			changes = changes_from_edits(other_edits)
		if new_text != text:
			changes.append(FileChange(path, text, new_text))
		check_syntax(new_text, rel)
		notes = [f"fix: {title}" for title in dict.fromkeys(titles)]
		if unfixable:
			notes.append("no fix offered for: " + "; ".join(unfixable[:5]))
		plan = EditPlan(f"Quick fixes in {rel} ({len(chosen)})", changes, notes)
		outcome = await finish_plan(
			session.client,
			workspace=session.workspace,
			roots=session.roots,
			plan=plan,
			title=plan.description,
			apply=apply,
			max_new_errors=max_new_errors,
		)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return outcome.text


def _same_edit(a: dict[str, Any], b: dict[str, Any]) -> bool:
	return a.get("edit") == b.get("edit")


TOOLS: list[tuple[Any, Any]] = [
	(quick_fix, tool_base.WRITES),
]
