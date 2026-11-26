"""v2 `rename`: the sites around a rename that ty's rename does not link, split into certain and uncertain.

Certain (written with the rename):
- exact dotted patch-target strings naming a renamed declaration (`"pkg.mod.Class.old"` -> `"pkg.mod.Class.new"`),
- keyword arguments of calls that ty links to a renamed declaration (`Config(old=1)`).

Uncertain (`OptionalSite`s the agent opts into with `sites=["u1", ...]`):
- attribute accesses ty cannot link (`mock.old`; receivers ty resolves elsewhere are not candidates at all),
- `Mock(old=...)` / `SimpleNamespace(old=...)` keywords,
- same-named `def old` in test classes ty did not link,
- comments and docstrings that mention the name like code (`.old`, `old(`, backticked); plain words stay notes.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_nav_shared.edits import TextEdit, decode_source, relative_name
from mcp_nav_shared.errors import TOOL_ERRORS, ToolInputError
from mcp_nav_shared.format import uri_to_path
from mcp_nav_shared.lsp_client import LspClient

from codenav_mcp.deps import iter_python_files, module_names_for
from codenav_mcp.mentions import Mention, covered_positions, find_mentions
from codenav_mcp.plan_sites import OptionalSite
from codenav_mcp.pysource import (
	PythonSyntaxError,
	definition_name_position,
	iter_definitions,
	node_position,
	parse_python,
	reject_nul,
	source_lines,
	utf16_column,
)


# Calls whose keyword arguments are attributes of a fake object (`Mock(execute=...)`).
MOCK_FACTORIES = frozenset(
	{"Mock", "MagicMock", "AsyncMock", "NonCallableMock", "NonCallableMagicMock", "PropertyMock", "SimpleNamespace"}
)
_TEST_DIRS = frozenset({"tests", "test", "testing"})
# Most keyword arguments one rename asks the type checker about (sequential requests; the rest stay as they are).
_MAX_KEYWORD_LOOKUPS = 300
SnippetWidth = 90


class UnknownSitesError(ToolInputError):
	"""`sites=` named an id the plan does not have."""


@dataclass(frozen=True)
class Declaration:
	"""A declaration the rename renames: where its name is (1-based line) and what to call it in a dotted path."""

	path: Path
	line: int
	qualname: str  # `Class.method`, `func`, `Class`


@dataclass
class SitePlan:
	certain: dict[Path, list[TextEdit]] = field(default_factory=dict)
	optional: list[OptionalSite] = field(default_factory=list)
	notes: list[str] = field(default_factory=list)
	certain_count: int = 0


@dataclass
class _Raw:
	"""An uncertain site before it is numbered."""

	path: Path
	line: int  # 1-based
	column: int  # 0-based UTF-16
	kind: str
	text: str
	edit: TextEdit


def is_test_path(rel: str) -> bool:
	parts = rel.split("/")
	name = parts[-1]
	return (
		any(part in _TEST_DIRS for part in parts[:-1])
		or name.startswith("test_")
		or name.endswith("_test.py")
		or name == "conftest.py"
	)


def dotted_targets(declarations: Sequence[Declaration], roots: Sequence[Path]) -> set[str]:
	"""Every `module.qualname` string that names one of the renamed declarations."""
	names: set[str] = set()
	for declaration in declarations:
		for module in module_names_for(declaration.path, roots):
			names.add(f"{module}.{declaration.qualname}")
	return names


def _in_hidden_dir(rel: str) -> bool:
	return any(part.startswith(".") for part in rel.split("/")[:-1])


def _callee_name(call: ast.Call) -> str:
	func = call.func
	if isinstance(func, ast.Name):
		return func.id
	if isinstance(func, ast.Attribute):
		return func.attr
	return ""


def _code_like_spans(row: str, old: str, start: int = 0, end: int | None = None) -> list[tuple[int, int]]:
	"""Spans of `old` in `row` that read like code: after a dot, in backticks, or called (`old(`)."""
	spans = []
	for match in re.finditer(rf"(?<!\w){re.escape(old)}(?!\w)", row[: end if end is not None else len(row)]):
		if match.start() < start:
			continue
		before = row[match.start() - 1 : match.start()] if match.start() else ""
		after = row[match.end() : match.end() + 1]
		if before in (".", "`") or after in ("(", "`"):
			spans.append((match.start(), match.end()))
	return spans


@dataclass
class _FileScan:
	patch: list[TextEdit] = field(default_factory=list)
	keywords: list[tuple[int, int, bool]] = field(default_factory=list)  # (line, 1-based column, in a Mock-like call)
	optional: list[_Raw] = field(default_factory=list)


def _scan_python(
	path: Path,
	rel: str,
	old: str,
	new: str,
	dotted: set[str],
	covered: set[tuple[int, int]],
	*,
	test_file: bool,
) -> _FileScan:
	scan = _FileScan()
	try:
		text = decode_source(path.read_bytes())[0]
	except (OSError, UnicodeDecodeError):
		return scan
	if old not in text:
		return scan
	try:
		reject_nul(text, rel)
		tree = parse_python(text, rel)
	except (PythonSyntaxError, ValueError, SystemError):
		return scan
	lines = source_lines(text)
	rows = [line.rstrip("\r\n") for line in lines]

	def add(kind: str, row_number: int, start: int, end: int) -> None:
		row = rows[row_number - 1]
		column = utf16_column(row, start)
		edit = TextEdit(row_number - 1, column, row_number - 1, utf16_column(row, end), new)
		scan.optional.append(_Raw(path, row_number, column, kind, row.strip(), edit))

	# Patch targets and comments come from the tokenizer, so multi-line calls and odd quoting are positioned exactly.
	try:
		for token in tokenize.generate_tokens(io.StringIO("\n".join(rows)).readline):
			row_number, col = token.start
			if token.type == tokenize.STRING and dotted:
				raw = token.string
				if raw[:1] in ("'", '"') and raw[:3] not in ('"""', "'''") and token.end[0] == row_number:
					content = raw[1:-1]
					if content in dotted and raw[-1] == raw[0]:
						index = col + 1 + len(content) - len(old)
						row = rows[row_number - 1]
						start = utf16_column(row, index)
						scan.patch.append(
							TextEdit(
								row_number - 1, start, row_number - 1, start + len(old.encode("utf-16-le")) // 2, new
							)
						)
			elif token.type == tokenize.COMMENT and old in token.string:
				row = rows[row_number - 1]
				for start, end in _code_like_spans(row, old, col):
					if (row_number - 1, utf16_column(row, start)) not in covered:
						add("comment", row_number, start, end)
	except (tokenize.TokenError, IndentationError, SyntaxError):
		pass

	for node in ast.walk(tree):
		if isinstance(node, ast.Call):
			mock = _callee_name(node) in MOCK_FACTORIES
			for keyword in node.keywords:
				if keyword.arg == old:
					line, column = node_position(lines, keyword)
					if (line - 1, column - 1) not in covered:
						scan.keywords.append((line, column, mock))
		if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
			first = node.body[0]
			if (
				isinstance(first, ast.Expr)
				and isinstance(first.value, ast.Constant)
				and isinstance(first.value.value, str)
			):
				constant = first.value
				last_row = constant.end_lineno or constant.lineno
				for row_number in range(constant.lineno, last_row + 1):
					row = rows[row_number - 1]
					start = 0
					end = None
					if row_number == constant.lineno:
						start = len(row.encode("utf-8")[: constant.col_offset].decode("utf-8", errors="ignore"))
					if row_number == last_row and constant.end_col_offset is not None:
						end = len(row.encode("utf-8")[: constant.end_col_offset].decode("utf-8", errors="ignore"))
					for span_start, span_end in _code_like_spans(row, old, start, end):
						add("docstring", row_number, span_start, span_end)
	if test_file:
		for definition in iter_definitions(tree, lines):
			if definition.kind == "method" and definition.name == old:
				line, column = definition_name_position(lines, definition)
				if (line - 1, column - 1) not in covered:
					start = _char_index(rows[line - 1], column - 1)
					add("def", line, start, start + len(old))
	return scan


def _char_index(row: str, utf16_units: int) -> int:
	"""Index into `row` of the character at a UTF-16 offset."""
	count = 0
	for index, char in enumerate(row):
		if count >= utf16_units:
			return index
		count += 2 if ord(char) > 0xFFFF else 1
	return len(row)


def _points_at_renamed(item: dict[str, Any], renamed_at: dict[Path, set[tuple[int, int]]]) -> bool:
	uri = str(item.get("uri") or item.get("targetUri") or "")
	if not uri.startswith("file:"):
		return False
	start = (item.get("range") or item.get("targetSelectionRange") or {}).get("start", {})
	return (int(start.get("line", -1)), int(start.get("character", -1))) in renamed_at.get(
		uri_to_path(uri).resolve(), set()
	)


SplitAttrs = Callable[..., Awaitable[tuple[list[Mention], list[Mention], int]]]


async def plan_v2_sites(
	client: LspClient,
	workspace: Path,
	roots: Sequence[Path],
	*,
	old_name: str,
	new_name: str,
	declarations: Sequence[Declaration],
	is_member: bool,
	parameter: bool,
	edits: dict[Path, list[TextEdit]],
	split_attrs: SplitAttrs,
	is_unlinked_attr: Callable[[Mention], bool],
) -> SitePlan:
	"""The certain extra edits and the numbered optional sites for renaming `old_name` -> `new_name`.

	`edits` are the edits already planned (ty's rename plus linked members): their positions are never planned twice."""
	covered = covered_positions(edits)
	plan = SitePlan()
	dotted = set() if parameter else dotted_targets(declarations, roots)
	raws: list[_Raw] = []
	keyword_candidates: list[tuple[Path, int, int]] = []
	width = len(old_name.encode("utf-16-le")) // 2
	for path in iter_python_files(workspace):
		rel = relative_name(path, workspace)
		if _in_hidden_dir(rel):
			continue
		resolved = path.resolve()
		scan = _scan_python(
			path,
			rel,
			old_name,
			new_name,
			dotted,
			covered.get(resolved, set()),
			test_file=is_test_path(rel) and is_member,
		)
		if scan.patch:
			plan.certain.setdefault(resolved, []).extend(scan.patch)
			plan.certain_count += len(scan.patch)
		raws.extend(scan.optional)
		for line, column, mock in scan.keywords:
			if mock:
				if is_member:
					edit = TextEdit(line - 1, column - 1, line - 1, column - 1 + width, new_name)
					raws.append(_Raw(resolved, line, column - 1, "keyword", _line_text(path, line), edit))
			else:
				keyword_candidates.append((resolved, line, column))
	# Keyword arguments: certain when ty links them to a renamed declaration, otherwise left alone.
	checked = 0
	for path, line, column in keyword_candidates:
		if checked >= _MAX_KEYWORD_LOOKUPS:
			plan.notes.append(
				f"{len(keyword_candidates) - checked} keyword argument(s) named `{old_name}` were not checked"
			)
			break
		checked += 1
		try:
			locations = await client.definition(str(path), line, column)
		except TOOL_ERRORS:
			continue
		if locations and all(_points_at_renamed(item, covered) for item in locations):
			plan.certain.setdefault(path, []).append(
				TextEdit(line - 1, column - 1, line - 1, column - 1 + width, new_name)
			)
			plan.certain_count += 1
	# Attribute accesses ty cannot link (`mock.old`): the ones it resolves to another symbol are not candidates at all.
	mentions = find_mentions(workspace, old_name, covered)
	if is_member:
		candidates = [m for m in mentions if is_unlinked_attr(m)]
		renamable, other, unchecked = await split_attrs(client, workspace, candidates, covered)
		for mention in renamable:
			column = mention.column or 0
			edit = TextEdit(mention.line - 1, column, mention.line - 1, column + width, new_name)
			raws.append(_Raw((workspace / mention.path).resolve(), mention.line, column, "attr", mention.snippet, edit))
		if unchecked:
			plan.notes.append(
				f"{unchecked} attribute access(es) of `{old_name}` could not be checked and are not listed"
			)
		attr_lines = {(m.path, m.line) for m in candidates}
	else:
		attr_lines = set()
	listed = {(relative_name(raw.path, workspace), raw.line) for raw in raws} | {
		(relative_name(path, workspace), edit.start_line + 1) for path, es in plan.certain.items() for edit in es
	}
	rest = [m for m in mentions if (m.path, m.line) not in listed and (m.path, m.line) not in attr_lines]
	if rest:
		counts: dict[str, int] = {}
		for mention in rest:
			counts[mention.kind] = counts.get(mention.kind, 0) + 1
		labels = {"code": "bare name", "string": "string", "comment": "comment", "text": "non-Python file"}
		parts = [
			f"{count} {labels[kind]}{'s' if count != 1 and kind != 'text' else ''}" for kind, count in counts.items()
		]
		plan.notes.append(f"`{old_name}` also appears in {', '.join(parts)} (not listed: plain words, other symbols)")
	unique: dict[tuple[Path, int, int, str], _Raw] = {}
	for raw in raws:
		unique.setdefault((raw.path, raw.line, raw.column, raw.kind), raw)
	ordered = sorted(unique.values(), key=lambda r: (relative_name(r.path, workspace), r.line, r.column, r.kind))
	plan.optional = [
		OptionalSite(f"u{number}", relative_name(raw.path, workspace), raw.line, raw.kind, raw.text[:140], raw.edit)
		for number, raw in enumerate(ordered, 1)
	]
	return plan


def _line_text(path: Path, line: int) -> str:
	try:
		return decode_source(path.read_bytes())[0].split("\n")[line - 1].strip()
	except (OSError, UnicodeDecodeError, IndexError):
		return ""


def select_sites(optional: Sequence[OptionalSite], sites: Sequence[str] | None) -> list[OptionalSite]:
	"""The optional sites `sites=` asks for (`"all"` or ids); an unknown id lists the valid ones."""
	if not sites:
		return []
	if any(site.lower() == "all" for site in sites):
		return list(optional)
	known = {site.id: site for site in optional}
	unknown = [site for site in sites if site not in known]
	if unknown:
		valid = f"valid ids: {', '.join(known)}" if known else "this rename has no optional sites"
		raise UnknownSitesError(f"unknown site id(s) {', '.join(repr(s) for s in unknown)}; {valid}")
	return [known[site] for site in dict.fromkeys(sites)]


def format_site(site: OptionalSite) -> str:
	text = site.text if len(site.text) <= SnippetWidth else site.text[: SnippetWidth - 3] + "..."
	return f"{site.id:<3} {site.path}:{site.line}  {site.kind}  {text}"
