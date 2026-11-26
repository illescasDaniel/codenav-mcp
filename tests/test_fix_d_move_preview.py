"""D4 (F8): `move(apply=false)` into a new file is a normal preview, not an error, and the plan can be applied."""

from __future__ import annotations

import pytest
from mcp_nav_shared.errors import FailedText

from codenav_mcp import edit_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/dates.py": "def parse_date(text: str) -> str:\n\treturn text\n",
	"src/pkg/uses.py": "from pkg.dates import parse_date\n\n\ndef go() -> str:\n\treturn parse_date('x')\n",
}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(tmp_path, monkeypatch, _FILES)


@pytest.mark.parametrize(
	"call",
	[
		{"name": "parse_date", "to_file": "src/pkg/parsing.py"},
		{"file_path": "src/pkg/dates.py", "to_file": "src/pkg/renamed.py"},
	],
)
def test_given_a_move_preview_into_a_new_file_then_it_is_a_normal_reply_with_the_apply_call(project, loop, call):
	before = {p: p.read_text() for p in project.rglob("*.py")}
	text = loop.run_until_complete(edit_tools.move(apply=False, **call))
	assert not isinstance(text, FailedText)
	assert "refused" not in text.lower()
	assert "nothing written" in text
	assert 'edits(action="apply", id="' in text
	assert {p: p.read_text() for p in project.rglob("*.py")} == before  # a preview creates nothing
