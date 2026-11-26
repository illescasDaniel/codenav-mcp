"""`edit_symbol(edits=[...])`: several edits as one plan, all or nothing, against a real `ty`."""

from __future__ import annotations

import re

import pytest
from mcp.client import Client

from codenav_mcp import edit_tools, edits_tools, server, writes

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_CORE = (
	"def parse(text: str) -> int:\n"
	'\t"""Parse."""\n'
	"\treturn int(text)\n"
	"\n\n"
	"def helper() -> int:\n"
	"\treturn 1\n"
	"\n\n"
	"class Box:\n"
	"\tdef size(self) -> int:\n"
	"\t\tlimit = 10\n"
	"\t\treturn limit\n"
)
_USER = "from pkg.core import parse\n\n\ndef use() -> int:\n\treturn parse('1')\n"
_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/core.py": _CORE,
	"src/pkg/user.py": _USER,
	"src/pkg/other.py": "VALUE = 1\nNAME = 'a'\n",
}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	root = make_workspace(tmp_path, monkeypatch, _FILES)
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))
	return root


def _run(loop, coro):
	return loop.run_until_complete(coro)


def _multi(loop, items, **kwargs) -> str:
	return str(_run(loop, edit_tools.edit_symbol(edits=items, **kwargs)))


def _text_item(path: str, old: str, new: str, **extra) -> dict:
	return {"file_path": path, "old_string": old, "new_string": new, **extra}


def _read(project, name: str) -> str:
	return (project / name).read_text()


def test_given_three_matching_items_across_two_files_then_one_plan_is_applied_with_one_undo_id(project, loop):
	text = _multi(
		loop,
		[
			_text_item("src/pkg/other.py", "VALUE = 1", "VALUE = 2"),
			_text_item("src/pkg/user.py", "parse('1')", "parse('2')"),
			{"name": "helper", "action": "replace", "source": "def helper() -> int:\n\treturn 3\n"},
		],
	)

	assert text.startswith("Apply 3 edits (3 files)\n  item 1: Edit src/pkg/other.py")
	assert "Applied as edit" in text and "Type check: 0 new error(s)" in text
	assert "tests and docs not checked" in text
	assert "VALUE = 2" in _read(project, "src/pkg/other.py")
	assert "parse('2')" in _read(project, "src/pkg/user.py")
	assert "return 3" in _read(project, "src/pkg/core.py")
	edit_id = re.search(r'edits\(action="undo", id="(\w+)"\)', text).group(1)
	assert len(writes.write_state(project).journal.history()) == 1

	undone = str(_run(loop, edits_tools.edits(action="undo", id=edit_id)))

	assert undone.startswith(f"Reverted edit {edit_id}")
	assert _read(project, "src/pkg/other.py") == _FILES["src/pkg/other.py"]
	assert _read(project, "src/pkg/user.py") == _USER
	assert _read(project, "src/pkg/core.py") == _CORE


def test_given_the_third_item_does_not_match_then_nothing_is_written_and_no_preview_is_stored(project, loop):
	text = _multi(
		loop,
		[
			_text_item("src/pkg/other.py", "VALUE = 1", "VALUE = 2"),
			_text_item("src/pkg/user.py", "parse('1')", "parse('2')"),
			_text_item("src/pkg/core.py", "return nothing_like_this", "return 0"),
		],
	)

	assert text.startswith("Nothing written: 1 of 3 edits failed.")
	assert "item 3 (src/pkg/core.py): old_string was not found in the file" in text
	assert "item 1" not in text and "item 2" not in text  # only the failing item is listed
	assert _read(project, "src/pkg/other.py") == _FILES["src/pkg/other.py"]
	assert _read(project, "src/pkg/user.py") == _USER
	state = writes.write_state(project)
	assert state.plans.pending() == [] and state.journal.history() == []


def test_given_several_failing_items_then_every_one_is_named(project, loop):
	text = _multi(
		loop,
		[
			_text_item("src/pkg/other.py", "nope", "x"),
			{"name": "does_not_exist", "action": "delete"},
			_text_item("src/pkg/missing.py", "a", "b"),
		],
	)

	assert text.startswith("Nothing written: 3 of 3 edits failed.")
	for label in ("item 1 (src/pkg/other.py)", "item 2 (delete does_not_exist)", "item 3 (src/pkg/missing.py)"):
		assert label in text


def test_given_apply_false_then_it_previews_under_one_id_and_edits_apply_writes_it(project, loop):
	preview = _multi(
		loop,
		[_text_item("src/pkg/other.py", "VALUE = 1", "VALUE = 2"), _text_item("src/pkg/user.py", "'1'", "'7'")],
		apply=False,
	)
	plan_id = re.search(r'edits\(action="apply", id="(\w+)"\)', preview).group(1)
	assert "Preview only, nothing written" in preview
	assert _read(project, "src/pkg/other.py") == _FILES["src/pkg/other.py"]
	writes.reset_state()  # a restart in between

	applied = str(_run(loop, edits_tools.edits(action="apply", id=plan_id)))

	assert applied.startswith(f"Applied edit {plan_id}: Apply 2 edits")
	assert "VALUE = 2" in _read(project, "src/pkg/other.py") and "'7'" in _read(project, "src/pkg/user.py")


def test_given_edits_in_one_file_then_distant_ones_merge_and_overlapping_ones_name_both_items(project, loop):
	merged = _multi(
		loop,
		[
			_text_item("src/pkg/core.py", "return int(text)", "return int(text) + 1"),
			_text_item("src/pkg/core.py", "limit = 10", "limit = 20"),
		],
	)
	assert "Applied as edit" in merged
	core = _read(project, "src/pkg/core.py")
	assert "int(text) + 1" in core and "limit = 20" in core

	clash = _multi(
		loop,
		[
			_text_item("src/pkg/other.py", "VALUE = 1", "VALUE = 2"),
			_text_item("src/pkg/other.py", "VALUE = 1\nNAME", "VALUE = 3\nNAME"),
		],
	)
	assert clash.startswith("Nothing written: 1 of 2 edits failed.")
	assert "item 1 and item 2 both change other.py" in clash
	assert _read(project, "src/pkg/other.py") == _FILES["src/pkg/other.py"]


def test_given_symbol_items_in_one_file_then_replace_body_insert_and_delete_combine(project, loop):
	text = _multi(
		loop,
		[
			{"name": "Box.size", "action": "replace_body", "source": "return 99"},
			{
				"name": "parse",
				"action": "insert_after",
				"file_path": "src/pkg/core.py",
				"source": "def extra() -> int:\n\treturn 2\n",
			},
			{"name": "helper", "action": "delete"},
		],
	)

	assert "Applied as edit" in text, text
	core = _read(project, "src/pkg/core.py")
	assert "return 99" in core and "limit = 10" not in core
	assert "def extra()" in core and "def helper" not in core
	assert "class Box:\n\tdef size(self) -> int:\n" in core


def test_given_an_item_that_would_only_be_previewed_then_the_call_fails_instead_of_half_applying(project, loop):
	(project / "src/pkg/user.py").write_text(_USER + "\n\ndef again() -> int:\n\treturn parse('2')\n")

	text = _multi(
		loop, [_text_item("src/pkg/other.py", "VALUE = 1", "VALUE = 2"), {"name": "parse", "action": "delete"}]
	)

	assert text.startswith("Nothing written: 1 of 2 edits failed.")
	assert "item 2 (delete parse)" in text
	assert _read(project, "src/pkg/other.py") == _FILES["src/pkg/other.py"]


def test_given_new_type_errors_in_the_combined_plan_then_nothing_is_written_and_the_plan_is_kept(project, loop):
	text = _multi(
		loop,
		[
			_text_item("src/pkg/other.py", "VALUE = 1", "VALUE = 2"),
			{
				"name": "parse",
				"action": "replace",
				"source": "def parse(text: str, base: int) -> int:\n\treturn int(text, base)\n",
			},
		],
	)

	assert "NOT applied" in text and "new error" in text
	assert _read(project, "src/pkg/other.py") == _FILES["src/pkg/other.py"]
	assert re.search(r'edits\(action="apply", id="\w+"\)', text)
	assert len(writes.write_state(project).plans.pending()) == 1


def test_given_max_new_errors_null_then_the_combined_plan_is_written(project, loop):
	text = _multi(
		loop,
		[
			{
				"name": "parse",
				"action": "replace",
				"source": "def parse(text: str, base: int) -> int:\n\treturn int(text, base)\n",
			}
		],
		max_new_errors=None,
	)

	assert "Applied as edit" in text


def test_given_bad_shapes_then_the_error_is_specific(project, loop):
	assert "non-empty list" in _multi(loop, [])
	assert "unknown key(s) old for a text edit" in _multi(loop, [{"file_path": "a.py", "old_string": "x", "old": "y"}])
	assert "each item is an object" in _multi(loop, ["x = 1"])
	assert "needs `file_path`" in _multi(loop, [{"old_string": "a", "new_string": "b"}])
	assert "put them inside the items of `edits`" in _multi(loop, [{"name": "x"}], name="parse")
	assert "put them inside" in str(_run(loop, edit_tools.edit_symbol(action="delete", edits=[])))
	assert "empty item" in _multi(loop, [{}])


def test_given_over_the_limit_items_then_it_refuses(project, loop):
	items = [_text_item("src/pkg/other.py", "VALUE", "VALUE") for _ in range(51)]

	assert "limit 50" in _multi(loop, items)


def test_given_the_1x_single_calls_then_they_still_work(project, loop):
	replaced = str(
		_run(loop, edit_tools.edit_symbol(action="replace", name="helper", source="def helper() -> int:\n\treturn 5\n"))
	)
	body = str(_run(loop, edit_tools.edit_symbol(action="replace_body", name="Box.size", source="return 7")))
	default_action = str(_run(loop, edit_tools.edit_symbol(name="helper", source="def helper() -> int:\n\treturn 6\n")))
	after = str(
		_run(
			loop,
			edit_tools.edit_symbol(
				action="insert_after",
				name="helper",
				file_path="src/pkg/core.py",
				source="def tail() -> int:\n\treturn 0\n",
			),
		)
	)

	assert all("Applied as edit" in text for text in (replaced, body, default_action, after))
	core = _read(project, "src/pkg/core.py")
	assert "return 7" in core and "return 6" in core and "def tail()" in core
	assert "Apply" not in replaced.splitlines()[0]  # a single call is not worded as a multi-edit


def test_given_replace_body_on_a_one_line_function_or_a_class_then_it_refuses(project, loop):
	(project / "src/pkg/one.py").write_text("def one(): return 1\n\n\nclass K:\n\tpass\n")

	inline = str(_run(loop, edit_tools.edit_symbol(action="replace_body", name="one", source="return 2")))
	klass = str(_run(loop, edit_tools.edit_symbol(action="replace_body", name="K", source="pass")))

	assert "body on the signature line" in inline
	assert "is a class" in klass


def test_given_replace_body_without_a_docstring_then_a_note_says_it_is_gone(project, loop):
	text = str(_run(loop, edit_tools.edit_symbol(action="replace_body", name="parse", source="return 0", apply=False)))

	assert "the old docstring was part of the body and is gone" in text
	assert '-\t"""Parse."""' in text


# -- over MCP: the schema accepts the list, the served call is remembered for a stale refusal


def _mcp(loop, tool: str, args: dict):
	async def scenario():
		async with Client(server.mcp, mode="legacy") as client:
			return await client.call_tool(tool, args)

	return loop.run_until_complete(scenario())


def _text(result) -> str:
	return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def test_given_the_call_over_mcp_when_a_file_changes_then_the_stale_refusal_repeats_the_exact_call(project, loop):
	args = {"edits": [_text_item("src/pkg/other.py", "VALUE = 1", "VALUE = 2")], "apply": False}
	preview = _mcp(loop, "edit_symbol", args)
	assert not preview.is_error, _text(preview)
	plan_id = re.search(r'edits\(action="apply", id="(\w+)"\)', _text(preview)).group(1)
	(project / "src/pkg/other.py").write_text("VALUE = 1\nNAME = 'b'\n")
	writes.reset_state()

	stale = _mcp(loop, "edits", {"action": "apply", "id": plan_id})

	assert stale.is_error
	assert "changed since the preview was made: src/pkg/other.py" in _text(stale)
	assert (
		'edit_symbol(edits=[{"file_path": "src/pkg/other.py", "old_string": "VALUE = 1", "new_string": "VALUE = 2"}], apply=false)'
		in _text(stale)
	)


def test_given_edits_over_mcp_then_a_failing_item_is_an_error_result_and_writes_nothing(project, loop):
	result = _mcp(
		loop,
		"edit_symbol",
		{
			"edits": [
				_text_item("src/pkg/other.py", "VALUE = 1", "VALUE = 2"),
				_text_item("src/pkg/other.py", "zzz", "y"),
			]
		},
	)

	assert result.is_error
	assert "item 2 (src/pkg/other.py)" in _text(result)
	assert _read(project, "src/pkg/other.py") == _FILES["src/pkg/other.py"]
