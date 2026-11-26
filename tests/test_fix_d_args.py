"""D5 (F3/F7, H3#7): argument aliases in nested edit items, unknown item keys, and `True` versus `1`."""

from __future__ import annotations

import asyncio

import pytest

from codenav_mcp.args import resolve_aliases


ITEM_KEYS = {"file_path", "old_string", "new_string", "replace_all", "new_text", "name", "action", "source"}


@pytest.mark.parametrize(("old", "new"), [("old", "new"), ("old_str", "new_str")])
def test_given_old_and_new_aliases_in_an_item_then_they_become_old_string_and_new_string(old, new):
	item, problem = resolve_aliases("edit item", ITEM_KEYS, {"file_path": "a.py", old: "x = 1", new: "x = 2"})
	assert problem is None
	assert item == {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}


def test_given_an_unknown_item_key_then_the_refusal_suggests_the_closest_key_and_shows_a_literal_item():
	_, problem = resolve_aliases("edit item", ITEM_KEYS, {"file_path": "a.py", "old_string": "a", "new_strng": "b"})
	assert problem is not None
	assert "'new_strng'" in problem and "did you mean" in problem and "'new_string'" in problem
	assert 'Example: {"file_path"' in problem and '"old_string"' in problem


def test_given_an_unknown_key_with_no_close_match_then_the_refusal_still_lists_keys_and_the_example():
	_, problem = resolve_aliases("edit item", ITEM_KEYS, {"file_path": "a.py", "zzz": 1})
	assert problem is not None and "'zzz'" in problem and "allowed:" in problem and "Example:" in problem


def test_given_a_top_level_tool_then_old_and_new_do_not_become_aliases_without_the_target():
	result, problem = resolve_aliases("rename", {"name", "new_name"}, {"name": "a", "new_name": "b"})
	assert problem is None and result == {"name": "a", "new_name": "b"}


def test_given_an_alias_true_and_its_target_one_then_they_are_a_conflict_not_a_merge():
	resolved, problem = resolve_aliases("t", {"line"}, {"line": 1, "lineno": True})
	assert problem is not None and "not both" in problem, resolved


def test_given_an_alias_and_target_with_the_same_value_then_they_still_merge():
	assert resolve_aliases("t", {"line"}, {"line": 3, "lineno": 3}) == ({"line": 3}, None)


def test_given_old_new_items_in_edit_symbol_then_the_edit_is_planned(tmp_path):
	"""Through `multi_edit`: an item written with `old`/`new` reaches the text-edit path (no unknown-key refusal)."""
	from codenav_mcp import multi_edit

	problem = asyncio.run(
		multi_edit._plan_item({"file_path": "a.py", "old": "x", "new": "x"}, _unused_symbol_action, None)
	)[1]
	assert problem == "`old_string` and `new_string` are identical"


async def _unused_symbol_action(*_args, **_kwargs) -> str:
	raise AssertionError("a text item never reaches the symbol action")
