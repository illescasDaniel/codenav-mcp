"""The ambiguity listing parses mcp-nav-shared's refusal text; these tests pin that wording (no ty needed)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from mcp_nav_shared import resolve
from mcp_nav_shared.errors import ToolInputError
from mcp_nav_shared.lsp_client import LspClient

from codenav_mcp import read_tools
from codenav_mcp.ambiguity import ambiguity_listing, parse_candidates


class _NoSymbols:
	async def document_symbol(self, file_path: str) -> list:
		return []


def _symbol(name: str, rel: str, line: int, kind: int = 12) -> dict[str, Any]:
	start = {"line": line, "character": 4}
	return {
		"name": name,
		"kind": kind,
		"location": {"uri": Path("/ws", rel).as_uri(), "range": {"start": start, "end": start}},
	}


def _shared_refusal(query: str, candidates: list[dict[str, Any]]) -> Exception:
	return asyncio.run(resolve._ambiguous(cast(LspClient, _NoSymbols()), query, candidates, Path("/ws")))  # type: ignore[arg-type]


def test_given_the_shared_ambiguity_refusal_then_its_candidates_parse():
	exc = _shared_refusal("build", [_symbol("build", "src/a.py", 0), _symbol("build", "src/b.py", 4)])

	parsed = parse_candidates(exc, "build")
	assert parsed is not None
	total, matched, candidates = parsed

	assert (total, matched) == (2, "build")
	assert [(c.display, c.kind, c.path, c.line) for c in candidates] == [
		("build", "Function", "src/a.py", 1),
		("build", "Function", "src/b.py", 5),
	]


def test_given_the_shared_ambiguity_refusal_then_the_listing_has_one_retry_per_candidate():
	exc = _shared_refusal("build", [_symbol("build", "src/a.py", 0), _symbol("build", "src/b.py", 4)])

	text = ambiguity_listing(exc, "symbol_info", "build")

	assert text == (
		"2 symbols are named 'build'. Retry with one of:\n"
		'  symbol_info(name="build", file_path="src/a.py")  [Function, src/a.py:1]\n'
		'  symbol_info(name="build", file_path="src/b.py")  [Function, src/b.py:5]'
	)


def test_given_more_candidates_than_the_shared_message_lists_then_the_listing_says_so():
	exc = _shared_refusal("f", [_symbol("f", f"src/m{i}.py", 0) for i in range(12)])

	text = ambiguity_listing(exc, "callers", "f")

	assert text is not None
	assert text.startswith("12 symbols are named 'f' (first 10 shown). Retry with one of:")
	assert text.count("callers(name=") == 10


def test_given_the_same_name_twice_in_one_file_then_no_retry_call_is_invented():
	exc = _shared_refusal("f", [_symbol("f", "src/a.py", 0), _symbol("f", "src/a.py", 6)])

	text = ambiguity_listing(exc, "symbol_info", "f")

	assert text is not None
	assert "symbol_info(" not in text
	assert "[Function] src/a.py:1:5" in text
	assert "cannot be chosen by name" in text


def test_given_a_dotted_query_whose_class_is_ambiguous_then_retries_keep_the_member():
	exc = _shared_refusal("Dup", [_symbol("Dup", "src/a.py", 0, kind=5), _symbol("Dup", "src/b.py", 0, kind=5)])

	text = ambiguity_listing(exc, "symbol_info", "Dup.go")

	assert text is not None
	assert 'symbol_info(name="Dup.go", file_path="src/a.py")' in text
	assert 'symbol_info(name="Dup.go", file_path="src/b.py")' in text


def test_given_other_errors_then_there_is_no_listing():
	assert ambiguity_listing(resolve.SymbolNotFoundError("No symbol found matching 'x'."), "symbol_info", "x") is None
	assert ambiguity_listing(ToolInputError("Pass `name`"), "symbol_info", "x") is None
	elsewhere = resolve.SymbolResolutionError(
		"No symbol 'x' in 'f.py'; 2 match(es) elsewhere:\nx  [Function]  (a.py:1:1)"
	)
	assert ambiguity_listing(elsewhere, "symbol_info", "x") is None
	assert ambiguity_listing(ValueError("boom"), "symbol_info", "x") is None


def test_given_a_message_about_another_name_then_there_is_no_listing():
	exc = _shared_refusal("build", [_symbol("build", "src/a.py", 0), _symbol("build", "src/b.py", 4)])

	assert ambiguity_listing(exc, "symbol_info", "other") is None


def test_given_reworded_shared_text_then_the_original_refusal_is_returned_unchanged():
	exc = resolve.SymbolResolutionError("Found 2 symbols named 'x': pick one\n  x (a.py:1)")

	assert read_tools._ambiguity(exc, "x", None, None, 20, 0) is None
	reply = read_tools._refusal(exc, read_tools.lookup)

	assert reply.startswith(str(exc) + "\nExample: ")
