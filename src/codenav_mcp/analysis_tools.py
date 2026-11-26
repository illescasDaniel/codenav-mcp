"""v2 tools - WS2: unused, impact. Registered by server.py via TOOLS; see docs/v2-design.md."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Context
from mcp_nav_shared.errors import TOOL_ERRORS, FailedText, ToolInputError, format_tool_error
from mcp_nav_shared.output import max_output_chars

from codenav_mcp import tool_base
from codenav_mcp.impact import impact_reply
from codenav_mcp.reply import Reply, call, paginate
from codenav_mcp.unused import KINDS, find_unused


async def unused(
	path: str | None = None,
	kind: str | None = None,
	include_private: bool = True,
	limit: int = 30,
	offset: int = 0,
	ctx: Context | None = None,
) -> str:
	"""Find Python definitions (kind=class|constant|function|method) without linked production uses, even when grep finds same-named decoys. Example: `unused(path="src/", kind="function")`."""
	try:
		paginate([], limit, offset)
		if kind is not None and kind not in KINDS:
			raise ToolInputError(f"kind must be one of {', '.join(sorted(KINDS))}")
		session = await tool_base.open_session(ctx)
		scope = (session.workspace / Path(path or ".")).resolve()
		if not scope.is_relative_to(session.workspace):
			raise ToolInputError("path must be inside the workspace")
		if not scope.exists():
			raise FileNotFoundError(2, "No such file or directory", str(scope))
		if scope.is_file() and scope.suffix not in {".py", ".pyi"}:
			raise ToolInputError("path must be a Python file or directory")
		findings, notes = await find_unused(session, scope=scope, kind=kind, include_private=include_private)
	except (ValueError, *TOOL_ERRORS) as error:
		reply = Reply(str(format_tool_error(error)), items=[f"Example: {call('unused', kind='function')}"])
		return FailedText(reply.render())

	rows = [finding.render() for finding in findings]
	args = dict(path=path, kind=kind, include_private=include_private, limit=limit)
	reply = Reply(f"{len(rows)} candidate(s) without linked production uses.", notes=notes)
	reply.notes.append("Static analysis of workspace Python only. Dynamic uses and external consumers may be missed.")

	# Leave room for the footer so workspace-wide results can always be continued.
	budget = max_output_chars() - len(reply.render()) - len(call("unused", **args, offset=len(rows))) - 64
	count = 0
	for row in rows[offset : offset + limit]:
		budget -= len(row) + 1
		if budget < 0:
			break
		count += 1
	page = paginate(rows, max(1, count), offset)
	reply.add_page(page, str, "unused", **args)
	return reply.render()


async def impact(
	name: str,
	file_path: str | None = None,
	change: str = "any",
	depth: int = 1,
	limit: int = 30,
	ctx: Context | None = None,
) -> Any:
	"""What breaks if X changes? Groups calls (how args are passed), imports, overrides, tests, docs; grep can't. change=any|signature|rename|delete|type; depth 2-3 adds callers of callers. Example: `impact(name="send_mail", change="signature")`."""
	return await impact_reply(ctx, name, file_path, change, depth, limit)


TOOLS: list[tuple[Any, Any]] = [(unused, tool_base.READS), (impact, tool_base.READS)]
