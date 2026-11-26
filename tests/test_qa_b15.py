"""QA block B15: the `edit` write path and `verify_changes` (regression tests, one per RC)."""

from __future__ import annotations

import subprocess

import pytest
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp import read_tools, write_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/a.py": "def f() -> int:\n    return 1\n",
	"src/pkg/crlf.py": "def g() -> int:\r\n    return 1\r\n",
	"src/pkg/tabs.py": "def h() -> int:\n\treturn 2\n",
	"src/pkg/many.py": "x = 1\nx = 1\nx = 1\n",
}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(tmp_path, monkeypatch, _FILES)


def _run(loop, coro):
	return loop.run_until_complete(coro)


def _git_cmd(root, *args):
	subprocess.run(  # noqa: S603
		["git", "-C", str(root), "-c", "user.email=a@b.c", "-c", "user.name=t", *args],  # noqa: S607
		check=True,
		capture_output=True,
	)


def test_rc_019_mixed_new_text_and_replace_arguments_are_refused(project, loop):
	# given
	target = project / "src/pkg/a.py"
	before = target.read_bytes()
	# when
	text = _run(
		loop,
		write_tools._edit_text(
			"src/pkg/a.py",
			new_text="x = 1\n",
			old_string="return 1",
			new_string="return 2",
			apply=True,
			max_new_errors=None,
		),
	)
	# then
	assert "give either new_text (whole file) or old_string+new_string (replace), not both" in text
	assert target.read_bytes() == before


def test_rc_019_new_string_without_old_string_is_refused(project, loop):
	# given
	target = project / "src/pkg/a.py"
	before = target.read_bytes()
	# when
	text = _run(loop, write_tools._edit_text("src/pkg/a.py", new_string="return 2", apply=True, max_new_errors=None))
	# then
	assert "old_string and new_string go together" in text
	assert target.read_bytes() == before


def test_rc_018_empty_old_string_is_refused_even_with_replace_all(project, loop):
	# given
	target = project / "src/pkg/a.py"
	before = target.read_bytes()
	# when
	text = _run(
		loop,
		write_tools._edit_text(
			"src/pkg/a.py", old_string="", new_string="#", replace_all=True, apply=True, max_new_errors=None
		),
	)
	# then
	assert "old_string must not be empty" in text
	assert target.read_bytes() == before


def test_rc_057_oversized_new_text_is_refused_before_checking(project, loop):
	# when
	text = _run(loop, write_tools._edit_text("src/pkg/a.py", new_text="#" * 1_000_001, apply=False))
	# then
	assert "new_text is too large (1000001 characters; limit 1000000)" in text
	assert "split the change or edit the file directly" in text


def test_rc_060_crlf_file_with_lf_old_string_gets_a_line_ending_hint(project, loop):
	# given
	target = project / "src/pkg/crlf.py"
	before = target.read_bytes()
	# when
	text = _run(
		loop,
		write_tools._edit_text(
			"src/pkg/crlf.py", old_string="def g() -> int:\n    return 1", new_string="def g() -> int:\n    return 2"
		),
	)
	# then
	assert "old_string was not found in the file. The file uses CRLF line endings" in text
	assert target.read_bytes() == before


def test_rc_060_tab_indented_file_with_space_old_string_gets_an_indentation_hint(project, loop):
	# when
	text = _run(
		loop,
		write_tools._edit_text("src/pkg/tabs.py", old_string="    return 2", new_string="    return 3"),
	)
	# then
	assert "The file indents with tabs; old_string uses spaces." in text


def test_rc_112_ambiguous_match_lists_the_line_numbers(project, loop):
	# when
	text = _run(loop, write_tools._edit_text("src/pkg/many.py", old_string="x = 1", new_string="x = 2"))
	# then
	assert "old_string matches 3 times (lines 1, 2, 3); add context or pass replace_all=true" in text


def test_rc_113_replace_all_states_the_replacement_count(project, loop):
	# when
	text = _run(
		loop, write_tools._edit_text("src/pkg/many.py", old_string="x = 1", new_string="x = 2", replace_all=True)
	)
	# then
	assert "Replaced 3 occurrence(s)." in text


def test_rc_012_inserted_lines_in_a_crlf_file_keep_crlf(project, loop):
	# when
	_run(
		loop,
		write_tools._edit_text(
			"src/pkg/crlf.py",
			old_string="    return 1",
			new_string="    x = 1\n    return 1",
			apply=True,
			max_new_errors=None,
		),
	)
	# then
	data = (project / "src/pkg/crlf.py").read_bytes()
	assert data.count(b"\n") == data.count(b"\r\n")
	assert b"    x = 1\r\n    return 1\r\n" in data


def test_rc_048_outside_workspace_is_refused_before_the_file_is_read(project, loop):
	# given — a file outside the workspace (the parent of the project folder)
	outside = project.parent / "outside_b15.py"
	outside.write_text("value = 1\n", encoding="utf-8")
	# when
	text = _run(
		loop,
		write_tools._edit_text(str(outside), old_string="value", new_string="other", apply=True, max_new_errors=None),
	)
	# then
	assert "is outside the workspace; give a path inside it" in text
	assert str(project) not in text
	assert outside.read_text(encoding="utf-8") == "value = 1\n"


def test_rc_109_directory_is_reported_as_a_directory(project, loop):
	# when
	text = _run(loop, write_tools._edit_text("src/pkg", old_string="a", new_string="b", apply=True))
	# then
	assert "src/pkg is a directory, not a file" in text


def test_rc_104_git_failure_is_reported_as_one_short_line(tmp_path):
	# when / then
	with pytest.raises(ToolInputError) as caught:
		write_tools._git(tmp_path, "rev-parse", "--show-toplevel")
	message = str(caught.value)
	# A folder outside any repository gets the house sentence (B29), not git's raw stderr.
	assert message == "not a git repository: run check(since=...) inside the project's git repository"
	assert "\n" not in message


def test_rc_088_changed_config_file_is_reported_and_checked(project, loop):
	# given — a committed clean tree, then a rule change in pyproject.toml only
	_git_cmd(project, "init", "-q")
	_git_cmd(project, "add", "-A")
	_git_cmd(project, "commit", "-q", "-m", "init")
	pyproject = project / "pyproject.toml"
	pyproject.write_text(pyproject.read_text(encoding="utf-8") + '\n[tool.ty.rules]\nunresolved-import = "error"\n')
	# when
	text = _run(loop, read_tools.check(since="HEAD"))
	# then
	assert "Config files changed: pyproject.toml (the type check below uses the new configuration)." in text
	assert "No Python files differ from HEAD" not in text
