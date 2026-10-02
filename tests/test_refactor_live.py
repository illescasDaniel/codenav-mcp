"""Live checks of change_signature / move_symbol / move_module against a real `ty`."""

from __future__ import annotations

import ast

import pytest

from codenav_mcp import refactor_tools, write_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/mailer.py": (
		"def send_mail(to: str, subject: str, body: str = '') -> bool:\n\treturn bool(to and subject)\n\n\n"
		"class Sender:\n"
		"\tdef __init__(self, host: str, port: int = 25) -> None:\n\t\tself.host = host\n\t\tself.port = port\n\n"
		"\tdef send(self, to: str, text: str) -> bool:\n\t\treturn bool(to)\n\n\n"
		"class LoudSender(Sender):\n"
		"\tdef send(self, to: str, text: str) -> bool:\n\t\treturn super().send(to, text.upper())\n"
	),
	"src/pkg/app.py": (
		"from pkg.mailer import LoudSender, Sender, send_mail\n\n\n"
		"class Service:\n"
		"\tdef __init__(self, sender: Sender) -> None:\n\t\tself.sender = sender\n\n"
		"\tdef notify(self) -> bool:\n\t\treturn self.sender.send('a@b.c', 'hi') and send_mail('x', 'y')\n\n\n"
		"def main() -> bool:\n"
		"\tsender = Sender('localhost')\n"
		"\tok = send_mail(\n\t\t'a@b.c',\n\t\t'Subject',\n\t\tbody='text',\n\t)\n"
		"\tcallback = send_mail\n"
		"\targs = ['a', 'b']\n"
		"\tsend_mail(*args)\n"
		"\treturn ok and Sender.send(sender, 'z', 'w') and LoudSender('h').send('q', 'r') and bool(callback)\n"
	),
	"tests/__init__.py": "",
	"tests/test_mail.py": "from pkg.mailer import send_mail\n\n\ndef test_it():\n\tassert send_mail('a', 'b', 'c')\n",
}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(tmp_path, monkeypatch, _FILES)


def _run(loop, coro):
	return loop.run_until_complete(coro)


def _valid(*paths) -> None:
	for path in paths:
		ast.parse(path.read_text())


def test_given_new_param_with_default_when_change_signature_then_only_definition_changes(project, loop):
	# when
	text = _run(
		loop,
		refactor_tools.change_signature(
			name="send_mail",
			add=[{"name": "retries", "annotation": "int", "default": "3"}],
			apply=True,
			max_new_errors=None,
		),
	)
	# then
	mailer = (project / "src/pkg/mailer.py").read_text()
	assert "def send_mail(to: str, subject: str, body: str = '', retries: int = 3) -> bool:" in mailer
	assert "send_mail('x', 'y')" in (project / "src/pkg/app.py").read_text()
	assert "Applied as edit" in text
	_valid(project / "src/pkg/mailer.py")


def test_given_star_args_call_when_change_signature_then_gate_blocks_and_names_the_call(project, loop):
	# when — `send_mail(*args)` now feeds a str to the new int parameter; the type check must catch what the rewrite cannot
	text = _run(
		loop,
		refactor_tools.change_signature(
			name="send_mail", add=[{"name": "retries", "annotation": "int", "default": "3"}], apply=True
		),
	)
	# then
	assert "NOT applied: 1 new error" in text
	assert "src/pkg/app.py:21" in text and "uses *args/**kwargs unpacking" in text
	assert "retries" not in (project / "src/pkg/mailer.py").read_text()


def test_given_value_for_new_param_when_change_signature_then_every_call_site_gets_the_keyword(project, loop):
	# when
	text = _run(
		loop,
		refactor_tools.change_signature(
			name="send_mail",
			add=[{"name": "retries", "annotation": "int", "default": "3", "value": "5"}],
			apply=True,
			max_new_errors=None,
		),
	)
	# then
	app = (project / "src/pkg/app.py").read_text()
	assert "send_mail('x', 'y', retries=5)" in app
	assert "\t\tbody='text',\n\t\tretries=5,\n\t)" in app or "body='text', retries=5" in app
	assert "send_mail('a', 'b', 'c', retries=5)" in (project / "tests/test_mail.py").read_text()
	assert "needs manual attention" in text
	assert "used as a value, not called" in text and "uses *args/**kwargs unpacking" in text
	_valid(project / "src/pkg/app.py", project / "tests/test_mail.py")


def test_given_removed_param_when_change_signature_then_arguments_dropped_everywhere(project, loop):
	# when
	_run(loop, refactor_tools.change_signature(name="send_mail", remove=["body"], apply=True, max_new_errors=None))
	# then
	assert "def send_mail(to: str, subject: str) -> bool:" in (project / "src/pkg/mailer.py").read_text()
	app = (project / "src/pkg/app.py").read_text()
	assert "\t\t'a@b.c',\n\t\t'Subject',\n\t)" in app
	assert "send_mail('a', 'b')" in (project / "tests/test_mail.py").read_text()
	_valid(project / "src/pkg/app.py")


def test_given_method_with_override_when_change_signature_then_override_super_and_class_calls_follow(project, loop):
	# when
	text = _run(
		loop,
		refactor_tools.change_signature(
			name="Sender.send",
			add=[{"name": "urgent", "annotation": "bool", "default": "False", "value": "True"}],
			apply=True,
			max_new_errors=None,
		),
	)
	# then
	mailer = (project / "src/pkg/mailer.py").read_text()
	assert mailer.count("urgent: bool = False") == 2
	assert "super().send(to, text.upper(), urgent=True)" in mailer
	app = (project / "src/pkg/app.py").read_text()
	assert "self.sender.send('a@b.c', 'hi', urgent=True)" in app
	assert "Sender.send(sender, 'z', 'w', urgent=True)" in app  # unbound call: receiver kept
	assert "LoudSender('h').send('q', 'r', urgent=True)" in app
	assert "also changing the override LoudSender.send" in text
	_valid(project / "src/pkg/mailer.py", project / "src/pkg/app.py")


def test_given_class_name_when_change_signature_then_init_and_constructor_calls_change(project, loop):
	# when
	_run(
		loop,
		refactor_tools.change_signature(
			name="Sender",
			add=[{"name": "tls", "annotation": "bool", "default": "False", "value": "True"}],
			apply=True,
			max_new_errors=None,
		),
	)
	# then
	assert (
		"def __init__(self, host: str, port: int = 25, tls: bool = False)"
		in (project / "src/pkg/mailer.py").read_text()
	)
	assert "Sender('localhost', tls=True)" in (project / "src/pkg/app.py").read_text()


def test_given_reorder_when_change_signature_then_definition_and_positional_calls_swapped(project, loop):
	# when
	_run(
		loop,
		refactor_tools.change_signature(
			name="send_mail", reorder=["subject", "to", "body"], apply=True, max_new_errors=None
		),
	)
	# then
	assert "def send_mail(subject: str, to: str, body: str = '')" in (project / "src/pkg/mailer.py").read_text()
	assert "send_mail('b', 'a', 'c')" in (project / "tests/test_mail.py").read_text()
	assert "send_mail('y', 'x')" in (project / "src/pkg/app.py").read_text()


def test_given_preview_default_when_change_signature_then_nothing_written(project, loop):
	# given
	before = (project / "src/pkg/mailer.py").read_text()
	# when
	text = _run(loop, refactor_tools.change_signature(name="send_mail", remove=["body"]))
	# then
	assert "Preview only" in text and (project / "src/pkg/mailer.py").read_text() == before


def test_given_required_param_without_value_when_change_signature_then_explained(project, loop):
	assert "give a `default`" in _run(loop, refactor_tools.change_signature(name="send_mail", add=[{"name": "x"}]))
	assert "nothing to change" in _run(loop, refactor_tools.change_signature(name="send_mail"))
	assert "no parameter 'nope'" in _run(loop, refactor_tools.change_signature(name="send_mail", remove=["nope"]))


def test_given_invalid_resulting_signature_when_change_signature_then_refused(project, loop):
	# when — a required parameter after one with a default is a syntax error
	text = _run(
		loop,
		refactor_tools.change_signature(
			name="send_mail", add=[{"name": "z", "value": "1", "default": None, "position": 3}]
		),
	)
	required = _run(
		loop, refactor_tools.change_signature(name="Sender.send", add=[{"name": "z", "default": None, "value": "1"}])
	)
	# then
	assert "syntax error" in text.lower() or "give a `default`" in text
	assert "Preview" in required or "error" in required.lower()


def test_given_class_without_init_when_change_signature_then_explained(project, loop):
	# given
	(project / "src/pkg/data.py").write_text(
		"from dataclasses import dataclass\n\n\n@dataclass\nclass Row:\n\tid: int\n", encoding="utf-8"
	)
	# when
	text = _run(loop, refactor_tools.change_signature(name="Row", add=[{"name": "x", "default": "1"}]))
	# then
	assert "defines no __init__" in text


def test_given_applied_change_when_undo_then_all_files_restored(project, loop):
	# given
	originals = {p: p.read_text() for p in project.rglob("*.py")}
	_run(
		loop,
		refactor_tools.change_signature(
			name="send_mail", add=[{"name": "r", "default": "1", "value": "2"}], apply=True, max_new_errors=None
		),
	)
	# when
	_run(loop, write_tools.undo_edit())
	# then
	assert {p: p.read_text() for p in project.rglob("*.py")} == originals
