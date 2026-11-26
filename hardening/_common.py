"""Shared helpers for the differential scripts: symbol sampling, a codenav stdio session, reply parsers, reports.

Everything here is read-only with respect to the project under test.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import random
import re
import sys
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters


SKIPPED_DIRS = {".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__", ".tox", ".ropeproject"}
CALL_TIMEOUT = 180.0
Location = tuple[str, int]  # (path relative to the project, 1-based line)


@dataclass(frozen=True)
class Symbol:
	qualname: str  # `Class.method`, `Outer.Inner` or the bare name: what codenav is asked for
	name: str
	kind: str  # function | method | class
	path: str  # relative, posix
	line: int  # 1-based line of the name
	column: int  # 0-based character column of the name


# -- symbol sampling ----------------------------------------------------------------------------------------


def _python_files(project: Path) -> list[Path]:
	found = []
	for path in sorted(project.rglob("*.py")):
		if any(part in SKIPPED_DIRS or part.startswith(".") for part in path.relative_to(project).parts[:-1]):
			continue
		found.append(path)
	return found


def _name_column(line: str, name: str) -> int | None:
	match = re.search(rf"\b(?:def|class)\s+({re.escape(name)})\b", line)
	return match.start(1) if match else None


def _definitions(
	tree: ast.Module, prefix: str = "", *, in_class: bool = False
) -> list[tuple[str, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, str]]:
	"""(qualified name, node, kind) of module-level and class-level definitions; nothing inside functions."""
	found: list[tuple[str, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, str]] = []
	for node in tree.body:
		if isinstance(node, ast.ClassDef):
			found.append((prefix + node.name, node, "class"))
			found += _definitions(ast.Module(body=node.body, type_ignores=[]), f"{prefix}{node.name}.", in_class=True)
		elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
			found.append((prefix + node.name, node, "method" if in_class else "function"))
	return found


def collect_symbols(project: Path) -> list[Symbol]:
	"""Every function, method and class of the project that has a unique qualified name in its file.

	Dunder names are skipped (implicit uses, no name-based references) as are redefinitions of the same
	qualified name in one file (property setters, overloads): neither has a single definition to compare."""
	symbols: list[Symbol] = []
	for path in _python_files(project):
		try:
			source = path.read_text(encoding="utf-8")
			tree = ast.parse(source)
		except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
			continue
		lines = source.splitlines()
		definitions = _definitions(tree)
		counts: dict[str, int] = {}
		for qualname, _node, _kind in definitions:
			counts[qualname] = counts.get(qualname, 0) + 1
		for qualname, node, kind in definitions:
			name = qualname.rsplit(".", 1)[-1]
			line = node.lineno
			column = _name_column(lines[line - 1], name) if line <= len(lines) else None
			if counts[qualname] > 1 or (name.startswith("__") and name.endswith("__")) or column is None:
				continue
			symbols.append(Symbol(qualname, name, kind, path.relative_to(project).as_posix(), line, column))
	return symbols


def pick_symbols(project: Path, count: int, seed: int) -> list[Symbol]:
	symbols = collect_symbols(project)
	return random.Random(seed).sample(symbols, min(count, len(symbols)))  # noqa: S311 - seeded sampling, not crypto


# -- codenav over MCP stdio -------------------------------------------------------------------------------


@dataclass
class Reply:
	text: str
	is_error: bool


class Codenav:
	"""One codenav server (and so one ty session) for a project. Calls are sequential: ty rejects concurrent ones."""

	def __init__(self, client: Client) -> None:
		self._client = client
		self.seconds = 0.0
		self.calls = 0

	async def call(self, tool: str, **arguments: Any) -> Reply:
		started = time.monotonic()
		try:
			result = await asyncio.wait_for(self._client.call_tool(tool, arguments), CALL_TIMEOUT)
		finally:
			self.seconds += time.monotonic() - started
			self.calls += 1
		text = "\n".join(getattr(block, "text", "") for block in result.content)
		return Reply(text, bool(getattr(result, "is_error", False)))


@asynccontextmanager
async def codenav_session(project: Path) -> AsyncIterator[Codenav]:
	"""Start `python -m codenav_mcp.server` (this checkout's) on `project`, with its edit state in a temp dir."""
	with tempfile.TemporaryDirectory(prefix="cnav-diff-state-") as state_dir:
		params = StdioServerParameters(
			command=sys.executable,
			args=["-m", "codenav_mcp.server"],
			env={"CODENAV_MCP_WORKSPACE": str(project), "CODENAV_MCP_STATE_DIR": state_dir},
			cwd=str(project),
		)
		async with Client(params, mode="legacy") as client:
			yield Codenav(client)


class ToolRefused(RuntimeError):
	"""codenav answered with an error instead of a result. `kind`: ambiguous | not-found | refused."""

	def __init__(self, reply: Reply) -> None:
		first = next((line for line in reply.text.splitlines() if line.strip()), "(empty reply)")
		lowered = first.lower()
		self.kind = (
			"ambiguous" if " symbols match " in lowered else "not-found" if "no symbol" in lowered else "refused"
		)
		super().__init__(f"codenav {self.kind}: {first}")


def error_kind(exc: Exception) -> str:
	return exc.kind if isinstance(exc, ToolRefused) else "failed"


# -- reply parsers (pure; unit tested) --------------------------------------------------------------------

_REFERENCE_LINE = re.compile(r"^(?P<path>\S.*?):(?P<line>\d+)  column \d+")


def parse_references(text: str) -> tuple[set[Location], bool] | None:
	"""(reference locations, list was cut short) of a `lookup(show=["references"])` reply.

	None when the reply has no `References` section at all (so it is not a successful references answer)."""
	lines = text.splitlines()
	start = next((i for i, line in enumerate(lines) if line.startswith("References")), None)
	if start is None:
		return None
	found: set[Location] = set()
	cut_short = False
	for line in lines[start + 1 :]:
		match = _REFERENCE_LINE.match(line)
		if match:
			found.add((match["path"], int(match["line"])))
		elif line.startswith("…") and "omitted" in line:
			cut_short = True
		elif not line.strip():
			break
	return found, cut_short


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_FILE_COUNTS = re.compile(r"^  (?P<path>\S.*?): \+(?P<added>\d+) -(?P<removed>\d+)\s*$")


@dataclass
class RenamePreview:
	edited: set[Location]  # old-side lines the diff shows as removed
	file_counts: dict[str, int]  # per file: changed lines according to the `Files:` summary
	truncated: bool  # the diff in the reply is cut off, so `edited` is incomplete


def _really_changed(path: str, removed: list[tuple[int, str]], added: list[str]) -> set[Location]:
	"""Old lines of one `-`/`+` run that differ from their counterpart.

	codenav's diff shows the unchanged lines between two nearby edits as removed and added again (its `Files:`
	summary counts only the real edits), so a removed line equal to the added line at the same position is not an
	edit."""
	return {
		(path, number) for index, (number, text) in enumerate(removed) if index >= len(added) or added[index] != text
	}


def parse_rename_preview(text: str) -> RenamePreview | None:
	"""Edited lines of a rename preview's unified diff; None when the reply is not a rename preview."""
	if not text.startswith("Rename ") or "edit(s)" not in text.splitlines()[0]:
		return None
	lines = text.splitlines()
	counts = {m["path"]: int(m["removed"]) for line in lines if (m := _FILE_COUNTS.match(line))}
	edited: set[Location] = set()
	path: str | None = None
	i = 0
	while i < len(lines):
		line = lines[i]
		hunk = _HUNK.match(line)
		if line.startswith("--- a/"):
			path = line[len("--- a/") :]
		elif hunk and path is not None:
			old, remaining_old, remaining_new = int(hunk[1]), int(hunk[2] or 1), int(hunk[4] or 1)
			removed: list[tuple[int, str]] = []  # the current run of `-` lines (old number, text)
			added: list[str] = []  # the `+` lines that follow it
			while (remaining_old > 0 or remaining_new > 0) and i + 1 < len(lines):
				i += 1
				body = lines[i]
				if body.startswith(("-", "+")):
					if body.startswith("-"):
						if added:  # a new run starts: close the previous one
							edited.update(_really_changed(path, removed, added))
							removed, added = [], []
						removed.append((old, body[1:]))
						old += 1
						remaining_old -= 1
					else:
						added.append(body[1:])
						remaining_new -= 1
				elif body.startswith("\\"):
					continue  # "\ No newline at end of file"
				else:
					edited.update(_really_changed(path, removed, added))
					removed, added = [], []
					old += 1
					remaining_old -= 1
					remaining_new -= 1
			edited.update(_really_changed(path, removed, added))
		i += 1
	return RenamePreview(edited, counts, any(line.startswith("... diff truncated") for line in lines))


# -- reports ------------------------------------------------------------------------------------------------


def sorted_locations(locations: set[Location]) -> list[list[Any]]:
	return [[path, line] for path, line in sorted(locations)]


def build_parser(description: str) -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=description)
	parser.add_argument("project", type=Path, help="project directory (only read)")
	parser.add_argument("--symbols", type=int, default=40, help="how many symbols to sample (default 40)")
	parser.add_argument("--seed", type=int, default=0, help="sampling seed (default 0)")
	parser.add_argument("--output", type=Path, default=None, help="where to write the JSON report")
	return parser


def write_report(report: dict[str, Any], output: Path) -> None:
	output.parent.mkdir(parents=True, exist_ok=True)
	output.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def symbol_record(symbol: Symbol) -> dict[str, Any]:
	return asdict(symbol)


def rate(part: int, whole: int) -> float | None:
	return round(part / whole, 4) if whole else None
