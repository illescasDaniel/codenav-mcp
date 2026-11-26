"""`batch`: ordering, per-call isolation, stop_on_error, argument validation (fake tools, no ty)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp_nav_shared.errors import FailedText

from codenav_mcp import batch_tools


@pytest.fixture(autouse=True)
def fake_tools(monkeypatch):
	monkeypatch.setattr(batch_tools, "_registry", {})
	log: list[str] = []

	async def read_file(file_path: str, line: int = 1, ctx: Any = None) -> str:
		log.append(f"read {file_path}")
		return f"content of {file_path}:{line}"

	async def write_file(file_path: str, ok: bool = True, ctx: Any = None) -> str:
		log.append(f"write {file_path}")
		return f"Applied as edit abc ({file_path})" if ok else FailedText(f"NOT applied: errors in {file_path}")

	async def preview_file(file_path: str, ctx: Any = None) -> str:
		return "Preview only, nothing written."

	async def boom(ctx: Any = None) -> str:
		raise RuntimeError("kaboom")

	async def huge(ctx: Any = None) -> str:
		return "x" * (batch_tools.MAX_CALL_CHARS + 50)

	for name, fn, writes in [
		("read_file", read_file, False),
		("write_file", write_file, True),
		("preview_file", preview_file, True),
		("boom", boom, False),
		("huge", huge, False),
	]:
		batch_tools.register(name, fn, writes=writes)
	return log


def run(calls, stop_on_error=None) -> str:
	return asyncio.run(batch_tools.run_batch(calls, stop_on_error, None))


def test_given_several_reads_when_batch_then_one_numbered_section_each_in_order(fake_tools):
	# when
	text = run(
		[
			{"tool": "read_file", "args": {"file_path": "a.py"}},
			{"tool": "read_file", "args": {"file_path": "b.py", "line": 7}},
		]
	)
	# then
	assert "[1/2] read_file(file_path='a.py')\ncontent of a.py:1" in text
	assert "[2/2] read_file(file_path='b.py', line=7)\ncontent of b.py:7" in text
	assert text.endswith("batch: 2/2 ok")
	assert fake_tools == ["read a.py", "read b.py"]


def test_given_writes_when_batch_then_run_sequentially_and_each_reports_own_result(fake_tools):
	# when
	text = run(
		[{"tool": "write_file", "args": {"file_path": "a.py"}}, {"tool": "write_file", "args": {"file_path": "b.py"}}]
	)
	# then
	assert fake_tools == ["write a.py", "write b.py"]
	assert text.count("Applied as edit") == 2


def test_given_failed_write_when_default_stop_then_remaining_calls_skipped(fake_tools):
	# when
	text = run(
		[
			{"tool": "write_file", "args": {"file_path": "a.py", "ok": False}},
			{"tool": "write_file", "args": {"file_path": "b.py"}},
		]
	)
	# then
	assert fake_tools == ["write a.py"]
	assert "NOT DONE" in text
	assert "0/2 ok, 1 failed, 1 skipped" in text


def test_given_failed_write_when_stop_on_error_false_then_continues(fake_tools):
	# when
	text = run(
		[
			{"tool": "write_file", "args": {"file_path": "a.py", "ok": False}},
			{"tool": "write_file", "args": {"file_path": "b.py"}},
		],
		stop_on_error=False,
	)
	# then
	assert fake_tools == ["write a.py", "write b.py"]
	assert "1/2 ok, 1 failed" in text


def test_given_reads_only_when_one_call_is_invalid_then_others_still_run(fake_tools):
	# when
	text = run([{"tool": "nope"}, {"tool": "read_file", "args": {"file_path": "a.py"}}])
	# then
	assert "[1/2] ERROR" in text
	assert "content of a.py:1" in text
	assert "1/2 ok, 1 failed" in text


def test_given_preview_write_then_counts_as_ok(fake_tools):
	assert run([{"tool": "preview_file", "args": {"file_path": "a.py"}}]).endswith("1/1 ok")


def test_given_exception_in_tool_then_reported_in_its_section_only(fake_tools):
	# when
	text = run([{"tool": "boom"}, {"tool": "read_file", "args": {"file_path": "a.py"}}])
	# then
	assert "ERROR: RuntimeError: kaboom" in text
	assert "content of a.py:1" in text


def test_given_typo_in_tool_name_then_suggests_closest():
	assert "Did you mean 'read_file'" in run([{"tool": "read_fil"}])


def test_given_unknown_parameter_then_lists_valid_parameters():
	assert "unknown argument 'file_pth' for read_file; did you mean 'file_path'?; allowed: file_path, line" in run(
		[{"tool": "read_file", "args": {"file_pth": "a.py"}}]
	)


def test_given_close_unknown_parameter_then_suggests_the_real_name():
	assert "unknown argument 'file_paths' for read_file; did you mean 'file_path'?; allowed: file_path, line" in run(
		[{"tool": "read_file", "args": {"file_paths": "a.py"}}]
	)


def test_given_unrelated_unknown_parameter_then_no_suggestion():
	text = run([{"tool": "read_file", "args": {"zzzz": 1}}])
	assert "unknown argument 'zzzz' for read_file; allowed: file_path, line" in text
	assert "did you mean" not in text


def test_given_several_unknown_parameters_then_suggests_for_each_close_one():
	text = run([{"tool": "read_file", "args": {"file_paths": "a.py", "zzzz": 1, "lines": 2}}])
	assert "did you mean 'file_path' for 'file_paths', 'line' for 'lines'?" in text
	assert "allowed: file_path, line" in text


def test_given_wrong_type_then_validation_hint():
	assert "line must be a whole number" in run([{"tool": "read_file", "args": {"file_path": "a.py", "line": "x"}}])


def test_given_missing_required_then_hint():
	assert "missing required 'file_path'" in run([{"tool": "read_file", "args": {}}])


def test_given_flat_call_shape_then_accepted():
	assert "content of a.py:3" in run([{"tool": "read_file", "file_path": "a.py", "line": 3}])


def test_given_args_beside_wrong_keys_then_hint():
	assert 'inside "args"' in run([{"tool": "read_file", "args": {"file_path": "a.py"}, "line": 3}])


def test_given_nested_batch_then_refused():
	assert "cannot be nested" in run([{"tool": "batch", "args": {"calls": []}}])


def test_given_empty_or_too_many_calls_then_refused():
	assert "non-empty" in run([])
	assert "at most 25" in run([{"tool": "boom"}] * 26)


def test_given_huge_output_then_truncated(fake_tools):
	text = run([{"tool": "huge"}])
	assert "truncated 50 characters" in text


def test_success_containing_failure_words_is_not_a_failure():
	async def success() -> str:
		return "Preview only\n+NOT applied: user text in diff"

	batch_tools.register("success_with_diff", success, writes=True)
	result = run([{"tool": "success_with_diff"}])
	assert not isinstance(result, FailedText)
	assert "1/1 ok" in result


# Argument aliases: a guessed name is renamed to the real one before validation (an agent that has not seen the schema).


@pytest.mark.parametrize("alias", ["file", "filepath", "path"])
def test_given_file_alias_then_the_call_runs_with_file_path(fake_tools, alias):
	text = run([{"tool": "read_file", "args": {alias: "a.py"}}])
	assert "[1/1] read_file(file_path='a.py')\ncontent of a.py:1" in text
	assert fake_tools == ["read a.py"]


def test_given_alias_and_target_with_the_same_value_then_accepted(fake_tools):
	text = run([{"tool": "read_file", "args": {"file": "a.py", "file_path": "a.py"}}])
	assert text.endswith("batch: 1/1 ok")


def test_given_alias_and_target_with_different_values_then_refused_like_name_and_query(fake_tools):
	text = run([{"tool": "read_file", "args": {"file": "a.py", "file_path": "b.py"}}])
	assert "read_file: give either 'file' or 'file_path', not both" in text
	assert fake_tools == []


def test_given_line_aliases_then_renamed_to_line(fake_tools):
	assert "content of a.py:7" in run([{"tool": "read_file", "args": {"file_path": "a.py", "lineno": 7}}])
	assert "content of a.py:8" in run([{"tool": "read_file", "args": {"file_path": "a.py", "line_number": 8}}])


def test_given_a_tool_with_its_own_path_parameter_then_path_is_not_an_alias(monkeypatch):
	async def search(query: str, path: str | None = None, ctx: Any = None) -> str:
		return f"search {query} in {path}"

	batch_tools.register("search", search, writes=False)
	text = run([{"tool": "search", "args": {"query": "q", "path": "src/"}}])
	assert "search q in src/" in text


def test_given_an_alias_whose_target_the_tool_lacks_then_it_stays_unknown(fake_tools):
	text = run([{"tool": "read_file", "args": {"file_path": "a.py", "symbol": "x"}}])
	assert "unknown argument 'symbol' for read_file" in text


def test_given_a_tool_with_query_but_no_name_then_symbol_means_query(monkeypatch):
	async def find(query: str, ctx: Any = None) -> str:
		return f"found {query}"

	batch_tools.register("find", find, writes=False)
	assert "found Cart" in run([{"tool": "find", "args": {"symbol": "Cart"}}])


def test_given_an_unknown_argument_then_the_refusal_ends_with_the_docstring_example(monkeypatch):
	async def find(query: str, ctx: Any = None) -> str:
		"""Find a thing. Example: `find(query="Cart")`."""
		return query

	batch_tools.register("find", find, writes=False)
	assert run([{"tool": "find", "args": {"queyr": "x"}}]).count('Example: find(query="Cart")') == 1
	assert 'Parameters: find(query). Example: find(query="Cart")' in run([{"tool": "find", "args": {}}])
