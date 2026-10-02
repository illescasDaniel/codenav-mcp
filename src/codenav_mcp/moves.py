"""Moving code between modules: a top-level symbol, or a whole module file.

Both rewrite imports across the workspace from the syntax tree; the language
server is only used afterwards, to check the result. Anything that is not a
plain `from x import name` / `import x` (a module attribute access, a string
naming the old module) is listed for the caller instead of guessed at.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from mcp_nav_shared.edits import EditPlan, FileChange, read_source, relative_name
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp.deps import find_dependents, import_from_targets, module_names_for, primary_module_name
from codenav_mcp.imports import add_from_import, add_import, module_imports, remove_names
from codenav_mcp.mentions import find_mentions, format_mentions
from codenav_mcp.pysource import (
	check_syntax,
	leading_comment_start,
	loaded_names,
	names_used_outside,
	parse_python,
	source_lines,
	top_level_names,
)
from codenav_mcp.signature import SourceMap, apply_offset_edits
from codenav_mcp.symbol_tools import _delete_span, _locate
from codenav_mcp.tidy import sort_imports
from codenav_mcp.tool_base import Session


def _resolve_dest(session: Session, to_file: str) -> Path:
	candidate = Path(to_file)
	dest = (candidate if candidate.is_absolute() else session.workspace / candidate).resolve()
	if dest.suffix.lower() != ".py":
		raise ToolInputError(f"the destination must be a .py file, got {to_file!r}")
	try:
		dest.relative_to(session.workspace.resolve())
	except ValueError:
		raise ToolInputError(f"{to_file} is outside the workspace") from None
	return dest


def _module_or_fail(path: Path, roots: list[Path]) -> str:
	name = primary_module_name(path, roots)
	if name is None:
		raise ToolInputError(
			f"cannot derive an import path for {path}; it is outside the source roots "
			"(set CODENAV_MCP_SOURCE_ROOT if the importable code lives elsewhere)"
		)
	return name


def _import_requirements(
	tree: ast.Module, needed: set[str], src_module: str, src_names: set[str], is_init: bool
) -> tuple[list[tuple[str, str, str | None]], list[str]]:
	"""How the moved code gets each name it needs, as absolute imports for the destination:
	[("from", module, name, asname) | ("import", module, asname)] and the names defined in the source module itself."""
	requirements: list[tuple[str, str, str | None]] = []
	from_source: list[str] = []
	bound_by_import: dict[str, tuple[str, str, str | None]] = {}
	for stmt in module_imports(tree):
		if stmt.conditional:
			continue
		node = stmt.node
		for (name, asname), bound in zip(stmt.aliases, stmt.bound_names, strict=True):
			if isinstance(node, ast.ImportFrom):
				targets = sorted(import_from_targets(node, src_names, is_package_init=is_init))
				if not targets:
					continue
				bound_by_import[bound] = ("from", targets[0], name if asname is None else f"{name} as {asname}")
			else:
				bound_by_import[bound] = ("import", name, asname)
	for name in sorted(needed):
		if name in bound_by_import:
			requirements.append(bound_by_import[name])
		else:
			from_source.append(name)
	return requirements, from_source


async def plan_move_symbol(
	session: Session, name: str, to_file: str, file_path: str | None, keep_reexport: bool
) -> EditPlan:
	src, src_text, definition = await _locate(session, name, file_path)
	if definition.parent is not None:
		raise ToolInputError(
			f"{definition.qualname} is nested inside {definition.parent.name}; only module-level symbols can be moved"
		)
	dest = _resolve_dest(session, to_file)
	if dest == src:
		raise ToolInputError("the destination is the file the symbol is already in")
	workspace = session.workspace
	src_module = _module_or_fail(src, session.roots)
	dest_module = _module_or_fail(dest, session.roots)
	dest_text, dest_bom = read_source(dest) if dest.exists() else ("", False)
	src_tree = parse_python(src_text, relative_name(src, workspace))
	dest_tree = parse_python(dest_text, relative_name(dest, workspace)) if dest_text else ast.parse("")
	if definition.name in top_level_names(dest_tree):
		raise ToolInputError(f"{relative_name(dest, workspace)} already defines {definition.name!r}")
	lines = source_lines(src_text)
	first = leading_comment_start(lines, definition.first_line)
	moved_source = "".join(lines[first - 1 : definition.last_line])
	if not moved_source.endswith("\n"):
		moved_source += "\n"
	src_names = module_names_for(src, session.roots)
	needed = (loaded_names(definition.node) & top_level_names(src_tree)) - {definition.name}
	requirements, from_source = _import_requirements(src_tree, needed, src_module, src_names, src.name == "__init__.py")
	dest_bound = top_level_names(dest_tree)
	notes: list[str] = []

	# destination: the code, then the imports it needs
	body = moved_source if not dest_text.strip() else dest_text.rstrip("\r\n") + "\n\n\n" + moved_source
	for requirement in requirements:
		if requirement[0] == "from":
			imported = requirement[2]
			name_part, _, alias = imported.partition(" as ")
			if (alias or name_part) in dest_bound:
				continue
			body = add_from_import(body, requirement[1], [(name_part, alias or None)])
		else:
			bound = (requirement[2] or requirement[1]).split(".")[0]
			if bound not in dest_bound:
				body = add_import(body, requirement[1], requirement[2])
	back = [n for n in from_source if n not in dest_bound]
	if back:
		body = add_from_import(body, src_module, back)
		notes.append(
			f"{relative_name(dest, workspace)} now imports {', '.join(back)} from {src_module}: if {src_module} "
			f"imports {dest_module} back this is a circular import (move those helpers too, or import lazily)"
		)

	# source: the code removed, imports it alone needed dropped, a way back if it still uses the symbol
	new_src = _delete_span(src_text, definition)
	used_outside = names_used_outside(src_tree, first, definition.last_line)
	only_for_moved = {n for n in needed if n not in used_outside} & {
		b for s in module_imports(src_tree) for b in s.bound_names
	}
	if only_for_moved:
		new_src = remove_names(new_src, only_for_moved)
		notes.append(
			f"dropped from {relative_name(src, workspace)}: imports only the moved code used ({', '.join(sorted(only_for_moved))})"
		)
	if definition.name in used_outside or keep_reexport:
		new_src = add_from_import(new_src, dest_module, [definition.name])
		reason = "still uses it" if definition.name in used_outside else "keep_reexport"
		notes.append(f"{relative_name(src, workspace)} imports {definition.name} from {dest_module} ({reason})")

	# importers elsewhere
	changed: dict[Path, tuple[str, str, bool]] = {}
	for importer in find_dependents(workspace, [src], session.roots):
		if importer == dest:
			text_before, bom = body, dest_bom
		else:
			text_before, bom = read_source(importer)
		own = module_names_for(importer, session.roots)
		tree = parse_python(text_before, relative_name(importer, workspace))
		new_text = text_before
		for stmt in sorted(module_imports(tree), key=lambda s: -s.first_line):
			node = stmt.node
			if not isinstance(node, ast.ImportFrom) or not any(a.name == definition.name for a in node.names):
				continue
			if not import_from_targets(node, own, is_package_init=importer.name == "__init__.py") & src_names:
				continue
			alias = next(a for a in node.names if a.name == definition.name)
			bound = alias.asname or alias.name
			new_text = remove_names(new_text, {bound}, only_line=stmt.first_line)
			if importer != dest:
				new_text = add_from_import(new_text, dest_module, [(definition.name, alias.asname)])
		if importer == dest:
			body = new_text
		elif new_text != text_before:
			changed[importer] = (text_before, new_text, bom)
	attribute_users = _module_attribute_users(workspace, src, session.roots, src_names, definition.name)
	if attribute_users:
		notes.append(
			f"accessed through the old module (`{src_module.rsplit('.', 1)[-1]}.{definition.name}`), not rewritten: "
			+ ", ".join(attribute_users[:6])
		)

	sorted_changes: list[FileChange] = []
	for path, original, edited, bom in [
		(src, src_text, new_src, False),
		(dest, dest_text, body, dest_bom),
		*[(p, a, b, c) for p, (a, b, c) in changed.items()],
	]:
		edited = sort_imports(workspace, original, edited, relative_name(path, workspace)) if original else edited
		check_syntax(edited, relative_name(path, workspace))
		sorted_changes.append(FileChange(path, original if path.exists() else None, edited, bom, bom))
	unseen = [
		m for m in find_mentions(workspace, f"{src_module}.{definition.name}", {}) if m.kind in ("string", "text")
	]
	notes += format_mentions(unseen, f"{src_module}.{definition.name}")
	title = f"Move {definition.qualname} from {relative_name(src, workspace)} to {relative_name(dest, workspace)}"
	return EditPlan(title, sorted_changes, notes)


def _module_attribute_users(workspace: Path, src: Path, roots: list[Path], src_names: set[str], name: str) -> list[str]:
	"""Files using the symbol as `module.name` where `module` is the old module."""
	found = []
	leaves = {n.rsplit(".", 1)[-1] for n in src_names}
	for dependent in find_dependents(workspace, [src], roots):
		try:
			tree = ast.parse(read_source(dependent)[0])
		except (OSError, SyntaxError, UnicodeDecodeError):
			continue
		for node in ast.walk(tree):
			if (
				isinstance(node, ast.Attribute)
				and node.attr == name
				and ast.unparse(node.value).split(".")[-1] in leaves
			):
				found.append(f"{relative_name(dependent, workspace)}:{node.lineno}")
	return found


# -- moving a module ---------------------------------------------------------------


def _relative_module(importer_module: str, level: int, target: str, is_init: bool) -> str | None:
	"""`target` written relative to the importer, or None when absolute reads better."""
	parts = importer_module.split(".") if is_init else importer_module.split(".")[:-1]
	base = parts[: len(parts) - (level - 1)] if level - 1 <= len(parts) else []
	prefix = ".".join(base)
	if prefix and (target == prefix or target.startswith(prefix + ".")):
		return "." * level + target[len(prefix) + 1 :]
	return None


def plan_move_module(session: Session, from_file: str, to_file: str) -> EditPlan:
	workspace = session.workspace
	src_candidate = Path(from_file)
	src = (src_candidate if src_candidate.is_absolute() else workspace / src_candidate).resolve()
	if not src.is_file() or src.suffix.lower() != ".py":
		raise ToolInputError(f"{from_file} is not an existing .py file")
	if src.name == "__init__.py":
		raise ToolInputError("moving a package's __init__.py is not supported; move the modules inside it")
	dest = _resolve_dest(session, to_file)
	if dest.exists():
		raise ToolInputError(f"{relative_name(dest, workspace)} already exists")
	old_primary = _module_or_fail(src, session.roots)
	new_primary = _module_or_fail(dest, session.roots)
	mapping = {}
	for root in session.roots:
		before, after = module_names_for(src, [root]), module_names_for(dest, [root])
		if before and after:
			mapping[next(iter(before))] = next(iter(after))
	if not mapping:
		raise ToolInputError("source and destination are not under a common import root")
	changes: list[FileChange] = []
	notes: list[str] = []

	src_text, src_bom = read_source(src)
	moved_text = _absolutize_relative_imports(src_text, src, session)
	check_syntax(moved_text, relative_name(src, workspace))

	for importer in find_dependents(workspace, [src], session.roots):
		text, bom = read_source(importer)
		new_text = _rewrite_importer(text, importer, mapping, session, notes)
		if new_text != text:
			changes.append(FileChange(importer, text, new_text, bom, bom))
	changes.append(FileChange(src, src_text, None, src_bom, False))
	changes.append(FileChange(dest, None, moved_text, src_bom, src_bom))
	for change in changes:
		if change.new_text is not None:
			check_syntax(change.new_text, relative_name(change.path, workspace))
	if (
		dest.parent != src.parent
		and not (dest.parent / "__init__.py").exists()
		and (src.parent / "__init__.py").exists()
	):
		notes.append(
			f"{relative_name(dest.parent, workspace)} has no __init__.py (the old package does); add one if it must be a regular package"
		)
	unseen = [m for m in find_mentions(workspace, old_primary, {}) if m.kind in ("string", "text", "comment")]
	notes += format_mentions(unseen, old_primary)
	notes = list(dict.fromkeys(notes))
	return EditPlan(f"Move module {old_primary} -> {new_primary}", changes, notes)


def _absolutize_relative_imports(text: str, path: Path, session: Session) -> str:
	"""A moved file's relative imports would point somewhere else at the new location: spell them absolutely."""
	tree = parse_python(text, str(path))
	own = module_names_for(path, session.roots)
	smap = SourceMap(text)
	edits: list[tuple[int, int, str]] = []
	for node in ast.walk(tree):
		if isinstance(node, ast.ImportFrom) and node.level > 0:
			targets = sorted(import_from_targets(node, own, is_package_init=path.name == "__init__.py"))
			if not targets:
				continue
			segment = smap.segment(node)
			head = re.match(r"(\s*from\s+)(\.+)([\w.]*)(\s+import\b)", segment)
			if head:
				edits.append((smap.start(node) + len(head.group(1)), smap.start(node) + head.end(3), targets[0]))
	return apply_offset_edits(text, edits)


def _rewrite_importer(text: str, importer: Path, mapping: dict[str, str], session: Session, notes: list[str]) -> str:
	tree = parse_python(text, str(importer))
	smap = SourceMap(text)
	own = module_names_for(importer, session.roots)
	edits: list[tuple[int, int, str]] = []
	rename_names: dict[str, str] = {}
	chain_renames: dict[str, str] = {}
	repackage: list[tuple[int, str, str | None, str, str]] = []
	for old, new in mapping.items():
		parent_old, _, leaf_old = old.rpartition(".")
		parent_new, _, leaf_new = new.rpartition(".")
		for node in ast.walk(tree):
			if isinstance(node, ast.ImportFrom):
				segment = smap.segment(node)
				targets = import_from_targets(node, own, is_package_init=importer.name == "__init__.py")
				if old in targets and node.level == 0 or (node.level > 0 and old in targets):
					head = re.match(r"(\s*from\s+)([.\w]+)(\s+import\b)", segment)
					if head:
						edits.append((smap.start(node) + len(head.group(1)), smap.start(node) + head.end(2), new))
						if node.level > 0:
							notes.append(
								f"{relative_name(importer, session.workspace)}: relative import rewritten as absolute"
							)
				elif parent_old in targets and any(a.name == leaf_old for a in node.names):
					if parent_old == parent_new and (len(node.names) >= 1):
						for alias in node.names:
							if alias.name == leaf_old:
								pattern = re.compile(rf"\b{re.escape(leaf_old)}\b")
								match = pattern.search(segment[segment.index("import") :])
								if match:
									start = smap.start(node) + segment.index("import") + match.start()
									edits.append((start, start + len(leaf_old), leaf_new))
								if alias.asname is None:
									rename_names[leaf_old] = leaf_new
					else:
						for alias in node.names:
							if alias.name == leaf_old:
								repackage.append(
									(node.lineno, alias.asname or leaf_old, alias.asname, parent_new, leaf_new)
								)
								if alias.asname is None and leaf_old != leaf_new:
									rename_names[leaf_old] = leaf_new
			elif isinstance(node, ast.Import):
				for alias in node.names:
					if alias.name == old:
						segment = smap.segment(node)
						match = re.search(rf"\b{re.escape(old)}\b", segment)
						if match:
							start = smap.start(node) + match.start()
							edits.append((start, start + len(old), new))
						if alias.asname is None:
							chain_renames[old] = new
	if rename_names or chain_renames:
		for node in ast.walk(tree):
			if isinstance(node, ast.Name) and node.id in rename_names and isinstance(node.ctx, ast.Load):
				edits.append((smap.start(node), smap.end(node), rename_names[node.id]))
			elif isinstance(node, ast.Attribute) and ast.unparse(node) in chain_renames:
				edits.append((smap.start(node), smap.end(node), chain_renames[ast.unparse(node)]))
	deduped = list(dict.fromkeys(edits))
	result = apply_offset_edits(text, _drop_nested(deduped))
	for lineno, bound, asname, parent_new, leaf_new in sorted(repackage, reverse=True):
		result = remove_names(result, {bound}, only_line=lineno)
		result = (
			add_from_import(result, parent_new, [(leaf_new, asname)])
			if parent_new
			else add_import(result, leaf_new, asname)
		)
	return result


def _drop_nested(edits: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
	"""When an outer replacement contains an inner one (a dotted chain inside a longer chain), keep the outer."""
	kept = []
	for edit in sorted(edits, key=lambda e: (e[0], -(e[1] - e[0]))):
		if kept and edit[0] < kept[-1][1]:
			continue
		kept.append(edit)
	return kept
