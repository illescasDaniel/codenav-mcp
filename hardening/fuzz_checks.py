"""Pure helpers of the refactor fuzzer: symbol discovery, reply parsing, oracle parsing and the invariant checks.

Nothing here talks to a language server or runs a subprocess, so the module is unit-tested in the default suite
(tests/test_fuzz_support.py)."""

from __future__ import annotations

import ast
import random
import re
import tomllib
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


TOOL_NAMES = frozenset(
	{
		"lookup",
		"search",
		"outline",
		"check",
		"unused",
		"impact",
		"rename",
		"change_signature",
		"move",
		"edit_symbol",
		"fix",
		"edits",
	}
)

# Directories whose content is never compared (tool caches, virtualenvs, VCS metadata).
SKIP_DIRS = frozenset(
	{
		".git",
		".venv",
		"venv",
		"__pycache__",
		".pytest_cache",
		".ruff_cache",
		".mypy_cache",
		".tox",
		".nox",
		"node_modules",
	}
)


@dataclass(frozen=True)
class Project:
	name: str
	path: Path
	commit: str
	src_roots: tuple[str, ...]
	test_roots: tuple[str, ...]
	test_args: tuple[str, ...]
	repo: str = ""
	setup: str = ""
	baseline_passed: int = 0


def load_corpus(path: Path) -> dict[str, Project]:
	data = tomllib.loads(path.read_text(encoding="utf-8"))
	projects: dict[str, Project] = {}
	for entry in data["project"]:
		projects[entry["name"]] = Project(
			name=entry["name"],
			path=Path(entry["path"]),
			commit=entry["commit"],
			src_roots=tuple(entry["src_roots"]),
			test_roots=tuple(entry.get("test_roots", ())),
			test_args=tuple(entry.get("test_args", ())),
			repo=entry.get("repo", ""),
			setup=entry.get("setup", ""),
			baseline_passed=int(entry.get("baseline_passed", 0)),
		)
	return projects


# --- symbol discovery -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Symbol:
	"""A definition the fuzzer may refactor. `name` is what the tools take (`func`, `Class.member`)."""

	name: str
	file: str  # workspace-relative, posix
	line: int
	category: str  # function | method | class | constant | field | protocol_member

	@property
	def tail(self) -> str:
		return self.name.rsplit(".", 1)[-1]


def _decorator_names(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
	names: set[str] = set()
	for dec in node.decorator_list:
		target = dec.func if isinstance(dec, ast.Call) else dec
		if isinstance(target, ast.Attribute):
			names.add(target.attr)
		elif isinstance(target, ast.Name):
			names.add(target.id)
	return names


def _base_names(node: ast.ClassDef) -> set[str]:
	names: set[str] = set()
	for base in node.bases:
		target = base.value if isinstance(base, ast.Subscript) else base
		if isinstance(target, ast.Attribute):
			names.add(target.attr)
		elif isinstance(target, ast.Name):
			names.add(target.id)
	return names


def _duplicates(names: list[str]) -> set[str]:
	return {name for name, count in Counter(names).items() if count > 1}


def discover_symbols(project_root: Path, src_roots: tuple[str, ...]) -> list[Symbol]:
	"""Functions, methods (Protocol/ABC members flagged separately), classes, module constants and dataclass fields
	defined under the source roots. Names defined twice in one scope (overloads, property setters) are skipped."""
	parsed: list[tuple[str, ast.Module]] = []
	for root in src_roots:
		for path in sorted((project_root / root).rglob("*.py")):
			if any(part in SKIP_DIRS for part in path.relative_to(project_root).parts):
				continue
			try:
				tree = ast.parse(path.read_text(encoding="utf-8"))
			except (SyntaxError, UnicodeDecodeError, ValueError):
				continue
			parsed.append((path.relative_to(project_root).as_posix(), tree))
	# Classes used as a base somewhere in the project: their members are "protocol-like" when they are
	# Protocol/ABC classes that something else subclasses.
	subclassed: set[str] = set()
	for _rel, tree in parsed:
		for node in ast.walk(tree):
			if isinstance(node, ast.ClassDef):
				subclassed |= _base_names(node)
	symbols: list[Symbol] = []
	for rel, tree in parsed:
		top_names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
		dup_top = _duplicates(top_names)
		for node in tree.body:
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				if node.name not in dup_top and _usable(node.name):
					symbols.append(Symbol(node.name, rel, node.lineno, "function"))
			elif isinstance(node, ast.ClassDef):
				if node.name in dup_top or not _usable(node.name):
					continue
				symbols.append(Symbol(node.name, rel, node.lineno, "class"))
				symbols.extend(_class_members(node, rel, subclassed))
			elif isinstance(node, (ast.Assign, ast.AnnAssign)):
				for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
					if isinstance(target, ast.Name) and _usable(target.id) and target.id != "__all__":
						symbols.append(Symbol(target.id, rel, node.lineno, "constant"))
		# a module-level name assigned more than once is ambiguous for the tools
		counts = Counter(s.name for s in symbols if s.file == rel and s.category == "constant")
		symbols = [s for s in symbols if not (s.file == rel and s.category == "constant" and counts[s.name] > 1)]
	return symbols


def _usable(name: str) -> bool:
	return len(name) > 1 and not (name.startswith("__") and name.endswith("__"))


def _class_members(node: ast.ClassDef, rel: str, subclassed: set[str]) -> list[Symbol]:
	members: list[Symbol] = []
	method_names = [m.name for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
	dup = _duplicates(method_names)
	is_dataclass = "dataclass" in _decorator_names(node) or "NamedTuple" in _base_names(node)
	is_protocol = bool({"Protocol", "ABC"} & _base_names(node)) and node.name in subclassed
	for item in node.body:
		if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
			if item.name in dup or not _usable(item.name):
				continue
			category = "protocol_member" if is_protocol else "method"
			members.append(Symbol(f"{node.name}.{item.name}", rel, item.lineno, category))
		elif isinstance(item, ast.AnnAssign) and is_dataclass and isinstance(item.target, ast.Name):
			if _usable(item.target.id):
				members.append(Symbol(f"{node.name}.{item.target.id}", rel, item.lineno, "field"))
	return members


# --- reply parsing ---------------------------------------------------------------------------------------------

_SITE_RE = re.compile(r"^\s+u(\d+)\s+(?P<path>\S+?):(?P<line>\d+)\s+(?P<kind>\S+)")
_NEXT_RE = re.compile(r"^next: (?P<call>.+)$")
_RETRY_RE = re.compile(r"^\s+(?P<call>\w+\(.*\))\s+\[[^\]]*\]\s*$")
_EXAMPLE_RE = re.compile(r"^\s*Example: `?(?P<call>\w+\(.*?)`?\s*$")
_UNLISTED_RE = re.compile(r"(\d+) [^\n]*?(?:could not be checked and are not listed|not listed)")
_OPTIONAL_COUNT_RE = re.compile(r"\(\+(\d+) optional sites?\)")
_TOOL_ADVICE_RE = re.compile(r"\b(?:with|use|call|run|try)\s+`(\w+)`")
# Tool names of codenav 1.x that no longer exist: a reply telling the agent to use one sends it nowhere.
REMOVED_TOOLS = frozenset(
	{
		"edit",
		"apply_edit",
		"undo_edit",
		"quick_fix",
		"symbol_info",
		"search_symbol",
		"rename_symbol",
		"verify_changes",
		"batch",
		"hover",
		"definition",
		"references",
		"callers",
		"diagnostics",
		"workspace",
	}
)
_HINT_RE = re.compile(r"pass \w+=|Retry with|Example|rerun|next:|use `\w+`|with `\w+`")
_EDIT_ID_RE = re.compile(r"Applied as edit (?P<id>[0-9a-f]+)")


@dataclass
class Reply:
	text: str
	is_error: bool
	applied: bool = False
	held: bool = False
	optional_sites: list[tuple[str, int, str]] = field(default_factory=list)  # (path, line, kind)
	unlisted_note: bool = False  # the reply says some sites "could not be checked / are not listed"
	manual_attention: bool = False  # "needs manual attention" / "not updated" style notes
	edit_id: str | None = None
	optional_applied: int = 0  # "(+N optional sites)": sites=["all"] opted these in

	@property
	def listed_files(self) -> set[str]:
		files = {path for path, _line, _kind in self.optional_sites}
		files |= set(re.findall(r"[\w./-]+\.(?:py|md|toml|txt|json|rst|cfg|ini|ya?ml)", self.text))
		return files


def parse_reply(text: str, is_error: bool) -> Reply:
	reply = Reply(text=text, is_error=is_error)
	reply.applied = bool(_EDIT_ID_RE.search(text))
	m = _EDIT_ID_RE.search(text)
	reply.edit_id = m.group("id") if m else None
	lowered = text.lower()
	reply.held = (not reply.applied) and (
		"not applied" in lowered or "preview only" in lowered or "held back" in lowered
	)
	m_opt = _OPTIONAL_COUNT_RE.search(text)
	reply.optional_applied = int(m_opt.group(1)) if m_opt else 0
	reply.manual_attention = (
		"manual attention" in lowered or "update by hand" in lowered or "update manually" in lowered
	)
	for line in text.splitlines():
		site = _SITE_RE.match(line)
		if site:
			reply.optional_sites.append((site.group("path"), int(site.group("line")), site.group("kind")))
	reply.unlisted_note = bool(_UNLISTED_RE.search(text))
	return reply


# --- I5: reply format ------------------------------------------------------------------------------------------


def validate_call(call: str, schemas: dict[str, set[str]]) -> str | None:
	"""None when `call` is a syntactically valid call of an existing tool with known keywords and literal values."""
	try:
		node = ast.parse(call.strip(), mode="eval").body
	except SyntaxError as exc:
		return f"does not parse: {exc.msg}"
	if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
		return "not a plain function call"
	if node.func.id not in TOOL_NAMES:
		return f"unknown tool {node.func.id!r}"
	if node.args:
		return "positional arguments (tools take keywords only)"
	known = schemas.get(node.func.id)
	for kw in node.keywords:
		if kw.arg is None:
			return "**kwargs in a call"
		if known is not None and kw.arg not in known:
			return f"{node.func.id} has no parameter {kw.arg!r}"
		try:
			ast.literal_eval(kw.value)
		except ValueError:
			return f"value of {kw.arg} is not a literal"
	return None


def check_reply_format(text: str, is_error: bool, schemas: dict[str, set[str]]) -> list[str]:
	"""Violations of the v2 reply format (docs/v2-design.md, "Output conventions")."""
	problems: list[str] = []
	lines = text.splitlines()
	first = next((ln for ln in lines if ln.strip()), "")
	if not first:
		return ["empty reply"]
	if first.startswith((" ", "\t", "-", "next:", "Notes:")):
		problems.append(f"first line is not the answer line: {first[:80]!r}")
	if len(first) > 400:
		problems.append("answer line longer than 400 chars")
	if "Traceback (most recent call last)" in text or re.search(r"\b\w+Error: ", first):
		problems.append("raw exception text in the reply")
	if "\x1b[" in text:
		problems.append("ANSI escape codes in the reply")
	calls: list[str] = []
	for line in lines:
		m = _NEXT_RE.match(line)
		if m:
			calls.append(m.group("call"))
			continue
		m = _RETRY_RE.match(line)
		if m:
			calls.append(m.group("call"))
	for call in calls:
		bad = validate_call(call, schemas)
		if bad:
			problems.append(f"next/retry call {call[:100]!r}: {bad}")
	for advice in _TOOL_ADVICE_RE.findall(text):
		if advice in REMOVED_TOOLS:
			problems.append(f"reply points at the removed tool `{advice}`")
	if is_error and not calls and not _HINT_RE.search(text):
		problems.append("error reply without a corrected call, Example line or `pass x=` hint")
	problems.extend(_retry_section_problems(lines, schemas))
	return problems


def _retry_section_problems(lines: list[str], schemas: dict[str, set[str]]) -> list[str]:
	"""Entries listed under "Retry with one of" must be calls (copy-pastable), not bare locations."""
	problems: list[str] = []
	in_section = False
	for line in lines:
		if "Retry with" in line:
			in_section = True
			continue
		if not in_section:
			continue
		if not line.strip():
			in_section = False
		elif line.strip().startswith("("):
			continue
		elif line.startswith((" ", "\t")):
			text = re.sub(r"\s+\[[^\]]*\]\s*$", "", line).strip()
			bad = validate_call(text, schemas)
			if bad:
				problems.append(f"retry entry {text[:80]!r} is not a call ({bad})")
		else:
			in_section = False
	return problems


# --- oracle parsing --------------------------------------------------------------------------------------------

_TY_RE = re.compile(
	r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+): (?P<sev>error|warning)\[(?P<rule>[^\]]+)\] (?P<msg>.*)$"
)


@dataclass(frozen=True)
class Diag:
	file: str
	line: int
	rule: str
	msg: str

	@property
	def key(self) -> tuple[str, str]:
		return (self.rule, self.msg)


def parse_ty_output(text: str) -> list[Diag]:
	diags: list[Diag] = []
	for line in text.splitlines():
		m = _TY_RE.match(line)
		if m and m.group("sev") == "error":
			diags.append(Diag(m.group("file"), int(m.group("line")), m.group("rule"), m.group("msg")))
	return diags


def new_diagnostics(before: list[Diag], after: list[Diag], renames: dict[str, str] | None = None) -> list[Diag]:
	"""Diagnostics in `after` beyond the multiset in `before`, ignoring file and line (a refactor shifts both).
	`renames` maps new identifiers back to the old ones so a pre-existing error that merely quotes a renamed name
	is not reported as new."""
	remaining = Counter(d.key for d in before)
	fresh: list[Diag] = []
	for diag in after:
		msg = diag.msg
		for new, old in (renames or {}).items():
			msg = msg.replace(new, old)
		key = (diag.rule, msg)
		if remaining[key] > 0:
			remaining[key] -= 1
		else:
			fresh.append(diag)
	return fresh


def parse_junit(path: Path) -> tuple[set[tuple[str, str]], int, int]:
	"""(failed test ids, passed count, total count) from a pytest --junitxml file."""
	root = ET.parse(path).getroot()
	failed: set[tuple[str, str]] = set()
	total = passed = 0
	for case in root.iter("testcase"):
		total += 1
		if case.find("failure") is not None or case.find("error") is not None:
			failed.add((case.get("classname", ""), case.get("name", "")))
		elif case.find("skipped") is None:
			passed += 1
	return failed, passed, total


_FRAME_RE = re.compile(r"([\w./-]+\.py):(\d+)")


def parse_junit_frames(path: Path) -> dict[tuple[str, str], list[tuple[str, int]]]:
	"""(file, line) frames named in the traceback of each failing testcase."""
	frames: dict[tuple[str, str], list[tuple[str, int]]] = {}
	for case in ET.parse(path).getroot().iter("testcase"):
		bad = case.find("failure") if case.find("failure") is not None else case.find("error")
		if bad is not None:
			text = (bad.get("message") or "") + "\n" + (bad.text or "")
			frames[(case.get("classname", ""), case.get("name", ""))] = [
				(f, int(n)) for f, n in _FRAME_RE.findall(text)
			]
	return frames


def junit_test_file(classname: str, name: str, root: Path) -> str:
	"""Workspace-relative file of a junit testcase (classname `tests.test_a.TestX` -> `tests/test_a.py`)."""
	if not classname and name.endswith(".py"):
		return name
	parts = classname.split(".")
	for k in range(len(parts), 0, -1):
		candidate = "/".join(parts[:k]) + ".py"
		if (root / candidate).is_file():
			return candidate
	return classname


# --- I1: attributing new failures ------------------------------------------------------------------------------


@dataclass
class Attribution:
	traced: list[str] = field(default_factory=list)
	unlisted: list[str] = field(default_factory=list)  # the reply admitted residual sites it did not list
	silent: list[str] = field(default_factory=list)  # nothing in the reply points at the failure

	@property
	def ok(self) -> bool:
		return not self.unlisted and not self.silent


def attribute_failures(
	failures: dict[str, str], reply: Reply, frames: dict[str, list[tuple[str, int]]] | None = None
) -> Attribution:
	"""`failures` maps a description to the workspace file where the failure shows (test file or ty diag file).

	A failure is traced when its file holds a listed optional site, one of its traceback frames lies within 3 lines of one, is named in the reply (held plans, manual
	attention) or the plan was held. A failure the reply only covers with a count ("N could not be checked and are
	not listed") is `unlisted`; anything else is `silent`."""
	result = Attribution()
	listed = reply.listed_files
	for desc, file in failures.items():
		near_listed = any(
			(path, line)
			for path, line in (frames or {}).get(desc, [])
			for sp, sl, _k in reply.optional_sites
			if sp == path and abs(sl - line) <= 3
		)
		if reply.held or file in listed or near_listed or reply.manual_attention and file in reply.text:
			result.traced.append(desc)
		elif reply.unlisted_note:
			result.unlisted.append(desc)
		else:
			result.silent.append(desc)
	return result


# --- seeded planning --------------------------------------------------------------------------------------------

OPS = ("rename", "rename_all", "change_signature", "move", "delete")
OP_WEIGHTS = (30, 20, 20, 15, 15)


def pick_op(rng: random.Random) -> str:
	return rng.choices(OPS, weights=OP_WEIGHTS, k=1)[0]


def pick_symbol(rng: random.Random, symbols: list[Symbol], op: str) -> Symbol | None:
	"""Category first (uniform over the categories that fit), then a symbol: rare categories stay represented."""
	if op in ("change_signature",):
		allowed = {"function", "method", "protocol_member"}
	elif op == "move":
		allowed = {"function"}
	else:
		allowed = {"function", "method", "protocol_member", "class", "constant", "field"}
	by_category: dict[str, list[Symbol]] = {}
	for sym in symbols:
		if sym.category in allowed:
			by_category.setdefault(sym.category, []).append(sym)
	if not by_category:
		return None
	category = rng.choice(sorted(by_category))
	return rng.choice(by_category[category])


def fresh_name(base: str, salt: int) -> str:
	"""An identifier that exists nowhere in the project (round trips must not collide with real names)."""
	tail = base.rsplit(".", 1)[-1].lstrip("_")
	return f"zzfuzz{salt}_{tail}" if not tail[:1].isupper() else f"ZzFuzz{salt}{tail}"
