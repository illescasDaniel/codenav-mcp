"""Refactor fuzzer: random seeded refactors through codenav on real projects, checked against the project's own
test suite and `ty check` (see docs/hardening.md, section "Refactor fuzzer").

	uv run python -m hardening.fuzz_refactors --seed 1 --count 70 --out /tmp/cnav-h1-out
	uv run python -m hardening.fuzz_refactors --setup            # clone/prepare the OSS corpus projects

Each case: reset the project, start a fresh codenav MCP stdio server on it, apply one refactor, run the oracle
(ty + tests, compared with a baseline), apply the reverse rename where it applies, check the invariants:

	I1 no silent breakage        I2 round trip        I3 containment        I4 delete safety        I5 reply format
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from codenav_mcp.edit_state import STATE_DIR_ENV
from codenav_mcp.ty_command import resolve_ty_command
from hardening.fuzz_checks import (
	OPS,
	SKIP_DIRS,
	Diag,
	Project,
	Reply,
	Symbol,
	attribute_failures,
	check_reply_format,
	discover_symbols,
	fresh_name,
	junit_test_file,
	load_corpus,
	new_diagnostics,
	parse_junit,
	parse_junit_frames,
	parse_reply,
	parse_ty_output,
	pick_op,
	pick_symbol,
)


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
CORPUS_FILE = HERE / "corpus.toml"
INVARIANTS = ("I1", "I2", "I3", "I4", "I5")
DEFAULT_PROJECTS = ("srxy", "click", "tomlkit")
PROJECT_WEIGHTS = {"srxy": 4, "click": 3, "tomlkit": 3}
TEST_TIMEOUT = 600


def server_command() -> list[str]:
	script = Path(sys.executable).parent / "codenav-mcp"
	return [str(script)] if script.is_file() else [sys.executable, "-m", "codenav_mcp"]


def clean_env(**extra: str) -> dict[str, str]:
	env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT")}
	env["PYTHONDONTWRITEBYTECODE"] = "1"
	env.update(extra)
	return env


# --- project state ----------------------------------------------------------------------------------------------


def git(project: Project, *args: str) -> str:
	done = subprocess.run(["git", "-C", str(project.path), *args], capture_output=True, text=True, check=False)
	return done.stdout


def reset(project: Project) -> None:
	git(project, "checkout", "--", ".")
	git(project, "clean", "-fdq")


def dirty(project: Project) -> list[str]:
	return [ln for ln in git(project, "status", "--porcelain").splitlines() if ln.strip()]


def stat_snapshot(project: Project) -> dict[str, tuple[int, int]]:
	snap: dict[str, tuple[int, int]] = {}
	root = str(project.path)
	for dirpath, dirnames, filenames in os.walk(root):
		dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
		for name in filenames:
			full = os.path.join(dirpath, name)
			try:
				st = os.stat(full, follow_symlinks=False)
			except OSError:
				continue
			snap[os.path.relpath(full, root).replace(os.sep, "/")] = (st.st_size, st.st_mtime_ns)
	return snap


def touched_files(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> list[str]:
	return sorted({p for p in after if before.get(p) != after[p]} | {p for p in before if p not in after})


@dataclass
class Baseline:
	diags: list[Diag]
	failed: set[tuple[str, str]]
	flaky: set[tuple[str, str]]
	passed: int
	total: int
	test_seconds: float
	ty_seconds: float


def run_ty(project: Project) -> tuple[list[Diag], float]:
	ty = resolve_ty_command(project.path)[0]
	t0 = time.time()
	done = subprocess.run(
		[ty, "check", "--output-format", "concise", "--no-progress"],
		cwd=project.path,
		env=clean_env(),
		capture_output=True,
		text=True,
		check=False,
		timeout=300,
	)
	return parse_ty_output(done.stdout + done.stderr), time.time() - t0


LAST_FRAMES: dict[tuple[str, str], list[tuple[str, int]]] = {}


def run_tests(project: Project, tmp: Path) -> tuple[set[tuple[str, str]], int, int, float, str]:
	junit = tmp / "junit.xml"
	junit.unlink(missing_ok=True)
	python = project.path / ".venv" / "bin" / "python"
	cmd = [str(python), "-m", "pytest", *project.test_args, "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"]
	t0 = time.time()
	try:
		done = subprocess.run(
			cmd, cwd=project.path, env=clean_env(), capture_output=True, text=True, timeout=TEST_TIMEOUT, check=False
		)
		tail = (done.stdout.strip().splitlines() or [""])[-1]
	except subprocess.TimeoutExpired:
		return {("timeout", "tests")}, 0, 0, time.time() - t0, "timeout"
	secs = time.time() - t0
	if not junit.is_file():
		return {("no-junit", "tests")}, 0, 0, secs, tail
	failed, passed, total = parse_junit(junit)
	LAST_FRAMES.clear()
	LAST_FRAMES.update(parse_junit_frames(junit))
	return failed, passed, total, secs, tail


def take_baseline(project: Project, tmp: Path) -> Baseline:
	reset(project)
	diags, ty_secs = run_ty(project)
	f1, passed, total, secs1, tail = run_tests(project, tmp)
	f2, _p2, _t2, secs2, _ = run_tests(project, tmp)
	log(
		f"[{project.name}] baseline: {len(diags)} ty errors ({ty_secs:.1f}s); tests: {tail} ({secs1:.1f}s); flaky={len(f1 ^ f2)}"
	)
	if project.baseline_passed and passed != project.baseline_passed:
		log(f"[{project.name}] WARNING: expected {project.baseline_passed} passed, got {passed}")
	return Baseline(diags, f1 | f2, f1 ^ f2, passed, total, (secs1 + secs2) / 2, ty_secs)


def log(msg: str) -> None:
	print(msg, file=sys.stderr, flush=True)


# --- MCP session ------------------------------------------------------------------------------------------------


class Session:
	def __init__(self, client: Client, schemas: dict[str, set[str]]) -> None:
		self.client = client
		self.schemas = schemas
		self.calls: list[dict[str, Any]] = []

	async def call(self, tool: str, args: dict[str, Any]) -> tuple[Reply, float]:
		t0 = time.time()
		result = await self.client.call_tool(tool, args)
		secs = time.time() - t0
		text = "\n".join(getattr(b, "text", "") for b in result.content)
		is_error = bool(getattr(result, "is_error", False))
		self.calls.append({"tool": tool, "args": args, "is_error": is_error, "secs": round(secs, 2), "reply": text})
		return parse_reply(text, is_error), secs


@asynccontextmanager
async def open_session(project: Project, state_dir: Path) -> AsyncIterator[Session]:
	cmd = server_command()
	env = {
		"PATH": os.environ.get("PATH", ""),
		"HOME": os.environ.get("HOME", ""),
		"CODENAV_MCP_WORKSPACE": str(project.path),
		STATE_DIR_ENV: str(state_dir),
	}
	params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=env, cwd=str(project.path))
	async with Client(params, mode="legacy", read_timeout_seconds=600) as client:
		tools = (await client.list_tools()).tools
		schemas = {t.name: set((t.input_schema or {}).get("properties", {})) for t in tools}
		yield Session(client, schemas)


# --- cases ------------------------------------------------------------------------------------------------------


@dataclass
class CaseResult:
	id: str
	project: str
	commit: str
	op: str
	symbol: str
	file: str
	call: dict[str, Any]
	status: str = "skipped"  # applied | held | error | skipped
	checks: dict[str, str] = field(default_factory=dict)  # invariant -> pass | fail | n/a
	notes: list[str] = field(default_factory=list)  # human-readable reasons, failures first
	new_ty: list[str] = field(default_factory=list)
	new_failures: list[str] = field(default_factory=list)
	over_rename: list[str] = field(default_factory=list)
	touched: list[str] = field(default_factory=list)
	secs: dict[str, float] = field(default_factory=dict)
	replies: list[dict[str, Any]] = field(default_factory=list)

	def mark(self, inv: str, ok: bool | None, why: str = "") -> None:
		self.checks[inv] = "n/a" if ok is None else "pass" if ok else "fail"
		if ok is False:
			self.notes.insert(0, f"{inv}: {why}")


_NOT_PROJECT = {"venv", "node_modules", "build", "dist", "site-packages", "__pycache__"}


def allowed_path(project: Project, path: str, reply_text: str, allow_listed: bool) -> bool:
	parts = path.split("/")
	if any(p.startswith(".") for p in parts):
		return False
	if path.endswith(".py") and not any(p in _NOT_PROJECT for p in parts[:-1]):
		# any project Python file (examples/, scripts/ too): ty resolves references there, and a wrong edit shows
		# up as I1 breakage; containment is about non-project and non-Python files
		return True
	return allow_listed and path in reply_text


def _read_text(path: Path) -> str | None:
	try:
		return path.read_text(encoding="utf-8")
	except (OSError, UnicodeError):
		return None


async def run_oracle(
	project: Project, base: Baseline, tmp: Path, renames: dict[str, str]
) -> tuple[list[Diag], list[tuple[str, str]], dict[str, float]]:
	diags, ty_secs = await asyncio.to_thread(run_ty, project)
	failed, _passed, _total, test_secs, tail = await asyncio.to_thread(run_tests, project, tmp)
	new_diags = new_diagnostics(base.diags, diags, renames)
	new_failed = sorted((failed - base.failed) - base.flaky)
	return new_diags, new_failed, {"ty": round(ty_secs, 2), "tests": round(test_secs, 2)}


def build_call(op: str, sym: Symbol, index: int, seed: int) -> tuple[str, dict[str, Any]]:
	if op in ("rename", "rename_all"):
		args: dict[str, Any] = {
			"name": sym.name,
			"new_name": fresh_name(sym.name, seed * 1000 + index),
			"file_path": sym.file,
		}
		if op == "rename_all":
			args["sites"] = ["all"]
		return "rename", args
	if op == "change_signature":
		return "change_signature", {
			"name": sym.name,
			"file_path": sym.file,
			"add": [{"name": "fz_extra", "default": "None"}],
		}
	if op == "move":
		target = f"{Path(sym.file).parent.as_posix()}/fz_moved_{seed * 1000 + index}.py"
		return "move", {"name": sym.name, "file_path": sym.file, "to_file": target, "keep_reexport": True}
	return "edit_symbol", {"name": sym.name, "file_path": sym.file, "action": "delete"}


async def run_case(
	project: Project, base: Baseline, sym: Symbol, op: str, index: int, seed: int, state_dir: Path, tmp: Path
) -> CaseResult:
	tool, args = build_call(op, sym, index, seed)
	res = CaseResult(
		f"{project.name}-{seed}-{index}", project.name, project.commit, op, sym.name, sym.file, {"tool": tool, **args}
	)
	t_case = time.time()
	reset(project)
	before = stat_snapshot(project)
	async with open_session(project, state_dir) as sess:
		reply, secs = await sess.call(tool, args)
		if tool == "move" and reply.is_error and "allow_import_cycle" in reply.text and "import cycle" in reply.text:
			# moving into a fresh module with keep_reexport always cycles when the body uses its old module;
			# take the documented escape hatch so the move itself still gets exercised
			args = {**args, "allow_import_cycle": True}
			res.call = {"tool": tool, **args}
			res.notes.append("first move attempt refused for an import cycle; retried with allow_import_cycle=true")
			reply, secs = await sess.call(tool, args)
		res.secs["tool"] = round(secs, 2)
		res.status = "applied" if reply.applied else "error" if reply.is_error else "held"
		touched = touched_files(before, stat_snapshot(project))
		res.touched = touched

		# I3: containment (also: a held/errored call writes nothing)
		is_all = op == "rename_all"
		bad = [p for p in touched if not allowed_path(project, p, reply.text, allow_listed=is_all)]
		if reply.applied:
			res.mark("I3", not bad, f"edits outside source/test roots or in non-Python files: {bad[:5]}")
		else:
			res.mark("I3", not touched, f"{res.status} call wrote files: {touched[:5]}")

		if reply.applied:
			renames = {fresh_name(sym.name, seed * 1000 + index): sym.tail} if tool == "rename" else {}
			new_diags, new_failed, osecs = await run_oracle(project, base, tmp, renames)
			res.secs.update(osecs)
			res.new_ty = [f"{d.file}:{d.line} [{d.rule}] {d.msg[:140]}" for d in new_diags[:10]]
			res.new_failures = [f"{c}::{n}" if c else n for c, n in new_failed[:20]]
			failures = {f"ty {d.file}:{d.line} {d.msg[:100]}": d.file for d in new_diags}
			failures |= {f"test {c}::{n}": junit_test_file(c, n, project.path) for c, n in new_failed}
			frames = {f"test {c}::{n}": LAST_FRAMES.get((c, n), []) for c, n in new_failed}
			frames |= {f"ty {d.file}:{d.line} {d.msg[:100]}": [(d.file, d.line)] for d in new_diags}
			if args.get("allow_import_cycle") and failures:
				# the agent forced a move that cycles: breakage is the documented price, reported not counted as I1
				res.over_rename = list(failures)
				res.mark("I1", None)
				res.mark("I4", None)
			elif op == "delete":
				res.mark("I4", not failures, f"deleting unused-labelled {sym.name} broke: {list(failures)[:5]}")
				res.mark("I1", None)
			else:
				attr = attribute_failures(failures, reply, frames, read=lambda rel: _read_text(project.path / rel))
				if is_all and reply.optional_applied:
					# sites=["all"] opted in unchecked sites (e.g. `args.name` on an argparse namespace): a failure
					# there is over-rename damage the agent asked for, reported but not an I1 violation
					damage = list(failures)
					res.over_rename = damage
					attr.silent, attr.unlisted = [], []
					attr.traced += damage
				elif is_all:
					damage = [d for d in attr.silent + attr.unlisted if failures[d] in touched]
					res.over_rename = damage
					attr.silent = [d for d in attr.silent if d not in damage]
					attr.unlisted = [d for d in attr.unlisted if d not in damage]
					attr.traced += damage
				res.mark(
					"I1",
					attr.ok,
					f"new failures not traced to a listed site: unlisted={attr.unlisted[:5]} silent={attr.silent[:5]}",
				)
				res.mark("I4", None)
		else:
			res.mark("I1", None)
			res.mark("I4", None)

		# I2: round trip (rename only)
		if reply.applied and tool == "rename":
			reverse_name = f"{sym.name.rsplit('.', 1)[0]}.{args['new_name']}" if "." in sym.name else args["new_name"]
			back: dict[str, Any] = {"name": reverse_name, "new_name": sym.tail, "file_path": sym.file}
			if is_all:
				back["sites"] = ["all"]
			rreply, rsecs = await sess.call("rename", back)
			res.secs["reverse"] = round(rsecs, 2)
			left = dirty(project)
			res.mark(
				"I2",
				rreply.applied and not left,
				f"reverse rename {'failed: ' + rreply.text.splitlines()[0][:120] if not rreply.applied else 'left differences: ' + str(left[:6])}",
			)
		else:
			res.mark("I2", None)

		# I5: every reply of the case
		problems: list[str] = []
		for c in sess.calls:
			for p in check_reply_format(c["reply"], c["is_error"], sess.schemas):
				problems.append(f"{c['tool']}: {p}")
		res.mark("I5", not problems, "; ".join(problems[:4]))
		res.replies = [
			{"tool": c["tool"], "args": c["args"], "is_error": c["is_error"], "reply": c["reply"][:3000]}
			for c in sess.calls
		]
		if reply.is_error:
			res.notes.append(f"refused: {reply.text.splitlines()[0][:160] if reply.text else ''}")
	reset(project)
	res.secs["total"] = round(time.time() - t_case, 2)
	return res


async def collect_unused(project: Project, state_dir: Path) -> list[Symbol]:
	"""Symbols `unused` labels exactly `unused` (not `only in tests`/`only in strings`/`exported`) under src roots."""
	import re

	pattern = re.compile(r"^(?P<file>\S+?):(?P<line>\d+)\s+(?P<name>\S+) \((?P<kind>\w+), (?P<label>[^)]+)\)$")
	found: list[Symbol] = []
	reset(project)
	async with open_session(project, state_dir) as sess:
		offset = 0
		while offset < 1000:
			reply, _ = await sess.call("unused", {"limit": 100, "offset": offset})
			rows = [pattern.match(ln) for ln in reply.text.splitlines()]
			rows = [m for m in rows if m]
			for m in rows:
				file = m.group("file")
				in_src = any(file.startswith(r.rstrip("/") + "/") for r in project.src_roots)
				if (
					m.group("label") == "unused"
					and in_src
					and m.group("kind") in ("function", "class", "method", "constant")
				):
					found.append(Symbol(m.group("name"), file, int(m.group("line")), m.group("kind")))
			if "next: unused(" not in reply.text or not rows:
				break
			offset += 100
	reset(project)
	return found


# --- driver -----------------------------------------------------------------------------------------------------


def plan_counts(projects: list[str], total: int) -> dict[str, int]:
	weights = {p: PROJECT_WEIGHTS.get(p, 1) for p in projects}
	whole = sum(weights.values())
	counts = {p: total * w // whole for p, w in weights.items()}
	for p in sorted(projects, key=lambda p: -weights[p])[: total - sum(counts.values())]:
		counts[p] += 1
	return counts


async def fuzz(args: argparse.Namespace) -> list[CaseResult]:
	corpus = load_corpus(CORPUS_FILE)
	names = [n.strip() for n in args.projects.split(",") if n.strip()]
	ops_allowed = [o for o in args.ops.split(",") if o] if args.ops else list(OPS)
	results: list[CaseResult] = []
	out = Path(args.out)
	out.mkdir(parents=True, exist_ok=True)
	jsonl = out / f"results-seed{args.seed}.jsonl"
	jsonl.write_text("", encoding="utf-8")
	with tempfile.TemporaryDirectory(prefix="cnav-fuzz-") as tmp_name:
		tmp = Path(tmp_name)
		counts = plan_counts(names, args.count)
		for name in names:
			project = corpus[name]
			if not (project.path / ".venv").is_dir():
				log(f"[{name}] skipped: no .venv at {project.path} (run --setup)")
				continue
			actual = git(project, "rev-parse", "HEAD").strip()
			if not actual.startswith(project.commit[:7]):
				log(f"[{name}] WARNING: HEAD is {actual[:10]}, corpus.toml pins {project.commit[:10]}")
			base = take_baseline(project, tmp)
			symbols = discover_symbols(project.path, project.src_roots)
			unused_syms = await collect_unused(project, tmp / f"state-{name}-unused") if "delete" in ops_allowed else []
			log(f"[{name}] {len(symbols)} symbols, {len(unused_syms)} unused-labelled; {counts[name]} cases")
			for i in range(counts[name]):
				rng = random.Random(f"{args.seed}/{name}/{i}")
				op = pick_op(rng)
				while op not in ops_allowed:
					op = pick_op(rng)
				sym = (
					rng.choice(unused_syms)
					if op == "delete" and unused_syms
					else pick_symbol(rng, symbols, "rename" if op == "delete" else op)
				)
				if sym is None:
					continue
				try:
					res = await run_case(project, base, sym, op, i, args.seed, tmp / f"state-{name}-{i}", tmp)
				except Exception as exc:  # a crash of the harness or the server is itself reportable
					reset(project)
					res = CaseResult(f"{name}-{args.seed}-{i}", name, project.commit, op, sym.name, sym.file, {})
					res.status = "crash"
					res.notes.append(f"harness/server exception: {type(exc).__name__}: {exc}"[:400])
					res.checks["I5"] = "fail"
				results.append(res)
				with jsonl.open("a", encoding="utf-8") as fh:
					fh.write(json.dumps(asdict(res)) + "\n")
				verdict = (
					"ok"
					if "fail" not in res.checks.values() and res.status != "crash"
					else "FAIL " + "; ".join(res.notes[:2])[:200]
				)
				log(f"  {res.id} {op:16s} {sym.name} -> {res.status} {res.secs.get('total', 0):.1f}s {verdict}")
			reset(project)
	return results


def summarize(results: list[CaseResult]) -> str:
	lines = [f"# Refactor fuzzer summary ({len(results)} cases)", ""]
	lines.append("| invariant | pass | fail | n/a |")
	lines.append("|---|---|---|---|")
	for inv in INVARIANTS:
		c = Counter(r.checks.get(inv, "n/a") for r in results)
		lines.append(f"| {inv} | {c['pass']} | {c['fail']} | {c['n/a']} |")
	lines += ["", "| op | cases | applied | held | error | crash | median s |", "|---|---|---|---|---|---|---|"]
	for op in OPS:
		rs = [r for r in results if r.op == op]
		if rs:
			st = Counter(r.status for r in rs)
			med = statistics.median(r.secs.get("total", 0) for r in rs)
			lines.append(
				f"| {op} | {len(rs)} | {st['applied']} | {st['held']} | {st['error']} | {st['crash']} | {med:.1f} |"
			)
	lines += ["", "| project | cases | mean s/case | max s |", "|---|---|---|---|"]
	for name in sorted({r.project for r in results}):
		rs = [r.secs.get("total", 0) for r in results if r.project == name]
		lines.append(f"| {name} | {len(rs)} | {statistics.mean(rs):.1f} | {max(rs):.1f} |")
	fails = [r for r in results if "fail" in r.checks.values() or r.status == "crash"]
	lines += ["", f"## Failing cases ({len(fails)})", ""]
	for r in fails:
		lines.append(f"- `{r.id}` {r.op} `{r.symbol}` ({r.file}) -> {'; '.join(r.notes[:3])[:300]}")
	damage = [r for r in results if r.over_rename]
	lines += ["", f"## Over-rename damage at opted-in sites, not I1 ({len(damage)})", ""]
	for r in damage:
		lines.append(f"- `{r.id}` {r.symbol}: {r.over_rename[:3]}")
	return "\n".join(lines) + "\n"


def setup_corpus() -> None:
	corpus = load_corpus(CORPUS_FILE)
	for project in corpus.values():
		if not project.path.exists():
			if not project.repo:
				log(f"[{project.name}] missing and no repo URL; skip")
				continue
			project.path.parent.mkdir(parents=True, exist_ok=True)
			subprocess.run(["git", "clone", "-q", project.repo, str(project.path)], check=True)
			subprocess.run(["git", "-C", str(project.path), "checkout", "-q", project.commit], check=True)
		if project.setup and not (project.path / ".venv").is_dir():
			subprocess.run(project.setup, shell=True, cwd=project.path, check=True, env=clean_env())
		log(f"[{project.name}] ready at {project.path}")


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("--seed", type=int, default=1)
	ap.add_argument("--count", type=int, default=70, help="total number of refactors across the projects")
	ap.add_argument("--projects", default=",".join(DEFAULT_PROJECTS))
	ap.add_argument("--ops", default="", help=f"comma-separated subset of {','.join(OPS)}")
	ap.add_argument("--out", default="/tmp/cnav-h1-out")
	ap.add_argument("--setup", action="store_true", help="clone/prepare the corpus projects and exit")
	args = ap.parse_args(argv)
	if args.setup:
		setup_corpus()
		return 0
	t0 = time.time()
	results = asyncio.run(fuzz(args))
	summary = summarize(results)
	Path(args.out, f"summary-seed{args.seed}.md").write_text(summary, encoding="utf-8")
	print(summary)
	print(f"wall time {time.time() - t0:.0f}s", file=sys.stderr)
	failed = any("fail" in r.checks.values() or r.status == "crash" for r in results)
	return 1 if failed else 0


if __name__ == "__main__":
	if shutil.which("git") is None:
		sys.exit("git is required")
	sys.exit(main())
