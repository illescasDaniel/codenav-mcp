"""Fix round B2: an applied write says it was applied and gives the exact undo call, in one line."""

from __future__ import annotations

import re

import pytest

from codenav_mcp import edit_tools, writes
from codenav_mcp.writes import applied_line

from .live_support import make_loop_fixture, make_workspace, needs_ty


loop = make_loop_fixture()


def test_applied_line_carries_the_exact_undo_call():
	line = applied_line("ab12cd34")
	assert line == 'Applied as edit ab12cd34 (undo: edits(action="undo", id="ab12cd34")).'
	assert "\n" not in applied_line("ab12cd34", unchecked="2")
	assert 'edits(action="undo", id="ab12cd34")' in applied_line("ab12cd34", unchecked="2")


@needs_ty
@pytest.mark.integration
def test_edit_symbol_applied_reply_says_applied_and_how_to_undo(tmp_path, monkeypatch, loop):
	make_workspace(tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/a.py": "def f() -> int:\n\treturn 1\n"})
	reply = str(loop.run_until_complete(edit_tools.edit_symbol(name="f", action="replace_body", source="return 2")))
	match = re.search(r"^Applied as edit (\w+) \(undo: (edits\(action=\"undo\", id=\"(\w+)\"\))\)\.$", reply, re.M)
	assert match and match.group(1) == match.group(3), reply


@needs_ty
@pytest.mark.integration
def test_the_undo_call_survives_a_failed_error_check(tmp_path, monkeypatch, loop):
	make_workspace(tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/a.py": "def f() -> int:\n\treturn 1\n"})

	async def unavailable(*_args, **_kwargs):
		return "Not checked (language server unavailable): src/pkg/a.py (1 files)."

	monkeypatch.setattr(writes, "errors_after_write", unavailable)
	reply = str(loop.run_until_complete(edit_tools.edit_symbol(name="f", action="replace_body", source="return 2")))
	assert re.search(
		r'Applied as edit \w+ \(undo: edits\(action="undo", id="\w+"\)\)\. 1 file\(s\) could not', reply
	), reply
