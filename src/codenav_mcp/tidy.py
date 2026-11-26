"""Optional import sorting through `ruff`, applied to text and never to the disk."""

from __future__ import annotations

import ast
import shutil
import subprocess  # noqa: S404
import sys
import tomllib
from pathlib import Path


last_sort_failed: str | None = None


def sort_failure() -> str | None:
	"""Return the reason the most recent sort failed, for caller notes."""
	return last_sort_failed


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


def _imported_modules(text: str) -> set[str]:
	try:
		tree = ast.parse(text)
	except (SyntaxError, ValueError):
		return set()
	found: set[str] = set()
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			found.update(alias.name for alias in node.names)
		elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
			found.add(node.module)
	return found


def _configured_first_party(workspace: Path) -> list[str]:
	"""`known-first-party` from the project's ruff settings (they stay in force next to the names added below)."""
	names: list[str] = []
	for file, path in (("pyproject.toml", ("tool", "ruff")), ("ruff.toml", ()), (".ruff.toml", ())):
		try:
			data = tomllib.loads((workspace / file).read_text(encoding="utf-8"))
		except (OSError, ValueError):
			continue
		for key in path:
			data = data.get(key, {})
		for table in (data.get("lint", {}).get("isort", {}), data.get("isort", {})):
			names += [name for name in table.get("known-first-party", []) if isinstance(name, str)]
	return names


def _missing_first_party(workspace: Path, original: str, edited: str) -> list[str]:
	"""Top-level packages of project modules that `edited` newly imports but that are not on disk yet.

	Ruff tells first-party from third-party by looking for the module's file, so an import of a module that this
	very edit is about to create would be sorted among the third-party imports."""
	roots = [workspace, workspace / "src"]
	missing: set[str] = set()
	for module in _imported_modules(edited) - _imported_modules(original):
		parts = module.split(".")
		exists = any(
			(root.joinpath(*parts).is_dir() or root.joinpath(*parts).with_suffix(".py").is_file()) for root in roots
		)
		package = any((root / parts[0]).is_dir() or (root / f"{parts[0]}.py").is_file() for root in roots)
		if package and not exists:
			missing.add(parts[0])
	return sorted(missing)


def _run_fix(
	ruff: list[str], text: str, filename: str, cwd: Path, rules: str, first_party: list[str] | None = None
) -> str | None:
	overrides = []
	if first_party:
		overrides = ["--config", f"lint.isort.known-first-party = {first_party!r}".replace("'", '"')]
	try:
		result = subprocess.run(  # noqa: S603
			[*ruff, "check", "--fix", "--select", rules, *overrides, "--stdin-filename", filename, "--quiet", "-"],
			input=text.encode("utf-8"),  # bytes: text mode would turn every CRLF into LF
			capture_output=True,
			cwd=cwd,
			timeout=30,
			check=False,
		)
	except (OSError, subprocess.TimeoutExpired):
		return None
	if result.returncode not in (0, 1) or not result.stdout:
		return None
	try:
		return result.stdout.decode("utf-8")
	except UnicodeDecodeError:
		return None


def sort_imports(workspace: Path, original: str, edited: str, filename: str) -> str:
	"""`edited` with its imports sorted by ruff's isort rule, but only when `original` was already
	sorted that way: a file whose imports ruff would reorder anyway is left as the edit made it,
	so a refactoring never reshuffles imports it didn't touch."""
	global last_sort_failed
	last_sort_failed = None
	ruff = find_ruff(workspace)
	if original == edited:
		return edited
	if ruff is None:
		last_sort_failed = "imports were not sorted (ruff unavailable or failed)"
		return edited
	if original.strip():  # a file this edit creates has no imports to leave alone
		before = _run_fix(ruff, original, filename, workspace, "I001")
		if before is None:
			last_sort_failed = "imports were not sorted (ruff unavailable or failed)"
			return edited
		if before != original:
			return edited
	missing = _missing_first_party(workspace, original, edited)
	first_party = [*_configured_first_party(workspace), *missing] if missing else None
	fixed = _run_fix(ruff, edited, filename, workspace, "I001", first_party)
	if fixed is None:
		last_sort_failed = "imports were not sorted (ruff unavailable or failed)"
		return edited
	if fixed.count("\f") != edited.count("\f"):
		# ruff's isort fix normalizes the whitespace around the import block and silently drops form-feed
		# (page break) lines there; keeping the edit as written beats deleting characters nobody asked to touch.
		last_sort_failed = "imports were not sorted (ruff would drop form-feed lines)"
		return edited
	return fixed
