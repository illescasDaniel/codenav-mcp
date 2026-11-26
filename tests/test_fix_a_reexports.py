"""A3: renaming a symbol the package re-exports as `from .m import n as n`."""

from __future__ import annotations

import re

import pytest

from codenav_mcp import write_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_FILES = {
	"src/pkg/__init__.py": (
		'from .fmt import wrap as wrap\n\n__all__ = ["wrap", "other"]\n\n_LAZY_EXPORTS = {"wrap": ".fmt", "other": ".o"}\n'
	),
	"src/pkg/fmt.py": "def wrap(text: str) -> str:\n\treturn text\n",
	"src/pkg/use.py": 'import pkg\nfrom pkg import wrap\n\n\ndef run() -> str:\n\treturn wrap("a") + pkg.wrap("b")\n',
}


def test_reexport_alias_all_list_and_users_follow_the_rename(tmp_path, monkeypatch, loop):
	root = make_workspace(tmp_path, monkeypatch, _FILES)

	reply = str(
		loop.run_until_complete(write_tools.rename(name="wrap", new_name="wrap_text", file_path="src/pkg/fmt.py"))
	)

	assert "NOT applied" not in reply, reply
	init = (root / "src/pkg/__init__.py").read_text(encoding="utf-8")
	assert "from .fmt import wrap_text as wrap_text" in init
	assert '__all__ = ["wrap_text", "other"]' in init  # certain
	assert '_LAZY_EXPORTS = {"wrap": ' in init  # a string table is only offered
	use = (root / "src/pkg/use.py").read_text(encoding="utf-8")
	assert "from pkg import wrap_text" in use and 'wrap_text("a") + pkg.wrap_text("b")' in use
	assert re.search(r"\bu1\s+src/pkg/__init__\.py:5\b", reply), reply
	assert "also appears in 1 string" not in reply, reply  # the `__all__` entry is handled, not left over


def test_string_table_site_can_be_opted_in(tmp_path, monkeypatch, loop):
	root = make_workspace(tmp_path, monkeypatch, _FILES)

	loop.run_until_complete(
		write_tools.rename(name="wrap", new_name="wrap_text", file_path="src/pkg/fmt.py", sites=["all"])
	)

	assert '_LAZY_EXPORTS = {"wrap_text": ' in (root / "src/pkg/__init__.py").read_text(encoding="utf-8")
