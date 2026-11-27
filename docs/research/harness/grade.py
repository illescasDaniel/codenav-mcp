#!/usr/bin/env python3
"""Compare each run's edited lines with the ground truth: python3 -I grade.py CAMPAIGN_DIR TASK

TASK 10 or 12 (renames): the old-side line numbers a run's changes.patch edits, against ground-truth/task-TASK.txt.
Prints per run: expected lines hit, missed, and extra edited lines (outside the truth set), with the misses/extras.
"""

import re
import sys
from pathlib import Path


def edited_lines(patch: str) -> set[str]:
	out, path, old = set(), None, 0
	for line in patch.splitlines():
		if line.startswith("--- "):
			path = line[6:] if line.startswith("--- a/") else None
		elif hunk := re.match(r"@@ -(\d+)", line):
			old = int(hunk.group(1))
		elif path and line.startswith("-") and not line.startswith("---"):
			out.add(f"{path}:{old}")
			old += 1
		elif path and line.startswith(" "):
			old += 1
	return out


def main(campaign: str, task: str) -> None:
	truth_file = Path(__file__).parent.parent / "data" / "agent-ab" / "ground-truth" / f"task-{task}.txt"
	truth = {line.split()[0] for line in truth_file.read_text().splitlines() if line and not line.startswith("#")}
	print("| run | hit | missed | extra | details |\n|---|---|---|---|---|")
	for run in sorted(Path(campaign).glob(f"*/task-{task}-rep-*")):
		patch = run / "changes.patch"
		if not patch.exists():
			continue
		got = edited_lines(patch.read_text())
		missed, extra = sorted(truth - got), sorted(got - truth)
		detail = "; ".join([*(f"missed {m}" for m in missed), *(f"extra {e}" for e in extra[:8])])
		print(
			f"| {run.parent.name}/{run.name} | {len(truth & got)}/{len(truth)} | {len(missed)} | {len(extra)} | {detail} |"
		)


if __name__ == "__main__":
	main(sys.argv[1], sys.argv[2])
