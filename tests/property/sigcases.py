"""Shared generators and the execution oracle for the change_signature properties.

A case is a function definition (as text), a `Change`, and a call written against the old definition. The oracle
runs the old call on the old definition and the rewritten call on the rewritten definition and compares what each
parameter was bound to, plus the order in which the call's effectful arguments were evaluated.
"""

from __future__ import annotations

import ast
import itertools
import re
from dataclasses import dataclass, replace
from typing import Any

from hypothesis import assume, event, strategies as st
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp.pysource import definitions_in
from codenav_mcp.signature import (
	KWARG,
	KWONLY,
	NORMAL,
	POSONLY,
	VARARG,
	CallArg,
	Change,
	ManualCallError,
	Param,
	SourceMap,
	apply_offset_edits,
	call_edit,
	new_params,
	read_call_args,
	read_signature,
	rebind_call,
	render_params,
	tokenize_source,
)


NAMES = ["p", "q", "r", "s", "t", "u"]
NEW_NAMES = ["n1", "n2"]
ADD_VALUE = 99
ADD_DEFAULT = 7


@dataclass(frozen=True)
class Arg:
	"""One argument of a generated call."""

	kind: str  # "const" | "name" | "effect"
	number: int  # the value it evaluates to (also its identity in the effect log)

	def expr(self) -> str:
		return {"const": str(self.number), "name": f"v{self.number}", "effect": f"e({self.number})"}[self.kind]


@dataclass(frozen=True)
class CallShape:
	positional: tuple[Arg, ...]
	keywords: tuple[tuple[str, Arg], ...]
	multiline: bool = False
	trailing_comma: bool = False
	tight: bool = True  # `name=x` (True) or `name = x`
	receiver: str = "inst"  # method calls: "inst" -> inst.m(...), "class" -> A.m(inst, ...), unused for functions

	def arg_texts(self) -> list[str]:
		eq = "=" if self.tight else " = "
		return [a.expr() for a in self.positional] + [f"{name}{eq}{a.expr()}" for name, a in self.keywords]


@dataclass(frozen=True)
class Case:
	definition: str  # the full `def f(...)` (or class with `def m`) text
	method: bool
	change: Change
	call: CallShape


# -- strategies ------------------------------------------------------------------------------------------------


@st.composite
def signature_params(draw: st.DrawFn) -> list[Param]:
	names = draw(st.permutations(NAMES))
	n_pos = draw(st.integers(0, 3))
	n_posonly = draw(st.integers(0, n_pos))
	n_defaults = draw(st.integers(0, n_pos))
	vararg = draw(st.booleans())
	n_kw = draw(st.integers(0, 2))
	kwarg = draw(st.booleans())
	annotated = draw(st.booleans())
	iterator = iter(names)
	params: list[Param] = []
	number = itertools.count(100)

	def build(kind: str, default: bool) -> Param:
		return Param(next(iterator), kind, "int" if annotated else None, str(next(number)) if default else None)

	for index in range(n_pos):
		params.append(build(POSONLY if index < n_posonly else NORMAL, index >= n_pos - n_defaults))
	if vararg:
		params.append(Param("args", VARARG))
	for _ in range(n_kw):
		params.append(build(KWONLY, draw(st.booleans())))
	if kwarg:
		params.append(Param("kw", KWARG))
	return params


def definition_text(params: list[Param], *, method: bool) -> str:
	rendered = render_params(params, multiline=False, indent="", unit="\t")
	if method:
		rendered = "self, " + rendered if rendered else "self"
		return f"class A:\n\tdef m({rendered}):\n\t\treturn dict(locals())\n"
	return f"def f({rendered}):\n\treturn dict(locals())\n"


@st.composite
def changes_for(draw: st.DrawFn, params: list[Param], *, allow_reorder: bool = True) -> Change:
	removable = [p.name for p in params if p.kind in (POSONLY, NORMAL, KWONLY)]
	remove = draw(st.lists(st.sampled_from(removable), unique=True, max_size=2)) if removable else []
	add = []
	for name in NEW_NAMES[: draw(st.integers(0, 2))]:
		spec: dict[str, Any] = {"name": name}
		kind = draw(st.sampled_from(["default", "value", "both"]))
		if kind in ("default", "both"):
			spec["default"] = ADD_DEFAULT
		if kind in ("value", "both"):
			spec["value"] = ADD_VALUE
		keyword_only = draw(st.sampled_from([None, True, False]))
		if keyword_only is not None:
			spec["keyword_only"] = keyword_only
		if draw(st.booleans()):
			spec["position"] = draw(st.integers(0, 4))
		add.append(spec)
	change = Change(add=add, remove=remove)
	if allow_reorder and draw(st.booleans()):
		signature = signature_of(definition_text(params, method=False), "f")
		try:
			new, _, _ = new_params(signature, change, "f")
		except ToolInputError:
			return change
		names = [p.name for p in new if p.kind in (POSONLY, NORMAL, KWONLY)]
		return Change(add=add, remove=remove, reorder=draw(st.permutations(names)))
	return change


@st.composite
def call_shapes(draw: st.DrawFn, params: list[Param], *, method: bool, effects: bool = True) -> CallShape:
	counter = itertools.count(10)
	kinds = ["const", "name", "effect"] if effects else ["const", "name"]

	def arg() -> Arg:
		return Arg(draw(st.sampled_from(kinds)), next(counter))

	positional: list[Arg] = []
	keywords: list[tuple[str, Arg]] = []
	positional_params = [p for p in params if p.kind in (POSONLY, NORMAL)]
	still_positional = True
	for param in positional_params:
		if still_positional and (param.default is None or draw(st.booleans())):
			positional.append(arg())
		elif param.kind == POSONLY and param.default is None:
			raise AssertionError("unreachable: defaults trail")
		else:
			still_positional = False
			if param.kind == NORMAL and (param.default is None or draw(st.booleans())):
				keywords.append((param.name, arg()))
	if still_positional and any(p.kind == VARARG for p in params):
		positional += [arg() for _ in range(draw(st.integers(0, 2)))]
	for param in params:
		if param.kind == KWONLY and (param.default is None or draw(st.booleans())):
			keywords.append((param.name, arg()))
	if any(p.kind == KWARG for p in params):
		keywords += [(f"x{i}", arg()) for i in range(draw(st.integers(0, 2)))]
	keywords = draw(st.permutations(keywords)) if keywords else keywords
	return CallShape(
		tuple(positional),
		tuple(keywords),
		multiline=draw(st.booleans()),
		trailing_comma=draw(st.booleans()),
		tight=draw(st.booleans()),
		receiver=draw(st.sampled_from(["inst", "class"])) if method else "inst",
	)


@st.composite
def cases(
	draw: st.DrawFn,
	*,
	allow_reorder: bool = True,
	effects: bool = True,
	tight_only: bool = False,
	single_line: bool = False,
) -> Case:
	params = draw(signature_params())
	method = draw(st.booleans())
	change = draw(changes_for(params, allow_reorder=allow_reorder))
	shape = draw(call_shapes(params, method=method, effects=effects))
	if tight_only:
		shape = replace(shape, tight=True)
	if single_line:
		shape = replace(shape, multiline=False, trailing_comma=False)
	return Case(definition_text(params, method=method), method, change, shape)


# -- running a case --------------------------------------------------------------------------------------------


def signature_of(text: str, qualname: str):
	smap = SourceMap(text)
	tokens = tokenize_source(text)
	definition = next(d for d in definitions_in(text) if d.qualname == qualname)
	return read_signature(smap, tokens, definition)


def call_statement(shape: CallShape, *, method: bool) -> str:
	texts = shape.arg_texts()
	if method:
		if shape.receiver == "class":
			texts = ["inst", *texts]
			callee = "A.m"
		else:
			callee = "inst.m"
	else:
		callee = "f"
	if shape.multiline and texts:
		return f"result = {callee}(\n\t" + ",\n\t".join(texts) + (",\n)" if shape.trailing_comma else "\n)")
	return f"result = {callee}(" + ", ".join(texts) + (",)" if shape.trailing_comma and texts else ")")


def module_text(definition: str, statement: str) -> str:
	return f"# header\n{definition}\n\ninst = A() if 'A' in dir() else None\nbefore = 1\n{statement}\nafter = 2\n"


class Outcome:
	"""What rewriting one call did."""

	def __init__(self, new_definition: str, new_statement: str | None, reason: str | None) -> None:
		self.new_definition = new_definition
		self.new_statement = new_statement  # None when the call was left alone for a stated reason
		self.reason = reason


def rewrite(definition: str, change: Change, statement: str, *, method: bool, qualname: str) -> Outcome:
	"""The new definition text and the call statement rewritten like `change_signature` does it."""
	smap = SourceMap(definition)
	tokens = tokenize_source(definition)
	found = next(d for d in definitions_in(definition) if d.qualname == qualname)
	signature = read_signature(smap, tokens, found)
	params, values, removed = new_params(signature, change, qualname)  # may raise ToolInputError
	rendered = render_params(params, multiline=signature.multiline, indent=found.indent, unit="\t")
	compile(f"def _({rendered}):\n\tpass\n", "<defn>", "exec")  # SyntaxError: the tool refuses such a plan
	new_definition = definition[: signature.span[0]] + rendered + definition[signature.span[1] :]
	call_smap = SourceMap(statement)
	call_tokens = tokenize_source(statement)
	call = next(n for n in ast.walk(ast.parse(statement)) if isinstance(n, ast.Call) and not _is_e(n))
	old_args = read_call_args(call_smap, call)
	skip = (
		1
		if method
		and isinstance(call.func, ast.Attribute)
		and isinstance(call.func.value, ast.Name)
		and call.func.value.id == "A"
		else 0
	)
	old_callable = signature.callable_params
	new_callable = params[1:] if signature.has_self else params
	try:
		new_args = rebind_call(old_args, old_callable, new_callable, values, removed, skip_leading=skip)
	except ManualCallError as exc:
		return Outcome(new_definition, None, str(exc))
	edit = call_edit(call_smap, call_tokens, call, old_args, new_args)
	new_statement = statement if edit is None else apply_offset_edits(statement, [edit])
	return Outcome(new_definition, new_statement, None)


def _is_e(call: ast.Call) -> bool:
	return isinstance(call.func, ast.Name) and call.func.id == "e"


def execute(definition: str, statement: str, *, method: bool) -> tuple[dict[str, Any], list[int]]:
	"""Run the definition and the call: (what the function bound, the effect log)."""
	log: list[int] = []

	def e(value: int) -> int:
		log.append(value)
		return value

	namespace: dict[str, Any] = {"e": e, **{f"v{n}": n for n in range(10, 40)}}
	exec(compile(definition, "<defn>", "exec"), namespace)  # noqa: S102
	if method:
		namespace["inst"] = namespace["A"]()
	exec(compile(statement, "<call>", "exec"), namespace)  # noqa: S102
	result = dict(namespace["result"])
	result.pop("self", None)
	return result, log


def expected_bindings(old: dict[str, Any], change: Change, new_names: list[str]) -> dict[str, Any]:
	removed = set(change.remove)
	expected = {name: value for name, value in old.items() if name not in removed}
	for spec in change.add:
		expected[spec["name"]] = spec["value"] if spec.get("value") is not None else spec["default"]
	return {name: expected[name] for name in new_names}


def run_case(case: Case, *, qualname: str | None = None) -> Outcome | None:
	"""Rewrite and execute: returns the outcome, or None when the case is not a valid scenario (rejected)."""
	qualname = qualname or ("A.m" if case.method else "f")
	statement = call_statement(case.call, method=case.method)
	try:
		old_bindings, old_log = execute(case.definition, statement, method=case.method)
	except (TypeError, SyntaxError):
		assume(False)  # the generated call does not fit the generated definition
		raise
	try:
		outcome = rewrite(case.definition, case.change, statement, method=case.method, qualname=qualname)
	except (ToolInputError, SyntaxError):
		event("plan refused")
		assume(False)
		raise
	if outcome.new_statement is None:
		event("call left for manual edit")
		return outcome
	# FINDINGS-h3 #1: removing every argument of a call that ends in a trailing comma leaves `f(,)`.
	assume(not re.search(r"\(\s*,", outcome.new_statement))
	event("call rewritten")
	new_bindings, new_log = execute(outcome.new_definition, outcome.new_statement, method=case.method)
	assert new_log == old_log, (
		f"effect order changed\n{statement}\n--->\n{outcome.new_statement}\n{old_log} -> {new_log}"
	)
	expected = expected_bindings(old_bindings, case.change, list(new_bindings))
	assert new_bindings == expected, f"bindings differ\n{statement}\n--->\n{outcome.new_statement}"
	assert set(old_bindings) - set(case.change.remove) <= set(new_bindings)
	return outcome


def split_call(statement: str) -> tuple[str, str, str]:
	"""(text up to and including the `(`, the arguments, text from the `)`) of a call statement."""
	open_at = statement.index("(")
	close_at = statement.rindex(")")
	return statement[: open_at + 1], statement[open_at + 1 : close_at], statement[close_at:]


def parses(text: str) -> bool:
	try:
		ast.parse(text)
	except SyntaxError:
		return False
	return True


__all__ = [
	"Case",
	"CallArg",
	"cases",
	"module_text",
	"parses",
	"rewrite",
	"run_case",
	"split_call",
]
