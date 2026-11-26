"""QA block B29: verify_changes added-file refinement (BUG-DIAG-R01), enum argument hints (RC-031),
git-stderr-free not-a-repo message (RC-104)."""

from __future__ import annotations

import asyncio
import subprocess  # noqa: S404
from pathlib import Path

import pytest
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp import read_tools, server, write_tools
from codenav_mcp.args import argument_problem
from codenav_mcp.edit_tools import edit_symbol

from .live_support import make_workspace, needs_ty


def _git(root: Path, *args: str) -> None:
	subprocess.run(  # noqa: S603
		["git", "-C", str(root), "-c", "user.email=test@example.invalid", "-c", "user.name=test", *args],  # noqa: S607
		check=True,
		capture_output=True,
	)


def _committed_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
	root = make_workspace(tmp_path, monkeypatch, {"src/old.py": "value: int = 1\n"})
	_git(root, "init", "-q")
	_git(root, "add", "-A")
	_git(root, "commit", "-qm", "baseline")
	return root


def _verify() -> str:
	async def run() -> str:
		try:
			return await read_tools.check(since="HEAD")
		finally:
			if server._client is not None:
				await server._client.stop()

	return asyncio.run(run())


@needs_ty
def test_rc_diag_r01_mixed_diff_checks_modified_and_reports_added_unchecked(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	# given — a committed file that gains an error, and a new file with an error of its own
	root = _committed_project(tmp_path, monkeypatch)
	(root / "src/old.py").write_text('value: int = "bad"\n')
	(root / "src/newmod.py").write_text('x: int = "bad"\n')
	# when
	result = _verify()
	# then — the modified file is compared, the added file is named as not checked, and the call succeeds
	assert not result.startswith("Cannot verify changes")
	assert "1 new error(s)" in result
	assert "src/old.py" in result
	assert "1 added Python file(s) not checked: src/newmod.py (new files have no baseline to compare)" in result
	new_lines = [line for line in result.splitlines() if line.lstrip().startswith("+")]
	assert not any("newmod" in line for line in new_lines)


@needs_ty
def test_rc_diag_r01_only_additions_are_refused_without_a_check(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	# given — two new files and nothing to compare against
	root = _committed_project(tmp_path, monkeypatch)
	(root / "src/first.py").write_text("a: int = 1\n")
	(root / "src/second.py").write_text("b: int = 2\n")

	def no_check(*args: object, **kwargs: object) -> None:
		raise AssertionError("diagnostics_delta must not run when nothing can be compared")

	monkeypatch.setattr(write_tools, "diagnostics_delta", no_check)
	# when
	result = _verify()
	# then
	assert result.startswith("Cannot verify changes: the only changed Python file(s) are new")
	assert "src/first.py, src/second.py" in result
	assert "new files have no baseline to compare" in result


@needs_ty
def test_rc_diag_r01_no_additions_keeps_the_comparison_unchanged(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	# given — only a modified file
	root = _committed_project(tmp_path, monkeypatch)
	(root / "src/old.py").write_text('value: int = "bad"\n')
	# when
	result = _verify()
	# then
	assert "1 new error(s)" in result
	assert "added Python file" not in result
	assert "Cannot verify changes" not in result


def test_rc_031_invalid_action_lists_the_valid_actions() -> None:
	# when
	problem = argument_problem(
		"edit_symbol", edit_symbol, {"action": "update", "name": "f", "file_path": "bom.py", "source": "x"}
	)
	# then
	assert problem == (
		"edit_symbol: action must be one of 'replace', 'replace_body', 'insert', 'insert_after', 'insert_before', "
		"'delete' (got 'update'). Example: edit_symbol(name=\"parse\", action=\"delete\")"
	)


def test_rc_031_missing_action_defaults_to_replace() -> None:
	# v2: `action` is optional (replace is the default), so omitting it is not an argument problem
	assert argument_problem("edit_symbol", edit_symbol, {"name": "f", "file_path": "bom.py", "source": "x"}) is None


def test_rc_031_other_enum_parameters_list_their_values() -> None:
	# when
	problem = argument_problem(
		"edit_symbol",
		edit_symbol,
		{"action": "insert", "name": "f", "file_path": "bom.py", "source": "x", "position": "sideways"},
	)
	# then
	assert problem == (
		"edit_symbol: position must be one of 'after', 'before', 'into', 'end' (got 'sideways'). Example: edit_symbol(name=\"parse\", action=\"delete\")"
	)


def test_rc_104_not_a_repository_gives_the_house_sentence_not_git_stderr(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	# given — a project folder that is not a git repository, in a Spanish locale (git would translate its message)
	monkeypatch.setenv("LC_ALL", "es_ES.UTF-8")
	make_workspace(tmp_path, monkeypatch, {"src/a.py": "a = 1\n"})
	# when
	with pytest.raises(ToolInputError) as caught:
		write_tools._git(tmp_path, "rev-parse", "--show-toplevel")
	# then
	assert str(caught.value) == "not a git repository: run check(since=...) inside the project's git repository"
