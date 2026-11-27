"""Same-named stand-ins a signature change leaves alone: test fakes the type checker does not link to the target.

A local `class _Fake: def execute(...)` in a test, or a function wired in with `mock.execute.side_effect = _execute`,
stands in for the changed callable without ty knowing. When the change makes callers pass something new, these
stand-ins start failing with a TypeError at test time. They are found by name plus parameter overlap with the
target, so unrelated methods that merely share the name (`connection.execute(sql)`) are not reported.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from mcp_nav_shared.edits import read_source, relative_name

from codenav_mcp.pysource import parse_python


_RECEIVERS = {"self", "cls"}


@dataclass(frozen=True)
class StandIn:
	path: Path
	line: int  # 1-based line of the `def`
	label: str  # `Class.method`, `function`, or `function (side_effect of .name, line N)`


def _param_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
	args = node.args
	names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
	if names and names[0] in _RECEIVERS:
		names = names[1:]
	return [name.lstrip("_") for name in names]


def _looks_like(node: ast.FunctionDef | ast.AsyncFunctionDef, target: set[str]) -> bool:
	"""At least half of the target's parameters (and at least one) appear in `node`'s, ignoring leading `_`."""
	if not target:
		return False
	shared = len(target & set(_param_names(node)))
	return shared >= 1 and shared * 2 >= len(target)


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
	return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _qualname(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
	parts = [getattr(node, "name", "?")]
	parent = parents.get(node)
	while parent is not None and not isinstance(parent, ast.Module):
		if isinstance(parent, ast.ClassDef):
			parts.append(parent.name)
		elif isinstance(parent, ast.FunctionDef | ast.AsyncFunctionDef):
			break  # a class local to a test function: `_Fake.execute` reads better than the test's name
		parent = parents.get(parent)
	return ".".join(reversed(parts))


def _wired_function(node: ast.Assign, name: str) -> tuple[str, int] | None:
	"""`x.<name>.side_effect = f` or `x.<name> = f`: (f, line)."""
	if not isinstance(node.value, ast.Name):
		return None
	for target in node.targets:
		if isinstance(target, ast.Attribute):
			if target.attr == name:
				return node.value.id, node.lineno
			if target.attr == "side_effect" and isinstance(target.value, ast.Attribute) and target.value.attr == name:
				return node.value.id, node.lineno
	return None


def find_stand_ins(
	files: Iterable[Path],
	workspace: Path,
	*,
	name: str,
	target_params: list[str],
	changed: set[tuple[Path, int]],
) -> list[StandIn]:
	"""Functions named `name`, or wired in place of `.name`, whose parameters look like the target's.

	`changed`: (resolved path, def line) of the declarations the plan already changes; those are skipped."""
	target = {param.lstrip("_") for param in target_params if param not in _RECEIVERS}
	found: list[StandIn] = []
	for path in files:
		try:
			text = read_source(path)[0]
		except (OSError, UnicodeError, ValueError):
			continue
		if name not in text:
			continue
		try:
			tree = parse_python(text, path.name)
		except (SyntaxError, ValueError):
			continue
		resolved = path.resolve()
		parents = _parents(tree)
		functions: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
		for node in ast.walk(tree):
			if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
				functions.setdefault(node.name, []).append(node)
		seen: set[int] = set()
		for node in functions.get(name, []):
			if (resolved, node.lineno) in changed or not _looks_like(node, target):
				continue
			seen.add(node.lineno)
			found.append(StandIn(path, node.lineno, _qualname(node, parents)))
		for node in ast.walk(tree):
			wired = _wired_function(node, name) if isinstance(node, ast.Assign) else None
			if wired is None:
				continue
			function, line = wired
			for definition in functions.get(function, []):
				if definition.lineno in seen or (resolved, definition.lineno) in changed:
					continue
				if not _looks_like(definition, target):
					continue
				seen.add(definition.lineno)
				label = f"{function} (wired in as .{name}, line {line})"
				found.append(StandIn(path, definition.lineno, label))
	found.sort(key=lambda item: (relative_name(item.path, workspace), item.line))
	return found


def stand_ins_note(stand_ins: list[StandIn], workspace: Path, *, qualname: str, added: list[str]) -> str | None:
	if not stand_ins:
		return None
	if added:
		what = ", ".join(f"`{name}`" for name in added)
		fix = f"calls that now pass {what} would raise TypeError there; add the parameter by hand if they stand in for {qualname}"
	else:
		fix = f"update them by hand if they stand in for {qualname}"
	shown = stand_ins[:10]
	rows = "\n".join(f"    {relative_name(s.path, workspace)}:{s.line}  {s.label}" for s in shown)
	more = f"\n    ... {len(stand_ins) - 10} more" if len(stand_ins) > 10 else ""
	return (
		f"possible fakes with the same name and parameters, not linked by the type checker, left unchanged ({fix}):\n"
		+ rows
		+ more
	)
