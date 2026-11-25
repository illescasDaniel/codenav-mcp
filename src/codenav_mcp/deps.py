"""Which Python files import a given module (one level, from the import statements alone).

The diagnostics check after an edit covers the edited files plus the files that
import them: a changed signature or a renamed export breaks importers, not the
file that was edited. Parsing every file's imports is cached by (mtime, size).
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from mcp_nav_shared.edits import decode_source
from mcp_nav_shared.exclude import excluded_names


ENV_PREFIX = "CODENAV_MCP"
MAX_DEPENDENTS = 2000
_MAX_SKIPPED_SCAN_FILES = 5000
# Never worth looking into when looking for importers that were left out: caches, VCS data, installed packages.
_NEVER_SCAN = frozenset(
	{
		"node_modules", ".git", "__pycache__", ".venv", "venv", ".venv-bare", "site-packages", ".tox",
		".mypy_cache", ".pytest_cache", ".ruff_cache", ".pytest-testmon", ".eggs",
	}
)  # fmt: skip
_VCS_DIRS = frozenset({".git", ".hg", ".svn", ".bzr"})

SkipReason = Literal["excluded dir", "nested repo", "unparsable", "unreadable", "limit"]


@dataclass(frozen=True)
class Skipped:
	"""An importer (or a group of them) that was not examined or not updated, and why."""

	path_or_dir: str
	reason: SkipReason
	detail: str
	count: int = 1
	files: tuple[str, ...] = ()  # the importer files of a group, workspace-relative (for the note)


@dataclass
class DependentsReport:
	files: list[Path] = field(default_factory=list)
	skipped: list[Skipped] = field(default_factory=list)


# path -> (content digest, module names as written)
_imports_cache: dict[Path, tuple[bytes, frozenset[str]]] = {}


def module_names_for(path: Path, roots: Iterable[Path]) -> set[str]:
	"""Dotted names `path` can be imported as, one per root containing it."""
	names: set[str] = set()
	resolved = path.resolve()
	for root in roots:
		try:
			parts = list(resolved.relative_to(root.resolve()).with_suffix("").parts)
		except ValueError:
			continue
		if parts and parts[-1] == "__init__":
			parts.pop()
		if parts:
			names.add(".".join(parts))
	return names


def primary_module_name(path: Path, roots: Iterable[Path]) -> str | None:
	"""The dotted name of `path` under the most specific (deepest) root that contains it; the
	first of equally deep roots wins."""
	best: tuple[int, str] | None = None
	for root in roots:
		names = module_names_for(path, [root])
		if not names:
			continue
		depth = len(root.resolve().parts)
		if best is None or depth > best[0]:
			best = (depth, next(iter(names)))
	return best[1] if best else None


def _walk(workspace: Path, pruned: list[tuple[Path, SkipReason]] | None = None) -> Iterable[Path]:
	root = workspace.resolve()
	skip_names = excluded_names(ENV_PREFIX)
	pending = [root]
	while pending:
		directory = pending.pop()
		try:
			with os.scandir(directory) as it:
				entries = sorted(it, key=lambda e: e.name)
		except OSError:
			if pruned is not None:
				pruned.append((directory, "unreadable"))
			continue
		for entry in entries:
			try:
				if entry.is_dir(follow_symlinks=False):
					child = Path(entry.path)
					if entry.name in skip_names:
						if pruned is not None:
							pruned.append((child, "excluded dir"))
					elif (child / ".git").exists():
						if pruned is not None:
							pruned.append((child, "nested repo"))
					else:
						pending.append(child)
				elif entry.name.endswith((".py", ".pyi")):
					yield Path(entry.path)
				elif entry.is_symlink() and pruned is not None:
					pruned.append((Path(entry.path), "unreadable"))
			except OSError:
				if pruned is not None:
					pruned.append((Path(entry.path), "unreadable"))


def iter_python_files(workspace: Path) -> Iterable[Path]:
	"""Workspace `.py`/`.pyi` files, skipping excluded directories and nested checkouts."""
	return _walk(workspace)


def _imported_modules(path: Path, own_names: set[str]) -> frozenset[str]:
	return _parse_imports(path, own_names)[0]


def _read_no_symlink(path: Path) -> tuple[os.stat_result, bytes]:
	"""Read a regular file without following a final-component symlink."""
	if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():
		raise OSError("symlink not followed")
	fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
	with os.fdopen(fd, "rb") as handle:
		info = os.fstat(handle.fileno())
		if not stat.S_ISREG(info.st_mode):
			raise OSError("not a regular file")
		return info, handle.read()


def _parse_imports(path: Path, own_names: set[str]) -> tuple[frozenset[str], str | None]:
	"""Modules `path` imports, and why it could not be read ("unreadable" / "unparsable: <detail>") if so."""
	try:
		_, data = _read_no_symlink(path)
	except OSError:
		return frozenset(), "unreadable"
	key = hashlib.blake2b(data, digest_size=32).digest()
	hit = _imports_cache.get(path)
	if hit is not None and hit[0] == key:
		return _resolve_relative(hit[1], own_names), None
	try:
		tree = ast.parse(decode_source(data)[0])  # a UTF-8 BOM would make ast.parse fail
	except (OSError, UnicodeDecodeError):
		return frozenset(), "unreadable"
	except SyntaxError as error:
		return frozenset(), f"syntax error at line {error.lineno or 1}"
	except ValueError:
		return frozenset(), "unparsable"
	found: set[str] = set()
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			for alias in node.names:
				found.add(alias.name)
				found.update(  # `import a.b.c` runs a and a.b too
					".".join(alias.name.split(".")[:i]) for i in range(1, alias.name.count(".") + 1)
				)
		elif isinstance(node, ast.ImportFrom):
			base = "." * node.level + (node.module or "")
			found.add(base)
			found.update(f"{base}.{alias.name}" if base.strip(".") else f"{base}{alias.name}" for alias in node.names)
	_imports_cache[path] = (key, frozenset(found))
	return _resolve_relative(frozenset(found), own_names), None


def _resolve_relative(modules: frozenset[str], own_names: set[str]) -> frozenset[str]:
	"""Turn `.x` / `..x.y` into absolute names using the importing file's own module names."""
	resolved: set[str] = set()
	for module in modules:
		if not module.startswith("."):
			resolved.add(module)
			continue
		level = len(module) - len(module.lstrip("."))
		rest = module.lstrip(".")
		for own in own_names:
			package = own.split(".")
			# `own` is the module's own dotted name; the package is everything before the last part
			# (an `__init__` module's name already is the package, handled by the caller adding both).
			base = package[: len(package) - level] if level <= len(package) else []
			if level > len(package):
				continue
			resolved.add(".".join([*base, *([rest] if rest else [])]).strip("."))
	return frozenset(resolved)


def _rel(path: Path, workspace: Path) -> str:
	try:
		return path.relative_to(workspace.resolve()).as_posix()
	except ValueError:
		return path.as_posix()


def _mentions_any(path: Path, words: re.Pattern[str]) -> bool | None:
	try:
		return words.search(_read_no_symlink(path)[1].decode("utf-8", errors="replace")) is not None
	except OSError:
		return None


def _skipped_importers(
	workspace: Path,
	pruned: list[tuple[Path, SkipReason]],
	wanted: set[str],
	roots: list[Path],
	target_set: set[Path],
) -> list[Skipped]:
	"""Per pruned directory: how many of its files import one of the `wanted` modules."""
	words = re.compile("|".join(rf"\b{re.escape(name.rsplit('.', 1)[-1])}\b" for name in sorted(wanted)))
	found: list[Skipped] = []
	scanned = 0
	limit_reached = False

	def onerror(error: OSError) -> None:
		path = _rel(Path(error.filename), workspace) + "/" if error.filename else "an excluded directory"
		found.append(Skipped(path, "unreadable", f"{path}: directory could not be fully scanned"))

	for directory, reason in sorted(pruned, key=lambda item: item[0]):
		rel_dir = _rel(directory, workspace) + "/"
		if reason == "unreadable":
			detail = "symlink was not scanned" if directory.is_symlink() else "directory could not be read"
			found.append(Skipped(rel_dir, "unreadable", f"{rel_dir}: {detail}"))
			continue
		if directory.name in _VCS_DIRS:
			continue  # version-control data never holds importers; listing it would be noise
		if directory.name in _NEVER_SCAN:
			found.append(Skipped(rel_dir, "unreadable", f"{rel_dir}: excluded directory was not scanned"))
			continue
		if limit_reached:
			found.append(Skipped(rel_dir, "unreadable", f"{rel_dir}: not scanned because the scan limit was reached"))
			continue
		count = 0
		importer_files: list[str] = []

		for dirpath, dirs, filenames in os.walk(directory, onerror=onerror):
			for name in sorted(d for d in dirs if Path(dirpath, d).is_symlink()):
				child = _rel(Path(dirpath, name), workspace) + "/"
				found.append(Skipped(child, "unreadable", f"{child}: symlinked directory was not scanned"))
			dirs[:] = [d for d in dirs if not Path(dirpath, d).is_symlink()]
			for name in sorted(d for d in dirs if d in _NEVER_SCAN and d not in _VCS_DIRS):
				child = _rel(Path(dirpath, name), workspace) + "/"
				found.append(Skipped(child, "unreadable", f"{child}: excluded directory was not scanned"))
			dirs[:] = sorted(d for d in dirs if d not in _NEVER_SCAN)
			for filename in sorted(filenames):
				if not filename.endswith((".py", ".pyi")):
					path = Path(dirpath, filename)
					if path.is_symlink():
						rel = _rel(path, workspace)
						found.append(Skipped(rel, "unreadable", f"{rel}: symlink was not scanned"))
					continue
				scanned += 1
				if scanned > _MAX_SKIPPED_SCAN_FILES:
					found.append(
						Skipped(
							rel_dir,
							"unreadable",
							f"{rel_dir}: excluded directory scan limit reached; importers may be missing",
						)
					)
					limit_reached = True
					break
				path = Path(dirpath, filename)
				mention = _mentions_any(path, words)
				if mention is None:
					rel = _rel(path, workspace)
					found.append(Skipped(rel, "unreadable", f"{rel}: file could not be read or is a symlink"))
					continue
				if path in target_set or not mention:
					continue
				own = module_names_for(path, roots)
				relative_bases = {f"{name}.__init__" if path.name == "__init__.py" else name for name in own}
				imported, problem = _parse_imports(path, relative_bases)
				if problem is None and not wanted & imported:
					continue
				count += 1
				importer_files.append(_rel(path, workspace))
			if limit_reached:
				break
		if count:
			noun = "file imports" if count == 1 else "files import"
			found.append(
				Skipped(_rel(directory, workspace) + "/", reason, f"{count} {noun} it", count, tuple(importer_files))
			)
	return found


def find_dependents_report(
	workspace: Path,
	targets: Iterable[Path],
	roots: Iterable[Path],
	*,
	limit: int = MAX_DEPENDENTS,
) -> DependentsReport:
	"""Files (other than `targets`) that import one of the `targets` modules, plus what was not examined:
	importers in excluded directories or nested checkouts, files that could not be parsed or read, and
	importers beyond `limit`."""
	root_list = list(roots)
	target_set = {t.resolve() for t in targets}
	wanted: set[str] = set()
	for target in target_set:
		wanted |= module_names_for(target, root_list)
	report = DependentsReport()
	if not wanted:
		return report
	words = re.compile("|".join(rf"\b{re.escape(name.rsplit('.', 1)[-1])}\b" for name in sorted(wanted)))
	pruned: list[tuple[Path, SkipReason]] = []
	over_limit = 0
	for path in _walk(workspace, pruned):
		resolved = path
		if resolved in target_set:
			continue
		own = module_names_for(resolved, root_list)
		# A package's `__init__` imports relative to the package itself; give it an extra name part.
		relative_bases = {f"{name}.__init__" if resolved.name == "__init__.py" else name for name in own}
		imported, problem = _parse_imports(resolved, relative_bases)
		if problem is not None:
			if problem == "unreadable" or _mentions_any(resolved, words) is not False:
				rel = _rel(resolved, workspace)
				reason: SkipReason = "unreadable" if problem == "unreadable" else "unparsable"
				report.skipped.append(Skipped(rel, reason, f"{rel}: {problem}"))
			continue
		if wanted & imported:
			if len(report.files) >= limit:
				over_limit += 1
			else:
				report.files.append(resolved)
	if over_limit:
		report.skipped.append(Skipped(f"{over_limit} more files", "limit", f"more than {limit} importers", over_limit))
	report.skipped.extend(_skipped_importers(workspace, pruned, wanted, root_list, target_set))
	return report


def find_dependents(
	workspace: Path,
	targets: Iterable[Path],
	roots: Iterable[Path],
	*,
	limit: int = MAX_DEPENDENTS,
) -> list[Path]:
	"""Files (other than `targets`) that import one of the `targets` modules."""
	return find_dependents_report(workspace, targets, roots, limit=limit).files


def _plural(count: int, word: str = "file") -> str:
	return f"{count} {word}" + ("" if count == 1 else "s")


_MAX_LISTED_IMPORTERS = 10


def _excluded_group(skipped: Skipped) -> str:
	"""`dir/ (N files: a.py, b.py)`: the directory and its skipped importer files, the first few named."""
	listed = list(skipped.files[:_MAX_LISTED_IMPORTERS])
	if skipped.files and len(skipped.files) > _MAX_LISTED_IMPORTERS:
		listed.append(f"and {len(skipped.files) - _MAX_LISTED_IMPORTERS} more")
	files = f": {', '.join(listed)}" if listed else ""
	return f"{skipped.path_or_dir} ({_plural(skipped.count)}{files})"


def format_skipped(skipped: list[Skipped], *, verb: str = "updated") -> list[str]:
	"""Message lines for what `find_dependents_report` did not examine, one line per group.

	`verb` says what was left undone for excluded-directory importers: "updated" for a move or rename,
	"checked" for an edit (which only reads diagnostics of importers and never rewrites them).
	"""
	lines: list[str] = []
	excluded = [s for s in skipped if s.reason == "excluded dir"]
	if excluded:
		groups = "; ".join(_excluded_group(s) for s in excluded)
		names = ",".join(dict.fromkeys(s.path_or_dir.rstrip("/").rsplit("/", 1)[-1] for s in excluded))
		lines.append(
			f"Importers not {verb} (excluded directories): {groups}. Pass {ENV_PREFIX}_INCLUDE_DIRS={names} to include them."
		)
	nested = [s for s in skipped if s.reason == "nested repo"]
	if nested:
		groups = ", ".join(f"{s.path_or_dir} ({_plural(s.count)})" for s in nested)
		lines.append(f"Importers not updated (nested git repositories, treated as other projects): {groups}.")
	unread = [s for s in skipped if s.reason in ("unparsable", "unreadable")]
	if unread:
		lines.append(
			"Importers not updated (could not be parsed or read): " + "; ".join(s.detail for s in unread) + "."
		)
	for s in skipped:
		if s.reason == "limit":
			lines.append(f"Importers not updated ({s.detail}): {_plural(s.count)} more.")
	return lines


def import_from_targets(node: ast.ImportFrom, own_names: set[str], *, is_package_init: bool = False) -> set[str]:
	"""Absolute module names a `from ... import` statement refers to (relative levels resolved
	against the importing file's own module names)."""
	if node.level == 0:
		return {node.module} if node.module else set()
	resolved: set[str] = set()
	for own in own_names:
		parts = [*own.split("."), *(["__init__"] if is_package_init else [])]
		keep = len(parts) - node.level
		if keep < 0:
			continue
		resolved.add(".".join([*parts[:keep], *([node.module] if node.module else [])]))
	return {name for name in resolved if name}
