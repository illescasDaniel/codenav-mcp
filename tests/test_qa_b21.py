"""QA block B21: leaks, messages and protocol. Each test is a black-box QA case (qa/rerun G1, G2, G4) in miniature."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp import args, read_tools, server, write_tools

from .live_support import TY, make_loop_fixture, make_workspace, needs_ty


_base_loop = make_loop_fixture()


_test_loop: asyncio.AbstractEventLoop | None = None


@pytest.fixture
def loop(_base_loop):
	global _test_loop
	_test_loop = _base_loop
	return _base_loop


@pytest.fixture(autouse=True)
def _workspace_from_cwd(monkeypatch, tmp_path):
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(tmp_path))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))


REPO_ROOT = Path(__file__).resolve().parents[1]


def _text(result) -> str:
	return result.content[0].text if hasattr(result, "content") else str(result)


def _call(tool: str, args: dict):
	async def scenario():
		async with Client(server.mcp, mode="legacy") as client:
			return await client.call_tool(tool, args)

	assert _test_loop is not None
	return _test_loop.run_until_complete(scenario())


def _git_init(root: Path, *, commit: bool) -> None:
	env = {
		**os.environ,
		"GIT_AUTHOR_NAME": "t",
		"GIT_AUTHOR_EMAIL": "t@t",
		"GIT_COMMITTER_NAME": "t",
		"GIT_COMMITTER_EMAIL": "t@t",
	}
	subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, env=env)  # noqa: S607
	if commit:
		subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)  # noqa: S607
		subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True, env=env)  # noqa: S607


# (1) NAVN-034/035, UNDO-010: no home path in the workspace() reply, only the ty command or a workspace-relative path.


def test_rc_b21_workspace_reply_hides_ty_home_path(tmp_path, monkeypatch, loop):
	project = make_workspace(tmp_path, monkeypatch, {"src/a.py": "x = 1\n"})
	home_ty = TY  # the repo .venv ty, under the home folder in this checkout
	monkeypatch.setattr(server, "resolve_ty_command", lambda _root: [str(home_ty), "server"])

	text = _text(asyncio.run(read_tools.check()))

	ty_line = next(line for line in text.splitlines() if line.strip().startswith("- ty:"))
	assert str(Path.home()) not in ty_line
	assert str(home_ty) not in text
	assert "ty" in ty_line
	assert str(project) in text  # the workspace line itself is the one allowed absolute path


def test_rc_b21_workspace_reply_uses_workspace_relative_venv_path(tmp_path, monkeypatch, loop):
	project = make_workspace(tmp_path, monkeypatch, {"src/a.py": "x = 1\n"})
	venv_ty = project / ".venv" / "bin" / "ty"
	venv_ty.parent.mkdir(parents=True)
	venv_ty.write_text("#!/bin/sh\n", encoding="utf-8")
	monkeypatch.setattr(server, "resolve_ty_command", lambda _root: [str(venv_ty), "server"])
	monkeypatch.setattr(read_tools, "resolve_ty_command", lambda _root: [str(TY), "server"])

	text = _text(asyncio.run(read_tools.check()))

	assert "ty: .venv/bin/ty" in text
	assert str(project / ".venv") not in text


# (2) NAVP-012: a string line number is refused with the strict-type message, as other parameters are.


@pytest.mark.parametrize("bad", ["3", "3.0", "", True])
def test_rc_b21_string_line_number_is_refused(bad):
	problem = args.argument_problem("lookup", read_tools.lookup, {"file_path": "a.py", "line": bad, "column": 1})

	assert problem is not None
	assert problem.startswith("lookup: line must be a whole number")


def test_rc_b21_whole_number_line_is_still_accepted():
	assert args.argument_problem("lookup", read_tools.lookup, {"file_path": "a.py", "line": 3, "column": 1}) is None


def test_rc_b21_string_line_number_refused_by_direct_call(tmp_path, monkeypatch, loop):
	make_workspace(tmp_path, monkeypatch, {"a.py": "x = 1\n"})

	result = _call("lookup", {"file_path": "a.py", "line": "3", "column": 1})

	assert result.is_error
	assert "whole number" in _text(result)


# (4) SRCH-007: a path outside the workspace is explained, not reported as an empty result.


@pytest.mark.parametrize("path", ["../", "/etc"])
def test_rc_b21_search_path_outside_workspace_is_explained(tmp_path, monkeypatch, loop, path):
	make_workspace(tmp_path, monkeypatch, {"a.py": "class Cart:\n    pass\n"})

	text = _text(_call("search", {"query": "Cart", "path": path}))

	assert "outside the workspace" in text


# (5) DIAG-021 / DIAG-028 / DIAG-014: verify_changes messages.


def test_rc_b21_verify_clean_tree_message_is_documented_wording(tmp_path, monkeypatch, loop):
	make_workspace(tmp_path, monkeypatch, {"a.py": "x = 1\n"})
	_git_init(tmp_path, commit=True)

	text = _text(_call("check", {"since": "HEAD"}))

	assert "No uncommitted Python changes." in text


def test_rc_b21_verify_unborn_head_has_friendly_message(tmp_path, monkeypatch, loop):
	make_workspace(tmp_path, monkeypatch, {"a.py": "x = 1\n"})
	_git_init(tmp_path, commit=False)

	result = _call("check", {"since": "HEAD"})
	text = _text(result)

	assert "git failed" not in text
	assert "bad revision" not in text
	assert "no commits" in text.lower()


@needs_ty
def test_rc_b21_verify_without_dependents_says_so(tmp_path, monkeypatch, loop):
	project = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/a.py": "def f(x: int) -> int:\n    return x\n",
			"src/b.py": "from a import f\n\nprint(f(1))\n",
		},
	)
	_git_init(project, commit=True)
	(project / "src" / "a.py").write_text("def f(x: int, y: int) -> int:\n    return x + y\n", encoding="utf-8")

	text = _text(_call("check", {"since": "HEAD", "include_dependents": False}))

	assert "Dependent files were not checked" in text


@needs_ty
def test_rc_b21_undo_twice_says_already_undone(tmp_path, monkeypatch, loop):
	project = make_workspace(tmp_path, monkeypatch, {"src/a.py": "x = 1\n"})
	applied = _text(
		_call("edit_symbol", {"edits": [{"file_path": "src/a.py", "old_string": "x = 1", "new_string": "x = 2"}]})
	)
	edit_id = applied.split('edits(action="undo", id="')[1].split('"')[0]
	assert (project / "src" / "a.py").read_text(encoding="utf-8") == "x = 2\n"
	first = _text(_call("edits", {"action": "undo", "id": edit_id}))
	second = _text(_call("edits", {"action": "undo", "id": edit_id}))

	assert first.startswith("Reverted edit")
	assert "already undone" in second


def test_rc_b21_undo_unknown_id_points_at_the_persistent_history(tmp_path, monkeypatch, loop):
	make_workspace(tmp_path, monkeypatch, {"a.py": "x = 1\n"})

	text = _text(_call("edits", {"action": "undo", "id": "deadbeef"}))

	assert "not in the undo history" in text
	assert 'edits(action="list")' in text
	assert "in memory" not in text


# (8) WRX-011: a symbol move must not broaden permissions on private source code.


@needs_ty
@pytest.mark.skipif(os.name == "nt", reason="Windows does not support POSIX umask permissions")
def test_rc_b21_new_move_destination_preserves_private_permissions(tmp_path, monkeypatch, loop):
	project = make_workspace(
		tmp_path, monkeypatch, {"src/real.py": "def helper():\n    return 1\n\n\ndef other():\n    return 2\n"}
	)
	(project / "src" / "real.py").chmod(0o600)
	old = os.umask(0o022)
	try:
		_call("move", {"name": "helper", "file_path": "src/real.py", "to_file": "src/umask_dest.py", "apply": True})
	finally:
		os.umask(old)

	assert (project / "src" / "umask_dest.py").stat().st_mode & 0o777 == 0o600


# (8b) WRX-020: a path starting with ~ is refused, not written as a literal '~' folder.


def test_rc_b21_tilde_path_is_refused_not_written(tmp_path, monkeypatch, loop):
	project = make_workspace(tmp_path, monkeypatch, {"a.py": "x = 1\n"})

	result = _call("edit_symbol", {"edits": [{"file_path": "~/x.py", "new_text": "x = 1\n"}]})

	assert result.is_error
	assert "~" in _text(result)
	assert not (project / "~").exists()


def test_rc_b21_tilde_path_refused_by_edit_target(tmp_path):
	with pytest.raises(ToolInputError, match="~"):
		write_tools._edit_target(tmp_path, "~/x.py")
	assert not (tmp_path / "~").exists()


# (9) LANG-018: a PEP 695 type parameter is refused with the reason, not with an unrelated 'elsewhere' match.


@needs_ty
def test_rc_b21_pep695_type_parameter_refusal_has_reason(tmp_path, monkeypatch, loop):
	make_workspace(
		tmp_path,
		monkeypatch,
		{"src/pep695.py": "class Box[T]:\n    def get(self) -> T:\n        raise NotImplementedError\n"},
	)

	with (tmp_path / "pyproject.toml").open("a", encoding="utf-8") as config:
		config.write('python-version = "3.12"\n')

	result = _call("rename", {"name": "T", "file_path": "src/pep695.py", "new_name": "U"})
	text = _text(result)

	assert result.is_error
	if sys.version_info < (3, 12):
		assert "No symbol found" in text  # The 3.11 parser cannot parse PEP 695 syntax.
	else:
		assert "type parameter" in text
	assert "elsewhere" not in text


# (10) EDIT-011: a near-miss old_string names the nearest line.


def test_rc_b21_edit_not_found_names_nearest_line():
	current = "import os\n\n\ndef calculate_total(items):\n    return sum(items)\n"

	text = write_tools._not_found_message(current, "def calculate_totl(items):")

	assert "Nearest line" in text
	assert "4" in text
	assert "def calculate_total(items):" in text


def test_nearest_line_counts_only_python_line_breaks():
	current = 'value = "\f\u2028"\n\ndef calculate_total(items):\n    return sum(items)\n'
	text = write_tools._not_found_message(current, "def calculate_totl(items):")
	assert "Nearest line 3:" in text


def test_rc_b21_edit_not_found_without_near_line_is_short():
	text = write_tools._not_found_message("x = 1\n", "zzzzzzzzzz qqqqq")

	assert text == "old_string was not found in the file."
