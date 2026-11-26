"""Fix round B3: impact scans beyond the package (scripts/, examples/, docs) and counts test fakes."""

from __future__ import annotations

import pytest

from codenav_mcp import analysis_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/port.py": (
		"from typing import Protocol\n\n\n"
		"class Sender(Protocol):\n\tdef deliver(self, msg: str) -> None: ...\n\n\n"
		"def send(to: str, body: str = '') -> None:\n\tprint(to, body)\n"
	),
	"src/pkg/use.py": "from pkg.port import Sender, send\n\n\ndef run(s: Sender) -> None:\n\ts.deliver('x')\n\tsend('a')\n",
	"tests/test_use.py": (
		"from pkg.use import run\n\n\nclass FakeSender:\n\tdef deliver(self, msg: str) -> None:\n\t\tself.got = msg\n\n\n"
		"def test_run() -> None:\n\trun(FakeSender())\n"
	),
	"scripts/tool.py": "from pkg.port import send\n\nsend('script')\n",
	"examples/demo.py": "import pkg.port as port\n\nport.send('example', body='b')\n",
	"docs/api-reference.md": "# API\n\n`send(to, body='')` sends a message.\n",
}


def _impact(loop, **kwargs) -> str:
	return str(loop.run_until_complete(analysis_tools.impact(**kwargs)))


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	make_workspace(tmp_path, monkeypatch, FILES)
	(tmp_path / "pyproject.toml").write_text(
		'[project]\nname = "demo"\nversion = "0"\n[tool.ty.environment]\nroot = ["src", "."]\n'
		'[tool.ty.src]\nexclude = ["scripts/**", "examples/**"]\n',
		encoding="utf-8",
	)
	return tmp_path


def test_signature_change_shows_docs_and_callers_in_scripts_and_examples(project, loop):
	text = _impact(loop, name="send", change="signature")
	print(text)
	assert "docs/api-reference.md:3" in text
	assert "scripts/tool.py:1,3" in text and "examples/demo.py:3" in text
	assert "3 unresolved same-name use(s)" in text.splitlines()[0]


def test_a_protocol_method_with_test_fakes_does_not_report_zero_tests(project, loop):
	text = _impact(loop, name="Sender.deliver", change="signature")
	print(text)
	assert "Tests: 0 file(s)" not in text
	assert "FakeSender.deliver  tests/test_use.py:5" in text
	assert "Tests: 1 file(s)" in text.splitlines()[0]


def test_unscanned_directories_are_named_in_one_line(project, loop):
	(project / "build").mkdir()
	(project / "build" / "gen.py").write_text("def helper(to):\n\tpass\n", encoding="utf-8")
	text = _impact(loop, name="send", change="signature")
	print(text)
	assert "Not scanned" in text and "build/" in text
