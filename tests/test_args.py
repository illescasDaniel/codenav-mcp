"""Argument aliases and refusals on the real v2 tools (no language server).

These pin what `args.py` promises on every tool: guessed names are renamed, bad calls are refused by name with
the allowed parameters and the tool's `Example:`. They replace the checks that lived beside the removed `batch` tool.
"""

from __future__ import annotations

import ast

import pytest

from codenav_mcp import (
	args,
	multi_edit,
	server as _server,  # noqa: F401  (importing it registers the tools)
)


TOOLS = [
	"lookup",
	"search",
	"outline",
	"check",
	"unused",
	"impact",
	"rename",
	"change_signature",
	"move",
	"edit_symbol",
	"fix",
	"edits",
]


def _fn(name: str):
	fn = args.signature_of(name)
	assert fn is not None
	return fn


def _refusal(tool: str, arguments: dict) -> str:
	"""The refusal `_checked_call_tool` would give (aliases first, then the argument check); "" when the call passes."""
	fn = _fn(tool)
	arguments, problem = args.apply_aliases(tool, fn, arguments)
	return problem or args.argument_problem(tool, fn, arguments) or ""


def test_given_every_tool_then_it_is_registered_and_nothing_else():
	assert args.registered_tools() == sorted(TOOLS)


def test_given_a_typo_in_a_tool_name_then_the_closest_is_suggested():
	message = args.unknown_tool_message("lookpu")
	assert "Unknown tool 'lookpu'" in message and "Did you mean 'lookup'" in message
	assert "Available: " in message and "edit_symbol" in message


def test_given_a_removed_v1_tool_name_then_it_is_refused_with_the_available_tools():
	message = args.unknown_tool_message("symbol_info")
	assert "Unknown tool 'symbol_info'" in message and "lookup" in message


def test_given_an_absurdly_long_tool_name_then_the_refusal_stays_short():
	assert len(args.unknown_tool_message("x" * 10_000)) < 400


@pytest.mark.parametrize("tool", TOOLS)
def test_given_an_unknown_argument_then_every_tool_refuses_it_with_its_example(tool):
	text = _refusal(tool, {"zzzz": 1})
	assert f"unknown argument 'zzzz' for {tool}" in text
	assert "allowed: " in text and "did you mean" not in text
	assert f"Example: {tool}(" in text


@pytest.mark.parametrize("tool", TOOLS)
def test_given_a_tool_then_its_example_is_a_valid_call_of_that_tool(tool):
	example = args.example_call(_fn(tool))
	assert example is not None, f"{tool} has no `Example:` in its description"
	call = ast.parse(example, mode="eval").body
	assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == tool
	arguments = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords if kw.arg}
	assert len(arguments) == len(call.keywords), "the example uses only keyword arguments"
	assert _refusal(tool, arguments) == ""


@pytest.mark.parametrize(
	("tool", "wrong", "real"),
	[
		("lookup", "file_pth", "file_path"),
		("search", "querry", "query"),
		("rename", "new_nme", "new_name"),
		("change_signature", "remov", "remove"),
		("edit_symbol", "aply", "apply"),
		("edits", "sitess", "sites"),
		("fix", "file_paths", "file_path"),
	],
)
def test_given_a_close_unknown_argument_then_the_real_name_is_suggested(tool, wrong, real):
	arguments = {"name": "x", "new_name": "y", "query": "q", "file_path": "a.py"}
	text = _refusal(tool, {**{k: v for k, v in arguments.items() if k in _params(tool)}, wrong: 1})
	assert f"unknown argument {wrong!r} for {tool}; did you mean {real!r}?" in text


def _params(tool: str) -> set[str]:
	import inspect

	return set(inspect.signature(args._bare(_fn(tool))).parameters)


def test_given_several_unknown_arguments_then_each_close_one_gets_a_suggestion():
	text = _refusal("lookup", {"file_paths": "a.py", "zzzz": 1, "lines": 2})
	assert "did you mean 'file_path' for 'file_paths', 'line' for 'lines'?" in text
	assert "allowed: name, file_path, line, column, show, limit, offset" in text


def test_given_many_unknown_arguments_then_the_refusal_names_at_most_five():
	text = _refusal("outline", {f"bad{i}": i for i in range(20)})
	assert "... and 15 more" in text and len(text) < 700


def test_given_a_wrong_type_then_the_hint_is_short_and_names_the_type():
	assert _refusal("lookup", {"name": "x", "limit": "ten"}).startswith(
		"lookup: limit must be a whole number (got 'ten')"
	)
	assert "apply must be true or false (got 'yes')" in _refusal(
		"rename", {"name": "a", "new_name": "b", "apply": "yes"}
	)
	assert "show must be a list" in _refusal("lookup", {"name": "x", "show": "doc"})
	assert "edits must be a list of objects" in _refusal("edit_symbol", {"edits": "x"})


def test_given_a_missing_required_argument_then_the_hint_shows_the_signature_and_example():
	text = _refusal("rename", {"name": "x"})
	assert text.startswith("rename: missing required 'new_name'.")
	assert "Parameters: rename(name=..., file_path=..." in text and "Example: rename(" in text


@pytest.mark.parametrize(
	("tool", "alias", "real", "base"),
	[
		("lookup", "symbol", "name", {}),
		("lookup", "qualified_name", "name", {}),
		("lookup", "file", "file_path", {"name": "x"}),
		("lookup", "col", "column", {"file_path": "a.py"}),
		("lookup", "line_number", "line", {"file_path": "a.py"}),
		("outline", "filepath", "file_path", {}),
		("check", "file", "file_path", {}),
		("fix", "lineno", "line", {"file_path": "a.py"}),
		("fix", "line_number", "line", {"file_path": "a.py"}),
		("fix", "file", "file_path", {}),
		("impact", "symbol_name", "name", {}),
		("rename", "symbol", "name", {"new_name": "y"}),
		("change_signature", "symbol", "name", {}),
		("move", "file", "file_path", {"to_file": "b.py"}),
		("move", "to", "to_file", {"name": "f"}),
		("move", "destination", "to_file", {"name": "f"}),
		("edit_symbol", "symbol", "name", {}),
		("edit_symbol", "path", "file_path", {}),
		("edits", "edit_id", "id", {}),
		("search", "symbol", "query", {}),
		("search", "name", "query", {}),
		("search", "in_", "scope", {"query": "q"}),
	],
)
def test_given_a_guessed_name_then_it_is_renamed_and_the_call_passes_the_check(tool, alias, real, base):
	fn = _fn(tool)
	value = "mentions" if real == "scope" else (7 if real in ("line", "column") else "x.py" if "file" in real else "x")
	renamed, problem = args.apply_aliases(tool, fn, {**base, alias: value})
	assert problem is None and renamed == {**base, real: value}
	if tool != "rename" or real != "name":
		assert args.argument_problem(tool, fn, renamed) is None


@pytest.mark.parametrize("tool", ["search", "unused"])
def test_given_a_tool_with_its_own_path_parameter_then_path_is_not_an_alias(tool):
	arguments = {"query": "q", "path": "src/"} if tool == "search" else {"path": "src/"}
	renamed, problem = args.apply_aliases(tool, _fn(tool), arguments)
	assert (renamed, problem) == (arguments, None)


def test_given_an_alias_whose_target_the_tool_lacks_then_it_stays_unknown():
	text = _refusal("search", {"query": "x", "col": 3})
	assert "unknown argument 'col' for search" in text
	assert "unknown argument 'symbol' for outline" in _refusal("outline", {"file_path": "a.py", "symbol": "x"})


def test_given_an_alias_and_its_target_with_the_same_value_then_accepted():
	assert _refusal("outline", {"file": "a.py", "file_path": "a.py"}) == ""


@pytest.mark.parametrize(
	("tool", "arguments", "pair"),
	[
		("outline", {"file": "a.py", "file_path": "b.py"}, "'file' or 'file_path'"),
		("lookup", {"symbol": "A", "name": "B"}, "'symbol' or 'name'"),
		("search", {"symbol": "A", "query": "B"}, "'symbol' or 'query'"),
	],
)
def test_given_an_alias_and_its_target_with_different_values_then_refused(tool, arguments, pair):
	text = _refusal(tool, arguments)
	assert f"{tool}: give either {pair}, not both" in text


def test_given_a_null_alias_beside_the_real_value_then_the_real_value_wins():
	renamed, problem = args.apply_aliases("outline", _fn("outline"), {"file_path": "a.py", "file": None})
	assert (renamed, problem) == ({"file_path": "a.py"}, None)


def test_given_guessed_names_inside_an_edit_item_then_they_are_renamed():
	keys = multi_edit._TEXT_KEYS | multi_edit._SYMBOL_KEYS
	renamed, problem = args.resolve_aliases(
		"edit item", keys, {"file": "a.py", "old_str": "x = 1", "new_str": "x = 2", "symbol": "f"}
	)
	assert problem is None
	assert renamed == {"file_path": "a.py", "old_string": "x = 1", "new_string": "x = 2", "name": "f"}


def test_given_conflicting_names_inside_an_edit_item_then_the_item_is_refused():
	keys = multi_edit._TEXT_KEYS | multi_edit._SYMBOL_KEYS
	_, problem = args.resolve_aliases("edit item", keys, {"old_str": "a", "old_string": "b"})
	assert problem is not None and "give either 'old_str' or 'old_string', not both" in problem
