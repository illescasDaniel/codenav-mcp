"""v2 tools - WS2: unused, impact. Registered by server.py via TOOLS; see docs/v2-design.md."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context

from codenav_mcp import tool_base
from codenav_mcp.impact import impact_reply


async def impact(
	name: str,
	file_path: str | None = None,
	change: str = "any",
	depth: int = 1,
	limit: int = 30,
	ctx: Context | None = None,
) -> Any:
	"""What breaks if X changes? Groups calls (how args are passed), imports, overrides, tests, docs; grep can't. Example: `impact(name="send_mail", change="signature")`.

	`change`: any|signature|rename|delete|type. `depth` 2-3 adds callers of callers. `file_path` narrows `name`.
	"""
	return await impact_reply(ctx, name, file_path, change, depth, limit)


TOOLS: list[tuple[Any, Any]] = [
	(impact, tool_base.READS),
]
