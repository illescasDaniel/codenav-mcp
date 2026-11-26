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
			allow_large=True,  # the fixture has a value use and a *args call the tool cannot update
		),
	)
	# then
	mailer = (project / "src/pkg/mailer.py").read_text()
	assert "def send_mail(to: str, subject: str, body: str = '', *, retries: int = 3) -> bool:" in mailer
	assert "send_mail('x', 'y')" in (project / "src/pkg/app.py").read_text()
	assert "Applied as edit" in text
	assert "is unused" not in text  # an added parameter nothing reads yet is a hint, not a regression
	_valid(project / "src/pkg/mailer.py")


def test_given_star_args_call_when_change_signature_then_plan_is_held_as_incomplete(project, loop):
	# when — `send_mail(*args)` and the `callback = send_mail` use cannot be rewritten
	text = _run(
		loop,
		refactor_tools.change_signature(
			name="send_mail",
			add=[{"name": "retries", "annotation": "int", "default": "3", "keyword_only": False}],
			apply=True,
		),
	)
	# then — nothing written, and the top of the reply says why and how to write it anyway
	assert "NOT applied: the plan is incomplete: 2 call site(s) were not updated" in text
	assert "uses *args/**kwargs unpacking" in text and "used as a value, not called" in text
	assert 'apply_edit(id="' in text and "writes it anyway" in text
	assert "retries" not in (project / "src/pkg/mailer.py").read_text()
	# when — the held plan is written on request
	plan_id = text.split('apply_edit(id="', 1)[1].split('"', 1)[0]
	written = _run(loop, write_tools.apply_edit(id=plan_id))
	# then
	assert "retries: int = 3" in (project / "src/pkg/mailer.py").read_text()
	assert "NOT" not in written.splitlines()[0]


def test_given_preview_of_incomplete_plan_when_change_signature_then_says_apply_would_be_refused(project, loop):
	text = _run(loop, refactor_tools.change_signature(name="send_mail", remove=["body"], apply=False))
	assert "apply=true would be refused: the plan is incomplete" in text


def test_given_effectful_reorder_when_change_signature_then_keywords_in_original_order_are_written(
	tmp_path, monkeypatch, loop
):
	# given — `args.query`/`args.path` are attribute reads: swapping their positions could change evaluation order
	project = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/fmt.py": "def message(path: str, query: str) -> str:\n\treturn f'{query} in {path}'\n",
			"src/pkg/cli.py": (
				"import argparse\n\nfrom pkg.fmt import message\n\n\n"
				"def run(args: argparse.Namespace) -> str:\n"
				"\treturn message(args.query, args.path) + message('a', 'b')\n"
			),
		},
	)
	# when
	text = _run(loop, refactor_tools.change_signature(name="message", reorder=["query", "path"]))
	# then — written (complete plan), keywords keep the old order and meaning, constants are simply swapped
	assert "Applied as edit" in text
	assert "def message(query: str, path: str)" in (project / "src/pkg/fmt.py").read_text()
	cli = (project / "src/pkg/cli.py").read_text()
	assert "message(path=args.query, query=args.path)" in cli
	assert "message('b', 'a')" in cli
	assert "needs manual attention" not in text
	_valid(project / "src/pkg/cli.py")


def test_given_new_positional_param_with_value_when_effectful_arguments_then_the_calls_are_written(
	tmp_path, monkeypatch, loop
):
	# given — `root / "a"` is an expression: inserting a keyword argument next to it moves nothing it evaluates
	project = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/find.py": "def search(path: object, query: str, *, names: bool = True) -> bool:\n\treturn names\n",
			"src/pkg/use.py": (
				"from pathlib import Path\n\nfrom pkg.find import search\n\n\n"
				"def go(root: Path) -> bool:\n\treturn search(root / 'a', 'q', names=False)\n"
			),
		},
	)
	# when
	text = _run(
		loop,
		refactor_tools.change_signature(
			name="search",
			add=[{"name": "exact", "annotation": "bool", "default": "False", "value": "False", "keyword_only": False}],
		),
	)
	# then
	assert "Applied as edit" in text and "needs manual attention" not in text
	assert "search(root / 'a', 'q', exact=False, names=False)" in (project / "src/pkg/use.py").read_text()


def test_given_positional_insert_held_by_positional_only_when_change_signature_then_hint_is_keyword_only(
	tmp_path, monkeypatch, loop
):
	# given — `a` and `b` are positional-only: a new first parameter would shift them, so the calls cannot be rewritten
	project = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/calc.py": "def add(a: int, b: int, /) -> int:\n\treturn a + b\n",
			"src/pkg/use.py": "from pkg.calc import add\n\n\ndef go() -> int:\n\treturn add(1, 2) + add(3, 4)\n",
		},
	)
	spec = {"name": "scale", "annotation": "int", "default": "1", "value": "1", "position": 0}
	# when
	held = _run(loop, refactor_tools.change_signature(name="add", add=[spec], apply=True))
	# then — held, and the reply names keyword_only=true as the way out
	assert "NOT applied: the plan is incomplete: 2 call site(s) were not updated" in held
	assert "keyword_only=true" in held
	assert "scale" not in (project / "src/pkg/calc.py").read_text()
	# when — the suggested variant
	done = _run(loop, refactor_tools.change_signature(name="add", add=[{**spec, "keyword_only": True}], apply=True))
	# then
	assert "Applied as edit" in done and "needs manual attention" not in done
	assert "scale: int = 1" in (project / "src/pkg/calc.py").read_text()
	assert "add(1, 2, scale=1)" in (project / "src/pkg/use.py").read_text()


def test_given_hold_keyword_only_cannot_fix_when_change_signature_then_no_keyword_only_hint(project, loop):
	text = _run(loop, refactor_tools.change_signature(name="send_mail", remove=["body"], apply=False))
	assert "the plan is incomplete" in text and "keyword_only" not in text


def test_given_effectful_reorder_of_positional_only_when_change_signature_then_held_for_manual_work(
	tmp_path, monkeypatch, loop
):
	# given
	project = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/fmt.py": "def message(path: str, query: str, /) -> str:\n\treturn f'{query} in {path}'\n",
			"src/pkg/cli.py": (
				"import argparse\n\nfrom pkg.fmt import message\n\n\n"
				"def run(args: argparse.Namespace) -> str:\n\treturn message(args.query, args.path)\n"
			),
		},
	)
	# when
	text = _run(loop, refactor_tools.change_signature(name="message", reorder=["query", "path"]))
	# then — reorder keeps the group, but the call cannot take keywords: held, call untouched
	assert "NOT applied: the plan is incomplete: 1 call site(s)" in text
	assert "positional-only" in text
	assert "def message(path: str, query: str, /)" in (project / "src/pkg/fmt.py").read_text()


def test_given_value_for_new_param_when_change_signature_then_every_call_site_gets_the_keyword(project, loop):
	# when
	text = _run(
		loop,
		refactor_tools.change_signature(
			name="send_mail",
			add=[{"name": "retries", "annotation": "int", "default": "3", "value": "5"}],
			apply=True,
			max_new_errors=None,
			allow_large=True,
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
	_run(
		loop,
		refactor_tools.change_signature(
			name="send_mail", remove=["body"], apply=True, max_new_errors=None, allow_large=True
		),
	)
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
		"def __init__(self, host: str, port: int = 25, *, tls: bool = False)"
		in (project / "src/pkg/mailer.py").read_text()
	)
	assert "Sender('localhost', tls=True)" in (project / "src/pkg/app.py").read_text()


def test_given_reorder_when_change_signature_then_definition_and_positional_calls_swapped(project, loop):
	# when
	_run(
		loop,
		refactor_tools.change_signature(
			name="send_mail", reorder=["subject", "to", "body"], apply=True, max_new_errors=None, allow_large=True
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
	text = _run(loop, refactor_tools.change_signature(name="send_mail", remove=["body"], apply=False))
	# then
	assert "Preview only" in text and (project / "src/pkg/mailer.py").read_text() == before


def test_given_required_param_without_value_when_change_signature_then_explained(project, loop):
	assert "give a `default`" in _run(
		loop, refactor_tools.change_signature(name="send_mail", add=[{"name": "x"}], apply=False)
	)
	assert "nothing to change" in _run(loop, refactor_tools.change_signature(name="send_mail", apply=False))
	assert "no parameter 'nope'" in _run(
		loop, refactor_tools.change_signature(name="send_mail", remove=["nope"], apply=False)
	)


def test_given_invalid_resulting_signature_when_change_signature_then_refused(project, loop):
	# when — a required parameter after one with a default is a syntax error
	text = _run(
		loop,
		refactor_tools.change_signature(
			name="send_mail", add=[{"name": "z", "value": "1", "default": None, "position": 3}], apply=False
		),
	)
	required = _run(
		loop,
		refactor_tools.change_signature(
			name="Sender.send", add=[{"name": "z", "default": None, "value": "1"}], apply=False
		),
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
	text = _run(loop, refactor_tools.change_signature(name="Row", add=[{"name": "x", "default": "1"}], apply=False))
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
