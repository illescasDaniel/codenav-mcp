"""An agent that has not seen the tool schemas still gets served (live ty).

Argument aliases on both call paths, `references` by name, ambiguous names answered as a list of exact retries
by the read tools (and still refused by the write tools), `rename_symbol(include_unlinked_attrs=...)`, and the
`callers` caveat for a Protocol member.
"""

from __future__ import annotations

import re

import pytest
from mcp import Client
from mcp_nav_shared.errors import FailedText

from codenav_mcp import edit_tools, edits_tools, read_tools, server, write_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty, required_match


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/ports.py": (
		"from typing import Protocol\n\n\n"
		"class DesktopPort(Protocol):\n\tdef reveal_path(self, path: str) -> None: ...\n\n\n"
		"class StoragePort(Protocol):\n\tdef get_hash(self, key: str) -> str: ...\n"
	),
	"src/pkg/desktop.py": (
		"class LinuxDesktop:\n\tdef reveal_path(self, path: str) -> None:\n\t\tprint(path)\n\n\n"
		"class FakeDesktop:\n\tdef reveal_path(self, path: str) -> None:\n\t\tpass\n"
	),
	"src/pkg/use.py": (
		"from pkg.ports import DesktopPort, StoragePort\n\n\n"
		"def go(desktop: DesktopPort) -> None:\n\tdesktop.reveal_path('x')\n\n\n"
		"def digest(storage: StoragePort) -> str:\n\treturn storage.get_hash('k')\n"
	),
	"src/pkg/a.py": "def build() -> int:\n\treturn 1\n\n\nclass Dup:\n\tdef go(self) -> int:\n\t\treturn 1\n",
	"src/pkg/b.py": "def build() -> int:\n\treturn 2\n\n\nclass Dup:\n\tdef go(self) -> int:\n\t\treturn 2\n",
	"src/pkg/p1.py": "from typing import Protocol\n\n\nclass Port(Protocol):\n\tdef run(self) -> None: ...\n",
	"src/pkg/p2.py": "from typing import Protocol\n\n\nclass Port(Protocol):\n\tdef run(self) -> None: ...\n",
	"src/pkg/plain.py": "class Plain:\n\tdef only(self) -> int:\n\t\treturn 1\n\n\ndef use_plain(p: Plain) -> int:\n\treturn p.only()\n",
	"tests/__init__.py": "",
	"tests/test_gui.py": (
		"from unittest.mock import Mock\n\n\n"
		"def test_it() -> None:\n"
		"\tdesktop = Mock()\n"
		"\tdesktop.reveal_path('a')\n"
		"\tlabel = 'reveal_path'\n"
		"\t# reveal_path is called above\n"
		"\treveal_path = 3\n"
		"\tassert label and reveal_path\n"
		"\tmock_build = Mock()\n"
		"\tmock_build.build()\n"
	),
}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	root = make_workspace(tmp_path, monkeypatch, _FILES)
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))
	return root


def _run(loop, coro):
	return loop.run_until_complete(coro)


def _mcp(loop, tool: str, args: dict):
	async def scenario():
		async with Client(server.mcp, mode="legacy") as client:
			return await client.call_tool(tool, args)

	return loop.run_until_complete(scenario())


def _text(result) -> str:
	if isinstance(result, str):
		return result
	return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def _failed(result) -> bool:
	"""A refusal: the tool functions return it as an error result (or FailedText when called bare)."""
	return isinstance(result, FailedText) or bool(getattr(result, "is_error", False))


# --- aliases on the direct MCP path -----------------------------------------------------------------------------


def test_given_symbol_for_name_when_called_over_mcp_then_it_runs(project, loop):
	result = _mcp(loop, "lookup", {"show": ["callers"], "symbol": "StoragePort.get_hash"})
	assert not result.is_error
	assert "Callers (1)" in _text(result)


def test_given_file_and_col_aliases_when_called_over_mcp_then_they_reach_the_tool(project, loop):
	result = _mcp(loop, "lookup", {"file": "src/pkg/use.py", "line_number": 5, "col": 10})
	assert not result.is_error
	assert "reveal_path" in _text(result)


def test_given_alias_when_the_value_looks_like_json_then_the_literal_string_path_uses_the_real_name(project, loop):
	# `new_text="[]"` is JSON-looking text: the call takes the SDK-bypassing dispatch, which must see `file_path`.
	result = _mcp(loop, "edit_symbol", {"edits": [{"filepath": "src/pkg/plain.py", "new_text": "[]"}]})
	assert not result.is_error, _text(result)
	assert (project / "src/pkg/plain.py").read_text().strip() == "[]"


def test_given_alias_and_target_with_different_values_when_called_over_mcp_then_refused(project, loop):
	result = _mcp(loop, "lookup", {"show": ["callers"], "symbol": "StoragePort.get_hash", "name": "Plain.only"})
	assert result.is_error
	assert "give either 'symbol' or 'name', not both" in _text(result)


def test_given_alias_and_target_with_the_same_value_over_mcp_then_accepted(project, loop):
	result = _mcp(
		loop, "lookup", {"show": ["callers"], "symbol": "StoragePort.get_hash", "name": "StoragePort.get_hash"}
	)
	assert not result.is_error


def test_given_search_symbol_path_when_called_over_mcp_then_path_stays_its_own_filter(project, loop):
	result = _mcp(loop, "search", {"query": "build", "path": "src/pkg/a.py"})
	text = _text(result)
	assert not result.is_error
	assert "src/pkg/a.py" in text
	assert "src/pkg/b.py" not in text


def test_given_unknown_argument_when_called_over_mcp_then_refusal_ends_with_the_example(project, loop):
	result = _mcp(loop, "lookup", {"show": ["callers"], "nope": 1})
	assert result.is_error
	assert _text(result).endswith('Example: lookup(name="Cart.total")')


def test_given_missing_required_argument_then_refusal_ends_with_the_example(project, loop):
	result = _mcp(loop, "lookup", {"file_path": "src/pkg/use.py"})
	assert result.is_error
	assert "Give name, or file_path with 1-based line and column." in _text(result)
	assert _text(result).endswith('Example: lookup(name="Cart.total")')


# --- aliases in a batch ------------------------------------------------------------------------------------------


# --- references by name ------------------------------------------------------------------------------------------


def test_given_name_when_references_then_lists_usages(project, loop):
	text = _run(loop, read_tools.lookup(name="StoragePort.get_hash", show=["references"]))
	assert not _failed(text)
	assert "References (2)" in text
	assert "src/pkg/use.py" in text


def test_given_name_and_file_path_when_references_then_file_path_only_narrows_the_name(project, loop):
	text = _run(loop, read_tools.lookup(name="build", file_path="src/pkg/a.py", show=["references"]))
	assert not _failed(text)
	assert "src/pkg/a.py" in text
	assert "src/pkg/b.py" not in text


def test_given_query_when_references_then_accepted_like_name(project, loop):
	text = _run(loop, read_tools.lookup(name="StoragePort.get_hash", show=["references"]))
	assert "src/pkg/use.py" in text


def test_given_position_when_references_positionally_then_still_works(project, loop):
	text = _run(loop, read_tools.lookup(file_path="src/pkg/ports.py", line=9, column=6, show=["references"]))
	assert "References (2)" in text
	assert "src/pkg/use.py" in text


def test_given_neither_name_nor_position_when_references_then_refused_with_an_example(project, loop):
	text = _run(loop, read_tools.lookup(show=["references"]))
	assert _failed(text)
	text = _text(text)
	assert "Give name, or file_path with 1-based line and column." in text
	assert 'Example: lookup(name="Cart.total")' in text


def test_given_name_and_position_when_references_then_refused(project, loop):
	text = _run(
		loop,
		read_tools.lookup(
			name="StoragePort.get_hash", file_path="src/pkg/ports.py", line=9, column=6, show=["references"]
		),
	)
	assert _failed(text)
	assert "Choose name with optional file_path" in _text(text)


def test_given_ambiguous_name_when_references_then_a_normal_listing_of_retries(project, loop):
	text = _run(loop, read_tools.lookup(name="build", show=["references"]))
	assert not _failed(text)
	assert text.startswith("2 symbols match 'build'.")
	assert 'lookup(name="build", file_path="src/pkg/a.py", show=["references"])' in text


# --- ambiguous names in read tools -------------------------------------------------------------------------------


def _retry_calls(text: str, tool: str) -> list[str]:
	return re.findall(rf"({tool}\([^\n]*\))$", text, flags=re.MULTILINE)


def test_given_ambiguous_name_when_symbol_info_then_not_an_error_and_every_candidate_has_a_retry(project, loop):
	text = _run(loop, read_tools.lookup(name="reveal_path"))
	assert not _failed(text)
	assert text.startswith("3 symbols match 'reveal_path'.")
	assert sorted(_retry_calls(text, "lookup")) == [
		'lookup(name="DesktopPort.reveal_path", file_path="src/pkg/ports.py")',
		'lookup(name="FakeDesktop.reveal_path", file_path="src/pkg/desktop.py")',
		'lookup(name="LinuxDesktop.reveal_path", file_path="src/pkg/desktop.py")',
	]
	assert "src/pkg/ports.py:5  DesktopPort.reveal_path [Method]" in text


def test_given_a_listed_retry_when_run_then_it_resolves(project, loop):
	text = _run(loop, read_tools.lookup(name="reveal_path"))
	retry = _retry_calls(text, "lookup")[0]
	name = required_match(r'name="([^"]+)"', retry).group(1)
	resolved = _run(loop, read_tools.lookup(name=name))
	assert resolved.startswith(name + "  ")


def test_given_ambiguous_name_when_callers_then_not_an_error_and_lists_callers_retries(project, loop):
	text = _run(loop, read_tools.lookup(name="reveal_path", show=["callers"]))
	assert not _failed(text)
	assert 'lookup(name="DesktopPort.reveal_path", file_path="src/pkg/ports.py", show=["callers"])' in text


def test_given_same_name_in_two_files_then_the_retry_adds_file_path(project, loop):
	text = _run(loop, read_tools.lookup(name="build"))
	assert not _failed(text)
	assert 'lookup(name="build", file_path="src/pkg/a.py")' in text
	assert 'lookup(name="build", file_path="src/pkg/b.py")' in text


def test_given_ambiguous_class_in_a_dotted_name_then_the_retry_keeps_the_member_and_adds_file_path(project, loop):
	text = _run(loop, read_tools.lookup(name="Dup.go"))
	assert not _failed(text)
	assert 'lookup(name="Dup.go", file_path="src/pkg/a.py")' in text
	assert 'lookup(name="Dup.go", file_path="src/pkg/b.py")' in text


def test_given_ambiguous_port_when_implementations_then_a_normal_listing(project, loop):
	text = _run(loop, read_tools.lookup(name="Port", show=["implementations"]))
	assert not _failed(text)
	assert 'lookup(name="Port", file_path="src/pkg/p1.py", show=["implementations"])' in text


def test_given_unknown_name_when_symbol_info_then_still_an_error(project, loop):
	text = _run(loop, read_tools.lookup(name="no_such_thing_anywhere"))
	assert _failed(text)
	assert _text(text).startswith("No symbol found matching")


@pytest.mark.parametrize(
	("tool", "args"),
	[
		("rename", {"name": "reveal_path", "new_name": "reveal_in_file_manager"}),
		("edit_symbol", {"action": "delete", "name": "build"}),
		("move", {"name": "build", "to_file": "src/pkg/moved.py"}),
		("change_signature", {"name": "build", "add": [{"name": "x", "annotation": "int", "default": "0"}]}),
	],
)
def test_given_ambiguous_name_when_a_write_tool_then_still_a_refusal_that_writes_nothing(project, loop, tool, args):
	before = {p: p.read_text() for p in project.rglob("*.py")}
	result = _mcp(loop, tool, args)
	assert result.is_error
	assert re.search(r"\d+ symbols match '", _text(result))
	assert {p: p.read_text() for p in project.rglob("*.py")} == before


# --- edit_symbol delete: a same-named symbol's own call is not a blocker ------------------------------------------


def test_given_same_named_function_called_elsewhere_when_delete_then_the_other_symbols_call_does_not_hold(
	tmp_path, monkeypatch, loop
):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/a.py": "def helper() -> int:\n\treturn 1\n",
			"src/pkg/b.py": "def helper() -> int:\n\treturn 2\n\n\ndef run() -> int:\n\treturn helper()\n",
		},
	)
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))
	text = _run(loop, edit_tools.edit_symbol(action="delete", name="helper", file_path="src/pkg/a.py"))
	assert "Applied as edit" in text, text
	assert "1 other use(s) of the name `helper` resolve to a different symbol and were ignored" in text
	assert "helper" not in (root / "src/pkg/a.py").read_text()
	assert "return helper()" in (root / "src/pkg/b.py").read_text()


def test_given_a_name_that_is_really_used_when_delete_then_still_held(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/a.py": "def helper() -> int:\n\treturn 1\n",
			"src/pkg/b.py": "from pkg import a\n\n\ndef run() -> int:\n\treturn a.helper()\n",
		},
	)
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))
	text = _run(loop, edit_tools.edit_symbol(action="delete", name="helper"))
	assert "Not deleted" in text
	assert "def helper" in (root / "src/pkg/a.py").read_text()


@pytest.mark.parametrize(
	("tool", "args", "expected"),
	[
		(
			"edit_symbol",
			{"action": "delete", "name": "build"},
			'edit_symbol(action="delete", name="build", file_path="src/pkg/a.py")',
		),
		(
			"edit_symbol",
			{"action": "replace", "name": "build", "source": "def build() -> int: ..."},
			'edit_symbol(action="replace", name="build", file_path="src/pkg/b.py")',
		),
		(
			"rename",
			{"name": "build", "new_name": "make"},
			'rename(new_name="make", name="build", file_path="src/pkg/a.py")',
		),
		(
			"change_signature",
			{"name": "build", "add": [{"name": "x", "annotation": "int", "default": "0"}]},
			'change_signature(name="build", file_path="src/pkg/b.py")',
		),
		(
			"move",
			{"name": "build", "to_file": "src/pkg/c.py"},
			'move(to_file="src/pkg/c.py", name="build", file_path="src/pkg/a.py")',
		),
	],
)
def test_given_ambiguous_name_when_a_write_tool_refuses_then_it_lists_the_exact_retry_calls(
	project, loop, tool, args, expected
):
	result = _mcp(loop, tool, args)
	text = _text(result)
	assert result.is_error
	assert re.search(r"\d+ symbols match '", text)
	assert "Nothing was changed. Retry with one of" in text
	assert expected in text


# --- rename_symbol(include_unlinked_attrs) -----------------------------------------------------------------------


def _rename(loop, **kwargs):
	return _run(loop, write_tools.rename(name="DesktopPort.reveal_path", new_name="reveal_in_file_manager", **kwargs))


def test_given_default_when_rename_member_preview_then_unlinked_attr_stays_and_note_says_how_to_include_it(
	project, loop
):
	text = _rename(loop, apply=False)
	assert "optional site(s), not changed" in text
	assert "tests/test_gui.py:6" in text
	assert 'sites=["u1"]' in text
	assert "-\tdesktop.reveal_path('a')" not in text


def test_given_default_when_rename_member_applied_then_the_note_says_to_undo_then_rerun(project, loop):
	text = _rename(loop)
	assert (project / "tests/test_gui.py").read_text() == _FILES["tests/test_gui.py"]
	edit_id = required_match(r"Applied as edit (\w+)", text).group(1)
	assert f'edits(action="apply", id="{edit_id}", sites=["u1"])' in text
	assert f'edits(action="undo", id="{edit_id}")' in text


def test_given_flag_when_rename_member_preview_then_the_attr_edit_is_in_the_plan_and_nothing_is_written(project, loop):
	text = _rename(loop, apply=False, sites=["u1"])
	assert "-\tdesktop.reveal_path('a')" in text
	assert "+\tdesktop.reveal_in_file_manager('a')" in text
	assert "1 optional site(s) included" in text
	assert "nothing was changed" not in text
	assert (project / "tests/test_gui.py").read_text() == _FILES["tests/test_gui.py"]


def test_given_a_preview_when_apply_edit_then_no_placeholder_leaks_in_either_reply(project, loop):
	preview = _rename(loop, apply=False)
	edit_id = required_match(r'edits\(action="apply", id="(\w+)"\)', preview).group(1)
	applied = _run(loop, edits_tools.edits(id=edit_id, action="apply"))
	assert "<rerun" not in preview + applied
	assert 'sites=["u1"]' in preview
	assert "Applied edit" in applied


def test_given_str_replace_argument_names_when_edit_over_mcp_then_they_are_old_string_and_new_string(project, loop):
	result = _mcp(
		loop, "edit_symbol", {"edits": [{"path": "src/pkg/plain.py", "old_str": "return 1", "new_str": "return 2"}]}
	)
	assert not result.is_error, _text(result)
	assert "return 2" in (project / "src/pkg/plain.py").read_text()


def test_given_flag_when_rename_member_applied_then_only_the_attribute_access_changes(project, loop):
	text = _rename(loop, sites=["u1"])
	assert "Applied as edit" in text
	body = (project / "tests/test_gui.py").read_text()
	assert "desktop.reveal_in_file_manager('a')" in body
	assert "label = 'reveal_path'" in body  # string untouched
	assert "# reveal_path is called above" in body  # comment untouched
	assert "reveal_path = 3" in body  # bare name untouched
	assert "reveal_path" not in (project / "src/pkg/ports.py").read_text()


_DECOY_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/ports.py": "from typing import Protocol\n\n\nclass RunnerPort(Protocol):\n\tdef execute(self, sql: str) -> None: ...\n",
	"src/pkg/db.py": (
		"import sqlite3\n\n\n"
		"class Other:\n\tdef execute(self, a: int, b: int) -> None:\n\t\tpass\n\n\n"
		"def query(connection: sqlite3.Connection, other: Other) -> None:\n"
		"\tconnection.execute('select 1')\n"
		"\tother.execute(1, 2)\n"
	),
	"tests/__init__.py": "",
	"tests/test_run.py": "from unittest.mock import Mock\n\n\ndef test_it() -> None:\n\truntime = Mock()\n\truntime.execute('a')\n",
	".agents/tool.py": "def go(thing) -> None:\n\tthing.execute('x')\n",
}


def test_given_flag_when_attr_resolves_to_another_symbol_then_only_the_mock_access_is_renamed(
	tmp_path, monkeypatch, loop
):
	# given — a typed sqlite3.Connection, a local class with the same method name, a Mock, and a dot-directory
	root = make_workspace(tmp_path, monkeypatch, _DECOY_FILES)
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))
	# when
	text = _run(
		loop,
		write_tools.rename(name="RunnerPort.execute", new_name="run", sites=["all"]),
	)
	# then
	assert "Applied as edit" in text
	assert "runtime.run('a')" in (root / "tests/test_run.py").read_text()
	db = (root / "src/pkg/db.py").read_text()
	assert "connection.execute('select 1')" in db and "other.execute(1, 2)" in db
	assert "def execute(self, a: int, b: int)" in db
	assert (root / ".agents/tool.py").read_text() == _DECOY_FILES[".agents/tool.py"]
	assert "1 optional site(s) included" in text
	assert "2 attribute access(es) resolve to another symbol" in text
	assert ".agents" not in text


def test_given_flag_when_the_rename_is_a_module_function_then_ignored_with_a_note_and_attrs_untouched(project, loop):
	text = _run(
		loop,
		write_tools.rename(name="build", file_path="src/pkg/a.py", new_name="make"),
	)
	assert "Applied as edit" in text
	assert "mock_build.build()" in (project / "tests/test_gui.py").read_text()


def test_given_flag_when_rename_is_a_bare_name_then_no_attr_edits_are_added(project, loop):
	text = _run(
		loop,
		write_tools.rename(name="build", file_path="src/pkg/a.py", new_name="make", apply=False),
	)
	assert "Preview only" in text
	assert "tests/test_gui.py" not in text.split("Files:")[1].split("Notes:")[0]


# --- callers on a Protocol member --------------------------------------------------------------------------------


def test_given_tests_dir_outside_the_source_root_when_implementations_then_says_it_was_not_scanned(project, loop):
	text = _run(loop, read_tools.lookup(name="DesktopPort", show=["implementations"]))
	assert "Scanned only src; tests/ not scanned" in text
	assert "CODENAV_MCP_EXTRA_SOURCE_ROOTS=tests" in text
	empty = _run(loop, read_tools.lookup(name="StoragePort", show=["implementations"]))
	assert "CODENAV_MCP_EXTRA_SOURCE_ROOTS=tests" in empty


def test_given_extra_root_set_when_implementations_then_no_unscanned_note(project, loop, monkeypatch):
	monkeypatch.setattr(server, "EXTRA_SOURCE_ROOTS", [project / "tests"])
	text = _run(loop, read_tools.lookup(name="DesktopPort", show=["implementations"]))
	assert "not scanned" not in text


def test_given_protocol_member_when_callers_then_the_note_points_to_implementations(project, loop):
	text = _run(loop, read_tools.lookup(name="StoragePort.get_hash", show=["callers"]))
	assert "Callers (1)" in text
	assert "Through StoragePort.get_hash (1)" in text


def test_given_plain_method_without_subclasses_when_callers_then_no_static_note(project, loop):
	text = _run(loop, read_tools.lookup(name="Plain.only", show=["callers"]))
	assert "Static callers only" not in text


# --- references / symbol_info list every file within `limit` -----------------------------------------------------


@pytest.fixture
def many_users(project):
	(project / "src/pkg/core.py").write_text("def shared() -> int:\n\treturn 1\n", encoding="utf-8")
	for index in range(30):
		(project / f"src/pkg/user_{index:02d}.py").write_text(
			f"from pkg.core import shared\n\n\ndef use_{index}() -> int:\n\treturn shared() + shared()\n",
			encoding="utf-8",
		)
	return project


def test_given_thirty_files_and_a_big_limit_when_references_then_every_file_is_listed(many_users, loop):
	text = _text(_mcp(loop, "lookup", {"show": ["references"], "name": "shared", "limit": 200}))
	assert "References (91)" in text  # the definition, 30 imports, 60 calls
	assert all(f"src/pkg/user_{index:02d}.py:" in text for index in range(30))
	assert "more file(s)" not in text and "more omitted" not in text


def test_given_a_small_limit_when_references_then_hits_beyond_it_are_counted_with_the_hint(many_users, loop):
	text = _text(_mcp(loop, "lookup", {"show": ["references"], "name": "shared", "limit": 20}))
	assert "References (91)" in text
	assert "... 71 more" in text
	assert "more file(s)" not in text


def test_given_thirty_files_when_symbol_info_then_the_omitted_files_line_says_how_to_list_them(many_users, loop):
	text = _text(_mcp(loop, "lookup", {"name": "shared"}))
	call = 'lookup(name="shared", show=["references"])'
	assert f"91 references (next: {call})" in text
	# and that call does list every file
	listed = _text(
		_mcp(
			loop,
			"lookup",
			{"show": ["references"], "file_path": "src/pkg/core.py", "line": 1, "column": 5, "limit": 91},
		)
	)
	assert all(f"src/pkg/user_{index:02d}.py:" in listed for index in range(30))
