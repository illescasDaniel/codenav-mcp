#!/usr/bin/env python3
"""Table of a small-model campaign: python3 -I summarize.py CAMPAIGN_DIR > CAMPAIGN_DIR/summary.md

Reads each run's stream.jsonl (claude -p stream-json or codex exec --json), ty.txt, pytest.txt and diffstat.txt.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path


# calls by tool, failed calls, token usage, turns, seconds (None for codex), codenav calls
RunStats = tuple[Counter, int, dict, int, float | None, list]


SEARCH_EDIT = re.compile(r"\b(rg|grep|sed|awk|perl|find|nl|cat|head|tail)\b|python3? -c|apply_patch")


def claude_run(stream: Path) -> RunStats:
	calls, failed, usage, turns, ms, codenav = Counter(), 0, {}, 0, 0, []
	ids = {}
	for line in stream.open():
		e = json.loads(line)
		if e.get("type") == "assistant":
			for c in e["message"]["content"]:
				if c.get("type") == "tool_use":
					ids[c["id"]] = c["name"]
					name = c["name"]
					if name.startswith("mcp__codenav__"):
						codenav.append(name.removeprefix("mcp__codenav__"))
						calls["codenav"] += 1
					elif name == "Bash":
						calls[
							"shell-search/edit" if SEARCH_EDIT.search(c["input"].get("command", "")) else "shell-other"
						] += 1
					else:
						calls[name] += 1
		elif e.get("type") == "user":
			for c in e["message"].get("content", []):
				if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
					failed += 1
		elif e.get("type") == "result":
			u = e.get("usage", {})
			usage = {
				"in": u.get("input_tokens", 0)
				+ u.get("cache_creation_input_tokens", 0)
				+ u.get("cache_read_input_tokens", 0),
				"out": u.get("output_tokens", 0),
			}
			turns, ms = e.get("num_turns", 0), e.get("duration_ms", 0)
	return calls, failed, usage, turns, ms / 1000, codenav


def codex_run(stream: Path) -> RunStats:
	calls, failed, usage, turns, codenav = Counter(), 0, {"in": 0, "out": 0}, 0, []
	for line in stream.open():
		e = json.loads(line)
		it = e.get("item", {})
		if e.get("type") == "item.completed":
			kind = it.get("type")
			if kind == "mcp_tool_call":
				codenav.append(it.get("tool"))
				calls["codenav" if it.get("server") == "codenav" else "other-mcp"] += 1
				failed += it.get("status") != "completed" or bool(it.get("error"))
			elif kind == "command_execution":
				calls["shell-search/edit" if SEARCH_EDIT.search(it.get("command", "")) else "shell-other"] += 1
				failed += (it.get("exit_code") or 0) != 0
			elif kind == "file_change":
				calls["apply_patch"] += 1
			elif kind not in ("agent_message", "reasoning", "todo_list"):
				calls[kind] += 1
			turns += kind != "reasoning"
		elif e.get("type") == "turn.completed":
			u = e["usage"]
			usage["in"] += u.get("input_tokens", 0)
			usage["out"] += u.get("output_tokens", 0) + u.get("reasoning_output_tokens", 0)
	return calls, failed, usage, turns, None, codenav


def exit_of(path: Path) -> str:
	m = re.search(r"exit (\d+)\s*$", path.read_text()) if path.exists() else None
	return m.group(1) if m else "?"


def tests_of(path: Path) -> str:
	m = re.findall(r"(\d+ (?:passed|failed)[^\n]*) in ", path.read_text()) if path.exists() else []
	return m[-1] if m else "?"


def main(campaign: str) -> None:
	rows = []
	for run in sorted(Path(campaign).glob("*/task-*")):
		stream = run / "stream.jsonl"
		if not stream.exists() or not stream.stat().st_size:
			continue
		runner = run.parent.name
		calls, failed, usage, turns, secs, codenav = (claude_run if runner == "haiku" else codex_run)(stream)
		meta = (run / "metadata.txt").read_text()
		if secs is None:
			from datetime import datetime

			t = re.findall(r"(?:started|finished)=(\S+)", meta)
			secs = (datetime.fromisoformat(t[1]) - datetime.fromisoformat(t[0])).total_seconds() if len(t) == 2 else 0
		diff = (run / "diffstat.txt").read_text().strip().splitlines()
		build = re.search(r"codenav=(\S+).*?dirty=(\d+)", meta)
		build = f"{build[1]} dirty={build[2]}" if build else "?"
		rows.append(
			(
				runner,
				run.name,
				build,
				int(secs),
				usage.get("in", 0),
				usage.get("out", 0),
				sum(calls.values()),
				", ".join(f"{k} {v}" for k, v in sorted(calls.items())),
				failed,
				" ".join(codenav) or "-",
				exit_of(run / "ty.txt"),
				tests_of(run / "pytest.txt"),
				diff[-1].strip() if diff else "no changes",
			)
		)
	print(
		"| runner | run | codenav build | s | tokens in | out | calls | by kind | failed | codenav calls | ty | tests | diff |"
	)
	print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
	for r in rows:
		print("| " + " | ".join(str(x) for x in r) + " |")


if __name__ == "__main__":
	main(sys.argv[1])
