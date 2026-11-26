"""QA block B23: previews that would create a new file are refused (fail-closed cleanup, RC-091)."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import cast

import pytest
from mcp_nav_shared.edits import EditPlan, FileChange
from mcp_nav_shared.errors import FailedText
from mcp_nav_shared.lsp_client import LspClient

from codenav_mcp import writes
from codenav_mcp.writes import finish_plan


class _StubClient:
	"""Stands in for a started language server in previews that never touch a new file."""

	def overlay(self, texts: dict[Path, str]):
		return contextlib.nullcontext()

	async def diagnostics(self, file_path: str) -> list[dict]:
		return []


@pytest.fixture(autouse=True)
def _fresh_write_state():
	writes.reset_state()
	yield
	writes.reset_state()


def _client(workspace: Path) -> LspClient:
	"""A client that is never started: the refusal happens before any language-server call."""
	return LspClient(workspace_root=workspace, command=["true"], language_id="python")


def _create_plan(workspace: Path, name: str = "new_mod.py") -> EditPlan:
	return EditPlan("Edit new_mod.py", [FileChange(workspace / name, None, "VALUE = 1\n", False, False)])


async def _preview(workspace: Path, plan: EditPlan, client: LspClient | None = None):
	return await finish_plan(
		client or _client(workspace),
		workspace=workspace,
		roots=[workspace],
		plan=plan,
		title=plan.description,
		apply=False,
		max_new_errors=None,
	)


def test_rc_091_preview_of_a_new_file_is_refused_and_the_plan_is_kept(tmp_path):
	# given a plan whose only change creates a file that does not exist
	workspace = tmp_path
	# when it is previewed
	outcome = asyncio.run(_preview(workspace, _create_plan(workspace)))
	# then the preview is refused, names the file, and offers the existing approval flow
	assert isinstance(outcome.text, FailedText)
	assert "Preview refused: the plan creates 1 new file ('new_mod.py')" in outcome.text
	assert "previews never create files" in outcome.text
	assert 'edits(action="apply", id="' in outcome.text
	assert 'edits(action="undo", id="' in outcome.text
	# and the plan is kept under the id in the message
	assert outcome.preview_id is not None
	assert writes.write_state(workspace).plans.get(outcome.preview_id) is not None


def test_rc_091_refused_new_file_preview_never_touches_the_disk(tmp_path):
	# given
	workspace = tmp_path
	# when the preview is refused
	asyncio.run(_preview(workspace, _create_plan(workspace)))
	# then no file was created and no directory appeared
	assert list(workspace.iterdir()) == []


def test_rc_091_approving_the_kept_plan_writes_it_and_undo_removes_it(tmp_path):
	# given a refused preview whose plan was kept
	workspace = tmp_path
	target = workspace / "new_mod.py"
	outcome = asyncio.run(_preview(workspace, _create_plan(workspace)))
	plan = writes.write_state(workspace).plans.get(outcome.preview_id)
	# when the write is approved through the existing flow
	entry = writes.write_state(workspace).journal.apply(plan)
	# then the file exists and the undo removes it again
	assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
	writes.write_state(workspace).journal.undo(entry.id)
	assert not target.exists()


def test_rc_091_move_preview_creating_the_destination_is_refused(tmp_path):
	# given a move-like plan: create the destination, delete the source (which exists)
	workspace = tmp_path
	source = workspace / "old.py"
	source.write_text("VALUE = 1\n", encoding="utf-8")
	plan = EditPlan(
		"Move module old.py",
		[
			FileChange(workspace / "new_pkg" / "moved.py", None, "VALUE = 1\n", False, False),
			FileChange(source, "VALUE = 1\n", None, False, False),
		],
	)
	# when it is previewed
	outcome = asyncio.run(_preview(workspace, plan))
	# then the preview is refused with the destination named, and nothing was created or deleted
	assert "Preview refused: the plan creates 1 new file ('new_pkg/moved.py')" in outcome.text
	assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
	assert not (workspace / "new_pkg").exists()


def test_rc_091_edit_of_an_existing_file_still_previews(tmp_path):
	# given an existing file and a plan that only modifies it
	workspace = tmp_path
	source = workspace / "old.py"
	source.write_text("VALUE = 1\n", encoding="utf-8")
	plan = EditPlan("Edit old.py", [FileChange(source, "VALUE = 1\n", "VALUE = 2\n", False, False)])
	# when it is previewed (the stub answers every check with no diagnostics)
	outcome = asyncio.run(_preview(workspace, plan, client=cast(LspClient, _StubClient())))
	# then it is still a normal preview
	assert not isinstance(outcome.text, FailedText)
	assert "Preview only, nothing written." in outcome.text
	assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
