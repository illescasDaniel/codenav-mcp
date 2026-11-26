"""Fix round B5: deleting a variable points at a call that works in v2."""

from __future__ import annotations

import ast
import re

import pytest

from codenav_mcp import edit_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()


def test_the_suggested_call_deletes_a_multi_line_constant(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/core.py": "LIMITS = {\n\t'a': 1,\n\t'b': 2,\n}\n\n\ndef f() -> int:\n\treturn 1\n",
		},
	)
	reply = str(
		loop.run_until_complete(edit_tools.edit_symbol(name="LIMITS", action="delete", file_path="src/pkg/core.py"))
	)
	match = re.search(r"Example: edit_symbol\(edits=(.*)\)$", reply, re.M)
	assert match, reply
	items = ast.literal_eval(match.group(1))
	done = str(loop.run_until_complete(edit_tools.edit_symbol(edits=items)))
	assert "Applied as edit" in done, done
	assert (root / "src/pkg/core.py").read_text(encoding="utf-8") == "\n\ndef f() -> int:\n\treturn 1\n"
