"""Read tools answer an ambiguous name with a list of exact retries, not with an error.

`resolve_symbol` (mcp-nav-shared) raises a plain `SymbolResolutionError` whose text is
"N symbols match 'x'; <hint>:" followed by one `Qualified.name  [kind]  (path:line:col)` line per
candidate. A read tool has nothing to undo, so the useful answer is the list of calls that
would work; a write tool keeps the refusal (it must never guess). The text is parsed here;
if the shared wording ever changes, nothing parses and the original refusal is returned
unchanged (`tests/test_ambiguity.py` pins the wording).
"""

from __future__ import annotations

import ast
import re
import unicodedata
from dataclasses import dataclass

from mcp_nav_shared.errors import FailedText, format_tool_error
from mcp_nav_shared.resolve import SymbolNotFoundError, SymbolResolutionError


_HEADER = re.compile(r"(\d+) symbols match ('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\");")
_CANDIDATE = re.compile(r"(?P<display>\S+)  \[(?P<kind>[^\]]+)\]  \((?P<path>.+):(?P<line>\d+):(?P<column>\d+)\)")


@dataclass(frozen=True)
class Candidate:
	display: str  # `Class.member` for a member, else the bare name
	kind: str
	path: str
	line: int
	column: int


def parse_candidates(exc: Exception, query: str | None) -> tuple[int, str, list[Candidate]] | None:
	"""(total count, the name the message is about, listed candidates) for an ambiguity refusal, else None.

	The message names `query` itself, or just its first part when a dotted `Class.member` hit several `Class`es.
	"""
	if not isinstance(exc, SymbolResolutionError) or isinstance(exc, SymbolNotFoundError) or not query:
		return None
	first, _, rest = str(exc).partition("\n")
	header = _HEADER.match(first)
	if header is None:
		return None
	try:
		matched = ast.literal_eval(header.group(2))
	except (ValueError, SyntaxError):
		return None
	asked = unicodedata.normalize("NFKC", query)
	if not isinstance(matched, str) or not (asked == matched or asked.startswith(f"{matched}.")):
		return None
	found = [_CANDIDATE.fullmatch(line) for line in rest.splitlines()]
	candidates = [
		Candidate(m["display"], m["kind"], m["path"], int(m["line"]), int(m["column"])) for m in found if m is not None
	]
	return (int(header.group(1)), matched, candidates) if candidates else None


def _quote(text: str) -> str:
	return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ambiguity_listing(exc: Exception, tool: str, query: str | None, *, fixed: str = "") -> str | None:
	"""The reply for `tool` when `exc` says `query` matches several symbols; None for any other error.

	One line per candidate with its kind, `file:line` and the exact call that selects it: the qualified name,
	plus `file_path` only when the qualified name alone is shared by several candidates (or when it
	is the class part of a dotted query that is ambiguous). `fixed` is argument text that goes first in every call
	(`action="delete", `), for a tool that needs more than the name.
	"""
	parsed = parse_candidates(exc, query)
	if parsed is None or query is None:
		return None
	total, matched, candidates = parsed
	# `A.run` asked for, candidates are the classes called `A`: the member name does not select, the file does.
	container = matched != unicodedata.normalize("NFKC", query)
	names = [query if container else candidate.display for candidate in candidates]
	lines: list[str] = []
	unselectable = False
	for candidate, name in zip(candidates, names, strict=True):
		same = [c for c, n in zip(candidates, names, strict=True) if n == name]
		args = f"{fixed}name={_quote(name)}"
		if len(same) > 1:
			if len({c.path for c in same}) < len(same):
				unselectable = True  # the same name twice in one file: no name or file_path tells them apart
				lines.append(f"  [{candidate.kind}] {candidate.path}:{candidate.line}:{candidate.column}")
				continue
			args += f", file_path={_quote(candidate.path)}"
		lines.append(f"  {tool}({args})  [{candidate.kind}, {candidate.path}:{candidate.line}]")
	more = f" (first {len(candidates)} shown)" if total > len(candidates) else ""
	head = f"{total} symbols are named {query!r}{more}. Retry with one of:"
	if unselectable:
		lines.append(
			"  (a name used twice in one file cannot be chosen by name: use lookup with "
			"file_path, line and column from the listing.)"
		)
	return "\n".join([head, *lines])


def write_refusal(exc: Exception, tool: str, query: str | None, *, fixed: str = "") -> FailedText:
	"""A write tool's refusal of an ambiguous name (nothing is written, it must never guess), plus the exact retry calls.

	Any other error is formatted as usual. `fixed` is argument text that goes first in each retry call."""
	listing = ambiguity_listing(exc, tool, query, fixed=fixed)
	if listing is None:
		return format_tool_error(exc)
	first = str(format_tool_error(exc)).partition("\n")[0]
	retries = listing.partition("\n")[2]
	return FailedText(f"{first}\nNothing was changed. Retry with one of (other arguments unchanged):\n{retries}")
