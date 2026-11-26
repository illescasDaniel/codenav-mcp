"""A2: attribute names written as strings (`getattr(obj, "name")`, `monkeypatch.setattr`, `patch.object`)."""

from __future__ import annotations

import re

import pytest

from codenav_mcp import write_tools
from codenav_mcp.attr_strings import find_attr_strings
from codenav_mcp.pysource import source_lines

from .live_support import make_loop_fixture, make_workspace, needs_ty


loop = make_loop_fixture()

_CORE = (
	"class Ctl:\n\tdef target(self) -> int:\n\t\treturn 1\n\n\n"
	"class Sub(Ctl):\n\tpass\n\n\n"
	"class Other:\n\tdef target(self) -> int:\n\t\treturn 2\n"
)
_USE = (
	"from unittest import mock\n\nfrom pkg import core\nfrom pkg.core import Ctl, Other, Sub\n\n\n"
	"def run(obj, typed: Ctl, sub: Sub, other: Other) -> None:\n"
	"\tgetattr(typed, 'target')\n"
	"\tgetattr(obj, 'target')\n"
	"\thasattr(sub, 'target')\n"
	"\tgetattr(other, 'target')\n"
	"\tmock.patch.object(Ctl, 'target')\n"
	"\tsetattr(core.Ctl, 'target', None)\n"
)


def _line(needle: str) -> int:
	return next(number for number, row in enumerate(_USE.splitlines(), 1) if row == needle)


def test_find_attr_strings_only_takes_the_name_slot_of_getattr_style_calls():
	text = (
		"getattr(a, 'old')\nhasattr(b.c, \"old\", 1)\nmp.setattr('pkg.old', 1)\nmp.setattr(a, 'old', 1)\n"
		"x.getattr(a, 'old')\ngetattr(a, 'older')\ngetattr(a, f'old')\npatch.object(T, 'old')\n"
	)
	found = find_attr_strings(text, source_lines(text), "old")
	assert [(f.line, f.start, f.end) for f in found] == [(1, 12, 15), (2, 14, 17), (4, 15, 18), (8, 17, 20)]


@pytest.mark.integration
@needs_ty
def test_string_attribute_names_follow_the_type_of_the_object(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/core.py": _CORE, "src/pkg/use.py": _USE}
	)

	preview = str(
		loop.run_until_complete(
			write_tools.rename(name="Ctl.target", new_name="goal", file_path="src/pkg/core.py", apply=False)
		)
	)

	# exact (the object is a Ctl or the class itself): written, not offered
	assert "+\tgetattr(typed, 'goal')" in preview, preview
	assert "+\tmock.patch.object(Ctl, 'goal')" in preview, preview
	assert "+\tsetattr(core.Ctl, 'goal', None)" in preview, preview
	# a subclass inherits the member, an untyped object may be one: offered by id, not written
	for needle in ("\tgetattr(obj, 'target')", "\thasattr(sub, 'target')"):
		assert re.search(rf"\bu\d+\s+src/pkg/use\.py:{_line(needle)}\b", preview), preview
	# another class that happens to have the same member is left alone
	other_line = _line("\tgetattr(other, 'target')")
	assert not re.search(rf"\bu\d+\s+src/pkg/use\.py:{other_line}\b", preview), preview
	assert (root / "src/pkg/use.py").read_text(encoding="utf-8") == _USE


@pytest.mark.integration
@needs_ty
def test_rename_all_leaves_string_naming_a_local_definition_in_another_module(tmp_path, monkeypatch, loop):
	use = "from pkg import core, decoy\n\ngetattr(core, 'target')\ngetattr(decoy, 'target')\n"
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/core.py": "def target() -> int:\n\treturn 1\n",
			"src/pkg/decoy.py": "def target() -> int:\n\treturn 2\n",
			"src/pkg/use.py": use,
		},
	)
	for old, new, expected in [
		("target", "renamed", use.replace("core, 'target'", "core, 'renamed'")),
		("renamed", "target", use),
	]:
		reply = loop.run_until_complete(
			write_tools.rename(name=old, file_path="src/pkg/core.py", new_name=new, sites=["all"])
		)
		assert "Applied as edit" in reply, reply
		assert (root / "src/pkg/use.py").read_text() == expected, reply
