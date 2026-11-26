"""Fix C5: an applied edit whose files were all put back outside the tool is reported as reverted, not as a change."""

from __future__ import annotations

import pytest

from codenav_mcp import edits_tools, write_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty, required_match


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_CORE = "def compute(value: int) -> int:\n\treturn value * 2\n"
_USER = "from pkg.core import compute\n\n\ndef use() -> int:\n\treturn compute(1)\n"
_FILES = {"src/pkg/__init__.py": "", "src/pkg/core.py": _CORE, "src/pkg/user.py": _USER}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(tmp_path, monkeypatch, _FILES)


def _edits(loop, **kwargs) -> str:
	return str(loop.run_until_complete(edits_tools.edits(**kwargs)))


def _applied(loop) -> str:
	text = loop.run_until_complete(write_tools.rename(name="compute", new_name="calculate"))
	return required_match(r'edits\(action="undo", id="(\w+)"\)', text).group(1)


def test_given_every_file_reset_outside_the_tool_then_list_says_reverted_without_naming_files(project, loop):
	edit_id = _applied(loop)
	(project / "src/pkg/core.py").write_text(_CORE)
	(project / "src/pkg/user.py").write_text(_USER)

	listing = _edits(loop)

	assert f"{edit_id}  Rename compute" in listing
	assert "[changed since: reverted outside edits]" in listing
	assert "src/pkg" not in listing.split("[2 files]")[1].split("\n")[0]
	assert f'next: edits(action="undo", id="{edit_id}")' in listing


def test_given_every_file_reset_outside_the_tool_then_undo_retires_the_entry_and_touches_nothing(project, loop):
	edit_id = _applied(loop)
	(project / "src/pkg/core.py").write_text(_CORE)
	(project / "src/pkg/user.py").write_text(_USER)

	text = _edits(loop, action="undo", id=edit_id)

	assert "already reverted outside edits" in text and "nothing to undo" in text.lower()
	assert "revert it by hand" not in text
	assert (project / "src/pkg/core.py").read_text() == _CORE
	assert _edits(loop).startswith("No previews or applied edits")


def test_given_one_file_reset_and_another_hand_edited_then_undo_still_refuses(project, loop):
	edit_id = _applied(loop)
	(project / "src/pkg/core.py").write_text(_CORE)
	user = project / "src/pkg/user.py"
	user.write_text(user.read_text() + "# my note\n")

	text = _edits(loop, action="undo", id=edit_id)

	assert "changed since edit" in text and "Nothing was undone" in text
	assert "src/pkg/user.py" in _edits(loop).split("[2 files]")[1]


def test_given_only_one_file_reset_then_undo_still_refuses_and_leaves_the_other_file(project, loop):
	edit_id = _applied(loop)
	(project / "src/pkg/core.py").write_text(_CORE)

	text = _edits(loop, action="undo", id=edit_id)

	assert "changed since edit" in text and "Nothing was undone" in text
	assert "calculate" in (project / "src/pkg/user.py").read_text()
	assert "reverted outside edits" not in _edits(loop)
