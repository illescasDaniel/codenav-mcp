"""`impact`: pure classification/rendering (no language server) and a live run against a throwaway project."""

from __future__ import annotations

import pytest

from codenav_mcp import analysis_tools
from codenav_mcp.impact import (
	Args,
	Data,
	Overrides,
	Site,
	Subject,
	classify_sites,
	group_mentions,
	is_test_path,
	render_report,
	write_calls,
)
from codenav_mcp.mentions import Mention

from .live_support import make_loop_fixture, make_workspace, needs_ty


loop = make_loop_fixture()


# -- pure ------------------------------------------------------------------------------


def _loc(path, line, start, end):
	return {
		"uri": path.as_uri(),
		"range": {"start": {"line": line, "character": start}, "end": {"line": line, "character": end}},
	}


def test_given_calls_imports_values_then_sites_are_classified(tmp_path):
	source = (
		"from m import f as f\n"  # 0: import (two references on one line)
		"x = f(1, 2)\n"  # 1: call, positional
		"y = f(1, b=2)\n"  # 2: call, mixed
		"z = f(*xs, **kw)\n"  # 3: call, spreads
		"cb = f\n"  # 4: value
		"def g(a: f) -> f: ...\n"  # 5: annotations
		"class C(f): ...\n"  # 6: subclass
	)
	path = tmp_path / "a.py"
	path.write_text(source)
	locs = [
		_loc(path, 0, 14, 15),
		_loc(path, 0, 19, 20),
		_loc(path, 1, 4, 5),
		_loc(path, 2, 4, 5),
		_loc(path, 3, 4, 5),
		_loc(path, 4, 5, 6),
		_loc(path, 5, 9, 10),
		_loc(path, 5, 15, 16),
		_loc(path, 6, 8, 9),
	]
	sites = classify_sites(locs, tmp_path, lambda p: p.read_text())
	kinds = [(s.kind, s.line) for s in sites]
	assert kinds == [
		("import", 1),
		("call", 2),
		("call", 3),
		("call", 4),
		("value", 5),
		("annotation", 6),
		("annotation", 6),
		("subclass", 7),
	]
	assert sites[1].args == Args(2, (), False, False)
	assert sites[2].args == Args(1, ("b",), False, False)
	assert sites[3].args is not None and sites[3].args.star and sites[3].args.kwstar and sites[3].args.spread


def test_given_unparsable_file_then_sites_are_still_reported_as_values(tmp_path):
	path = tmp_path / "bad.py"
	path.write_text("def (:\n")
	sites = classify_sites([_loc(path, 0, 0, 1)], tmp_path, lambda p: p.read_text())
	assert [(s.kind, s.path) for s in sites] == [("value", "bad.py")]


def test_given_paths_then_test_files_are_recognised():
	assert is_test_path("tests/unit/test_a.py")
	assert is_test_path("src/pkg/conftest.py")
	assert is_test_path("src/pkg/a_test.py")
	assert not is_test_path("src/pkg/testing_utils.py")


def test_given_mentions_then_groups_split_exports_patch_targets_and_strings():
	files = {"pkg/__init__.py": '__all__ = [\n\t"f",\n]\n', "t.py": ""}
	mentions = [
		Mention("pkg/__init__.py", 2, "string", '"f",'),
		Mention("t.py", 4, "string", 'mock.patch("pkg.mod.f")'),
		Mention("t.py", 5, "string", 'mock.patch("pkg.other.f")'),
		Mention("t.py", 6, "string", 'getattr(m, "f")'),
		Mention("t.py", 6, "string", 'getattr(m, "f")'),
		Mention("README.md", 3, "text", "call f"),
		Mention("t.py", 9, "comment", "# f"),
		Mention("t.py", 10, "attr", "m.f()", 2),
	]
	grouped = group_mentions(mentions, "f", {"pkg.mod.f"}, files.get)
	assert [m.line for m in grouped.all_exports] == [2]
	assert [(m.line, exact) for m, exact in grouped.patch_targets] == [(4, True), (5, False)]
	assert len(grouped.strings) == 1  # the duplicate on one line is one mention
	assert len(grouped.text) == len(grouped.comments) == len(grouped.unlinked) == 1


def _data(kind="function", **parts):
	return Data(Subject("f", None, kind, "src/a.py:3", file="src/a.py"), **parts)


def test_given_cut_groups_then_reply_says_how_many_and_how_to_see_more():
	sites = [Site(f"src/m{i}.py", 1, 0, "value", "use(f)") for i in range(5)]
	text = render_report(_data(sites=sites), "any", 1, 2).render()
	assert "Used as a value" in text and "... 3 more" in text
	assert 'next: impact(name="f", limit=5)' in text


def test_given_no_uses_then_answer_says_so():
	text = render_report(_data(), "delete", 1, 30).render()
	assert text.startswith("Nothing in the project references f")
	assert 'edit_symbol(name="f", action="delete", apply=false)' in text.splitlines()[-1]


def test_given_change_then_matching_write_call_is_last():
	subject = Subject("Cart.add", "src/cart.py", "method", "src/cart.py:9")
	assert write_calls(subject, "signature")[0].startswith(
		'change_signature(name="Cart.add", file_path="src/cart.py", add='
	)
	assert write_calls(subject, "rename")[0].startswith("rename")  # `rename_symbol` until the v2 tool is registered
	assert 'action="delete"' in write_calls(subject, "delete")[0]
	assert len(write_calls(subject, "any")) == 2
	port = Subject("Port", None, "class", "p.py:1", protocol=True)
	assert len(write_calls(port, "any")) == 1


def test_given_overrides_and_tests_then_they_have_their_own_groups():
	data = _data(
		sites=[
			Site("src/a.py", 4, 0, "call", "", Args(1, ("k",), False, False)),
			Site("tests/test_a.py", 8, 0, "call", "", Args(0, (), False, False)),
			Site("tests/test_a.py", 9, 0, "call", "", Args(0, (), False, False)),
		],
		overrides=[Overrides("B.f", "src/b.py:5")],
	)
	text = render_report(data, "signature", 1, 30).render()
	assert "src/a.py:4  (1 positional, kw: k)" in text
	assert "Implementations/overrides that must change together (1):" in text
	assert "tests/test_a.py  2 calls" in text
	assert "3 calls (1 in code, 2 in tests)" in text


# -- live ------------------------------------------------------------------------------

_FILES = {
	"src/pkg/__init__.py": 'from pkg.service import send\n\n__all__ = ["send"]\n',
	"src/pkg/service.py": (
		"from typing import Protocol\n\n\n"
		"def send(to: str, body: str = '', *, retries: int = 0) -> None:\n\tprint(to, body, retries)\n\n\n"
		"class Sender(Protocol):\n\tdef deliver(self, msg: str) -> None: ...\n\n\n"
		"class Mail:\n\tdef deliver(self, msg: str) -> None:\n\t\tsend(msg)\n\n\n"
		"class Loud(Mail):\n\tdef deliver(self, msg: str) -> None:\n\t\tsend(msg.upper())\n\n\n"
		"LIMIT = 3\n"
	),
	"src/pkg/app.py": (
		"from pkg.service import Loud, Mail, send\n\n\n"
		"def run(items: list[str], opts: dict[str, int]) -> None:\n"
		"\tsend('a')\n\tsend('a', 'b', retries=2)\n\tsend(*items)\n\tsend(**opts)\n"
		"\thandlers = [send]\n\tMail().deliver('x')\n\tLoud().deliver('y')\n\tprint(handlers)\n\n\n"
		"def main() -> None:\n\trun([], {})\n"
	),
	"src/pkg/dead.py": "def lonely() -> None:\n\tpass\n",
	"tests/test_app.py": (
		"from unittest import mock\n\nimport pkg\nfrom pkg.service import send\n\n\n"
		"def test_send():\n\tsend('t')\n\tsend('u', body='v')\n\tassert callable(pkg.send)\n\n\n"
		"def test_patch():\n\twith mock.patch('pkg.service.send'):\n\t\tpass\n"
	),
	"docs/api.md": "# API\n\nCall `send(to)` to deliver.\n",
}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(tmp_path, monkeypatch, _FILES)


def live(fn):
	return pytest.mark.integration(needs_ty(fn))


def _impact(loop, **kwargs):
	return str(loop.run_until_complete(analysis_tools.impact(**kwargs)))


@live
def test_given_function_then_every_group_is_reported(project, loop):
	text = _impact(loop, name="send", change="any")
	assert text.splitlines()[0].startswith(
		"Changing send (function, src/pkg/service.py:4) touches 8 calls, 2 used as value, 3 imports"
	)
	assert "src/pkg/service.py:14  (1 positional)" in text
	assert "src/pkg/app.py:5  (1 positional)" in text
	assert "src/pkg/app.py:6  (2 positional, kw: retries)" in text
	assert "(*args)" in text and "(**kwargs)" in text
	assert "src/pkg/app.py:9" in text and "handlers = [send]" in text  # used as a value
	assert "tests/test_app.py:10  assert callable(pkg.send)" in text
	assert "Re-exported in __all__ (1):\n  src/pkg/__init__.py:3" in text
	assert "[exact patch target]" in text
	assert "docs/api.md:3" in text
	assert "tests/test_app.py  2 calls, 1 import" in text
	assert text.splitlines()[-2].startswith('next: change_signature(name="send"')
	assert "# preview; type check only; tests and docs not checked" in text.splitlines()[-2]


@live
def test_given_method_then_overrides_and_their_callers_are_listed(project, loop):
	text = _impact(loop, name="Mail.deliver", change="signature")
	assert "Implementations/overrides that must change together (2):" in text
	assert "Loud.deliver  src/pkg/service.py:18" in text
	assert "Sender.deliver  src/pkg/service.py:9" in text
	assert "Uses of implementations/overrides" in text
	assert "src/pkg/app.py:10  (1 positional)" in text  # Mail().deliver
	assert "Loud.deliver: 1 call (e.g. src/pkg/app.py:11)" in text  # Loud().deliver, reported apart
	assert "Mail.deliver  src/pkg/service.py:13" in _impact(loop, name="Sender.deliver", change="signature")


@live
def test_given_unused_function_then_answer_says_nothing_references_it(project, loop):
	text = _impact(loop, name="lonely", change="delete")
	assert text.startswith("Nothing in the project references lonely")
	assert text.splitlines()[-1].startswith('next: edit_symbol(name="lonely", action="delete", apply=false)')


@live
def test_given_variable_then_uses_are_found_without_a_definition_node(project, loop):
	text = _impact(loop, name="LIMIT", change="rename")
	assert "LIMIT (variable" in text.splitlines()[0]
	assert text.splitlines()[-1].startswith("next: rename")


@live
def test_given_depth_two_then_callers_of_callers_are_added(project, loop):
	text = _impact(loop, name="send", change="signature", depth=2)
	assert "Transitive callers (depth 2)" in text
	assert "src/pkg/app.py:15 main  (calls run)" in text
	assert "no callers found" not in text


@live
def test_given_bad_arguments_then_the_error_names_the_fix(project, loop):
	assert "change must be one of" in _impact(loop, name="send", change="explode")
	assert "depth must be 1 to 3" in _impact(loop, name="send", depth=9)
	assert "limit must be at least 1" in _impact(loop, name="send", limit=0)
	assert "No symbol found matching 'no_such_symbol'" in _impact(loop, name="no_such_symbol")


@live
def test_given_ambiguous_name_then_reply_lists_exact_retries(project, loop):
	text = _impact(loop, name="deliver")
	assert text.startswith("3 symbols are named 'deliver'") or "Retry with one of" in text
	assert 'impact(name="Mail.deliver")' in text
