"""`batch`: ordering, per-call isolation, stop_on_error, argument validation (fake tools, no ty)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

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
		return f"Applied as edit abc ({file_path})" if ok else f"NOT applied: errors in {file_path}"

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
	text = run([{"tool": "read_file", "args": {"path": "a.py"}}])
	assert "no parameter 'path'" in text
	assert "read_file(file_path, line=...)" in text


def test_given_wrong_type_then_validation_hint():
	assert "invalid 'line'" in run([{"tool": "read_file", "args": {"file_path": "a.py", "line": "x"}}])


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
