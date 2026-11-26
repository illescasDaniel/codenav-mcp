"""What `edit_symbol(action="delete")` and `unused` both ask: is the name still used somewhere the type checker did not link?

Both tools must agree, so the question lives here: a same-name bare/attribute use or a string mention outside
the symbol's own definition (and not an import about to be pruned) that the type checker does not resolve to
another symbol. `delete` holds the plan back on any such use; `unused` must not call the symbol plain "unused".
"""

from __future__ import annotations

import re
from collections.abc import Collection, Container
from pathlib import Path
from typing import Any

from mcp_nav_shared.errors import TOOL_ERRORS
from mcp_nav_shared.format import uri_to_path

from codenav_mcp.mentions import Mention, find_mentions
from codenav_mcp.tool_base import Session


# Most same-named usages one check asks the type checker about (sequential requests; the rest stay as uses).
MAX_NAME_LOOKUPS = 200


async def unlinked_uses(
	session: Session,
	name: str,
	*,
	rel: str,
	own_lines: Container[int],
	declared: tuple[Path, int],
	pruned: Container[tuple[str, int]] = (),
	files: Collection[str] | None = None,
) -> tuple[list[Mention], int]:
	"""(uses of `name` still to review, same-name uses that resolve to another symbol and were dropped).

	`rel`/`own_lines` is the symbol's own definition (its lines are not uses), `declared` its (path, name line),
	`pruned` the (file, line) import statements that go with it, `files` an optional prefilter of the workspace
	files that can mention the name at all."""
	mentions = find_mentions(
		session.workspace, name, {}, include_text=False, path_matches=None if files is None else files.__contains__
	)
	unseen = [
		m
		for m in mentions
		if not (m.path == rel and m.line in own_lines)
		and (m.path, m.line) not in pruned
		and m.kind in ("attr", "code", "string")
		and not (
			m.kind == "code"
			and m.path != rel
			and re.match(rf"(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b", m.snippet)
			and len(re.findall(rf"(?<!\w){re.escape(name)}(?!\w)", m.snippet)) == 1
		)
	]
	return await drop_other_symbols(session, unseen, declared)


async def drop_other_symbols(
	session: Session, mentions: list[Mention], declared: tuple[Path, int]
) -> tuple[list[Mention], int]:
	"""(mentions still to review, how many were dropped): a bare-name or attribute use whose definition the type
	checker finds somewhere other than `declared` (path, name line) is another symbol with the same name, not a use
	of the deleted one. A use with no definition, a failed lookup, a string, or one past the cap stays."""
	keep: list[Mention] = []
	dropped = lookups = 0
	for mention in mentions:
		if mention.kind not in ("code", "attr") or mention.column is None or lookups >= MAX_NAME_LOOKUPS:
			keep.append(mention)
			continue
		lookups += 1
		try:
			locations = await session.client.definition(
				str(session.workspace / mention.path), mention.line, mention.column + 1
			)
		except TOOL_ERRORS:
			keep.append(mention)
			continue
		found = [
			(uri_to_path(str(item.get("uri") or item.get("targetUri"))).resolve(), _start_line(item))
			for item in locations
			if str(item.get("uri") or item.get("targetUri") or "").startswith("file:")
		]
		if found and len(found) == len(locations) and declared not in found:
			dropped += 1
		else:
			keep.append(mention)
	return keep, dropped


def _start_line(item: dict[str, Any]) -> int:
	rng = item.get("range") or item.get("targetSelectionRange") or {}
	return int(rng.get("start", {}).get("line", -1)) + 1
