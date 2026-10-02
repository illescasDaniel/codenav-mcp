"""Edge cases for the write tools against a real `ty`: encodings, guards, concurrency."""

from __future__ import annotations

import asyncio
import dataclasses
import os

import pytest

from codenav_mcp import server as codenav_server, write_tools, writes

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/lib.py": "def helper(x: int) -> int:\n\treturn x\n",
	"src/pkg/use.py": "from pathlib import Path\n\nfrom pkg.lib import helper\n\n\ndef go() -> Path:\n\treturn Path(str(helper(1)))\n",
}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(tmp_path, monkeypatch, _FILES)


def _run(loop, coro):
	return loop.run_until_complete(coro)


def test_given_crlf_and_bom_files_when_rename_then_line_endings_and_bom_preserved(project, loop):
	# given
	(project / "src/pkg/lib.py").write_bytes(b"\xef\xbb\xbfdef helper(x: int) -> int:\r\n\treturn x\r\n")
	(project / "src/pkg/use.py").write_bytes(
		b"from pkg.lib import helper\r\n\r\n\r\ndef go() -> int:\r\n\treturn helper(1)\r\n"
	)
	# when
	text = _run(loop, write_tools.rename_symbol(name="helper", new_name="assist", apply=True))
	# then
	assert "Applied as edit" in text
	assert (project / "src/pkg/lib.py").read_bytes() == b"\xef\xbb\xbfdef assist(x: int) -> int:\r\n\treturn x\r\n"
	assert (
		project / "src/pkg/use.py"
	).read_bytes() == b"from pkg.lib import assist\r\n\r\n\r\ndef go() -> int:\r\n\treturn assist(1)\r\n"


def test_given_non_bmp_characters_before_symbol_when_rename_then_edit_lands_on_the_right_columns(project, loop):
	# given
	(project / "src/pkg/use.py").write_text(
		"from pkg.lib import helper\n\n\ndef go() -> int:\n\treturn len('\U0001f600\U0001f600') + helper(1)  # \U0001f600 helper\n",
		encoding="utf-8",
	)
	# when
	_run(loop, write_tools.rename_symbol(name="helper", new_name="assist", apply=True))
	# then
	assert (project / "src/pkg/use.py").read_text(encoding="utf-8").endswith("+ assist(1)  # \U0001f600 helper\n")


def test_given_library_symbol_when_rename_then_refused_and_nothing_written(project, loop):
	# given — `Path` comes from the standard library
	before = (project / "src/pkg/use.py").read_text()
	# when
	text = _run(
		loop, write_tools.rename_symbol(file_path="src/pkg/use.py", line=1, column=21, new_name="MyPath", apply=True)
	)
	# then
	assert "cannot be renamed" in text or "outside the workspace" in text or "never edit" in text
	assert (project / "src/pkg/use.py").read_text() == before


def test_given_symlink_pointing_outside_when_check_edit_apply_then_refused(project, loop, tmp_path_factory):
	# given
	outside = tmp_path_factory.mktemp("outside") / "secret.py"
	outside.write_text("x = 1\n", encoding="utf-8")
	os.symlink(outside, project / "src/pkg/link.py")
	# when
	text = _run(loop, write_tools.check_edit("src/pkg/link.py", new_text="x = 2\n", apply=True))
	# then
	assert "outside the workspace" in text
	assert outside.read_text() == "x = 1\n"


def test_given_too_many_files_when_apply_then_blocked_unless_allow_large(project, loop):
	# given
	state = writes.write_state(project)
	state.guard = dataclasses.replace(state.guard, max_files=1)
	state.journal.guard = state.guard
	# when
	blocked = _run(loop, write_tools.rename_symbol(name="helper", new_name="assist", apply=True))
	allowed = _run(loop, write_tools.rename_symbol(name="helper", new_name="assist", apply=True, allow_large=True))
	# then
	assert "allow_large" in blocked
	assert "Applied" in allowed
	assert "def assist(" in (project / "src/pkg/lib.py").read_text()


def test_given_concurrent_calls_when_preview_runs_then_other_tools_see_the_disk_not_the_simulation(project, loop):
	# given — a preview holds the simulation overlay while a read tool and another preview run at the same time
	async def scenario():
		results = await asyncio.gather(
			write_tools.rename_symbol(name="helper", new_name="assist"),
			codenav_server.diagnostics("src/pkg/use.py"),
			write_tools.check_edit("src/pkg/lib.py", old_string="return x", new_string="return x + 1"),
			codenav_server.references("src/pkg/lib.py", 1, 5),
		)
		return results

	# when
	preview, diagnostics, edit, references = _run(loop, asyncio.wait_for(scenario(), timeout=60))
	# then
	assert "Preview only" in preview and "Preview only" in edit
	assert "No diagnostics" in diagnostics or "error" not in diagnostics.lower()
	assert "use.py" in references  # still the real names, not the simulated ones
	assert "def helper(" in (project / "src/pkg/lib.py").read_text()


def test_given_transient_new_file_during_simulation_when_done_then_workspace_is_clean(project, loop):
	# when — moving into a brand-new package directory simulates a file that does not exist yet
	from codenav_mcp import refactor_tools

	before = sorted(p.relative_to(project).as_posix() for p in project.rglob("*") if p.is_file() or p.is_dir())
	_run(loop, refactor_tools.move_symbol(name="helper", to_file="src/pkg/newdir/deeper/helpers.py"))
	after = sorted(p.relative_to(project).as_posix() for p in project.rglob("*") if p.is_file() or p.is_dir())
	# then
	assert before == [a for a in after if "__pycache__" not in a]


def test_given_write_failure_midway_when_apply_edit_then_every_file_is_restored(project, loop, monkeypatch):
	# given — a multi-file rename whose second write fails
	from mcp_nav_shared import transaction

	preview = _run(loop, write_tools.rename_symbol(name="helper", new_name="assist"))
	edit_id = preview.split('apply_edit(id="')[1].split('"')[0]
	originals = {p: p.read_bytes() for p in project.rglob("*.py")}
	real = transaction._write_atomic
	calls = {"n": 0}

	def flaky(path, data):
		calls["n"] += 1
		if calls["n"] == 2:
			raise OSError("disk full")
		real(path, data)

	monkeypatch.setattr(transaction, "_write_atomic", flaky)
	# when
	text = _run(loop, write_tools.apply_edit(edit_id))
	# then
	assert "disk full" in text and "no changes were kept" in text
	monkeypatch.setattr(transaction, "_write_atomic", real)
	assert {p: p.read_bytes() for p in project.rglob("*.py")} == originals
	assert not [p for p in project.rglob("*.tmp")]
