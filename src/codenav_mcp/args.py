"""Argument aliases and strict validation for MCP calls."""

from __future__ import annotations

import difflib
import inspect
import re
import types
import typing
from collections.abc import Awaitable, Callable, Mapping, Set
from typing import Any

import pydantic


# Names echoed back in a refusal: at most this many, each cut short (callers may send anything).
_MAX_NAMES = 5
_MAX_NAME_PROBE = 64
ToolFn = Callable[..., Awaitable[str]]

# Names an agent guesses when it has not seen a tool's schema. Each maps to the real parameter it most likely means;
# an alias applies only to a tool that lacks the alias as a parameter of its own (`search` keeps its `path`).
_ARGUMENT_ALIASES: dict[str, tuple[str, ...]] = {
	"symbol": ("name", "query"),
	"symbol_name": ("name", "query"),
	"qualified_name": ("name", "query"),
	"file": ("file_path", "path"),
	"filepath": ("file_path", "path"),
	"path": ("file_path",),
	"col": ("column",),
	"line_number": ("line",),
	"lineno": ("line",),
	"query": ("name",),
	"name": ("query",),
	"in_": ("scope",),
	"port_name": ("name",),
	"edit_id": ("id",),
	"old_str": ("old_string",),
	"new_str": ("new_string",),
	"old_name": ("name",),
	"old": ("old_string",),
	"new": ("new_string",),
}
_ITEM = "edit item"  # the name `multi_edit` resolves the items of `edit_symbol(edits=[...])` under
_ITEM_EXAMPLE = '{"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}'
_EXAMPLE = re.compile(r"Examples?:\s*`([^`]+)`")
_SHOW_SECTIONS = re.compile(r"show=\[([a-z_, ]+)\]")


_registry: dict[str, ToolFn] = {}


def _bare(fn: Any) -> ToolFn:
	"""The tool function without the notices wrapper."""
	return inspect.unwrap(fn)


def register(name: str, fn: Any) -> None:
	_registry[name] = _bare(fn)


def signature_of(name: str) -> ToolFn | None:
	return _registry.get(name)


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
	with the same value it is accepted. Runs before `argument_problem`.
	"""
	params = set(inspect.signature(_bare(fn)).parameters) - {"ctx"}
	return resolve_aliases(name, params, args)


def resolve_aliases(name: str, params: Set[str], args: Mapping[str, Any]) -> tuple[dict[str, Any], str | None]:
	"""Resolve aliases for a registered signature or a nested edit item."""
	result: dict[str, Any] = {}
	given_as: dict[Any, Any] = {}  # real parameter -> the key the caller used for it first
	for key, value in args.items():
		target = key
		if isinstance(key, str) and key not in params:
			target = next((t for t in _ARGUMENT_ALIASES.get(key, ()) if t in params), key)
		if target in result and result[target] is not None and value is not None:
			# Equal only when identical in kind too: Python's `True == 1` must not merge two different answers.
			if result[target] != value or type(result[target]) is not type(value):
				message = f"give either {given_as[target]!r} or {key!r}, not both (one parameter: {target})"
				return dict(args), f"{name}: {message}"
			continue
		if target in result and value is None:
			continue  # an explicit null adds nothing to the value already given
		result[target] = value
		given_as[target] = key
	if name == _ITEM:
		unknown = [key for key in result if key not in params]
		if unknown:
			return dict(args), _unknown_item_keys(unknown, params)
	return result, None


def _unknown_item_keys(unknown: list[Any], params: Set[str]) -> str:
	"""The refusal for keys no edit item takes: the closest valid key for each, the valid keys, a literal item."""
	close = [
		f"{_short_repr(key)} -> {match[0]!r}"
		for key in unknown[:_MAX_NAMES]
		if (match := difflib.get_close_matches(str(key)[:_MAX_NAME_PROBE], sorted(params), n=1, cutoff=0.6))
	]
	did_you_mean = f"; did you mean {', '.join(close)}?" if close else ""
	return (
		f"unknown key(s) {_names_text(unknown)} in an edit item{did_you_mean}; allowed: {', '.join(sorted(params))}. "
		f"Example: {_ITEM_EXAMPLE}"
	)


def _show_problem(name: str, fn: ToolFn, args: Mapping[str, Any]) -> str | None:
	"""A `show=[...]` value the tool's own description does not list: name the valid sections (the description is
	the one list the caller sees, so the refusal is read from it)."""
	shown = args.get("show")
	found = _SHOW_SECTIONS.search(inspect.getdoc(_bare(fn)) or "")
	if found is None or not isinstance(shown, list):
		return None
	valid = [section.strip() for section in found.group(1).split(",")]
	unknown = [section for section in shown if section not in valid]
	if not unknown:
		return None
	return f"{name}: unknown show section {_names_text(unknown)}; valid: {', '.join(valid)}.{_example_suffix(fn)}"


def argument_problem(name: str, fn: ToolFn, args: Mapping[str, Any]) -> str | None:
	"""The refusal for arguments `fn` cannot take (unknown name, wrong type, missing), or None."""
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
			return (
				f"{name}: {key} must be {_type_phrase(hints.get(key))} (got {_short_repr(value)}).{_example_suffix(fn)}"
			)
	if (problem := _show_problem(name, fn, args)) is not None:
		return problem
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
