"""QA block B14 (codenav side): write guard wiring from the environment, preview ids and undo messages."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp_nav_shared.transaction import WriteRefusedError

from codenav_mcp import edits_tools, write_tools, writes

from .live_support import make_loop_fixture, make_workspace, needs_ty, required_match


_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/lib.py": "def helper(x: int) -> int:\n\treturn x\n",
	"src/pkg/use.py": "from pkg.lib import helper\n\n\ndef go() -> int:\n\treturn helper(1)\n",
}


@pytest.fixture(autouse=True)
def _fresh_write_state(monkeypatch):
	for name in ("CODENAV_MCP_ALLOW_READONLY_FILES", "CODENAV_MCP_INCLUDE_DIRS", "CODENAV_MCP_EXCLUDE_DIRS"):
		monkeypatch.delenv(name, raising=False)
	writes.reset_state()
	yield
	writes.reset_state()


def _refusal(root: Path, path: Path) -> str | None:
	try:
		writes.write_state(root).guard.check_path(path)
	except WriteRefusedError as exc:
		return str(exc)
	return None


def test_rc_051_allow_readonly_env_reaches_the_write_guard(tmp_path, monkeypatch):
	# given a read-only (0444) Python file
	target = tmp_path / "ro.py"
	target.write_text("x = 1\n", encoding="utf-8")
	target.chmod(0o444)
	try:
		# when / then: refused by default, with the variable named in the message
		assert _refusal(tmp_path, target) == (
			"ro.py is read-only; make it writable or set CODENAV_MCP_ALLOW_READONLY_FILES=1"
		)
		# when / then: allowed once the variable is set
		monkeypatch.setenv("CODENAV_MCP_ALLOW_READONLY_FILES", "1")
		assert _refusal(tmp_path, target) is None
	finally:
		target.chmod(0o644)


def test_rc_002_build_refusal_offers_the_include_variable(tmp_path):
	# when / then
	assert _refusal(tmp_path, tmp_path / "build" / "steps.py") == (
		"build/steps.py is inside build/, which the write tools never edit "
		"(set CODENAV_MCP_INCLUDE_DIRS=build to allow)"
	)


def test_rc_002_include_dirs_env_lets_writes_reach_build(tmp_path, monkeypatch):
	# given the user treats build/ as first-party source
	monkeypatch.setenv("CODENAV_MCP_INCLUDE_DIRS", "build")
	# when / then
	assert _refusal(tmp_path, tmp_path / "build" / "steps.py") is None


def test_rc_002_exclude_dirs_env_blocks_a_custom_directory(tmp_path, monkeypatch):
	# given
	monkeypatch.setenv("CODENAV_MCP_EXCLUDE_DIRS", "generated")
	# when / then
	assert _refusal(tmp_path, tmp_path / "generated" / "x.py") == (
		"generated/x.py is inside generated/, which the write tools never edit "
		"(set CODENAV_MCP_INCLUDE_DIRS=generated to allow)"
	)


# ---- live: a real ty behind the write tools -------------------------------------------------------

loop = make_loop_fixture()


def _run(event_loop, coro):
	return event_loop.run_until_complete(coro)


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(tmp_path, monkeypatch, _FILES)


@pytest.mark.integration
@needs_ty
def test_rc_041_applied_preview_keeps_the_preview_id(project, loop):
	# given a preview
	preview = _run(loop, write_tools.rename(name="helper", new_name="assist", apply=False))
	preview_id = required_match(r'edits\(action="apply", id="([0-9a-f]+)"\)', preview).group(1)
	# when the preview is applied by its id
	applied = _run(loop, edits_tools.edits(id=preview_id, action="apply"))
	# then the applied edit has that same id, and the status line comes before the file list
	assert applied.startswith(f"Applied edit {preview_id}:")
	assert "After writing" in applied
	assert "src/pkg/lib.py" in applied
	assert "def assist(" in (project / "src/pkg/lib.py").read_text(encoding="utf-8")
	# and undo_edit with the preview id reverts it
	reverted = _run(loop, edits_tools.edits(id=preview_id, action="undo"))
	assert reverted.startswith(f"Reverted edit {preview_id}")
	assert "def helper(" in (project / "src/pkg/lib.py").read_text(encoding="utf-8")


@pytest.mark.integration
@needs_ty
def test_rc_059_undo_with_a_given_id_on_an_empty_journal_names_the_id(project, loop):
	# when
	text = _run(loop, edits_tools.edits(id="deadbeef", action="undo"))
	# then
	assert "edit deadbeef is not in the undo history" in text
	assert "nothing to undo" not in text
