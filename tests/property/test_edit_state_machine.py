"""Stateful test of previews, applied edits and undo (`edit_state`, `edit_ops`, `edits_tools._list`) on real files.

A model that never looks at the code (plain dicts of file texts) is driven alongside the real stores. The rules are
the things that happen to a workspace: preview, apply (with optional sites), apply an applied edit's remaining
sites, undo, someone else changing or deleting a file, a git-like reset to an older state of a file, a server
restart (stores reloaded from disk), and bursts that push the stores past their 50-entry limit.

Invariants after every step: files equal the model; a refused apply/undo wrote nothing and kept its entries; the
stores hold exactly the model's entries (newest first) and never more than 50, in memory and in the JSON on
disk; `edits list` flags exactly the entries whose files no longer match.

The type check is replaced by a stub (the language server is not part of this check).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest
from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from mcp_nav_shared.edits import EditPlan, FileChange, TextEdit, hash_bytes
from mcp_nav_shared.errors import ToolInputError
from mcp_nav_shared.lsp_client import LspClient

from codenav_mcp import edit_ops, edits_tools, writes
from codenav_mcp.edit_state import MAX_ENTRIES, STATE_DIR_ENV
from codenav_mcp.plan_merge import site_followup, track_optional_files
from codenav_mcp.plan_sites import CodenavPlan, OptionalSite
from codenav_mcp.tool_base import Session


FILES = ["a.py", "b.py", "c.py"]
PREFIX = "s_"


def initial_text(name: str) -> str:
	stem = name[0]
	return "".join(f"{stem}{i} = {i}\n" for i in range(5))


@dataclass
class MPlan:
	id: str
	changes: dict[str, tuple[str, str]]  # file -> (old text, new text)
	kinds: dict[str, str]  # file -> "replace" | "insert" (the certain edit)
	sites: dict[str, tuple[str, int]]  # site id -> (file, line index in the old text)
	base: dict[str, str | None]  # file -> text the plan was made from (changes + site files)


@dataclass
class MFollow:
	sites: dict[str, tuple[str, int]]  # remaining site id -> (file, line index now)
	base: dict[str, str | None]  # file -> text it is valid against


@dataclass
class MEntry:
	id: str
	changes: dict[str, tuple[str, str]]
	follow: MFollow | None = None
	parent: str | None = None
	history: list[str] = field(default_factory=list)


async def _simulate(*_args: Any, **_kwargs: Any) -> Any:
	return SimpleNamespace(unchecked=False, new_errors=[])


async def _after(*_args: Any, **_kwargs: Any) -> str:
	return "After writing: no errors (stub)."


class EditStateMachine(RuleBasedStateMachine):
	def __init__(self) -> None:
		super().__init__()
		self.tmp = Path(tempfile.mkdtemp(prefix="h3-edit-")).resolve()
		self._saved_env = os.environ.get(STATE_DIR_ENV)
		os.environ[STATE_DIR_ENV] = str(self.tmp / "state")
		self.root = self.tmp / "ws"
		self.root.mkdir()
		self.disk: dict[str, str | None] = {}
		self.seen: dict[str, list[str]] = {}
		for name in FILES:
			text = initial_text(name)
			(self.root / name).write_text(text)
			self.disk[name] = text
			self.seen[name] = [text]
		self.previews: dict[str, MPlan] = {}
		self.entries: list[MEntry] = []
		self.counter = 0
		self.patches = [
			mock.patch.object(edit_ops, "simulate_plan", _simulate),
			mock.patch.object(edit_ops, "errors_after_write", _after),
		]
		for patch in self.patches:
			patch.start()
		writes.reset_state()

	def teardown(self) -> None:
		for patch in self.patches:
			patch.stop()
		writes.reset_state()
		if self._saved_env is None:
			os.environ.pop(STATE_DIR_ENV, None)
		else:
			os.environ[STATE_DIR_ENV] = self._saved_env
		shutil.rmtree(self.tmp, ignore_errors=True)

	# -- helpers -------------------------------------------------------------------------------------------------

	@property
	def state(self) -> writes.WriteState:
		return writes.write_state(self.root)

	def session(self) -> Session:
		return Session(
			client=cast(LspClient, object()),
			workspace=self.root,
			roots=[self.root],
			state=writes.write_state(self.root),
		)

	def fresh(self) -> int:
		self.counter += 1
		return self.counter

	def read(self, name: str) -> str | None:
		path = self.root / name
		return path.read_text() if path.exists() else None

	def write(self, name: str, text: str | None) -> None:
		path = self.root / name
		if text is None:
			path.unlink(missing_ok=True)
		else:
			path.write_text(text)
		self.disk[name] = text
		if text is not None:
			self.seen[name].append(text)

	def run(self, coroutine: Any) -> Any:
		return asyncio.run(coroutine)

	def entry(self, entry_id: str) -> MEntry | None:
		return next((e for e in self.entries if e.id == entry_id), None)

	def active_child(self, entry_id: str) -> bool:
		return any(e.parent == entry_id for e in self.entries)

	def add_entry(self, entry: MEntry) -> None:
		self.entries.append(entry)
		del self.entries[:-MAX_ENTRIES]

	def stale(self, base: dict[str, str | None]) -> list[str]:
		return [name for name, text in base.items() if self.disk[name] != text]

	def site_objects(self, plan: MPlan) -> list[OptionalSite]:
		objects = []
		for sid, (name, line) in plan.sites.items():
			text = (plan.base[name] or "").splitlines(keepends=True)[line].strip()
			objects.append(OptionalSite(sid, name, line + 1, "text", text, TextEdit(line, 0, line, 0, PREFIX)))
		return objects

	def make_plan(self, names: list[str], kinds: list[str], picks: list[tuple[str, int]]) -> MPlan | None:
		"""A plan from the files as they are now, or None when the files cannot support it."""
		names = [n for n in dict.fromkeys(names) if self.disk[n]]
		if not names:
			return None
		changes: dict[str, tuple[str, str]] = {}
		plan_kinds: dict[str, str] = {}
		for index, name in enumerate(names):
			old = cast(str, self.disk[name])
			kind = kinds[index % len(kinds)]
			lines = old.splitlines(keepends=True)
			if kind == "insert" or not lines:
				new = f"ins{self.fresh()} = 0\n" + old
				kind = "insert"
			else:
				lines[0] = f"rep{self.fresh()} = 0\n"
				new = "".join(lines)
			changes[name] = (old, new)
			plan_kinds[name] = kind
		sites: dict[str, tuple[str, int]] = {}
		taken: set[tuple[str, int]] = set()
		for name, raw in picks:
			text = self.disk[name]
			if not text:
				continue
			lines = text.splitlines(keepends=True)
			line = raw % len(lines)
			if plan_kinds.get(name) == "replace" and line == 0:
				continue
			if (name, line) in taken or not lines[line].strip():
				continue
			taken.add((name, line))
			sites[f"u{len(sites) + 1}"] = (name, line)
		base: dict[str, str | None] = {name: old for name, (old, _) in changes.items()}
		for name, _ in sites.values():
			base.setdefault(name, self.disk[name])
		return MPlan("", changes, plan_kinds, sites, base)

	def real_plan(self, plan: MPlan) -> CodenavPlan:
		changes = [FileChange(self.root / name, old, new) for name, (old, new) in plan.changes.items()]
		sites = self.site_objects(plan)
		certain = track_optional_files(EditPlan(f"Edit {self.fresh()}", changes), sites, self.root)
		return CodenavPlan(certain, sites)

	def final_texts(self, plan: MPlan, chosen: list[str]) -> dict[str, str]:
		final: dict[str, str] = {name: new for name, (_, new) in plan.changes.items()}
		touched = {plan.sites[sid][0] for sid in chosen}
		for name in touched:
			final.setdefault(name, cast(str, plan.base[name]))
		by_file: dict[str, set[int]] = {}
		for sid in chosen:
			name, line = plan.sites[sid]
			by_file.setdefault(name, set()).add(line + (1 if plan.kinds.get(name) == "insert" else 0))
		for name, lines_to_mark in by_file.items():
			rows = final[name].splitlines(keepends=True)
			for line in lines_to_mark:
				rows[line] = PREFIX + rows[line]
			final[name] = "".join(rows)
		return final

	# -- rules ---------------------------------------------------------------------------------------------------

	@rule(
		names=st.lists(st.sampled_from(FILES), min_size=1, max_size=2, unique=True),
		kinds=st.lists(st.sampled_from(["replace", "insert"]), min_size=1, max_size=2),
		picks=st.lists(st.tuples(st.sampled_from(FILES), st.integers(0, 9)), max_size=3),
	)
	def preview(self, names: list[str], kinds: list[str], picks: list[tuple[str, int]]) -> None:
		model = self.make_plan(names, kinds, picks)
		if model is None:
			return
		model.id = self.state.plans.add(self.real_plan(model))
		self.previews[model.id] = model
		while len(self.previews) > MAX_ENTRIES:
			del self.previews[next(iter(self.previews))]

	@rule(count=st.integers(10, 30), name=st.sampled_from(FILES))
	def preview_many(self, count: int, name: str) -> None:
		for _ in range(count):
			model = self.make_plan([name], ["replace"], [])
			if model is None:
				return
			model.id = self.state.plans.add(self.real_plan(model))
			self.previews[model.id] = model
			while len(self.previews) > MAX_ENTRIES:
				del self.previews[next(iter(self.previews))]

	@rule(
		pick=st.integers(0, 60),
		mode=st.sampled_from(["none", "all", "some", "unknown"]),
		subset=st.lists(st.integers(0, 3), max_size=3),
	)
	def apply_preview(self, pick: int, mode: str, subset: list[int]) -> None:
		ids = list(self.previews)
		if not ids:
			with pytest.raises(ToolInputError):
				self.run(edit_ops.apply_stored(self.session(), "deadbeef"))
			return
		plan = self.previews[ids[pick % len(ids)]]
		available = list(plan.sites)
		if mode == "all":
			wanted: list[str] | None = ["all"]
			chosen = available
		elif mode == "some":
			wanted = [available[i % len(available)] for i in subset] if available else None
			chosen = sorted(set(wanted or []), key=available.index)
			wanted = wanted or None
		elif mode == "unknown":
			wanted, chosen = ["u99"], []
		else:
			wanted, chosen = None, []
		if wanted and not available:
			bad = True
		else:
			bad = mode == "unknown"
		stale = self.stale(plan.base)
		if stale or bad:
			before = dict(self.disk)
			with pytest.raises(ToolInputError):
				self.run(edit_ops.apply_stored(self.session(), plan.id, sites=wanted))
			assert before == self.disk
			return
		final = self.final_texts(plan, chosen)
		applied = self.run(edit_ops.apply_stored(self.session(), plan.id, sites=wanted))
		assert applied.entry.id == plan.id
		changes = {name: (cast(str, self.disk[name]), text) for name, text in final.items()}
		for name, (_, text) in changes.items():
			self.disk[name] = text
			self.seen[name].append(text)
		del self.previews[plan.id]
		left = {
			sid: (name, line + (1 if plan.kinds.get(name) == "insert" else 0))
			for sid, (name, line) in plan.sites.items()
			if sid not in chosen
		}
		follow = None
		if left:
			base: dict[str, str | None] = dict(final)
			for name, _ in left.values():
				base.setdefault(name, self.disk[name])
			follow = MFollow(left, base)
		self.add_entry(MEntry(plan.id, changes, follow))

	@rule(
		pick=st.integers(0, 60),
		subset=st.lists(st.integers(0, 3), min_size=1, max_size=2),
		mode=st.sampled_from(["all", "some"]),
	)
	@precondition(lambda self: any(e.follow for e in self.entries))
	def apply_remaining_sites(self, pick: int, subset: list[int], mode: str) -> None:
		parents = [e for e in self.entries if e.follow]
		parent = parents[pick % len(parents)]
		assert parent.follow is not None
		available = list(parent.follow.sites)
		wanted = ["all"] if mode == "all" else sorted({available[i % len(available)] for i in subset})
		chosen = available if mode == "all" else wanted
		before = dict(self.disk)
		if self.active_child(parent.id) or self.stale(parent.follow.base):
			with pytest.raises(ToolInputError):
				self.run(edit_ops.apply_stored(self.session(), parent.id, sites=wanted))
			assert before == self.disk
			return
		applied = self.run(edit_ops.apply_stored(self.session(), parent.id, sites=wanted))
		by_file: dict[str, set[int]] = {}
		for sid in chosen:
			name, line = parent.follow.sites[sid]
			by_file.setdefault(name, set()).add(line)
		changes: dict[str, tuple[str, str]] = {}
		for name, lines_to_mark in by_file.items():
			old = cast(str, self.disk[name])
			rows = old.splitlines(keepends=True)
			for line in lines_to_mark:
				rows[line] = PREFIX + rows[line]
			changes[name] = (old, "".join(rows))
		for name, (_, text) in changes.items():
			self.disk[name] = text
			self.seen[name].append(text)
		left = {sid: where for sid, where in parent.follow.sites.items() if sid not in chosen}
		base = dict(parent.follow.base)
		for name, (_, text) in changes.items():
			base[name] = text
		follow = MFollow(left, base) if left else None
		self.add_entry(MEntry(applied.entry.id, changes, follow, parent=parent.id))

	@rule(names=st.lists(st.sampled_from(FILES), min_size=1, max_size=2, unique=True), with_sites=st.booleans())
	def apply_directly(self, names: list[str], with_sites: bool) -> None:
		"""What a write tool does with apply=true: journal.apply with the follow-up of the sites it left."""
		model = self.make_plan(
			names, ["replace", "insert"], [("a.py", 1), ("b.py", 2), ("c.py", 3)] if with_sites else []
		)
		if model is None:
			return
		real = self.real_plan(model)
		followup = site_followup(real.plan, real.optional_sites, self.root)
		entry = self.state.journal.apply(real.plan, followup=followup)
		for name, (_, text) in model.changes.items():
			self.disk[name] = text
			self.seen[name].append(text)
		follow = None
		if model.sites:
			left = {
				sid: (n, line + (1 if model.kinds.get(n) == "insert" else 0)) for sid, (n, line) in model.sites.items()
			}
			base: dict[str, str | None] = {name: new for name, (_, new) in model.changes.items()}
			for name, _ in left.values():
				base.setdefault(name, self.disk[name])
			follow = MFollow(left, base)
		self.add_entry(MEntry(entry.id, dict(model.changes), follow))

	@rule(count=st.integers(10, 30), name=st.sampled_from(FILES))
	def apply_many(self, count: int, name: str) -> None:
		for _ in range(count):
			model = self.make_plan([name], ["replace"], [])
			if model is None:
				return
			entry = self.state.journal.apply(self.real_plan(model).plan)
			for file, (_, text) in model.changes.items():
				self.disk[file] = text
				self.seen[file].append(text)
			self.add_entry(MEntry(entry.id, dict(model.changes)))

	@rule(pick=st.integers(0, 60), latest=st.booleans())
	def undo(self, pick: int, latest: bool) -> None:
		if not self.entries:
			before = dict(self.disk)
			with pytest.raises(ToolInputError):
				edit_ops.undo_stored(self.state, None)
			assert before == self.disk
			return
		target = self.entries[-1] if latest else self.entries[pick % len(self.entries)]
		stale = [name for name, (_, new) in target.changes.items() if self.disk[name] != new]
		before = dict(self.disk)
		if stale or self.active_child(target.id):
			with pytest.raises(ToolInputError):
				edit_ops.undo_stored(self.state, target.id)
			assert before == self.disk
			return
		reverted = edit_ops.undo_stored(self.state, target.id)
		assert reverted.id == target.id
		for name, (old, _) in target.changes.items():
			self.disk[name] = old
			self.seen[name].append(old)
		self.entries.remove(target)

	@rule()
	@precondition(lambda self: bool(self.entries))
	def undo_latest_by_default(self) -> None:
		target = self.entries[-1]
		stale = [name for name, (_, new) in target.changes.items() if self.disk[name] != new]
		before = dict(self.disk)
		if stale or self.active_child(target.id):
			with pytest.raises(ToolInputError):
				edit_ops.undo_stored(self.state, None)
			assert before == self.disk
			return
		edit_ops.undo_stored(self.state, None)
		for name, (old, _) in target.changes.items():
			self.disk[name] = old
			self.seen[name].append(old)
		self.entries.remove(target)

	@rule(name=st.sampled_from(FILES), how=st.sampled_from(["rewrite", "append", "delete", "restore"]))
	def external_change(self, name: str, how: str) -> None:
		current = self.disk[name]
		if how == "delete":
			self.write(name, None)
		elif how == "append" and current is not None:
			self.write(name, current + f"extra{self.fresh()} = 0\n")
		elif how == "restore" and current is None:
			self.write(name, self.seen[name][-1])
		else:
			self.write(name, f"ext{self.fresh()} = 0\nmore = 1\n")

	@rule(name=st.sampled_from(FILES), pick=st.integers(0, 10_000))
	def external_reset(self, name: str, pick: int) -> None:
		"""A git-like reset of one file to any state it has ever had (the initial text, or after any edit)."""
		history = self.seen[name]
		self.write(name, history[pick % len(history)])

	@rule()
	def external_reset_everything(self) -> None:
		for name in FILES:
			self.write(name, self.seen[name][0])

	@rule()
	def restart(self) -> None:
		writes.reset_state()
		self.state  # noqa: B018  # the new process loads both stores from disk

	# -- invariants ----------------------------------------------------------------------------------------------

	@invariant()
	def files_equal_the_model(self) -> None:
		for name in FILES:
			assert self.read(name) == self.disk[name], name

	@invariant()
	def stores_hold_exactly_the_models_entries_and_never_more_than_the_limit(self) -> None:
		pending = [stored.id for stored in self.state.plans.pending()]
		history = [entry.id for entry in self.state.journal.history()]
		assert pending == list(reversed(self.previews))
		assert history == [entry.id for entry in reversed(self.entries)]
		assert len(pending) <= MAX_ENTRIES and len(history) <= MAX_ENTRIES
		directory = self.state.journal._directory
		for file, key in (("plans.json", "plans"), ("journal.json", "entries")):
			path = directory / file
			if path.exists():
				assert len(json.loads(path.read_text())[key]) <= MAX_ENTRIES

	@invariant()
	def the_list_flags_exactly_what_no_longer_matches(self) -> None:
		text = edits_tools._list(self.state, limit=MAX_ENTRIES)
		shown_previews: dict[str, bool] = {}
		shown_applied: dict[str, bool] = {}
		section = None
		for line in text.splitlines():
			if line.startswith("Previews"):
				section = shown_previews
			elif line.startswith("Applied"):
				section = shown_applied
			elif section is not None and (found := re.match(r"^  ([0-9a-f]{8})  ", line)):
				flag = "STALE (" in line if section is shown_previews else "[changed since:" in line
				section[found.group(1)] = flag
		assert set(shown_previews) == set(self.previews), text
		assert set(shown_applied) == {entry.id for entry in self.entries}, text
		for plan_id, flagged in shown_previews.items():
			assert flagged == bool(self.stale(self.previews[plan_id].base)), (plan_id, text)
		for entry in self.entries:
			differs = any(self.disk[name] != new for name, (_, new) in entry.changes.items())
			assert shown_applied[entry.id] == differs, (entry.id, text)

	@invariant()
	def applied_hashes_match_what_was_written(self) -> None:
		for entry in self.state.journal.history():
			model = self.entry(entry.id)
			assert model is not None
			for path, digest in entry.applied_hashes.items():
				assert digest == hash_bytes(model.changes[path.name][1].encode()), path


TestEditStateMachine = EditStateMachine.TestCase
_FUZZ = os.environ.get("HYPOTHESIS_PROFILE") == "fuzz"
TestEditStateMachine.settings = settings(
	max_examples=400 if _FUZZ else 12,
	stateful_step_count=45 if _FUZZ else 25,
	deadline=None,
	database=None,
	derandomize=not _FUZZ,
)
