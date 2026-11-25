"""MCP tools that change code: rename, checked edits, and applying/undoing them.

Every tool simulates its edit against the type checker first and reports the
diagnostics it would add or fix. Refactorings preview by default
(`apply=false`); `apply_edit` writes a preview, `undo_edit` reverts an applied
edit if its files are untouched since.
"""

from __future__ import annotations

import difflib
import re
import subprocess  # noqa: S404
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context
from mcp_nav_shared.diagnostics_delta import format_delta
from mcp_nav_shared.edits import (
	EditPlan,
	FileChange,
	convert_eol,
	decode_source,
	detect_eol,
	read_source,
	relative_name,
	split_lines,
)
from mcp_nav_shared.errors import TOOL_ERRORS, ToolInputError, format_tool_error
from mcp_nav_shared.transaction import refuse_home_path

from codenav_mcp import tool_base
from codenav_mcp.deps import Skipped, format_skipped, primary_module_name
from codenav_mcp.linked import ClassRef
from codenav_mcp.pysource import check_syntax
from codenav_mcp.rename import plan_rename, resolve_target
from codenav_mcp.simulate import CHECK_LIMIT, diagnostics_delta, files_to_check
from codenav_mcp.tool_base import Session, open_session
from codenav_mcp.writes import PYTHON_SUFFIXES, errors_after_write, finish_plan, require_workspace_file


_PROBE_PATH = Path(".codenav_probe_rename.py")
# Largest `new_text` / `new_string` the edit tools accept: a bigger preview takes minutes and wedges the language server.
MAX_INPUT_CHARS = 1_000_000
# Project settings that change what the type checker reports for every file.
CONFIG_FILES = frozenset({"pyproject.toml", "ty.toml", ".ty.toml"})
_MATCH_LINES_SHOWN = 10
_LEADING_TABS = re.compile(r"(?m)^\t+")
_LEADING_SPACES = re.compile(r"(?m)^(?: {4})+")


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
	apply: bool = True,
	linked: bool = True,
	max_new_errors: int | None = 0,
	allow_large: bool = False,
	ctx: Context | None = None,
) -> str:
	"""Rename a symbol everywhere it is used, type-checking the result before writing it.

	Example: `rename_symbol(name="UserService.create_user", new_name="register_user")`.
	Give `name` (dotted `Class.method` accepted; `file_path` narrows it) or
	`file_path` + `line` + `column` (1-indexed). Use `parameter="x"` with a
	function's name to rename one of its parameters (keyword arguments at call
	sites follow).

	Built on ty's rename (imports, aliases, keyword arguments) plus what ty
	misses: with `linked=true` (default) overriding methods in subclasses,
	`super().name()` calls, and Protocol members together with the classes that
	satisfy them are renamed as one, since renaming only one side would silently
	break the override or the conformance. The result lists every extra member
	it included, and the places still using the old name that the type checker
	could not link (untyped attribute accesses, strings, comments, docs).

	Writing: `apply=true` (default) writes when the new errors are within
	`max_new_errors` (default 0); otherwise nothing is written and you get a
	preview id for `apply_edit`. `apply=false` always previews. `undo_edit` reverts.
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
			roots=session.roots,
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


def _edit_target(workspace: Path, file_path: str) -> Path:
	"""The resolved path an `edit` may touch. Outside the workspace or a directory is refused before anything is read."""
	refuse_home_path(file_path)
	candidate = Path(file_path)
	resolved = (candidate if candidate.is_absolute() else workspace / candidate).resolve()
	if resolved.exists():
		return require_workspace_file(workspace, file_path)
	try:
		resolved.relative_to(workspace.resolve())
	except ValueError:
		display = f"<outside workspace>/{resolved.name}" if candidate.is_absolute() else file_path
		raise ToolInputError(f"{display} is outside the workspace; give a path inside it") from None
	return resolved


def _check_edit_arguments(new_text: str | None, old_string: str | None, new_string: str | None) -> None:
	"""Refuse argument combinations that would be ambiguous, empty or too large, before the file is touched."""
	if new_text is not None:
		if old_string is not None or new_string is not None:
			raise ToolInputError("give either new_text (whole file) or old_string+new_string (replace), not both")
	elif old_string is None and new_string is None:
		raise ToolInputError("pass new_text (the whole file), or both old_string and new_string")
	elif old_string is None or new_string is None:
		raise ToolInputError("old_string and new_string go together")
	elif old_string == "":
		raise ToolInputError("old_string must not be empty")
	for label, value in (("new_text", new_text), ("new_string", new_string)):
		if value is not None and len(value) > MAX_INPUT_CHARS:
			raise ToolInputError(
				f"{label} is too large ({len(value)} characters; limit {MAX_INPUT_CHARS}); "
				"split the change or edit the file directly"
			)


def _match_lines(current: str, old_string: str) -> list[int]:
	"""1-based line numbers where `old_string` starts, non-overlapping like `str.replace`."""
	lines: list[int] = []
	index = current.find(old_string)
	while index != -1:
		lines.append(current.count("\n", 0, index) + 1)
		index = current.find(old_string, index + len(old_string))
	return lines


def _nearest_line(current: str, old_string: str) -> tuple[int, str] | None:
	"""The 1-based number and text of the file line most like the first line of `old_string`, for a near miss."""
	first = next((line.strip() for line in split_lines(old_string) if line.strip()), "")
	if len(first) < 4:
		return None
	lines = split_lines(current)
	stripped = [line.strip() for line in lines]
	match = difflib.get_close_matches(first, stripped, n=1, cutoff=0.6)
	if not match:
		return None
	index = stripped.index(match[0])
	return index + 1, match[0]


def _not_found_message(current: str, old_string: str) -> str:
	"""The 'not found' refusal, with the one likely cause it can be recognised as (line endings or indentation)."""
	message = "old_string was not found in the file."
	eol = detect_eol(current)
	with_file_eol = convert_eol(old_string, eol)
	if with_file_eol != old_string and with_file_eol in current:
		name = "CRLF" if eol == "\r\n" else "LF"
		return f"{message} The file uses {name} line endings; old_string must use them too."
	spaced = _LEADING_TABS.sub(lambda found: "    " * len(found.group(0)), old_string)
	if spaced != old_string and spaced in current:
		return f"{message} The file indents with spaces; old_string uses tabs."
	tabbed = _LEADING_SPACES.sub(lambda found: "\t" * (len(found.group(0)) // 4), old_string)
	if tabbed != old_string and tabbed in current:
		return f"{message} The file indents with tabs; old_string uses spaces."
	near = _nearest_line(current, old_string)
	if near is not None:
		return f"{message} Nearest line {near[0]}: {near[1]}"
	return message


def _single_file_plan(session: Session, path: Path, new_text: str, description: str) -> EditPlan:
	workspace = session.workspace
	if path.suffix.lower() not in PYTHON_SUFFIXES:
		raise ToolInputError(f"codenav only edits Python files (.py/.pyi), got {relative_name(path, workspace)!r}")
	old_text, old_bom = (None, False)
	if path.exists():
		old_text, old_bom = read_source(path)
	check_syntax(new_text, relative_name(path, workspace))
	return EditPlan(description, [FileChange(path, old_text, new_text, old_bom, old_bom)])


async def _edit_text(
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
	no limit). The usual flow: `edit(apply=false)` first, change your approach if it
	reports new errors, then apply.
	"""
	try:
		_check_edit_arguments(new_text, old_string, new_string)
		session = await open_session(ctx)
		workspace = session.workspace
		path = _edit_target(workspace, file_path)
		current = read_source(path)[0] if path.is_file() else None
		description = f"Edit {relative_name(path, workspace)}"
		title = description
		if new_text is None:
			if current is None:
				raise ToolInputError(f"File not found: {file_path} (old_string/new_string need an existing file).")
			if old_string is None or new_string is None:  # _check_edit_arguments ruled this out already
				raise ToolInputError("pass new_text (the whole file), or both old_string and new_string")
			lines = _match_lines(current, old_string)
			if not lines:
				raise ToolInputError(_not_found_message(current, old_string))
			if len(lines) > 1 and not replace_all:
				# Several matches can start on one line: list each line once, but the count stays the match count.
				distinct = list(dict.fromkeys(lines))
				shown = ", ".join(str(number) for number in distinct[:_MATCH_LINES_SHOWN])
				more = f" and {len(distinct) - _MATCH_LINES_SHOWN} more" if len(distinct) > _MATCH_LINES_SHOWN else ""
				raise ToolInputError(
					f"old_string matches {len(lines)} times (lines {shown}{more}); add context or pass replace_all=true"
				)
			replacement = convert_eol(new_string, detect_eol(current))
			new_text = current.replace(old_string, replacement)
			if new_string != old_string:
				title += f"\nReplaced {len(lines)} occurrence(s)."
		elif current is not None:
			new_text = convert_eol(new_text, detect_eol(current))
		plan = _single_file_plan(session, path, new_text, description)
		outcome = await finish_plan(
			session.client,
			workspace=workspace,
			roots=session.roots,
			plan=plan,
			title=title,
			apply=apply,
			max_new_errors=max_new_errors,
			include_dependents=include_dependents,
		)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return outcome.text


def _has_commits(workspace: Path) -> bool:
	"""False in a repository whose HEAD is unborn (`git init` with no commit)."""
	try:
		result = subprocess.run(  # noqa: S603
			["git", "-C", str(workspace), "rev-parse", "--verify", "-q", "HEAD"],  # noqa: S607
			capture_output=True,
			text=True,
			timeout=30,
			check=False,
		)
	except (OSError, subprocess.TimeoutExpired):
		return True  # let the git call below report the real problem
	return result.returncode == 0


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
		lines = (result.stderr.strip() or result.stdout.strip()).splitlines()
		first = (lines[0] if lines else f"exit status {result.returncode}")[:200]
		raise ToolInputError(f"git failed: {first}")
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
	"before" side. Read-only. A changed pyproject.toml, ty.toml or .ty.toml changes the rules for every file:
	then the whole project is checked and every current diagnostic is listed, because the old configuration
	cannot be re-checked.
	"""
	try:
		if since.startswith("-"):
			raise ToolInputError(f"invalid revision {since!r}")
		session = await open_session(ctx)
		workspace = session.workspace
		top = Path(_git(workspace, "rev-parse", "--show-toplevel").strip()).resolve()
		if since == "HEAD" and not _has_commits(workspace):
			raise ToolInputError(
				"this git repository has no commits yet, so there is no HEAD to compare with; "
				"make a first commit, or pass since=<revision> once there is one"
			)
		status = _git(workspace, "diff", "--name-status", "-z", "--no-renames", since, "--", ".").split("\0")
		changed: list[tuple[str, str]] = []
		for index in range(0, len(status) - 1, 2):
			changed.append((status[index], status[index + 1]))
		untracked = [
			p for p in _git(workspace, "ls-files", "--others", "--exclude-standard", "-z", "--", ".").split("\0") if p
		]
		before: dict[Path, str] = {}
		touched: list[Path] = []
		added: set[Path] = set()
		deleted: set[Path] = set()
		missing_base: set[Path] = set()
		config: list[str] = []
		for code, rel_to_top in changed:
			path = (top / rel_to_top).resolve()
			if Path(rel_to_top).name in CONFIG_FILES:
				config.append(relative_name(path, workspace))
			if path.suffix.lower() not in PYTHON_SUFFIXES:
				continue
			touched.append(path)
			if code == "A":
				added.add(path)
			else:
				old = _git_show(workspace, since, rel_to_top)
				if old is None:
					missing_base.add(path)
				else:
					before[path] = old
				if code == "D":
					deleted.add(path)
		for rel in untracked:
			path = (workspace / rel).resolve()
			if Path(rel).name in CONFIG_FILES:
				config.append(relative_name(path, workspace))
			if path.suffix.lower() in PYTHON_SUFFIXES:
				touched.append(path)
				added.add(path)
		if not touched and not config:
			return (
				"No uncommitted Python changes. No Python files differ from HEAD."
				if since == "HEAD"
				else f"No Python files differ from {since}."
			)
		if added:
			# The server sees the working tree during both passes; it cannot hide newly
			# added modules while checking the historical snapshot and its importers.
			names = ", ".join(sorted(relative_name(path, workspace) for path in added))
			raise ToolInputError(
				"Cannot verify changes: the historical comparison cannot hide added Python files "
				f"({names}); inspect the changes manually."
			)
		skipped: list[Skipped] = []
		if config:
			# The old configuration cannot be re-run, so nothing is compared: the project's files are checked as they are now.
			pool = list(dict.fromkeys([*touched, *_project_python_files(workspace)]))
			paths, left_out = pool[:CHECK_LIMIT], max(0, len(pool) - CHECK_LIMIT)
			skip_before = set(paths)
		else:
			paths, left_out = files_to_check(
				workspace, session.roots, touched, include_dependents=include_dependents, skipped=skipped
			)
			skip_before = added | missing_base
		delta = await diagnostics_delta(
			session.client,
			workspace,
			before=before,
			after={},
			check=paths,
			skip_before=skip_before,
			skip_after=deleted | missing_base,
		)
		delta.unchecked = sorted(set(delta.unchecked) | {relative_name(p, workspace) for p in missing_base})
		delta.files_checked -= len(missing_base.intersection(paths))
		if left_out:
			delta.unchecked.append(f"{left_out} more importing file(s) beyond the check limit")
		# Importers in excluded directories are not checked on purpose; say so instead of passing silently.
		delta.notes.extend(format_skipped([s for s in skipped if s.reason == "excluded dir"], verb="checked"))
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	if touched:
		names = ", ".join(relative_name(p, workspace) for p in touched[:8]) + (
			f" (+{len(touched) - 8} more)" if len(touched) > 8 else ""
		)
	else:
		names = "no Python files"
	header = [f"Changed since {since}: {names}"]
	if not include_dependents:
		header.append(
			"Dependent files were not checked (include_dependents=false): the files importing the changed files "
			"are not in this result."
		)
	if config:
		header.append(
			f"Config files changed: {', '.join(sorted(set(config)))} (the type check below uses the new configuration)."
		)
		header.append(
			"Nothing is compared with the old configuration: every current diagnostic in the checked files is listed."
		)
	body = "\n".join(header)
	return f"{body}\n\n{format_delta(delta)}"


def _project_python_files(workspace: Path) -> list[Path]:
	"""Python files git knows about (tracked or not ignored), as they exist now."""
	listed = _git(workspace, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.py", "*.pyi")
	paths = [(workspace / rel).resolve() for rel in listed.split("\0") if rel]
	return [path for path in paths if path.is_file()]


async def apply_edit(id: str, allow_large: bool = False, ctx: Context | None = None) -> str:  # noqa: A002
	"""Write a previewed edit (the id comes from a write tool's preview). The applied edit keeps that id, so
	undo_edit(id) reverts it. Fails if any file changed since the preview."""
	try:
		session = await open_session(ctx)
		plan = session.state.plans.get(id)
		entry = session.state.journal.apply(plan, allow_large=allow_large, entry_id=id)
		session.state.plans.discard(id)
		after = await errors_after_write(session.client, session.workspace, plan)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return (
		f"Applied edit {entry.id}: {plan.description}\n\n{after}\n\nFiles:\n{plan.summary(session.workspace)}\n\n"
		f'Revert with undo_edit(id="{entry.id}").'
	)


async def undo_edit(id: str | None = None, ctx: Context | None = None) -> str:  # noqa: A002
	"""Revert an edit applied by the write tools (the latest one by default). The undo history keeps the last 20
	edits of this server session; older ones, and ids that were already undone, cannot be reverted.

	Refuses when any of its files was changed after the edit, so later work is never overwritten. An edit
	blocked by a newer one names that edit, which must be undone first.
	"""
	try:
		session = await open_session(ctx)
		entry = session.state.journal.undo(id)
	except TOOL_ERRORS as exc:
		return format_tool_error(exc)
	return f"Reverted edit {entry.id}: {entry.plan.description}\n\nFiles restored:\n{entry.plan.summary(session.workspace)}"


TOOLS: list[tuple[Any, Any]] = [
	(rename_symbol, tool_base.WRITES),
	(verify_changes, tool_base.READS),
	(apply_edit, tool_base.WRITES),
	(undo_edit, tool_base.WRITES),
]
