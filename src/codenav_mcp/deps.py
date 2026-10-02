"""Which Python files import a given module (one level, from the import statements alone).

The diagnostics check after an edit covers the edited files plus the files that
import them: a changed signature or a renamed export breaks importers, not the
file that was edited. Parsing every file's imports is cached by (mtime, size).
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterable
from pathlib import Path

from mcp_nav_shared.exclude import EXCLUDED_DIR_NAMES


# path -> ((mtime_ns, size), module names the file imports, absolute and as written by `from x import y` -> x, x.y)
_imports_cache: dict[Path, tuple[tuple[int, int], frozenset[str]]] = {}


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
	"""The dotted name of `path` under the first root that contains it."""
	for root in roots:
		names = module_names_for(path, [root])
		if names:
			return next(iter(names))
	return None


def iter_python_files(workspace: Path) -> Iterable[Path]:
	"""Workspace `.py`/`.pyi` files, skipping excluded directories and nested checkouts."""
	root = workspace.resolve()
	pending = [root]
	while pending:
		directory = pending.pop()
		try:
			with os.scandir(directory) as it:
				entries = sorted(it, key=lambda e: e.name)
		except OSError:
			continue
		if directory != root and any(e.name == ".git" for e in entries):
			continue
		for entry in entries:
			try:
				if entry.is_dir(follow_symlinks=False):
					if entry.name not in EXCLUDED_DIR_NAMES:
						pending.append(Path(entry.path))
				elif entry.name.endswith((".py", ".pyi")):
					yield Path(entry.path)
			except OSError:
				continue


def _imported_modules(path: Path, own_names: set[str]) -> frozenset[str]:
	try:
		stat = path.stat()
	except OSError:
		return frozenset()
	key = (stat.st_mtime_ns, stat.st_size)
	hit = _imports_cache.get(path)
	if hit is not None and hit[0] == key:
		return _resolve_relative(hit[1], own_names)
	try:
		tree = ast.parse(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
		return frozenset()
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
	return _resolve_relative(frozenset(found), own_names)


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


def find_dependents(
	workspace: Path,
	targets: Iterable[Path],
	roots: Iterable[Path],
	*,
	limit: int = 200,
) -> list[Path]:
	"""Files (other than `targets`) that import one of the `targets` modules."""
	root_list = list(roots)
	target_set = {t.resolve() for t in targets}
	wanted: set[str] = set()
	for target in target_set:
		wanted |= module_names_for(target, root_list)
	if not wanted:
		return []
	dependents: list[Path] = []
	for path in iter_python_files(workspace):
		resolved = path.resolve()
		if resolved in target_set:
			continue
		own = module_names_for(resolved, root_list)
		# A package's `__init__` imports relative to the package itself; give it an extra name part.
		relative_bases = {f"{name}.__init__" if resolved.name == "__init__.py" else name for name in own}
		if wanted & _imported_modules(resolved, relative_bases):
			dependents.append(resolved)
			if len(dependents) >= limit:
				break
	return dependents
