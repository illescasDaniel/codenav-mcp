"""Formatting and input checks for the v2 read tools."""

from __future__ import annotations

import asyncio

import pytest
from mcp_nav_shared.errors import FailedText

from codenav_mcp import server  # noqa: F401  (registers the tool signatures)
from codenav_mcp.args import apply_aliases, signature_of
from codenav_mcp.mentions import find_mentions
from codenav_mcp.read_tools import _signature_and_doc, lookup, search


@pytest.mark.parametrize(
	"hover",
	[
		"```python\ndef total() -> int\n```\n\nFirst line.\nSecond line.\n\nOther paragraph.",
		"def total() -> int\n-----------------\nFirst line.\nSecond line.\n\nOther paragraph.",
	],
)
def test_hover_is_split_into_signature_and_first_paragraph(hover):
	assert _signature_and_doc(hover, None, brief=True) == ("def total() -> int", "First line. Second line.")


@pytest.mark.parametrize(
	"tool,args",
	[
		(lookup, {}),
		(lookup, {"name": "total", "column": 1}),
		(lookup, {"name": "total", "show": ["unknown"]}),
		(lookup, {"name": "total", "limit": 0}),
		(lookup, {"name": "total", "offset": -1}),
		(search, {"query": "   "}),
		(search, {"query": "total", "offset": -1}),
	],
)
def test_invalid_requests_include_an_exact_example(tool, args):
	result = asyncio.run(tool(**args))
	assert isinstance(result, FailedText)
	assert f"Example: {tool.__name__}(" in result


@pytest.mark.parametrize(
	"tool,args,result",
	[
		("lookup", {"symbol": "total", "file": "x.py"}, {"name": "total", "file_path": "x.py"}),
		("lookup", {"query": "total"}, {"name": "total"}),
		("lookup", {"file": "x.py", "line": 1, "col": 1}, {"file_path": "x.py", "line": 1, "column": 1}),
		(
			"search",
			{"name": "total", "file": "src", "in_": "mentions"},
			{"query": "total", "path": "src", "scope": "mentions"},
		),
	],
)
def test_v1_parameter_aliases_work(tool, args, result):
	fn = signature_of(tool)
	assert fn is not None
	assert apply_aliases(tool, fn, args) == (result, None)


def test_multiline_string_mention_points_to_the_matching_line(tmp_path):
	(tmp_path / "notes.py").write_text('"""First line.\nCall total here.\n"""\n', encoding="utf-8")
	mentions = find_mentions(tmp_path, "total", {})
	assert [(hit.kind, hit.line) for hit in mentions] == [("string", 2)]
