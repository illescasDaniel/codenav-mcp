"""Persistent previews and undo history (edit_state): what a restarted server sees."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from mcp_nav_shared.edits import EditPlan, FileChange, TextEdit
from mcp_nav_shared.errors import ToolInputError
from mcp_nav_shared.transaction import StaleFileError, WriteGuard

from codenav_mcp import edit_state, writes
from codenav_mcp.edit_state import STATE_DIR_ENV, PersistentJournal, PersistentPlanStore, changed_files
from codenav_mcp.plan_sites import CodenavPlan, OptionalSite


def _plan(path: Path, old: str, new: str, note: str = "") -> EditPlan:
	return EditPlan("Edit a.py", [FileChange(path, old, new)], [note] if note else [])


def _stores(root: Path) -> tuple[PersistentPlanStore, PersistentJournal]:
	"""A fresh pair of stores for `root`: what a newly started server builds."""
	writes.reset_state()
	state = writes.write_state(root)
	return state.plans, state.journal


def _project(tmp_path: Path, text: str = "x = 1\n") -> tuple[Path, Path]:
	root = tmp_path / "proj"
	root.mkdir()
	path = root / "a.py"
	path.write_bytes(text.encode("utf-8"))
	return root.resolve(), path.resolve()


def test_given_state_dir_env_then_it_is_the_root_and_each_workspace_gets_its_own_subdirectory(tmp_path, monkeypatch):
	monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "state"))

	first = edit_state.workspace_state_dir(tmp_path / "one")
	second = edit_state.workspace_state_dir(tmp_path / "two")

	assert first.parent == second.parent == tmp_path / "state"
	assert first != second


def test_given_no_env_then_xdg_state_home_then_home_local_state(tmp_path, monkeypatch):
	monkeypatch.delenv(STATE_DIR_ENV)
	monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
	assert edit_state.state_root() == tmp_path / "xdg" / "codenav-mcp"

	monkeypatch.delenv("XDG_STATE_HOME")
	monkeypatch.setenv("HOME", str(tmp_path / "home"))
	assert edit_state.state_root() == Path(os.path.expanduser("~")) / ".local" / "state" / "codenav-mcp"


def test_given_a_preview_when_the_server_restarts_then_the_plan_and_its_optional_sites_are_still_there(tmp_path):
	root, path = _project(tmp_path, "x = 1\ny = old\n")
	site = OptionalSite("u1", "a.py", 2, "attr", "y = old", TextEdit(1, 4, 1, 7, "new"))
	plans, _ = _stores(root)
	plan_id = plans.add(
		CodenavPlan(_plan(path, "x = 1\ny = old\n", "x = 2\ny = old\n", "a note"), [site]), rerun="r(a=1)"
	)

	reborn, _ = _stores(root)
	stored = reborn.get_stored(plan_id)

	assert stored.rerun == "r(a=1)"
	assert stored.plan.plan.description == "Edit a.py"
	assert stored.plan.plan.notes == ["a note"]
	assert stored.plan.plan.changes[0].new_text == "x = 2\ny = old\n"
	assert stored.plan.plan.changes[0].path == path
	assert stored.plan.plan.base_hashes == _plan(path, "x = 1\ny = old\n", "").base_hashes
	assert stored.plan.optional_sites == [site]
	assert reborn.get(plan_id) is not None


def test_given_a_bom_and_a_mode_when_the_plan_is_stored_then_they_come_back_as_planned_not_as_on_disk(tmp_path):
	root, path = _project(tmp_path, "x = 1\n")
	change = FileChange(path, "x = 1\n", "x = 2\n", old_bom=True, new_bom=True, old_mode=0o640, new_mode=0o640)
	plans, _ = _stores(root)
	plan_id = plans.add(EditPlan("d", [change]))

	loaded = _stores(root)[0].get(plan_id).changes[0]

	assert (loaded.old_bom, loaded.new_bom, loaded.old_mode, loaded.new_mode) == (True, True, 0o640, 0o640)


def test_given_a_plan_added_in_memory_only_api_then_get_returns_an_edit_plan_as_the_1x_tools_expect(tmp_path):
	root, path = _project(tmp_path)
	plans, _ = _stores(root)
	plan = _plan(path, "x = 1\n", "x = 2\n")

	plan_id = plans.add(plan)

	assert isinstance(plans.get(plan_id), EditPlan)
	assert [stored.id for stored in plans.pending()] == [plan_id]


def test_given_unknown_and_applied_preview_ids_then_the_errors_point_at_edits(tmp_path):
	root, path = _project(tmp_path)
	plans, _ = _stores(root)
	plan_id = plans.add(_plan(path, "x = 1\n", "x = 2\n"))
	plans.discard(plan_id)

	with pytest.raises(ToolInputError, match="unknown or expired preview id") as unknown:
		plans.get("deadbeef")
	with pytest.raises(ToolInputError, match="already applied") as applied:
		_stores(root)[0].get(plan_id)

	assert 'edits(action="list")' in str(unknown.value)
	assert f'edits(action="undo", id="{plan_id}")' in str(applied.value)
	assert "in memory" not in str(unknown.value)


def test_given_more_than_50_previews_then_the_oldest_are_dropped_on_disk(tmp_path):
	root, path = _project(tmp_path)
	plans, _ = _stores(root)
	ids = [plans.add(_plan(path, "x = 1\n", f"x = {n}\n")) for n in range(55)]

	kept = [stored.id for stored in _stores(root)[0].pending()]

	assert len(kept) == 50
	assert kept == list(reversed(ids[5:]))  # newest first


def test_given_an_applied_edit_when_the_server_restarts_then_history_and_undo_still_work(tmp_path):
	root, path = _project(tmp_path)
	_, journal = _stores(root)
	entry = journal.apply(_plan(path, "x = 1\n", "x = 2\n"))
	assert path.read_text() == "x = 2\n"

	_, reborn = _stores(root)

	assert [e.id for e in reborn.history()] == [entry.id]
	assert reborn.applied_at[entry.id] > 0
	reborn.undo(entry.id)
	assert path.read_text() == "x = 1\n"
	assert reborn.history() == []
	assert _stores(root)[1].was_undone(entry.id)
	with pytest.raises(ToolInputError, match="already undone"):
		_stores(root)[1].undo(entry.id)


def test_given_a_file_changed_by_hand_after_apply_when_undo_then_it_refuses_and_keeps_the_edit(tmp_path):
	root, path = _project(tmp_path)
	_, journal = _stores(root)
	entry = journal.apply(_plan(path, "x = 1\n", "x = 2\n"))
	path.write_text("x = 3  # by hand\n")

	_, reborn = _stores(root)
	with pytest.raises(StaleFileError):
		reborn.undo(entry.id)

	assert path.read_text() == "x = 3  # by hand\n"
	assert [e.id for e in _stores(root)[1].history()] == [entry.id]


def test_given_a_stale_preview_then_changed_files_names_it_and_apply_refuses(tmp_path):
	root, path = _project(tmp_path)
	plans, journal = _stores(root)
	plan_id = plans.add(_plan(path, "x = 1\n", "x = 2\n"))
	path.write_text("x = 5\n")

	plan = _stores(root)[0].get(plan_id)

	assert changed_files(plan, root) == ["a.py"]
	with pytest.raises(StaleFileError, match="a.py"):
		journal.apply(plan)
	assert path.read_text() == "x = 5\n"


def test_given_history_beyond_50_then_only_the_last_50_edits_are_kept(tmp_path):
	root, path = _project(tmp_path, "n = 0\n")
	_, journal = _stores(root)
	for n in range(1, 53):
		journal.apply(_plan(path, f"n = {n - 1}\n", f"n = {n}\n"))

	history = _stores(root)[1].history()

	assert len(history) == 50
	assert history[0].plan.changes[0].new_text == "n = 52\n"


def test_given_a_corrupt_or_foreign_state_file_then_the_stores_start_empty_and_keep_working(tmp_path):
	root, path = _project(tmp_path)
	plans, journal = _stores(root)
	directory = edit_state.workspace_state_dir(root)
	directory.mkdir(parents=True, exist_ok=True)
	(directory / "plans.json").write_text("{not json")
	(directory / "journal.json").write_text(json.dumps({"version": 99, "entries": [1]}))

	plans, journal = _stores(root)

	assert plans.pending() == []
	assert journal.history() == []
	plan_id = plans.add(_plan(path, "x = 1\n", "x = 2\n"))
	assert [s.id for s in _stores(root)[0].pending()] == [plan_id]


def test_given_one_unreadable_entry_then_the_others_survive(tmp_path):
	root, path = _project(tmp_path)
	plans, _ = _stores(root)
	good = plans.add(_plan(path, "x = 1\n", "x = 2\n"))
	file = edit_state.workspace_state_dir(root) / "plans.json"
	data = json.loads(file.read_text())
	data["plans"].insert(0, {"id": "bad", "created": 0})
	file.write_text(json.dumps(data))

	assert [s.id for s in _stores(root)[0].pending()] == [good]


def test_given_an_unwritable_state_dir_then_the_stores_still_work_in_memory_and_say_why(tmp_path, monkeypatch):
	root, path = _project(tmp_path)
	blocker = tmp_path / "blocker"
	blocker.write_text("a file, not a directory")
	monkeypatch.setenv(STATE_DIR_ENV, str(blocker))
	plans, journal = _stores(root)

	plan_id = plans.add(_plan(path, "x = 1\n", "x = 2\n"))

	assert plans.save_error
	assert plan_id  # the call did not fail
	journal.apply(_plan(path, "x = 1\n", "x = 2\n"))
	assert journal.save_error


def test_given_two_workspaces_then_their_state_is_separate(tmp_path):
	root, path = _project(tmp_path)
	other = tmp_path / "other"
	other.mkdir()
	(other / "a.py").write_text("x = 1\n")
	plans, _ = _stores(root)
	plan_id = plans.add(_plan(path, "x = 1\n", "x = 2\n"))

	other_plans, _ = _stores(other.resolve())

	assert other_plans.pending() == []
	assert [s.id for s in _stores(root)[0].pending()] == [plan_id]


def test_given_state_files_then_they_are_private_to_the_user(tmp_path):
	root, path = _project(tmp_path)
	plans, _ = _stores(root)
	plans.add(_plan(path, "x = 1\n", "x = 2\n"))

	file = edit_state.workspace_state_dir(root) / "plans.json"

	assert file.stat().st_mode & 0o077 == 0


def test_given_the_current_call_when_a_plan_is_stored_then_it_keeps_it_as_the_rerun_call(tmp_path):
	root, path = _project(tmp_path)
	plans, _ = _stores(root)
	token = edit_state.current_call.set(edit_state.call_text("rename", {"name": "a", "new_name": "b", "ctx": None}))
	try:
		plan_id = plans.add(_plan(path, "x = 1\n", "x = 2\n"))
	finally:
		edit_state.current_call.reset(token)

	assert plans.get_stored(plan_id).rerun == 'rename(name="a", new_name="b")'


def test_given_ages_then_they_read_in_the_largest_unit():
	assert edit_state.age_text(100, now=130) == "30s ago"
	assert edit_state.age_text(0, now=120) == "2m ago"
	assert edit_state.age_text(0, now=7300) == "2h ago"
	assert edit_state.age_text(0, now=200000) == "2d ago"


def test_given_a_guard_rebind_then_history_is_kept_not_cleared(tmp_path):
	root, path = _project(tmp_path)
	_, journal = _stores(root)
	journal.apply(_plan(path, "x = 1\n", "x = 2\n"))

	journal.rebind(WriteGuard(root=root, allowed_suffixes=frozenset({".py"})))

	assert len(journal.history()) == 1
