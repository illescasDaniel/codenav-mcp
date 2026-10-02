"""MCP tools that change code: rename, checked edits, and applying/undoing them.

Every tool simulates its edit against the type checker first and reports the
diagnostics it would add or fix. Refactorings preview by default
(`apply=false`); `apply_edit` writes a preview, `undo_edit` reverts an applied
edit if its files are untouched since.
"""

from __future__ import annotations

import subprocess  # noqa: S404
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context
from mcp_nav_shared.diagnostics_delta import format_delta
from mcp_nav_shared.edits import EditPlan, FileChange, decode_source, read_source, relative_name
from mcp_nav_shared.errors import TOOL_ERRORS, ToolInputError, format_tool_error

from codenav_mcp import tool_base
from codenav_mcp.deps import primary_module_name
from codenav_mcp.linked import ClassRef
from codenav_mcp.pysource import check_syntax
from codenav_mcp.rename import plan_rename, resolve_target
from codenav_mcp.simulate import diagnostics_delta, files_to_check
from codenav_mcp.tool_base import Session, open_session
from codenav_mcp.writes import PYTHON_SUFFIXES, errors_after_write, finish_plan


_PROBE_PATH = Path(".codenav_probe_rename.py")


def conforms_probe(session: Session) -> Any:
	"""A `conforms(port, candidate)` check that asks ty whether the class is assignable to the port."""
	srv = tool_base.server()

	async def conforms(port: ClassRef, candidate: ClassRef) -> bool:
		port_module = primary_module_name(port.path, session.roots)
		cand_module = primary_module_name(candidate.path, session.roots)
		if port_module is None or cand_module is None:
			return False
		code = srv._probe_source(port_module, port.name, cand_module, candidate.name)
		uri = (session.workspace / _PROBE_PATH).as_uri()
		async with srv._probe_lock:
			await session.client.open_scratch_document(uri, code)
			try:
				items = await session.client.pull_diagnostics(uri)
			finally:
				await session.client.close_scratch_document(uri)
		return not items

	return conforms


async def rename_symbol(
	new_name: str,
	name: str | None = None,
	file_path: str | None = None,
	line: int | None = None,
	column: int | None = None,
	parameter: str | None = None,
	apply: bool = False,
	linked: bool = True,
	max_new_errors: int | None = 0,
	allow_large: bool = False,
	ctx: Context | None = None,
) -> str:
	"""Rename a symbol everywhere it is used, then type-check the result before writing anything.

	Example: `rename_symbol(name="UserService.create_user", new_name="register_user")`.
	Give `name` (dotted `Class.method` accepted; `file_path` narrows it) or
	`file_path` + `line` + `column` (1-indexed). Use `parameter="x"` with a
	function's name to rename one of its parameters (keyword arguments at call
	sites follow).

	Built on ty's rename (imports, aliases, keyword arguments) plus what ty
	misses: with `linked=true` (default) overriding methods in subclasses,
	`super().name()` calls, and Protocol members together with the classes that
	satisfy them are renamed as one, since renaming only one side would silently
	break the override or the conformance. The preview lists every extra member
	it included, and the places still using the old name that the type checker
	could not link (untyped attribute accesses, strings, comments, docs).

	By default nothing is written (`apply=false`): the result is a diff plus the
	diagnostics the rename would add or fix, and an id for `apply_edit`. With
	`apply=true` it writes only if the new errors are within `max_new_errors`
	(default 0; pass null to apply regardless). Files are replaced atomically
	and `undo_edit` reverts them.
	"""
	try:
		session = await open_session(ctx)
		target = await resolve_target(
			session.client,
			session.workspace,
			name=name,
			file_path=file_path,
			line=line,
			column=column,
			parameter=parameter,
		)
		plan = await plan_rename(
			session.client,
			session.workspace,
			session.state.guard,
			target,
			new_name,
			linked=linked,
			conforms=conforms_probe(session) if linked else None,
		)
		outcome = await finish_plan(
			session.client,
			workspace=session.workspace,
			roots=session.roots,
			plan=plan,
			title=plan.description,
			apply=apply,
			max_new_errors=max_new_errors,
			allow_large=allow_large,
		)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return outcome.text


def _single_file_plan(session: Session, file_path: str, new_text: str | None, description: str) -> EditPlan:
	workspace = session.workspace
	candidate = Path(file_path)
	path = (candidate if candidate.is_absolute() else workspace / candidate).resolve()
	if path.suffix.lower() not in PYTHON_SUFFIXES:
		raise ToolInputError(f"codenav only edits Python files (.py/.pyi), got {file_path!r}")
	old_text, old_bom = (None, False)
	if path.exists():
		old_text, old_bom = read_source(path)
	if new_text is None:
		raise ToolInputError("nothing to check: pass new_text, or old_string and new_string")
	check_syntax(new_text, relative_name(path, workspace))
	return EditPlan(description, [FileChange(path, old_text, new_text, old_bom, old_bom)])


async def check_edit(
	file_path: str,
	new_text: str | None = None,
	old_string: str | None = None,
	new_string: str | None = None,
	replace_all: bool = False,
	apply: bool = False,
	max_new_errors: int | None = None,
	include_dependents: bool = True,
	ctx: Context | None = None,
) -> str:
	"""Dry-run an edit through the type checker, or make it, and see what it breaks before anything is written.

	Give the edit either as the whole new file (`new_text`) or like a text
	replacement (`old_string` -> `new_string`, which must match exactly once
	unless `replace_all`). Nothing is written by default: the result is the
	diagnostics the edit would add or fix in the file and in the files that
	import it, plus an id for `apply_edit`. Rejects text that is not valid Python.

	With `apply=true` the edit is written (atomically, undoable with
	`undo_edit`) unless it adds more errors than `max_new_errors` (null:
	no limit). The usual flow: `check_edit` first, change your approach if it
	reports new errors, then apply.
	"""
	try:
		session = await open_session(ctx)
		path = Path(file_path)
		path = (path if path.is_absolute() else session.workspace / path).resolve()
		if new_text is None:
			if old_string is None or new_string is None:
				raise ToolInputError("pass new_text (the whole file), or both old_string and new_string")
			if not path.is_file():
				raise ToolInputError(f"File not found: {file_path} (old_string/new_string need an existing file).")
			current = read_source(path)[0]
			count = current.count(old_string)
			if count == 0:
				raise ToolInputError("old_string was not found in the file.")
			if count > 1 and not replace_all:
				raise ToolInputError(f"old_string matches {count} times; make it unique or pass replace_all=true.")
			new_text = current.replace(old_string, new_string)
		plan = _single_file_plan(session, file_path, new_text, f"Edit {relative_name(path, session.workspace)}")
		outcome = await finish_plan(
			session.client,
			workspace=session.workspace,
			roots=session.roots,
			plan=plan,
			title=plan.description,
			apply=apply,
			max_new_errors=max_new_errors,
			include_dependents=include_dependents,
		)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return outcome.text


def _git(workspace: Path, *args: str) -> str:
	try:
		result = subprocess.run(  # noqa: S603
			["git", "-C", str(workspace), *args],  # noqa: S607
			capture_output=True,
			text=True,
			timeout=30,
			check=False,
		)
	except (OSError, subprocess.TimeoutExpired) as exc:
		raise ToolInputError(f"git is not usable here: {exc}") from exc
	if result.returncode != 0:
		raise ToolInputError(f"git {' '.join(args[:2])} failed: {result.stderr.strip() or result.stdout.strip()}")
	return result.stdout


def _git_show(workspace: Path, since: str, rel: str) -> str | None:
	try:
		result = subprocess.run(  # noqa: S603
			["git", "-C", str(workspace), "show", f"{since}:{rel}"],  # noqa: S607
			capture_output=True,
			timeout=30,
			check=False,
		)
	except (OSError, subprocess.TimeoutExpired):
		return None
	if result.returncode != 0:
		return None
	try:
		return decode_source(result.stdout)[0]
	except UnicodeDecodeError:
		return None


async def verify_changes(since: str = "HEAD", include_dependents: bool = True, ctx: Context | None = None) -> str:
	"""What did the edits since a git revision break? Compares ty diagnostics now against `since` (default HEAD).

	Run it after editing with any tool (your own Edit/Write included) to see
	the type errors the working tree has gained or lost relative to the last
	commit: the changed Python files and the files importing them are checked
	with the committed versions of the changed files substituted in for the
	"before" side. Read-only.
	"""
	try:
		if since.startswith("-"):
			raise ToolInputError(f"invalid revision {since!r}")
		session = await open_session(ctx)
		workspace = session.workspace
		top = Path(_git(workspace, "rev-parse", "--show-toplevel").strip()).resolve()
		status = _git(workspace, "diff", "--name-status", "-z", "--no-renames", since, "--", ".").split("\0")
		changed: list[tuple[str, str]] = []
		for index in range(0, len(status) - 1, 2):
			changed.append((status[index], status[index + 1]))
		untracked = [
			p for p in _git(workspace, "ls-files", "--others", "--exclude-standard", "-z", "--", ".").split("\0") if p
		]
		before: dict[Path, str] = {}
		touched: list[Path] = []
		for code, rel_to_top in changed:
			path = (top / rel_to_top).resolve()
			if path.suffix.lower() not in PYTHON_SUFFIXES:
				continue
			touched.append(path)
			if code == "A":
				before[path] = ""
			else:
				old = _git_show(workspace, since, rel_to_top)
				if old is not None:
					before[path] = old
		for rel in untracked:
			path = (workspace / rel).resolve()
			if path.suffix.lower() in PYTHON_SUFFIXES:
				touched.append(path)
				before[path] = ""
		if not touched:
			return f"No Python files differ from {since}."
		paths, left_out = files_to_check(workspace, session.roots, touched, include_dependents=include_dependents)
		delta = await diagnostics_delta(session.client, workspace, before=before, after={}, check=paths)
		if left_out:
			delta.unchecked.append(f"{left_out} more importing file(s) beyond the check limit")
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	names = ", ".join(relative_name(p, workspace) for p in touched[:8]) + (
		f" (+{len(touched) - 8} more)" if len(touched) > 8 else ""
	)
	return f"Changed since {since}: {names}\n\n{format_delta(delta)}"


async def apply_edit(id: str, allow_large: bool = False, ctx: Context | None = None) -> str:  # noqa: A002
	"""Write a previewed edit (the id comes from a write tool's preview). Fails if any file changed since the preview."""
	try:
		session = await open_session(ctx)
		plan = session.state.plans.get(id)
		entry = session.state.journal.apply(plan, allow_large=allow_large)
		session.state.plans.discard(id)
		after = await errors_after_write(session.client, session.workspace, plan)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return (
		f"Applied edit {entry.id}: {plan.description}\n\nFiles:\n{plan.summary(session.workspace)}\n\n{after}\n"
		f'Revert with undo_edit(id="{entry.id}").'
	)


async def undo_edit(id: str | None = None, ctx: Context | None = None) -> str:  # noqa: A002
	"""Revert an edit applied by the write tools (the latest one by default).

	Refuses when any of its files was changed after the edit, so later work is never overwritten.
	"""
	try:
		session = await open_session(ctx)
		entry = session.state.journal.undo(id)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return f"Reverted edit {entry.id}: {entry.plan.description}\n\nFiles restored:\n{entry.plan.summary(session.workspace)}"


TOOLS: list[tuple[Any, Any]] = [
	(rename_symbol, tool_base.WRITES),
	(check_edit, tool_base.WRITES),
	(verify_changes, tool_base.READS),
	(apply_edit, tool_base.WRITES),
	(undo_edit, tool_base.WRITES),
]
