"""Bugs the differential run against jedi/rope found (hardening/FINDINGS-h2.md). Each test states the expected
behaviour and is `xfail(strict=True)`: fixing the bug flips it to a failure, so the marker must then be removed."""

from __future__ import annotations

import pytest

from codenav_mcp import read_tools, server, write_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_CORE = (
	"class Ctl:\n"
	"\tdef __init__(self) -> None:\n"
	"\t\tself.activity: str | None = None\n\n"
	"\tdef start(self) -> None:\n\t\tself.activity = 'x'\n\n"
	"\tdef stop(self) -> None:\n\t\tself.activity = None\n\n"
	"\tdef target(self) -> int:\n\t\treturn 1\n"
)
_USE = (
	"from pkg.core import Ctl\n\n\n"
	"def check() -> None:\n"
	"\tc = Ctl()\n"
	"\tc.start()\n"
	"\tassert c.activity is not None\n"
	"\tc.stop()\n"
	"\tassert c.activity is None\n"
	"\tc.target()\n\n\n"
	"def patched(monkeypatch) -> None:\n"
	"\tfrom pkg import core\n\n"
	"\tmonkeypatch.setattr(core.Ctl, 'target', lambda self: 2)\n"
)


def _project(tmp_path, monkeypatch, loop, files):
	root = make_workspace(tmp_path, monkeypatch, files)
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))
	return root


@pytest.mark.xfail(strict=True, reason="FINDINGS-h2 #1")
def test_references_include_calls_after_ty_narrows_the_receiver_to_never(tmp_path, monkeypatch, loop):
	_project(tmp_path, monkeypatch, loop, {"src/pkg/__init__.py": "", "src/pkg/core.py": _CORE, "src/pkg/use.py": _USE})
	call_line = _USE.splitlines().index("\tc.target()") + 1

	text = loop.run_until_complete(read_tools.lookup(name="Ctl.target", show=["references"], limit=100))

	assert f"src/pkg/use.py:{call_line}" in text, text  # runtime-reachable call, absent from a "References (1)" list


@pytest.mark.xfail(strict=True, reason="FINDINGS-h2 #2")
def test_rename_all_sites_edits_setattr_style_name_strings(tmp_path, monkeypatch, loop):
	_project(tmp_path, monkeypatch, loop, {"src/pkg/__init__.py": "", "src/pkg/core.py": _CORE, "src/pkg/use.py": _USE})

	text = loop.run_until_complete(
		write_tools.rename(name="Ctl.target", new_name="renamed", apply=False, sites=["all"])
	)

	# `monkeypatch.setattr(core.Ctl, 'target', ...)` names the method as a string and breaks at runtime if left alone
	assert "+\tmonkeypatch.setattr(core.Ctl, 'renamed'" in text, text


@pytest.mark.xfail(strict=True, reason="FINDINGS-h2 #3")
def test_lookup_in_a_file_the_ty_config_excludes_says_so(tmp_path, monkeypatch, loop):
	files = {
		"pyproject.toml": (
			'[project]\nname = "demo"\nversion = "0"\n'
			'[tool.ty.environment]\nroot = ["src", "."]\n[tool.ty.src]\nexclude = ["scripts/**"]\n'
		),
		"src/pkg/__init__.py": "",
		"src/pkg/core.py": _CORE,
		"scripts/tool.py": "from pkg.core import Ctl\n\n\ndef helper() -> int:\n\treturn Ctl().target()\n",
	}
	_project(tmp_path, monkeypatch, loop, files)

	text = loop.run_until_complete(read_tools.lookup(name="helper", file_path="scripts/tool.py"))

	assert "No symbol found matching" not in text, text  # `helper` exists in that file
	assert "exclude" in text.lower(), text


@pytest.mark.xfail(strict=True, reason="FINDINGS-h2 #4")
def test_rename_preview_diff_does_not_show_unchanged_lines_as_edited(tmp_path, monkeypatch, loop):
	# difflib's autojunk (on from 200 lines) treats frequent lines (blank, `args = 1`) as noise, so the unchanged
	# lines between two nearby edits come out as removed and added again
	filler = "def filler() -> int:\n\targs = 1\n\tcontroller = 2\n\n\treturn args + controller\n\n\n" * 40
	use = (
		"def go() -> int:\n\tfrom pkg.core import target\n\n\targs = 1\n\tcontroller = 2\n\treturn target()\n\n\n"
		+ filler
	)
	files = {
		"src/pkg/__init__.py": "",
		"src/pkg/core.py": "def target() -> int:\n\treturn 1\n",
		"src/pkg/use.py": use,
	}
	_project(tmp_path, monkeypatch, loop, files)

	text = loop.run_until_complete(write_tools.rename(name="target", new_name="renamed", apply=False))

	assert "src/pkg/use.py: +2 -2" in text  # two lines change: the import and the call
	removed = [line for line in text.splitlines() if line.startswith("-") and not line.startswith("---")]
	assert len(removed) == 3, removed  # `def target` plus those two
