"""Bounded workspace size: refuse a tree too large to index before anything reads it into memory (QA B32).

Pointing the server at a whole home directory (about 890k Python files that ty indexes) made the language server
(and, less, the file watcher) allocate several GB within seconds and get OOM-killed. The count here is a constant-memory walk that stops at
the first file over the cap, so the refusal is cheap and comes before the language server starts.
"""

from __future__ import annotations

import os
from pathlib import Path

from codenav_mcp.deps import ENV_PREFIX


MAX_FILES_ENV = f"{ENV_PREFIX}_MAX_FILES"
DEFAULT_MAX_FILES = 250_000
_PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
# What ty skips by default (its `src.exclude`); everything else under the workspace, including other virtualenvs'
# site-packages, nested checkouts and vendored trees, is indexed by it, so it all counts toward the cap.
# `.gitignore` is not read: counting more than ty loads only makes the refusal earlier, never later.
_TY_SKIPPED_DIRS = frozenset(
	{
		".bzr", ".direnv", ".eggs", ".git", ".git-rewrite", ".hg", ".mypy_cache", ".nox", ".pants.d", ".pytype",
		".ruff_cache", ".svn", ".tox", ".venv", "__pypackages__", "_build", "buck-out", "dist", "node_modules", "venv",
	}
)  # fmt: skip


def max_files() -> int:
	"""The cap on Python files: `CODENAV_MCP_MAX_FILES`, or the default when unset, not a whole number or below 1."""
	try:
		value = int(os.environ.get(MAX_FILES_ENV, "").strip())
	except ValueError:
		return DEFAULT_MAX_FILES
	return value if value >= 1 else DEFAULT_MAX_FILES


def count_python_files(root: Path, limit: int) -> int:
	"""Python files (`.py`/`.pyi`) under `root`, counting at most `limit + 1`, so the walk stops at the first file over the cap.

	Skips the directories ty skips by default and never follows symlinked directories. Only the pending directory list
	and one directory listing are in memory.
	"""
	count = 0
	pending = [root]
	while pending:
		directory = pending.pop()
		try:
			with os.scandir(directory) as it:
				entries = list(it)
		except OSError:
			continue
		for entry in entries:
			try:
				if entry.is_dir(follow_symlinks=False):
					if entry.name not in _TY_SKIPPED_DIRS:
						pending.append(Path(entry.path))
				elif os.path.splitext(entry.name)[1].lower() in _PYTHON_SUFFIXES:
					count += 1
					if count > limit:
						return count
			except OSError:
				continue
	return count


def workspace_too_large(root: Path) -> str | None:
	"""The refusal message when `root` holds more Python files than the cap, else None."""
	limit = max_files()
	if count_python_files(root, limit) <= limit:
		return None
	return (
		f"{root} holds more than {limit:,} Python files, so it is too large to index; refusing to scan it. "
		f"Point CODENAV_MCP_WORKSPACE at your project folder, or raise {MAX_FILES_ENV} "
		f"(default {DEFAULT_MAX_FILES:,}) if you really want this tree indexed"
	)
