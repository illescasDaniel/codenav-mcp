"""Fix round B4: `unused` and `edit_symbol(action="delete")` use the same notion of "used"."""

from __future__ import annotations

import re

import pytest

from codenav_mcp import analysis_tools, edit_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/core.py": (
		"__all__ = [\"render_progress\"]\n\n\ndef render_progress() -> str:\n\treturn 'x'\n\n\n"
		"def really_dead() -> int:\n\treturn 1\n\n\n"
		"class Handle:\n\tdef close(self) -> None:\n\t\tpass\n\n\tdef flush(self) -> None:\n\t\tpass\n\n\n"
		"class Gone:\n\tpass\n"
	),
	"src/pkg/use.py": "def shut(thing):\n\tthing.close()\n\n\ndef note() -> str:\n\treturn 'see Gone for details'\n",
	"tests/test_core.py": "from pkg.core import render_progress\n\n\ndef test_it() -> None:\n\tassert render_progress() == 'x'\n",
}


def _run(loop, coro) -> str:
	return str(loop.run_until_complete(coro))


def test_a_function_used_only_by_tests_is_labelled_so(tmp_path, monkeypatch, loop):
	make_workspace(tmp_path, monkeypatch, FILES)
	listing = _run(loop, analysis_tools.unused(path="src/pkg"))
	print(listing)
	assert "render_progress (function, exported, used only in tests)" in listing
	assert "exported, unused" not in listing.split("render_progress")[1].splitlines()[0]


def test_every_plain_unused_symbol_is_deletable(tmp_path, monkeypatch, loop):
	"""The label `unused` promises a delete that is neither refused nor held; anything doubtful says why instead."""
	make_workspace(tmp_path, monkeypatch, FILES)
	listing = _run(loop, analysis_tools.unused(path="src/pkg"))
	print(listing)
	plain = re.findall(r"^\S+:\d+  (\S+) \((?:function|class|method), unused\)$", listing, re.M)
	assert "really_dead" in plain  # the test is not vacuous
	for qualname in plain:
		reply = _run(loop, edit_tools.edit_symbol(name=qualname, action="delete", file_path=None, apply=False))
		assert "Not deleted" not in reply and "held back" not in reply, (qualname, reply)
	# the doubtful ones are not called plain "unused": `thing.close()` is an untyped use, `'see Gone'` a mention
	assert "Handle.close (method, unused)" not in listing
	assert "Gone (class, unused)" not in listing


def test_a_symbol_in_a_ty_excluded_file_is_not_called_plain_unused(tmp_path, monkeypatch, loop):
	make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/a.py": "def dead() -> int:\n\treturn 1\n",
			"scripts/tool.py": "def helper() -> int:\n\treturn 1\n",
		},
	)
	(tmp_path / "pyproject.toml").write_text(
		'[project]\nname = "demo"\nversion = "0"\n[tool.ty.environment]\nroot = ["src", "."]\n[tool.ty.src]\nexclude = ["scripts/**"]\n',
		encoding="utf-8",
	)
	listing = _run(loop, analysis_tools.unused())
	print(listing)
	assert "dead (function, unused)" in listing
	assert "helper (function, in a file ty's config excludes: uses not resolved)" in listing
