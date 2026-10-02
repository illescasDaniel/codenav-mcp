"""Optional import sorting through `ruff`, applied to text and never to the disk."""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404
import sys
from pathlib import Path


def find_ruff(workspace: Path) -> list[str] | None:
	"""The project's own ruff, else the one installed beside this package, else `PATH`."""
	for candidate in (
		workspace / ".venv" / "bin" / "ruff",
		workspace / ".venv" / "Scripts" / "ruff.exe",
		Path(sys.executable).parent / "ruff",
		Path(sys.executable).parent / "ruff.exe",
	):
		if candidate.is_file():
			return [str(candidate)]
	on_path = shutil.which("ruff")
	return [on_path] if on_path else None


def _run_fix(ruff: list[str], text: str, filename: str, cwd: Path, rules: str) -> str | None:
	try:
		result = subprocess.run(  # noqa: S603
			[*ruff, "check", "--fix", "--select", rules, "--stdin-filename", filename, "--quiet", "-"],
			input=text,
			capture_output=True,
			text=True,
			cwd=cwd,
			timeout=30,
			check=False,
		)
	except (OSError, subprocess.TimeoutExpired):
		return None
	if result.returncode not in (0, 1) or not result.stdout:
		return None
	return result.stdout


def sort_imports(workspace: Path, original: str, edited: str, filename: str) -> str:
	"""`edited` with its imports sorted by ruff's isort rule, but only when `original` was already
	sorted that way: a file whose imports ruff would reorder anyway is left as the edit made it,
	so a refactoring never reshuffles imports it didn't touch."""
	ruff = find_ruff(workspace)
	if ruff is None or original == edited:
		return edited
	if _run_fix(ruff, original, filename, workspace, "I001") not in (None, original):
		return edited
	fixed = _run_fix(ruff, edited, filename, workspace, "I001")
	return fixed if fixed is not None else edited
