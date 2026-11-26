"""`batch`: several tool calls in one request.

An agent that wants to read three files or change five symbols would otherwise spend one
round-trip (and the tokens around it) per call. `batch` takes the list, runs the calls in
order through the real tool functions, and returns one numbered section per call. Each
call keeps its own behaviour: every write is type-checked, previewed or applied, and
undoable on its own, and later calls see what earlier calls wrote.
"""

from __future__ import annotations

import difflib
import inspect
import re
import types
import typing
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pydantic
from mcp.server.mcpserver import Context
from mcp_nav_shared.errors import FailedText
from mcp_nav_shared.output import cap_output, max_output_chars

from codenav_mcp import tool_base


MAX_CALLS = 25
MAX_CALL_CHARS = 20_000
# Names echoed back in a refusal: at most this many, each cut short (callers may send anything).
_MAX_NAMES = 5
_MAX_NAME_PROBE = 64
# A whole argument that names an earlier call's result, e.g. "$1" or "$2.path" (not supported in v1).
_RESULT_REFERENCE = re.compile(r"\$\d+(?:\.\w+)*")

ToolFn = Callable[..., Awaitable[str]]

# Names an agent guesses when it has not seen a tool's schema. Each maps to the real parameter it most likely means;
# an alias applies only to a tool that lacks the alias as a parameter of its own (`search_symbol` keeps its `path`).
_ARGUMENT_ALIASES: dict[str, tuple[str, ...]] = {
	"symbol": ("name", "query"),
	"symbol_name": ("name", "query"),
	"qualified_name": ("name", "query"),
	"file": ("file_path",),
	"filepath": ("file_path",),
	"path": ("file_path",),
	"col": ("column",),
	"line_number": ("line",),
	"lineno": ("line",),
	"old_str": ("old_string",),
	"new_str": ("new_string",),
}
_EXAMPLE = re.compile(r"Examples?:\s*`([^`]+)`")


@dataclass
class _Entry:
	fn: ToolFn
	writes: bool
	params: dict[str, inspect.Parameter]
	adapters: dict[str, pydantic.TypeAdapter[Any]]
	wants_ctx: bool


_registry: dict[str, _Entry] = {}
# Parameters of tools that are not batchable (`batch` itself), so direct calls can be checked too.
_signatures: dict[str, ToolFn] = {}


def _bare(fn: Any) -> ToolFn:
	"""The tool function without the notices wrapper (the batch appends notices once, at the end)."""
	return inspect.unwrap(fn)


def register(name: str, fn: Any, *, writes: bool) -> None:
	"""Make `fn` callable from `batch` under `name`."""
	bare = _bare(fn)
	params = dict(inspect.signature(bare).parameters)
	hints = typing.get_type_hints(bare)
	adapters = {
		key: pydantic.TypeAdapter(hints[key]) for key in params if key != "ctx" and key in hints and key != "return"
	}
	_registry[name] = _Entry(bare, writes, params, adapters, "ctx" in params)


def register_signature(name: str, fn: Any) -> None:
	"""Record the parameters of a tool that `batch` does not run (direct calls are still checked)."""
	_signatures[name] = _bare(fn)


def signature_of(name: str) -> ToolFn | None:
	"""The tool function behind `name` for argument checks, or None for an unknown tool."""
	entry = _registry.get(name)
	return entry.fn if entry is not None else _signatures.get(name)


def registered_tools() -> list[str]:
	return sorted(_registry)


def unknown_tool_message(name: str) -> str:
	"""The refusal for a tool name that is not registered. The name is cut short: callers may send anything."""
	close = difflib.get_close_matches(name[:_MAX_NAME_PROBE], _registry, n=2)
	suggestion = f" Did you mean {' or '.join(repr(c) for c in close)}?" if close else ""
	return f"Unknown tool {_short_repr(name)}.{suggestion} Available: {', '.join(registered_tools())}."


def _signature_hint(name: str, params: Mapping[str, inspect.Parameter]) -> str:
	parts = []
	for key, param in params.items():
		if key == "ctx":
			continue
		parts.append(key if param.default is inspect.Parameter.empty else f"{key}=...")
	return f"{name}({', '.join(parts)})"


def _type_phrase(annotation: Any) -> str:
	"""How a parameter's declared type reads in a refusal ("must be a whole number")."""
	origin = typing.get_origin(annotation)
	if origin is typing.Annotated:
		return _type_phrase(typing.get_args(annotation)[0])
	if origin in (typing.Union, types.UnionType):
		members = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
		return _type_phrase(members[0]) if len(members) == 1 else "a value of the documented type"
	if origin is typing.Literal:
		return _one_of(typing.get_args(annotation))
	base = origin or annotation
	if base is bool:
		return "true or false"
	if base is int:
		return "a whole number"
	if base is float:
		return "a number"
	if base is str:
		return "text"
	if base is list:
		element = next(iter(typing.get_args(annotation)), None)
		if element is dict or typing.get_origin(element) is dict:
			return "a list of objects"
		return "a list"
	if base is dict:
		return "an object"
	return "a value of the documented type"


def _one_of(values: tuple[Any, ...]) -> str:
	"""The allowed values of an enum-like parameter, quoted: "one of 'replace', 'insert', 'delete'"."""
	return "one of " + ", ".join(repr(value) for value in values)


def _allowed_values(annotation: Any) -> str | None:
	"""The allowed values behind a Literal parameter (optionally Optional), or None for other types."""
	origin = typing.get_origin(annotation)
	if origin in (typing.Union, types.UnionType):
		members = [arg for arg in typing.get_args(annotation) if arg is not type(None)]
		return _allowed_values(members[0]) if len(members) == 1 else None
	if origin is typing.Annotated:
		return _allowed_values(typing.get_args(annotation)[0])
	return _one_of(typing.get_args(annotation)) if origin is typing.Literal else None


def _short_repr(value: Any) -> str:
	text = repr(value)
	return text if len(text) <= 40 else f"{text[:37]}..."


def _names_text(names: list[Any]) -> str:
	"""At most `_MAX_NAMES` names, each cut short, so a refusal stays small whatever the caller sent."""
	shown = ", ".join(_short_repr(name) for name in names[:_MAX_NAMES])
	more = f", ... and {len(names) - _MAX_NAMES} more" if len(names) > _MAX_NAMES else ""
	return shown + more


def example_call(fn: ToolFn) -> str | None:
	"""The call shown after `Example:` in the tool's docstring, on one line, or None."""
	found = _EXAMPLE.search(inspect.getdoc(_bare(fn)) or "")
	return " ".join(found.group(1).split()) if found else None


def _example_suffix(fn: ToolFn) -> str:
	example = example_call(fn)
	return f" Example: {example}" if example else ""


def apply_aliases(name: str, fn: ToolFn, args: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
	"""`args` with guessed argument names (`symbol`, `file`, `col`, ...) renamed to the tool's real ones.

	Returns the rewritten arguments and a refusal (or None). An alias is only used when the tool has no parameter
	of that name and does have the target; giving the alias and its target with different values is refused,
	with the same value it is accepted. Runs before `argument_problem` on both the direct and the batch path.
	"""
	params = set(inspect.signature(_bare(fn)).parameters) - {"ctx"}
	result: dict[str, Any] = {}
	given_as: dict[Any, Any] = {}  # real parameter -> the key the caller used for it first
	for key, value in args.items():
		target = key
		if isinstance(key, str) and key not in params:
			target = next((t for t in _ARGUMENT_ALIASES.get(key, ()) if t in params), key)
		if target in result and result[target] is not None and value is not None:
			if result[target] != value:
				message = f"give either {given_as[target]!r} or {key!r}, not both (one parameter: {target})"
				return dict(args), f"{name}: {message}"
			continue
		if target in result and value is None:
			continue  # an explicit null adds nothing to the value already given
		result[target] = value
		given_as[target] = key
	return result, None


def argument_problem(name: str, fn: ToolFn, args: Mapping[str, Any]) -> str | None:
	"""The refusal for arguments `fn` cannot take (unknown name, wrong type, missing), or None.

	Shared by `batch` and the direct MCP path, so both answer the same way.
	"""
	bare = _bare(fn)
	params = {key: param for key, param in inspect.signature(bare).parameters.items() if key != "ctx"}
	hints = typing.get_type_hints(bare, include_extras=True)  # keep StrictBool: plain bool would accept "yes"
	unknown = [key for key in args if key not in params]
	if unknown:

		def _closest(key: Any) -> str | None:
			match = difflib.get_close_matches(str(key), list(params), n=1, cutoff=0.6)
			return match[0] if match else None

		allowed = ", ".join(params)
		noun = "argument" if len(unknown) == 1 else "arguments"
		if len(unknown) == 1:
			close = _closest(unknown[0])
			did_you_mean = f"; did you mean {close!r}?" if close else ""
		else:
			plural = [f"{c!r} for {_short_repr(key)}" for key in unknown[:_MAX_NAMES] if (c := _closest(key))]
			did_you_mean = f"; did you mean {', '.join(plural)}?" if plural else ""
		return (
			f"unknown {noun} {_names_text(unknown)} for {name}{did_you_mean}; allowed: {allowed}.{_example_suffix(fn)}"
		)
	for key, value in args.items():
		try:
			# Strict: a string "3" or a bool is not a line number, as in every other parameter (NAVP-012).
			pydantic.TypeAdapter(hints.get(key, Any)).validate_python(value, strict=True)
		except pydantic.ValidationError:
			return f"{name}: {key} must be {_type_phrase(hints.get(key))} (got {_short_repr(value)})"
	missing = [key for key, param in params.items() if param.default is inspect.Parameter.empty and key not in args]
	if missing:
		hint = _signature_hint(name, params)
		# Enum-like parameters name their allowed values, so the agent does not have to guess (RC-031).
		choices = [f"allowed {key}: {allowed}" for key in missing if (allowed := _allowed_values(hints.get(key)))]
		extra = f" ({'; '.join(choices)})" if choices else ""
		return (
			f"{name}: missing required {', '.join(map(repr, missing))}{extra}. Parameters: {hint}.{_example_suffix(fn)}"
		)
	return None


def _result_reference(value: Any) -> str | None:
	"""The first argument value (at any depth) that names an earlier call's result, e.g. "$1"."""
	if isinstance(value, str):
		found = _RESULT_REFERENCE.fullmatch(value.strip())
		return found.group(0) if found else None
	items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
	for item in items:
		found_in = _result_reference(item)
		if found_in is not None:
			return found_in
	return None


def _prepare(index: int, raw: Any) -> tuple[str, _Entry, dict[str, Any]] | str:
	"""Validate one call; return (name, entry, kwargs) or the error text for its section."""
	if not isinstance(raw, dict):
		return f'Call {index} must be an object like {{"tool": "outline", "args": {{"file_path": "src/a.py"}}}}.'
	raw = dict(raw)
	name = raw.pop("tool", None) or raw.pop("name", None)
	if not isinstance(name, str) or not name:
		return 'Call is missing "tool" (the tool name, e.g. "outline").'
	if name == "batch":
		return "batch calls cannot be nested."
	entry = _registry.get(name)
	if entry is None:
		return unknown_tool_message(name)
	args = raw.pop("args", None)
	if args is None:
		args = raw  # lenient: {"tool": "outline", "file_path": "x.py"}
	elif raw:
		return f'Put {_names_text(sorted(raw))} inside "args" (call shape: {{"tool": ..., "args": {{...}}}}).'
	if not isinstance(args, dict):
		return f'"args" must be an object of {name} parameters: {_signature_hint(name, entry.params)}.'
	args, problem = apply_aliases(name, entry.fn, args)
	problem = problem or argument_problem(name, entry.fn, args)
	if problem is not None:
		return problem
	reference = _result_reference(args)
	if reference is not None:
		return (
			f"batch call #{index}: result references ({reference}) are not supported in v1; "
			"make the calls independent or run them one by one"
		)
	kwargs: dict[str, Any] = {
		key: entry.adapters[key].validate_python(value) if key in entry.adapters else value
		for key, value in args.items()
	}
	return name, entry, kwargs


def _label(name: str, kwargs: dict[str, Any]) -> str:
	shown = ", ".join(f"{k}={v!r}" if len(repr(v)) <= 60 else f"{k}=..." for k, v in kwargs.items())
	return f"{name}({shown})"


async def run_batch(calls: list[dict[str, Any]], stop_on_error: bool | None, ctx: Context | None) -> str:
	if not isinstance(calls, list) or not calls:
		return FailedText(
			'batch needs a non-empty "calls" list, e.g. [{"tool": "outline", "args": {"file_path": "src/a.py"}}].'
		)
	if len(calls) > MAX_CALLS:
		return FailedText(f"batch takes at most {MAX_CALLS} calls, got {len(calls)}. Split it into several batches.")
	if ctx is not None:
		# Pick the workspace once; the calls then run with ctx=None, which skips the per-call lookup.
		await tool_base.server()._use_workspace(ctx)
	prepared = [_prepare(i, raw) for i, raw in enumerate(calls, 1)]
	has_write = any(not isinstance(p, str) and p[1].writes for p in prepared)
	stop = has_write if stop_on_error is None else stop_on_error
	sections: list[str] = []
	failed = skipped = 0
	halted = False
	total = len(prepared)
	for index, item in enumerate(prepared, 1):
		if halted:
			skipped += 1
			continue
		if isinstance(item, str):
			sections.append(f"[{index}/{total}] ERROR\n{item}")
			failed += 1
			halted = stop
			continue
		name, entry, kwargs = item
		header = f"[{index}/{total}] {_label(name, kwargs)}"
		call_kwargs = {**kwargs, "ctx": None} if entry.wants_ctx else kwargs
		try:
			text = await entry.fn(**call_kwargs)
		except Exception as exc:  # noqa: BLE001 - one broken call must not lose the others' results
			sections.append(f"{header}\nERROR: {type(exc).__name__}: {exc}")
			failed += 1
			halted = stop
			continue
		if isinstance(text, FailedText):
			failed += 1
			halted = stop
			text = f"NOT DONE\n{text}"
		if len(text) > MAX_CALL_CHARS:
			text = f"{text[:MAX_CALL_CHARS]}\n... [truncated {len(text) - MAX_CALL_CHARS} characters; narrow this call]"
		sections.append(f"{header}\n{text}")
	limit = max_output_chars()
	shortened: list[int] = []
	if sum(map(len, sections)) > limit:
		share = max((limit - 300 - 2 * total) // total, 400)
		for i, section in enumerate(sections):
			if len(section) > share:
				sections[i] = cap_output(section, share)
				shortened.append(int(section[1 : section.index("/")]))
	summary = f"batch: {total - failed - skipped}/{total} ok"
	if failed:
		summary += f", {failed} failed"
	if skipped:
		summary += f", {skipped} skipped after the first failure (stop_on_error)"
	if shortened:
		numbers = ", ".join(f"#{n}" for n in shortened)
		summary += f"\nOutput limit: call(s) {numbers} were shortened. Run them alone or narrow them (path=, limit=)."
	result = "\n\n".join([*sections, summary])
	return FailedText(result) if failed and (halted or failed == total) else result


def make_batch_tool() -> ToolFn:
	"""The `batch` tool function (call `register` for every tool it should reach first)."""

	async def batch(
		calls: list[dict[str, Any]], stop_on_error: pydantic.StrictBool | None = None, ctx: Context | None = None
	) -> str:
		"""Run several codenav tool calls in ONE request (saves round-trips and tokens).

		Example: `batch(calls=[{"tool": "outline", "args": {"file_path": "src/a.py"}},
		{"tool": "symbol_info", "args": {"name": "Cart.total"}}])`. Works for reads and writes
		alike, e.g. two edits in different files: `[{"tool": "edit", "args": {"file_path":
		"src/a.py", "old_string": "x = 1", "new_string": "x = 2"}}, {"tool": "edit_symbol", "args": {...}}]`.

		Each call takes the same arguments as the tool called alone (`args` is that tool's
		parameter object) and behaves as if you had called it separately: calls run in order,
		so a later call sees what an earlier write changed, and every write is type-checked,
		previewed/applied and undoable on its own (see its own section for the edit id).
		The result has one numbered section per call, then a summary line.

		`stop_on_error`: stop at the first call that fails or does not apply a write (the
		rest are skipped and listed). Default (null): stop when the batch contains a write,
		keep going when it only reads. Up to 25 calls; `batch` cannot be nested. A call cannot
		refer to an earlier call's result (`"$1"` is refused): make the calls independent.

		Annotated as possibly destructive because a batch may contain write calls; a batch
		made only of read tools (outline, symbol_info, hover, ...) writes nothing.

		Not atomic: calls that already ran stay applied (each has its own undo id). Line/column
		positions you computed before the batch may be stale after an earlier edit in it, so
		prefer name-based calls (symbol_info, edit_symbol, rename_symbol) after writes. A preview
		id is invalid once a later call changes the same file.
		"""
		return await run_batch(calls, stop_on_error, ctx)

	return batch
