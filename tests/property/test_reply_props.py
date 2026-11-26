"""Properties of `reply.py`: pagination partitions a list, and `next:` calls parse back to the same arguments."""

from __future__ import annotations

import ast
import json
from typing import Any

import pytest
from hypothesis import assume, given, strategies as st

from codenav_mcp.reply import Reply, call, next_page_call, paginate


json_values = st.recursive(
	st.none() | st.booleans() | st.integers(-(10**12), 10**12) | st.text(max_size=12),
	lambda children: st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=6), children, max_size=3),
	max_leaves=6,
)
arg_names = st.sampled_from(["name", "show", "scope", "file_path", "kind", "limit", "depth", "in_", "id"])
call_args = st.dictionaries(arg_names, json_values, max_size=4)


def parse_call(text: str) -> tuple[str, dict[str, Any]]:
	"""Read a `tool(key=json, ...)` line back: the tool name and each argument decoded as JSON."""
	node = ast.parse(text, mode="eval").body
	assert isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.args
	args = {}
	for keyword in node.keywords:
		assert keyword.arg is not None
		segment = ast.get_source_segment(text, keyword.value)
		assert segment is not None
		args[keyword.arg] = json.loads(segment)
	return node.func.id, args


@given(st.lists(st.integers(), max_size=40), st.integers(1, 12), st.integers(0, 45))
def test_given_a_list_then_a_page_is_the_slice_and_more_counts_what_is_left(items, limit, offset):
	page = paginate(items, limit, offset)
	assert page.items == items[offset : offset + limit]
	assert page.total == len(items) and page.offset == offset
	assert page.more == max(0, len(items) - offset - len(page.items))
	assert len(page.items) <= limit


@given(st.lists(st.integers(), max_size=40), st.integers(1, 12))
def test_given_next_calls_followed_from_the_start_then_the_pages_partition_the_list(items, limit):
	seen: list[int] = []
	offset = 0
	for _ in range(len(items) + 2):
		page = paginate(items, limit, offset)
		seen += page.items
		follow = next_page_call(page, "lookup", name="X", limit=limit)
		if follow is None:
			assert page.more == 0
			break
		_, args = parse_call(follow)
		assert args["offset"] == offset + len(page.items) and page.more > 0
		assert args["offset"] > offset, "a next page must advance"
		offset = args["offset"]
	else:
		pytest.fail("the next calls never ended")
	assert seen == items


@given(call_args, st.integers(0, 10**6))
def test_given_any_arguments_then_the_next_call_parses_back_to_the_same_arguments_plus_the_offset(args, offset):
	items = list(range(offset + 5))
	page = paginate(items, 2, offset)
	follow = next_page_call(page, "lookup", **args)
	assert follow is not None
	tool, parsed = parse_call(follow)
	assert tool == "lookup"
	assert parsed == {**{k: v for k, v in args.items() if v is not None}, "offset": offset + 2}


@given(call_args)
def test_given_a_call_then_none_values_are_dropped_and_everything_else_survives_a_json_round_trip(args):
	tool, parsed = parse_call(call("t", **args))
	assert tool == "t"
	assert parsed == {k: v for k, v in args.items() if v is not None}


@given(st.lists(st.text(alphabet="abc xyz", min_size=1, max_size=8), max_size=30), st.integers(1, 8), call_args)
def test_given_a_reply_page_then_cut_pages_end_with_the_more_line_and_the_exact_next_call(items, limit, args):
	reply = Reply("answer")
	page = paginate(items, limit)
	reply.add_page(page, lambda item: f"- {item}", "lookup", **args)
	lines = reply.render().split("\n")
	if page.more:
		assert f"... {page.more} more" in reply.items
		assert lines[-1].startswith("next: ")
		_, parsed = parse_call(lines[-1][len("next: ") :])
		assert parsed["offset"] == limit
	else:
		assert not reply.next_calls and not any(line.startswith("next:") for line in lines)
	assert [line for line in reply.items if line.startswith("- ")] == [f"- {item}" for item in page.items]


@given(st.integers(-5, 0))
def test_given_a_limit_below_one_or_a_negative_offset_then_paginate_refuses(value):
	with pytest.raises(ValueError, match="limit"):
		paginate([1], value)
	assume(value < 0)
	with pytest.raises(ValueError, match="offset"):
		paginate([1], 1, value)
