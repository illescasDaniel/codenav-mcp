"""AST and token inventory for workspace unused-symbol analysis.

This inventory deliberately makes no claim that a candidate is unused: callers
must still resolve references and account for dynamic uses and framework hooks.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from codenav_mcp.deps import iter_python_files


@dataclass(frozen=True)
class Candidate:
	path: str
	name: str
	kind: str
	line: int
	column: int


def inventory(workspace: Path) -> tuple[list[Candidate], Counter[str]]:
	"""Collect module-level definitions and methods, and count identifier tokens."""
	candidates: list[Candidate] = []
	counts: Counter[str] = Counter()
	for path in iter_python_files(workspace):
		try:
			with tokenize.open(path) as source:
				text = source.read()
			tree = ast.parse(text, filename=str(path))
		except (OSError, UnicodeError, SyntaxError, LookupError):
			continue
		try:
			counts.update(
				t.string for t in tokenize.generate_tokens(io.StringIO(text).readline) if t.type == tokenize.NAME
			)
		except (tokenize.TokenError, IndentationError):
			continue
		rel = path.relative_to(workspace).as_posix()

		def add(node: ast.AST, name: str, kind: str, rel: str = rel) -> None:
			candidates.append(Candidate(rel, name, kind, node.lineno, node.col_offset))  # type: ignore[attr-defined]

		for node in tree.body:
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
				add(node, node.name, "class" if isinstance(node, ast.ClassDef) else "function")
				if isinstance(node, ast.ClassDef):
					for member in node.body:
						if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
							add(member, member.name, "method")
			elif isinstance(node, (ast.Assign, ast.AnnAssign)):
				targets = node.targets if isinstance(node, ast.Assign) else [node.target]
				for target in targets:
					if isinstance(target, ast.Name) and target.id.isupper():
						add(target, target.id, "constant")
	return candidates, counts
