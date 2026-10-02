"""Changing a function's parameters and its call sites together.

The definition is rewritten from its parameter model; the call sites are the
places the type checker resolves to that function (references), found in the
syntax tree and re-bound argument by argument. Text outside the arguments is
never touched, and anything that cannot be rewritten safely (`*args` spreads,
the function passed as a value) is reported instead of guessed.
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass, field, replace
from typing import Any

from mcp_nav_shared.errors import ToolInputError

from codenav_mcp.pysource import Definition, PythonSyntaxError, parse_python, source_lines, utf16_column


# -- offsets -------------------------------------------------------------------


class SourceMap:
	"""Converts between (line, byte column) from `ast`, (line, UTF-16 column) from LSP, and string offsets."""

	def __init__(self, text: str) -> None:
		self.text = text
		self.lines = source_lines(text)
		self.starts = [0]
		for line in self.lines:
			self.starts.append(self.starts[-1] + len(line))

	def offset(self, line: int, byte_col: int) -> int:
		"""String offset of an `ast` position (1-based line, UTF-8 byte column)."""
		row = self.lines[line - 1] if 0 < line <= len(self.lines) else ""
		return self.starts[line - 1] + len(row.encode("utf-8")[:byte_col].decode("utf-8", errors="ignore"))

	def start(self, node: ast.AST) -> int:
		return self.offset(node.lineno, node.col_offset)  # type: ignore[attr-defined]

	def end(self, node: ast.AST) -> int:
		return self.offset(node.end_lineno, node.end_col_offset)  # type: ignore[attr-defined]

	def segment(self, node: ast.AST) -> str:
		return self.text[self.start(node) : self.end(node)]

	def lsp_end(self, node: ast.AST) -> tuple[int, int]:
		"""(0-based line, UTF-16 column) of a node's end, as the language server reports reference ends."""
		line = node.end_lineno  # type: ignore[attr-defined]
		row = self.lines[line - 1]
		return line - 1, utf16_column(
			row, len(row.encode("utf-8")[: node.end_col_offset].decode("utf-8", errors="ignore"))
		)  # type: ignore[attr-defined]


def tokenize_source(text: str) -> list[tokenize.TokenInfo]:
	try:
		return list(tokenize.generate_tokens(io.StringIO(text).readline))
	except (tokenize.TokenError, IndentationError) as exc:
		raise PythonSyntaxError(f"cannot tokenize the file: {exc}") from exc


def _token_offset(starts: list[int], position: tuple[int, int]) -> int:
	return starts[position[0] - 1] + position[1]


def paren_span(smap: SourceMap, tokens: list[tokenize.TokenInfo], after_offset: int) -> tuple[int, int] | None:
	"""(offset just after the first `(`, offset of its matching `)`) at or after `after_offset`,
	skipping any `[...]` before it (type parameters)."""
	depth = 0
	open_at: int | None = None
	for token in tokens:
		begin = _token_offset(smap.starts, token.start)
		if begin < after_offset or token.type != tokenize.OP:
			continue
		if token.string in ("(", "[", "{"):
			if token.string == "(" and open_at is None and depth == 0:
				open_at = _token_offset(smap.starts, token.end)
			depth += 1
		elif token.string in (")", "]", "}"):
			depth -= 1
			if depth == 0 and open_at is not None:
				return open_at, begin
	return None


# -- the parameter model ---------------------------------------------------------

POSONLY, NORMAL, KWONLY, VARARG, KWARG = "posonly", "normal", "kwonly", "vararg", "kwarg"


@dataclass(frozen=True)
class Param:
	name: str
	kind: str
	annotation: str | None = None
	default: str | None = None

	def render(self) -> str:
		text = self.name
		if self.kind == VARARG:
			text = "*" + text
		elif self.kind == KWARG:
			text = "**" + text
		if self.annotation:
			text += f": {self.annotation}"
		if self.default is not None:
			text += f" = {self.default}" if self.annotation else f"={self.default}"
		return text


@dataclass
class Signature:
	params: list[Param]
	has_self: bool  # first parameter is `self`/`cls` and belongs to the receiver, not the caller
	multiline: bool
	span: tuple[int, int]  # offsets of the text between the parentheses

	@property
	def self_param(self) -> Param | None:
		return self.params[0] if self.has_self else None

	@property
	def callable_params(self) -> list[Param]:
		return self.params[1:] if self.has_self else self.params


def read_signature(smap: SourceMap, tokens: list[tokenize.TokenInfo], definition: Definition) -> Signature:
	node = definition.node
	if isinstance(node, ast.ClassDef):
		raise ToolInputError(f"{definition.qualname} is a class; give a function or method (a class's __init__)")
	args = node.args
	positional = [*args.posonlyargs, *args.args]
	defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
	params: list[Param] = []

	def build(arg: ast.arg, kind: str, default: ast.expr | None) -> Param:
		return Param(
			arg.arg,
			kind,
			smap.segment(arg.annotation) if arg.annotation is not None else None,
			smap.segment(default) if default is not None else None,
		)

	for index, arg in enumerate(positional):
		params.append(build(arg, POSONLY if index < len(args.posonlyargs) else NORMAL, defaults[index]))
	if args.vararg:
		params.append(build(args.vararg, VARARG, None))
	for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
		params.append(build(arg, KWONLY, default))
	if args.kwarg:
		params.append(build(args.kwarg, KWARG, None))
	decorators = {ast.unparse(d) for d in node.decorator_list}
	is_method = definition.kind == "method"
	has_self = is_method and "staticmethod" not in decorators and bool(positional)
	span = paren_span(smap, tokens, smap.offset(node.lineno, node.col_offset) + len("def"))
	if span is None:
		raise ToolInputError(f"could not find the parameter list of {definition.qualname}")
	inner = smap.text[span[0] : span[1]]
	return Signature(params=params, has_self=has_self, multiline="\n" in inner, span=span)


def render_params(params: list[Param], *, multiline: bool, indent: str, unit: str) -> str:
	posonly = [p for p in params if p.kind == POSONLY]
	normal = [p for p in params if p.kind == NORMAL]
	vararg = [p for p in params if p.kind == VARARG]
	kwonly = [p for p in params if p.kind == KWONLY]
	kwarg = [p for p in params if p.kind == KWARG]
	pieces = [p.render() for p in posonly]
	if posonly:
		pieces.append("/")
	pieces += [p.render() for p in normal]
	if vararg:
		pieces.append(vararg[0].render())
	elif kwonly:
		pieces.append("*")
	pieces += [p.render() for p in kwonly]
	pieces += [p.render() for p in kwarg]
	if multiline and pieces:
		return "\n" + "".join(f"{indent}{unit}{piece},\n" for piece in pieces) + indent
	return ", ".join(pieces)


@dataclass
class Change:
	add: list[dict[str, Any]] = field(default_factory=list)
	remove: list[str] = field(default_factory=list)
	reorder: list[str] = field(default_factory=list)


def new_params(signature: Signature, change: Change, name: str) -> tuple[list[Param], dict[str, str], set[str]]:
	"""(the new parameter list, call-site values for new parameters, names removed)."""
	params = list(signature.params)
	fixed = params[:1] if signature.has_self else []
	rest = params[1:] if signature.has_self else params
	existing = {p.name for p in params}
	removed: set[str] = set()
	for dropped in change.remove:
		match = next((p for p in rest if p.name == dropped), None)
		if match is None:
			names = ", ".join(p.name for p in rest) or "none"
			raise ToolInputError(f"{name} has no parameter {dropped!r} to remove (parameters: {names})")
		rest.remove(match)
		removed.add(dropped)
	values: dict[str, str] = {}
	for spec in change.add:
		pname = str(spec.get("name") or "")
		if not pname.isidentifier():
			raise ToolInputError(f"add: {pname!r} is not a valid parameter name")
		if pname in existing and pname not in removed:
			raise ToolInputError(f"{name} already has a parameter {pname!r}")
		default = spec.get("default")
		value = spec.get("value")
		if default is None and value is None:
			raise ToolInputError(
				f"add {pname!r}: give a `default` (callers need not change) or a `value` (inserted at every call)"
			)
		keyword_only = bool(spec.get("keyword_only"))
		param = Param(
			pname, KWONLY if keyword_only else NORMAL, spec.get("annotation"), None if default is None else str(default)
		)
		if value is not None:
			values[pname] = str(value)
		position = spec.get("position")
		group = [p for p in rest if p.kind in ((KWONLY,) if keyword_only else (POSONLY, NORMAL))]
		if position is None:
			anchor = group[-1] if group else None
			index = rest.index(anchor) + 1 if anchor is not None else _default_insert_index(rest, keyword_only)
		else:
			named = [p for p in rest if p.kind in (POSONLY, NORMAL, KWONLY)]
			if not 0 <= int(position) <= len(named):
				raise ToolInputError(f"add {pname!r}: position {position} is out of range (0..{len(named)})")
			index = (
				rest.index(named[int(position)]) if int(position) < len(named) else _default_insert_index(rest, False)
			)
		rest.insert(index, param)
	if change.reorder:
		reorderable = [p for p in rest if p.kind in (POSONLY, NORMAL, KWONLY)]
		names = [p.name for p in reorderable]
		if sorted(change.reorder) != sorted(names):
			raise ToolInputError(f"reorder must list exactly these parameters: {', '.join(names)}")
		by_name = {p.name: p for p in reorderable}
		order = iter(change.reorder)
		rest = [by_name[next(order)] if p.kind in (POSONLY, NORMAL, KWONLY) else p for p in rest]
		_check_group_order(rest, change.reorder)
	return [*fixed, *rest], values, removed


def _check_group_order(rest: list[Param], order: list[str]) -> None:
	kinds = {p.name: p.kind for p in rest}
	seen_kinds = [kinds[name] for name in order]
	rank = {POSONLY: 0, NORMAL: 1, KWONLY: 2}
	if [rank[k] for k in seen_kinds] != sorted(rank[k] for k in seen_kinds):
		raise ToolInputError(
			"reorder cannot move a parameter across `/` or `*`; keep positional-only, normal and keyword-only groups in order"
		)


def _default_insert_index(rest: list[Param], keyword_only: bool) -> int:
	for index, param in enumerate(rest):
		if param.kind == KWARG or (param.kind == VARARG and not keyword_only):
			return index
		if keyword_only and param.kind == KWARG:
			return index
	return len(rest)


# -- call sites ------------------------------------------------------------------


@dataclass
class CallArg:
	text: str  # source text as written (`x`, `name=x`, `*xs`, `**kw`)
	keyword: str | None  # keyword name for `name=x`
	value: str  # the expression alone
	start: int
	end: int
	starred: bool = False


def read_call_args(smap: SourceMap, call: ast.Call) -> list[CallArg]:
	items: list[tuple[int, CallArg]] = []
	for arg in call.args:
		starred = isinstance(arg, ast.Starred)
		items.append(
			(
				smap.start(arg),
				CallArg(smap.segment(arg), None, smap.segment(arg), smap.start(arg), smap.end(arg), starred),
			)
		)
	for kw in call.keywords:
		whole = smap.segment(kw)
		items.append(
			(
				smap.start(kw),
				CallArg(whole, kw.arg, smap.segment(kw.value), smap.start(kw), smap.end(kw), starred=kw.arg is None),
			)
		)
	return [item for _, item in sorted(items, key=lambda pair: pair[0])]


class ManualCallError(Exception):
	"""A call site this tool will not rewrite on its own; the message says why."""


def rebind_call(
	args: list[CallArg],
	old: list[Param],
	new: list[Param],
	new_values: dict[str, str],
	removed: set[str],
	*,
	skip_leading: int = 0,
) -> list[str]:
	"""New argument texts for a call written against `old` parameters, now against `new`.

	`skip_leading` leading positional arguments belong to the receiver (`Class.method(obj, ...)`) and stay put.
	"""
	if any(a.starred for a in args):
		raise ManualCallError("uses *args/**kwargs unpacking")
	lead = [a.text for a in args[:skip_leading]]
	rest_args = args[skip_leading:]
	old_positional = [p for p in old if p.kind in (POSONLY, NORMAL)]
	positional_args = [a for a in rest_args if a.keyword is None]
	keyword_args = [a for a in rest_args if a.keyword is not None]
	provided: dict[str, tuple[str, bool]] = {}
	extras: list[str] = []
	for index, arg in enumerate(positional_args):
		if index < len(old_positional):
			provided[old_positional[index].name] = (arg.value, True)
		else:
			extras.append(arg.value)
	for arg in keyword_args:
		provided[arg.keyword] = (arg.value, False)  # type: ignore[index]
	kept_keywords = [a for a in keyword_args if a.keyword not in removed]
	for name in removed:
		provided.pop(name, None)
	for name, value in new_values.items():
		provided[name] = (value, False)
	new_positional = [p for p in new if p.kind in (POSONLY, NORMAL)]
	positional_out: list[str] = []
	keyword_out: list[str] = []
	emitted: set[str] = set()
	broken = False
	for param in new_positional:
		if param.name not in provided:
			broken = True
			continue
		text, was_positional = provided[param.name]
		if was_positional and not broken and param.name not in new_values:
			positional_out.append(text)
			continue
		if param.kind == POSONLY:
			raise ManualCallError(f"positional-only parameter {param.name!r} would have to be passed by keyword")
		broken = True
		keyword_out.append(f"{param.name}={text}")
		emitted.add(param.name)
	if extras:
		if broken:
			raise ManualCallError("passes extra positional arguments that would shift")
		positional_out += extras
	for arg in kept_keywords:
		if arg.keyword not in emitted:
			keyword_out.append(arg.text)
			emitted.add(arg.keyword)  # type: ignore[arg-type]
	for name, value in new_values.items():
		if name not in emitted:
			keyword_out.append(f"{name}={value}")
	return [*lead, *positional_out, *keyword_out]


def call_edit(
	smap: SourceMap, tokens: list[tokenize.TokenInfo], call: ast.Call, old_args: list[CallArg], new_args: list[str]
) -> tuple[int, int, str] | None:
	"""(start, end, text) replacing only the tail of the argument list that differs."""
	old_texts = [a.text for a in old_args]
	if old_texts == new_args:
		return None
	common = 0
	while common < len(old_texts) and common < len(new_args) and old_texts[common] == new_args[common]:
		common += 1
	span = paren_span(smap, tokens, smap.end(call.func))
	if span is None:
		raise ManualCallError("could not locate the argument list")
	if not old_args:
		return span[0], span[0], ", ".join(new_args)
	multiline = "\n" in smap.text[old_args[0].start : old_args[-1].end]
	if multiline and common < len(old_args):
		row = smap.text[: old_args[common].start].rsplit("\n", 1)[-1]
		indent = row[: len(row) - len(row.lstrip())]
		separator = ",\n" + indent
	else:
		separator = ", "
	tail = new_args[common:]
	if common == len(old_args):  # arguments only added
		return old_args[-1].end, old_args[-1].end, separator + separator.join(tail)
	if not tail:  # arguments only removed from the end
		if common == 0:
			return old_args[0].start, old_args[-1].end, ""
		return old_args[common - 1].end, old_args[-1].end, ""
	return old_args[common].start, old_args[-1].end, separator.join(tail)


def find_call_at(tree: ast.Module, smap: SourceMap, end: tuple[int, int]) -> ast.Call | None:
	"""The call whose callee (`name` or `obj.name`) ends at LSP position `end` (0-based line, UTF-16 column)."""
	best: ast.Call | None = None
	for node in ast.walk(tree):
		if isinstance(node, ast.Call) and smap.lsp_end(node.func) == end:
			if best is None or (node.lineno, node.col_offset) > (best.lineno, best.col_offset):
				best = node
	return best


def parse_checked(text: str, name: str) -> ast.Module:
	return parse_python(text, name)


def with_params(signature: Signature, params: list[Param]) -> Signature:
	return replace(signature, params=params)


def apply_offset_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
	"""Apply (start, end, new_text) replacements, all positioned against `text`."""
	ordered = sorted(edits, key=lambda e: (e[0], e[1]))
	for (_, prev_end, _), (start, _, _) in zip(ordered, ordered[1:], strict=False):
		if start < prev_end:
			raise ToolInputError("two changes overlap; split the refactoring into smaller steps")
	for start, end, new in reversed(ordered):
		text = text[:start] + new + text[end:]
	return text
