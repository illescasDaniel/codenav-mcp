#!/usr/bin/env python
"""Differential check: codenav `rename(apply=false)` against rope's `Rename(...).get_changes`.

    uv run python hardening/diff_rename.py PROJECT_DIR [--symbols N] [--seed S] [--output FILE]

For N seeded-random functions, methods and classes of PROJECT_DIR the set of edited (relative path, line) is
compared in two modes: codenav's certain sites against rope's default rename, and codenav with sites=["all"]
against rope with docs=True and every unsure occurrence accepted. When codenav's diff is cut off (150 lines) only
the per-file edited-line counts from its `Files:` summary are compared. A refusal by either tool is an error,
never an empty result. Nothing is written to the project (rope runs without a .ropeproject folder)."""

from __future__ import annotations

import asyncio
import difflib
import sys
import time
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rope.base.project import Project  # noqa: E402
from rope.refactor.rename import Rename  # noqa: E402

from hardening._common import (  # noqa: E402
	SKIPPED_DIRS,
	Codenav,
	Location,
	RenamePreview,
	Symbol,
	ToolRefused,
	build_parser,
	codenav_session,
	error_kind,
	parse_rename_preview,
	pick_symbols,
	rate,
	sorted_locations,
	symbol_record,
	write_report,
)


DEFAULT_OUTPUT = Path(__file__).resolve().parent / "results" / "diff_rename.json"
NEW_NAME = "zz_cnav_renamed"
MODES = {"certain": None, "all": ["all"]}  # mode -> codenav `sites`


async def codenav_preview(codenav: Codenav, symbol: Symbol, sites: list[str] | None) -> RenamePreview:
	arguments: dict[str, Any] = {
		"name": symbol.qualname,
		"new_name": NEW_NAME,
		"file_path": symbol.path,
		"apply": False,
	}
	if sites is not None:
		arguments["sites"] = sites
	reply = await codenav.call("rename", **arguments)
	if reply.is_error:
		raise ToolRefused(reply)
	preview = parse_rename_preview(reply.text)
	if preview is None:
		raise RuntimeError(f"codenav reply is not a rename preview: {reply.text[:200]!r}")
	return preview


def rope_changes(project: Project, symbol: Symbol, *, everything: bool) -> tuple[set[Location], dict[str, int]]:
	"""Edited lines (old numbering) and per-file edited-line counts of rope's rename of `symbol`."""
	resource = project.get_resource(symbol.path)
	offset = sum(len(line) + 1 for line in resource.read().split("\n")[: symbol.line - 1]) + symbol.column
	rename = Rename(project, resource, offset)
	if everything:
		changes = rename.get_changes(NEW_NAME, in_hierarchy=True, docs=True, unsure=lambda _occurrence: True)
	else:
		changes = rename.get_changes(NEW_NAME, in_hierarchy=True)
	edited: set[Location] = set()
	counts: dict[str, int] = {}
	for change in changes.changes:
		path = getattr(getattr(change, "resource", None), "path", None)
		if path is None or not hasattr(change, "new_contents"):
			raise RuntimeError(f"rope change is not a content edit: {change!r}")
		old_lines = change.resource.read().split("\n")  # not `do()`: nothing is ever written
		new_lines = change.new_contents.split("\n")
		for tag, start, end, _a, _b in difflib.SequenceMatcher(
			None, old_lines, new_lines, autojunk=False
		).get_opcodes():
			if tag != "equal":
				edited.update((path, number) for number in range(start + 1, end + 1))
		counts[path] = sum(1 for p, _n in edited if p == path)
	return edited, counts


def compare_mode(preview: RenamePreview, edited: set[Location], counts: dict[str, int]) -> dict[str, Any]:
	codenav_count = sum(preview.file_counts.values())
	if preview.truncated:
		files = sorted(set(preview.file_counts) | set(counts))
		return {
			"status": "ok",
			"basis": "file counts (codenav diff truncated)",
			"codenav_count": codenav_count,
			"rope_count": len(edited),
			"count_differences": {
				f: [preview.file_counts.get(f, 0), counts.get(f, 0)]
				for f in files
				if preview.file_counts.get(f, 0) != counts.get(f, 0)
			},
			"shared": [],
			"only_in_codenav": [],
			"only_in_rope": [],
		}
	return {
		"status": "ok",
		"basis": "lines",
		"codenav_count": len(preview.edited),
		"rope_count": len(edited),
		"count_differences": {},
		"shared": sorted_locations(preview.edited & edited),
		"only_in_codenav": sorted_locations(preview.edited - edited),
		"only_in_rope": sorted_locations(edited - preview.edited),
	}


async def compare(project: Path, symbols: list[Symbol]) -> list[dict[str, Any]]:
	rope_project = Project(
		str(project),
		ropefolder=None,  # no .ropeproject folder: nothing is written to the project
		source_folders=["src"] if (project / "src").is_dir() else [],
		ignored_resources=[".*", "*.pyc", "*~", *sorted(SKIPPED_DIRS)],  # the folders sampling skips, hidden ones too
	)
	records = []
	try:
		async with codenav_session(project) as codenav:
			for symbol in symbols:
				record: dict[str, Any] = {"symbol": symbol_record(symbol), "modes": {}}
				started = time.monotonic()
				for mode, sites in MODES.items():
					try:
						preview = await codenav_preview(codenav, symbol, sites)
						edited, counts = rope_changes(rope_project, symbol, everything=mode == "all")
						record["modes"][mode] = compare_mode(preview, edited, counts)
					except Exception as exc:  # noqa: BLE001 - an error result, never "no edits"
						record["modes"][mode] = {
							"status": "error",
							"error_kind": error_kind(exc),
							"error": f"{type(exc).__name__}: {exc}",
						}
				record["seconds"] = round(time.monotonic() - started, 2)
				records.append(record)
				print(describe(record), flush=True)
	finally:
		rope_project.close()
	return records


def mode_agrees(result: dict[str, Any]) -> bool:
	return (
		result["status"] == "ok"
		and not result["only_in_codenav"]
		and not result["only_in_rope"]
		and not result["count_differences"]
	)


def describe(record: dict[str, Any]) -> str:
	symbol = record["symbol"]
	lines = [f"{symbol['qualname']} ({symbol['path']}:{symbol['line']})"]
	for mode, result in record["modes"].items():
		if result["status"] == "error":
			lines.append(f"  [{mode}] ERROR {result['error']}")
			continue
		verdict = "agree" if mode_agrees(result) else "DIFFER"
		lines.append(
			f"  [{mode}] {verdict}: codenav {result['codenav_count']}, rope {result['rope_count']} ({result['basis']})"
		)
		for label, key in (("only codenav", "only_in_codenav"), ("only rope", "only_in_rope")):
			if result[key]:
				lines.append(f"      {label}: " + ", ".join(f"{path}:{line}" for path, line in result[key]))
		for path, (theirs, mine) in result["count_differences"].items():
			lines.append(f"      count differs in {path}: codenav {theirs}, rope {mine}")
	return "\n".join(lines)


def summarize(records: list[dict[str, Any]], seconds: float) -> dict[str, Any]:
	summary: dict[str, Any] = {"symbols": len(records), "seconds": round(seconds, 1)}
	for mode in MODES:
		results = [r["modes"][mode] for r in records]
		compared = [r for r in results if r["status"] == "ok"]
		by_lines = [r for r in compared if r["basis"] == "lines"]
		agreeing = [r for r in compared if mode_agrees(r)]
		only_codenav = sum(len(r["only_in_codenav"]) for r in by_lines)
		only_rope = sum(len(r["only_in_rope"]) for r in by_lines)
		shared = sum(r["codenav_count"] for r in by_lines) - only_codenav
		error_kinds: dict[str, int] = {}
		for result in results:
			if result["status"] == "error":
				error_kinds[result["error_kind"]] = error_kinds.get(result["error_kind"], 0) + 1
		summary[mode] = {
			"errors": len(results) - len(compared),
			"error_kinds": error_kinds,
			"compared": len(compared),
			"compared_by_lines": len(by_lines),
			"compared_by_file_counts": len(compared) - len(by_lines),
			"agreeing_symbols": len(agreeing),
			"agreement_rate": rate(len(agreeing), len(compared)),
			"shared_lines": shared,
			"only_in_codenav_lines": only_codenav,
			"only_in_rope_lines": only_rope,
			"codenav_lines_also_in_rope_rate": rate(shared, shared + only_codenav),
			"line_overlap_rate": rate(shared, shared + only_codenav + only_rope),
		}
	return summary


def main() -> int:
	args = build_parser(__doc__.splitlines()[0]).parse_args()
	project = args.project.resolve()
	started = time.monotonic()
	records = asyncio.run(compare(project, pick_symbols(project, args.symbols, args.seed)))
	summary = summarize(records, time.monotonic() - started)
	print("\nSUMMARY " + " ".join(f"{key}={value}" for key, value in summary.items()))
	write_report(
		{"project": str(project), "seed": args.seed, "summary": summary, "symbols": records},
		args.output or DEFAULT_OUTPUT,
	)
	return 0


if __name__ == "__main__":
	sys.exit(main())
