"""A1: a keyword argument in code ty types as `Never` is still a use of the renamed field (srxy FileSearchResult.lines)."""

from __future__ import annotations

import re

import pytest

from codenav_mcp import write_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_MODELS = (
	"from dataclasses import dataclass, field\n\n\n"
	"@dataclass\nclass Hit:\n\tpath: str\n\tscore: float\n\tlines: list[int] = field(default_factory=list)\n\n\n"
	"class Ctl:\n\tactivity: str | None = None\n\n\tdef start(self) -> None: ...\n\n\tdef stop(self) -> None: ...\n"
)
# The shape of srxy tests/gui/test_gui_controller.py:1177: ty keeps `activity is not None` through `stop()`, so after
# `assert c.activity is None` everything below is `Never` and the keyword has no definition for ty.
_USE = (
	"from pkg.models import Ctl, Hit\n\n\n"
	"def reachable() -> Hit:\n\treturn Hit(path='a', score=0.5, lines=[])\n\n\n"
	"def narrowed() -> None:\n"
	"\tc = Ctl()\n\tc.start()\n\tassert c.activity is not None\n\tc.stop()\n\tassert c.activity is None\n"
	"\tresult = Hit(path='b', score=0.9, lines=[])\n\tprint(result)\n"
)


def test_keyword_in_never_narrowed_code_is_an_optional_site(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/models.py": _MODELS, "src/pkg/use.py": _USE}
	)
	line = _USE.splitlines().index("\tresult = Hit(path='b', score=0.9, lines=[])") + 1

	preview = str(loop.run_until_complete(write_tools.rename(name="Hit.lines", new_name="rows", apply=False)))
	assert re.search(rf"\bu\d+\s+src/pkg/use\.py:{line}\b", preview), preview
	assert "`Never`" in preview, preview  # says why it is only offered

	text = str(loop.run_until_complete(write_tools.rename(name="Hit.lines", new_name="rows", sites=["all"])))
	assert "lines=[]" not in (root / "src/pkg/use.py").read_text(encoding="utf-8"), text


def test_parameter_rename_by_name_and_position(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/core.py": "def run(value: int) -> int:\n\treturn value\n\n\nrun(value=1)\n",
		},
	)
	for request in (
		write_tools.rename(name="run", parameter="value", new_name="amount", apply=False),
		write_tools.rename(file_path="src/pkg/core.py", line=1, column=9, new_name="amount", apply=False),
	):
		reply = loop.run_until_complete(request)
		assert "+def run(amount: int)" in reply, reply
		assert "+\treturn amount" in reply, reply
		assert "+run(amount=1)" in reply, reply
	assert "run(value=1)" in (root / "src/pkg/core.py").read_text()
