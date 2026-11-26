#!/usr/bin/env python
"""Differential check: codenav `lookup(show=["references"])` against jedi `get_references`.

    uv run python hardening/diff_references.py PROJECT_DIR [--symbols N] [--seed S] [--output FILE]

For N seeded-random functions, methods and classes of PROJECT_DIR the reference locations (relative path, line)
from both tools are compared. A symbol codenav refuses (ambiguous, not found, ...) or jedi cannot handle is an
error, never an empty result. The project is only read."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jedi  # noqa: E402

from hardening._common import (  # noqa: E402
	Codenav,
	Location,
	Symbol,
	ToolRefused,
	build_parser,
	codenav_session,
	error_kind,
	parse_references,
	pick_symbols,
	rate,
	sorted_locations,
	symbol_record,
	write_report,
)


DEFAULT_OUTPUT = Path(__file__).resolve().parent / "results" / "diff_references.json"


async def codenav_references(codenav: Codenav, symbol: Symbol) -> tuple[set[Location], bool]:
	reply = await codenav.call("lookup", name=symbol.qualname, file_path=symbol.path, show=["references"], limit=1000)
	ambiguous = " symbols match " in reply.text.partition("\n")[0]  # a read tool answers ambiguity with a list
	if reply.is_error or ambiguous:
		raise ToolRefused(reply)
	parsed = parse_references(reply.text)
	if parsed is None:
		raise RuntimeError(f"codenav reply has no References section: {reply.text[:200]!r}")
	return parsed


def jedi_references(project: Path, jedi_project: jedi.Project, symbol: Symbol) -> set[Location]:
	script = jedi.Script(path=str(project / symbol.path), project=jedi_project)
	found: set[Location] = set()
	for reference in script.get_references(symbol.line, symbol.column):
		module_path = reference.module_path
		if module_path is None or reference.line is None:
			continue
		try:
			relative = Path(module_path).resolve().relative_to(project.resolve())
		except ValueError:
			continue  # outside the project: stdlib, site-packages
		if relative.suffix == ".py":
			found.add((relative.as_posix(), reference.line))
	return found


def jedi_project_for(project: Path) -> jedi.Project:
	src = project / "src"  # the layout codenav auto-detects as the source root
	return jedi.Project(str(project), added_sys_path=[str(src)] if src.is_dir() else [])


async def compare(project: Path, symbols: list[Symbol]) -> list[dict[str, Any]]:
	jedi_project = jedi_project_for(project)
	records = []
	async with codenav_session(project) as codenav:
		for symbol in symbols:
			record: dict[str, Any] = {"symbol": symbol_record(symbol)}
			started = time.monotonic()
			try:
				theirs, cut_short = await codenav_references(codenav, symbol)
				mine = jedi_references(project, jedi_project, symbol)
			except Exception as exc:  # noqa: BLE001 - recorded as an error result, never as "no references"
				record.update(status="error", error_kind=error_kind(exc), error=f"{type(exc).__name__}: {exc}")
			else:
				record.update(
					status="ok",
					codenav_count=len(theirs),
					jedi_count=len(mine),
					codenav_truncated=cut_short,
					shared=sorted_locations(theirs & mine),
					only_in_codenav=sorted_locations(theirs - mine),
					only_in_jedi=sorted_locations(mine - theirs),
				)
			record["seconds"] = round(time.monotonic() - started, 2)
			records.append(record)
			print(describe(record), flush=True)
	return records


def describe(record: dict[str, Any]) -> str:
	name = record["symbol"]["qualname"]
	where = f"{record['symbol']['path']}:{record['symbol']['line']}"
	if record["status"] == "error":
		return f"ERROR {name} ({where}): {record['error']}"
	verdict = "agree" if not record["only_in_codenav"] and not record["only_in_jedi"] else "DIFFER"
	text = f"{verdict} {name} ({where}): codenav {record['codenav_count']}, jedi {record['jedi_count']}"
	for label, key in (("only codenav", "only_in_codenav"), ("only jedi", "only_in_jedi")):
		if record[key]:
			text += f"\n    {label}: " + ", ".join(f"{path}:{line}" for path, line in record[key])
	return text


def summarize(records: list[dict[str, Any]], seconds: float) -> dict[str, Any]:
	compared = [r for r in records if r["status"] == "ok"]
	agreeing = [r for r in compared if not r["only_in_codenav"] and not r["only_in_jedi"]]
	only_codenav = sum(len(r["only_in_codenav"]) for r in compared)
	only_jedi = sum(len(r["only_in_jedi"]) for r in compared)
	shared = sum(r["codenav_count"] for r in compared) - only_codenav
	error_kinds: dict[str, int] = {}
	for record in records:
		if record["status"] == "error":
			error_kinds[record["error_kind"]] = error_kinds.get(record["error_kind"], 0) + 1
	return {
		"symbols": len(records),
		"errors": len(records) - len(compared),
		"error_kinds": error_kinds,
		"compared": len(compared),
		"agreeing_symbols": len(agreeing),
		"agreement_rate": rate(len(agreeing), len(compared)),
		"shared_locations": shared,
		"only_in_codenav_locations": only_codenav,
		"only_in_jedi_locations": only_jedi,
		"location_overlap_rate": rate(shared, shared + only_codenav + only_jedi),
		"seconds": round(seconds, 1),
	}


def main() -> int:
	args = build_parser(__doc__.splitlines()[0]).parse_args()
	project = args.project.resolve()
	started = time.monotonic()
	records = asyncio.run(compare(project, pick_symbols(project, args.symbols, args.seed)))
	summary = summarize(records, time.monotonic() - started)
	print("\nSUMMARY " + " ".join(f"{key}={value}" for key, value in summary.items()))
	report = {"project": str(project), "seed": args.seed, "summary": summary, "symbols": records}
	write_report(report, args.output or DEFAULT_OUTPUT)
	return 0


if __name__ == "__main__":
	sys.exit(main())
