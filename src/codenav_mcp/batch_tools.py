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
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pydantic
from mcp.server.mcpserver import Context

from codenav_mcp import tool_base


MAX_CALLS = 25
MAX_CALL_CHARS = 20_000
# Text a write tool returns when it did what was asked (applied, undone, previewed, or nothing to do).
_WRITE_OK_MARKERS = ("Applied as edit", "Applied edit", "Reverted edit", "Preview only", "No changes")

ToolFn = Callable[..., Awaitable[str]]


@dataclass
class _Entry:
	fn: ToolFn
	writes: bool
	params: dict[str, inspect.Parameter]
	adapters: dict[str, pydantic.TypeAdapter[Any]]
	wants_ctx: bool


_registry: dict[str, _Entry] = {}


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


def registered_tools() -> list[str]:
	return sorted(_registry)


def _hint_for(name: str) -> str:
	close = difflib.get_close_matches(name, _registry, n=2)
	suggestion = f" Did you mean {' or '.join(repr(c) for c in close)}?" if close else ""
	return f"Unknown tool {name!r}.{suggestion} Available: {', '.join(registered_tools())}."


def _signature_hint(name: str, entry: _Entry) -> str:
	parts = []
	for key, param in entry.params.items():
		if key == "ctx":
			continue
		parts.append(key if param.default is inspect.Parameter.empty else f"{key}=...")
	return f"{name}({', '.join(parts)})"


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
		return _hint_for(name)
	args = raw.pop("args", None)
	if args is None:
		args = raw  # lenient: {"tool": "outline", "file_path": "x.py"}
	elif raw:
		return f'Put {sorted(raw)} inside "args" (call shape: {{"tool": ..., "args": {{...}}}}).'
	if not isinstance(args, dict):
		return f'"args" must be an object of {name} parameters: {_signature_hint(name, entry)}.'
	unknown = [key for key in args if key not in entry.params or key == "ctx"]
	if unknown:
		return f"{name} has no parameter {', '.join(map(repr, unknown))}. Parameters: {_signature_hint(name, entry)}."
	kwargs: dict[str, Any] = {}
	for key, value in args.items():
		try:
			kwargs[key] = entry.adapters[key].validate_python(value) if key in entry.adapters else value
		except pydantic.ValidationError as exc:
			first = exc.errors()[0]
			return f"{name}: invalid {key!r}: {first['msg']}. Parameters: {_signature_hint(name, entry)}."
	missing = [
		key
		for key, param in entry.params.items()
		if key != "ctx" and param.default is inspect.Parameter.empty and key not in kwargs
	]
	if missing:
		return f"{name}: missing required {', '.join(map(repr, missing))}. Parameters: {_signature_hint(name, entry)}."
	return name, entry, kwargs


def _label(name: str, kwargs: dict[str, Any]) -> str:
	shown = ", ".join(f"{k}={v!r}" if len(repr(v)) <= 60 else f"{k}=..." for k, v in kwargs.items())
	return f"{name}({shown})"


def _write_failed(entry: _Entry, text: str) -> bool:
	return entry.writes and ("NOT applied" in text or not any(marker in text for marker in _WRITE_OK_MARKERS))


async def run_batch(calls: list[dict[str, Any]], stop_on_error: bool | None, ctx: Context | None) -> str:
	if not isinstance(calls, list) or not calls:
		return 'batch needs a non-empty "calls" list, e.g. [{"tool": "outline", "args": {"file_path": "src/a.py"}}].'
	if len(calls) > MAX_CALLS:
		return f"batch takes at most {MAX_CALLS} calls, got {len(calls)}. Split it into several batches."
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
		if _write_failed(entry, text):
			failed += 1
			halted = stop
			text = f"NOT DONE\n{text}"
		if len(text) > MAX_CALL_CHARS:
			text = f"{text[:MAX_CALL_CHARS]}\n... [truncated {len(text) - MAX_CALL_CHARS} characters; narrow this call]"
		sections.append(f"{header}\n{text}")
	summary = f"batch: {total - failed - skipped}/{total} ok"
	if failed:
		summary += f", {failed} failed"
	if skipped:
		summary += f", {skipped} skipped after the first failure (stop_on_error)"
	return "\n\n".join([*sections, summary])


def make_batch_tool() -> ToolFn:
	"""The `batch` tool function (call `register` for every tool it should reach first)."""

	async def batch(calls: list[dict[str, Any]], stop_on_error: bool | None = None, ctx: Context | None = None) -> str:
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
		keep going when it only reads. Up to 25 calls; `batch` cannot be nested.

		Not atomic: calls that already ran stay applied (each has its own undo id). Line/column
		positions you computed before the batch may be stale after an earlier edit in it, so
		prefer name-based calls (symbol_info, edit_symbol, rename_symbol) after writes. A preview
		id is invalid once a later call changes the same file.
		"""
		return await run_batch(calls, stop_on_error, ctx)

	return batch
