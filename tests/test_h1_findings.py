"""Minimal repros of the bugs the refactor fuzzer found (hardening/FINDINGS-h1.md), as small synthetic workspaces.

Each test states the expected behaviour and is a strict xfail until the bug is fixed: fixing it flips the test."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from codenav_mcp import analysis_tools, edit_tools, refactor_tools, write_tools
from hardening.fuzz_checks import check_reply_format

from .live_support import TY, make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

SCHEMAS = {
	"rename": {"name", "new_name", "file_path", "line", "column", "parameter", "apply", "sites"},
	"lookup": {"name", "file_path", "line", "column", "show", "limit", "offset"},
}


def _run(loop, coro):
	return str(loop.run_until_complete(coro))


def _listed_or_rewritten(root: Path, reply: str, file: str, needle: str, new: str) -> bool:
	"""A site is handled when the file already holds the new name there or the reply lists it as an optional site
	(`u<N>  file:line`, which `sites=[...]` can opt in); an "e.g." example in a note is not an opt-in handle."""
	text = (root / file).read_text(encoding="utf-8")
	for number, line in enumerate(text.splitlines(), 1):
		if needle in line and new not in line and not re.search(rf"\bu\d+\s+{re.escape(file)}:{number}\b", reply):
			return False
	return True


def test_f1_string_attribute_names_are_rewritten_or_listed(tmp_path, monkeypatch, loop):
	"""rename of `helper` leaves hasattr/getattr/monkeypatch.setattr(mod, "helper", ...) alone and does not list them."""
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/core.py": "def helper() -> int:\n\treturn 1\n",
			"src/pkg/use.py": 'from pkg import core\n\n\ndef run() -> int:\n\tif hasattr(core, "helper"):\n\t\treturn getattr(core, "helper")()\n\treturn 0\n',
			"tests/test_use.py": 'import pytest\nfrom pkg import core\n\n\ndef test_x(monkeypatch: pytest.MonkeyPatch) -> None:\n\tmonkeypatch.setattr(core, "helper", lambda: 2)\n',
		},
	)
	reply = _run(loop, write_tools.rename(name="helper", new_name="assist", file_path="src/pkg/core.py"))
	print(reply)
	assert _listed_or_rewritten(root, reply, "src/pkg/use.py", '"helper"', '"assist"')
	assert _listed_or_rewritten(root, reply, "tests/test_use.py", '"helper"', '"assist"')


def test_f2_gate_checks_files_that_subclass_through_a_package_reexport(tmp_path, monkeypatch, loop):
	"""click: `change_signature(Command.parse_args, add=...)` applied with "0 new errors" although tests/ subclasses
	`click.Command` (import click; class X(click.Command)) and now overrides it incompatibly (ty error)."""
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "from .base import Base as Base\n",
			"src/pkg/base.py": "class Base:\n\tdef run(self, x: int) -> int:\n\t\treturn x\n",
			"tests/test_base.py": "import pkg\n\n\ndef test_it() -> None:\n\tclass Sub(pkg.Base):\n\t\tdef run(self, x: int) -> int:\n\t\t\treturn x + 1\n\n\tassert Sub().run(1) == 2\n",
		},
	)
	reply = _run(
		loop,
		refactor_tools.change_signature(
			name="Base.run", file_path="src/pkg/base.py", add=[{"name": "extra", "default": "None"}]
		),
	)
	print(reply)
	sub = (root / "tests/test_base.py").read_text(encoding="utf-8")
	assert "NOT applied" in reply or "extra" in sub or "tests/test_base.py:6" in reply
	checked = subprocess.run(
		[str(TY), "check", "--output-format", "concise"], cwd=root, capture_output=True, text=True, check=False
	)
	assert "invalid-method-override" not in checked.stdout or "NOT applied" in reply


def test_f3_delete_of_a_constant_does_not_point_at_a_removed_tool(tmp_path, monkeypatch, loop):
	make_workspace(tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/core.py": "LIMIT = 3\n"})
	reply = _run(loop, edit_tools.edit_symbol(name="LIMIT", action="delete", file_path="src/pkg/core.py"))
	print(reply)
	assert "`edit`" not in reply
	assert check_reply_format(reply, True, SCHEMAS) == []


def test_f4_ambiguity_retry_entries_are_calls(tmp_path, monkeypatch, loop):
	make_workspace(
		tmp_path,
		monkeypatch,
		{"src/pkg/__init__.py": "", "src/pkg/utils.py": "class Sentinel:\n\tUNSET = 1\n\n\nUNSET = Sentinel.UNSET\n"},
	)
	reply = _run(loop, write_tools.rename(name="UNSET", new_name="NOTSET", file_path="src/pkg/utils.py"))
	print(reply)
	assert check_reply_format(reply, True, SCHEMAS) == []


def test_f5_unused_and_delete_agree_on_a_string_forward_reference(tmp_path, monkeypatch, loop):
	make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/types.py": 'from typing import TypeVar\n\n\nclass Wrapper:\n\tpass\n\n\nWT = TypeVar("WT", bound="Wrapper")\n',
		},
	)
	listing = _run(loop, analysis_tools.unused(path="src/pkg"))
	print(listing)
	if re.search(r"Wrapper \(class, unused\)", listing):
		reply = _run(loop, edit_tools.edit_symbol(name="Wrapper", action="delete", file_path="src/pkg/types.py"))
		print(reply)
		assert "Not deleted" not in reply


def test_f6_rename_updates_the_package_reexport(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "from .fmt import wrap as wrap\n",
			"src/pkg/fmt.py": "def wrap(text: str) -> str:\n\treturn text\n",
		},
	)
	reply = _run(loop, write_tools.rename(name="wrap", new_name="wrap_text", file_path="src/pkg/fmt.py"))
	print(reply)
	assert "NOT applied" not in reply
	assert "wrap_text" in (root / "src/pkg/__init__.py").read_text(encoding="utf-8")


def test_f7_markdown_mentions_of_the_symbol_get_an_optional_site_id(tmp_path, monkeypatch, loop):
	"""srxy: a test compares committed docs/api-reference.md with the rendered API; renaming the class left it red.
	The reply only counts non-Python mentions ("3 non-Python file (not listed ...)"), so `sites=` cannot opt them in."""
	make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/core.py": "class Widget:\n\tpass\n",
			"docs/api.md": "# API\n\n`Widget` is the main class.\n",
		},
	)
	reply = _run(loop, write_tools.rename(name="Widget", new_name="Gadget", file_path="src/pkg/core.py"))
	print(reply)
	assert re.search(r"\bu\d+\s+docs/api\.md:3\b", reply)
