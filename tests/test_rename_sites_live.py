"""v2 `rename`: certain sites are written, uncertain ones are optional sites with ids (live ty)."""

from __future__ import annotations

import re

import pytest
from mcp_nav_shared.errors import FailedText

from codenav_mcp import edits_tools, server, write_tools, writes

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/ports.py": (
		"from typing import Protocol\n\n\nclass RunnerPort(Protocol):\n\tdef execute(self, sql: str) -> None: ...\n"
	),
	"src/pkg/runner.py": (
		"class Runner:\n"
		'\t"""Runs statements; call `execute` once per statement."""\n\n'
		"\tdef execute(self, sql: str) -> None:\n\t\tprint(sql)\n\n\n"
		"def go(port: 'RunnerPort') -> None:\n\tport.execute('x')\n\n\n"
		"from pkg.ports import RunnerPort  # noqa: E402\n"
	),
	"src/pkg/db.py": (
		"import sqlite3\n\n\n"
		"class Other:\n\tdef execute(self, a: int, b: int) -> None:\n\t\tpass\n\n\n"
		"def query(connection: sqlite3.Connection, other: Other) -> None:\n"
		"\tconnection.execute('select 1')\n"
		"\tother.execute(1, 2)\n"
	),
	"tests/__init__.py": "",
	"tests/test_run.py": (
		"from types import SimpleNamespace\n"
		"from unittest.mock import Mock\n\n\n"
		"class FakeRunner:\n\tdef execute(self, a, b, c):\n\t\tpass\n\n\n"
		"def test_it(monkeypatch) -> None:\n"
		"\tmonkeypatch.setattr('pkg.runner.Runner.execute', lambda self, sql: None)\n"
		"\truntime = Mock()\n"
		"\truntime.execute('a')\n"
		"\tservices = SimpleNamespace(runner=Mock(execute=print))\n"
		"\t# calls runtime.execute() once\n"
		"\t# execute the statements in order\n"
		"\tassert services\n"
	),
	".agents/tool.py": "def go(thing) -> None:\n\tthing.execute('x')\n",
}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	root = make_workspace(tmp_path, monkeypatch, _FILES)
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))
	return root


def _rename(loop, **kwargs):
	return loop.run_until_complete(write_tools.rename(name="RunnerPort.execute", new_name="run", **kwargs))


def _site_lines(text: str) -> dict[str, str]:
	return {m.group(1): m.group(0) for m in re.finditer(r"^\s*(u\d+)\s+.*$", text, re.M)}


def test_given_rename_without_sites_then_certain_sites_are_written_and_uncertain_ones_listed(project, loop):
	# when
	text = _rename(loop)
	# then: ty-linked members, the exact patch target and the typed calls are written
	assert "Applied as edit" in text
	assert "def run(self, sql: str)" in (project / "src/pkg/runner.py").read_text()
	assert "port.run('x')" in (project / "src/pkg/runner.py").read_text()
	body = (project / "tests/test_run.py").read_text()
	assert "'pkg.runner.Runner.run'" in body
	# then: nothing uncertain was written
	assert "runtime.execute('a')" in body and "Mock(execute=print)" in body and "def execute(self, a, b, c)" in body
	# then: each uncertain kind is an optional site with an id
	sites = _site_lines(text)
	kinds = {line.split()[2] for line in sites.values()}
	assert {"attr", "keyword", "def", "comment"} <= kinds
	assert "tests/test_run.py:13  attr  runtime.execute('a')" in text
	assert "execute the statements in order" not in "\n".join(sites.values())  # prose is not a site
	edit_id = re.search(r"Applied as edit (\w+)", text).group(1)
	assert f'edits(action="apply", id="{edit_id}", sites=["all"])' in text
	assert f'edits(action="undo", id="{edit_id}")' in text
	# then: receivers ty resolves elsewhere are not candidates, nor are dot-directories
	assert "connection.execute" not in text and "other.execute" not in text
	assert (project / "src/pkg/db.py").read_text() == _FILES["src/pkg/db.py"]
	assert (project / ".agents/tool.py").read_text() == _FILES[".agents/tool.py"]


def test_given_sites_all_then_every_optional_site_is_written_and_nothing_else(project, loop):
	text = _rename(loop, sites=["all"])
	assert "Applied as edit" in text and "optional site(s) included" in text
	body = (project / "tests/test_run.py").read_text()
	assert "runtime.run('a')" in body and "Mock(run=print)" in body and "def run(self, a, b, c)" in body
	assert "# calls runtime.run() once" in body
	assert "# execute the statements in order" in body  # a plain word in prose stays
	assert (project / "src/pkg/db.py").read_text() == _FILES["src/pkg/db.py"]
	assert (project / ".agents/tool.py").read_text() == _FILES[".agents/tool.py"]
	assert "`run` once per statement" in (project / "src/pkg/runner.py").read_text()


def test_given_one_site_id_then_only_that_site_is_added(project, loop):
	preview = _rename(loop, apply=False)
	sites = _site_lines(preview)
	keyword_id = next(site_id for site_id, line in sites.items() if " keyword " in line)
	text = _rename(loop, sites=[keyword_id])
	body = (project / "tests/test_run.py").read_text()
	assert "Mock(run=print)" in body
	assert "runtime.execute('a')" in body and "def execute(self, a, b, c)" in body
	assert keyword_id not in _site_lines(text) or " keyword " not in _site_lines(text)[keyword_id]


def test_given_preview_then_nothing_is_written_and_ids_are_stable(project, loop):
	first = _rename(loop, apply=False)
	second = _rename(loop, apply=False)
	assert "Preview only, nothing written" in first
	assert _site_lines(first) == _site_lines(second)
	assert (project / "tests/test_run.py").read_text() == _FILES["tests/test_run.py"]
	assert (project / "src/pkg/runner.py").read_text() == _FILES["src/pkg/runner.py"]


def test_given_unknown_site_id_then_the_reply_lists_the_valid_ids_and_an_exact_retry(project, loop):
	text = _rename(loop, sites=["u99"])
	assert isinstance(text, FailedText)
	assert "unknown site id(s) 'u99'; valid ids: u1" in text
	assert 'Example: rename(name="RunnerPort.execute", new_name="run", sites=["all"])' in text
	assert (project / "tests/test_run.py").read_text() == _FILES["tests/test_run.py"]


@pytest.mark.parametrize("preview", [False, True])
def test_given_rename_then_its_sites_can_be_applied_after_reloading_state(project, loop, preview):
	text = _rename(loop, apply=not preview)
	edit_id = re.search(r'edits\(action="apply", id="(\w+)"', text).group(1)
	writes.reset_state()
	shown = loop.run_until_complete(edits_tools.edits(action="show", id=edit_id))
	assert list(_site_lines(shown)) == list(_site_lines(text))

	result = loop.run_until_complete(edits_tools.edits(action="apply", id=edit_id, sites=["all"]))
	followup_id = re.search(r"Applied edit (\w+)", result).group(1)
	assert (followup_id == edit_id) is preview
	assert "runtime.run('a')" in (project / "tests/test_run.py").read_text()
	assert "def run(self, sql: str)" in (project / "src/pkg/runner.py").read_text()
	if not preview:
		writes.reset_state()
		loop.run_until_complete(edits_tools.edits(action="undo", id=followup_id))
		assert "runtime.execute('a')" in (project / "tests/test_run.py").read_text()
		assert "def run(self, sql: str)" in (project / "src/pkg/runner.py").read_text()


def test_applied_rename_refuses_stale_optional_only_file(project, loop):
	path = project / "tests/test_optional.py"
	path.write_text("from unittest.mock import Mock\n\nruntime = Mock()\nruntime.execute('a')\n")
	text = _rename(loop)
	edit_id = re.search(r"Applied as edit (\w+)", text).group(1)
	assert "tests/test_optional.py:4" in text
	path.write_text(path.read_text() + "\n# changed after rename\n")
	writes.reset_state()

	result = loop.run_until_complete(edits_tools.edits(action="apply", id=edit_id, sites=["all"]))

	assert "changed since edit" in result
	assert "runtime.execute('a')" in (project / "tests/test_run.py").read_text()


def test_given_a_partial_followup_then_other_sites_move_to_its_id_and_undo_is_ordered(project, loop):
	path = project / "tests/test_optional.py"
	path.write_text("from unittest.mock import Mock\n\nruntime = Mock()\nruntime.execute('a')\n")
	parent = _rename(loop)
	parent_id = re.search(r"Applied as edit (\w+)", parent).group(1)
	site_id = next(site_id for site_id, line in _site_lines(parent).items() if "tests/test_optional.py:4" in line)

	child = loop.run_until_complete(edits_tools.edits(action="apply", id=parent_id, sites=[site_id]))
	child_id = re.search(r"Applied edit (\w+)", child).group(1)
	writes.reset_state()
	shown = loop.run_until_complete(edits_tools.edits(action="show", id=child_id))
	assert site_id not in _site_lines(shown)
	assert f'edits(action="apply", id="{child_id}", sites=["all"])' in shown
	blocked = loop.run_until_complete(edits_tools.edits(action="undo", id=parent_id))
	assert f"undo {child_id} first" in blocked
	assert "runtime.run('a')" in path.read_text()


_DATACLASS = {
	"src/pkg/__init__.py": "",
	"src/pkg/model.py": "from dataclasses import dataclass\n\n\n@dataclass\nclass Job:\n\tlabel: str\n\tsize: int = 0\n",
	"src/pkg/use.py": "from pkg.model import Job\n\n\ndef make() -> Job:\n\treturn Job(label='a', size=1)\n",
	"tests/__init__.py": "",
	"tests/test_job.py": "from unittest.mock import Mock\n\n\ndef test_it() -> None:\n\tjob = Mock(label='x')\n\tassert job\n",
}


def test_given_field_rename_then_keyword_arguments_linked_by_ty_are_written(tmp_path, monkeypatch, loop):
	root = make_workspace(tmp_path, monkeypatch, _DATACLASS)
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))
	text = loop.run_until_complete(write_tools.rename(name="Job.label", new_name="title"))
	assert "Applied as edit" in text
	assert "title: str" in (root / "src/pkg/model.py").read_text()
	assert "Job(title='a', size=1)" in (root / "src/pkg/use.py").read_text()
	assert "Mock(label='x')" in (root / "tests/test_job.py").read_text()  # a mock keyword is optional
	assert " keyword " in text


_FUNCTION = {
	"src/pkg/__init__.py": "",
	"src/pkg/mod.py": "def helper() -> int:\n\treturn 1\n",
	"src/pkg/use.py": "from pkg.mod import helper\n\n\ndef go() -> int:\n\treturn helper()\n",
	"src/pkg/aliased.py": "from pkg.mod import helper as h\n\n\ndef go2() -> int:\n\treturn h()\n",
	"tests/__init__.py": "",
	"tests/test_use.py": (
		"def test_it(monkeypatch) -> None:\n"
		"\tmonkeypatch.setattr('pkg.use.helper', lambda: 2)\n"
		"\tmonkeypatch.setattr('pkg.mod.helper', lambda: 3)\n"
		"\tmonkeypatch.setattr('pkg.aliased.helper', lambda: 4)\n"
		"\tmonkeypatch.setattr('sqlite3.helper', lambda: 5)\n"
	),
}


def _function_project(tmp_path, monkeypatch):
	root = make_workspace(tmp_path, monkeypatch, _FUNCTION)
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))
	return root


def test_given_function_rename_then_patch_targets_in_the_defining_module_and_in_importers_follow(
	tmp_path, monkeypatch, loop
):
	root = _function_project(tmp_path, monkeypatch)
	text = loop.run_until_complete(write_tools.rename(name="helper", new_name="assist", file_path="src/pkg/mod.py"))
	assert "Applied as edit" in text
	body = (root / "tests/test_use.py").read_text()
	assert "'pkg.mod.assist'" in body  # the defining module's copy
	assert "'pkg.use.assist'" in body  # the importer rebinds the new name, so the patch target follows
	assert "from pkg.mod import assist\n" in (root / "src/pkg/use.py").read_text()
	assert "'sqlite3.helper'" in body  # not a workspace module: never touched
	# an aliased import keeps the old name: its patch target is an optional site, not written
	assert "'pkg.aliased.helper'" in body
	assert " string " in text and "tests/test_use.py:4" in text


def test_given_preview_then_the_site_list_comes_before_the_diff_and_notes_are_not_repeated(project, loop):
	text = _rename(loop, apply=False)
	assert text.count("Notes:") == 1
	assert text.index("optional site(s), not changed") < text.index("\n--- a/")
	assert text.index("next: edits(") < text.index("\n--- a/")


def test_given_null_file_path_over_mcp_then_it_is_accepted(project, loop):
	from mcp import Client

	async def scenario():
		async with Client(server.mcp, mode="legacy") as client:
			return await client.call_tool(
				"rename", {"name": "RunnerPort.execute", "new_name": "run", "file_path": None, "apply": False}
			)

	result = loop.run_until_complete(scenario())
	assert not result.is_error, result
