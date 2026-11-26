"""Tool contract (B10): argument refusals, batch `$N` and `stop_on_error`, names, paths, protocol and shutdown.

In-memory MCP tests run the real tool registration; the stdio tests start `python -m codenav_mcp.server`.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import LATEST_PROTOCOL_VERSION

from codenav_mcp import batch_tools, server

from .live_support import make_workspace, needs_ty


INIT = {
	"jsonrpc": "2.0",
	"id": 1,
	"method": "initialize",
	"params": {
		"protocolVersion": LATEST_PROTOCOL_VERSION,
		"capabilities": {},
		"clientInfo": {"name": "b10-test", "version": "0"},
	},
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
PING = {"jsonrpc": "2.0", "id": 9, "method": "ping"}


def _line(message: dict | str) -> str:
	return (message if isinstance(message, str) else json.dumps(message)) + "\n"


def _call(tool: str, args: dict):
	async def scenario():
		async with Client(server.mcp, mode="legacy") as client:
			return await client.call_tool(tool, args)

	return asyncio.run(scenario())


def _text(result) -> str:
	return result.content[0].text


@pytest.fixture
def workspace(tmp_path, monkeypatch):
	root = make_workspace(tmp_path, monkeypatch, {"src/a.py": "x = 1\n"})
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(server, "_selector", server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))
	return root


def _stdio_env(workspace: Path) -> dict[str, str]:
	return dict(os.environ, CODENAV_MCP_WORKSPACE=str(workspace))


def _session(
	workspace: Path, messages: list[dict | str], answer_ids: set[int], timeout: float = 60.0
) -> tuple[dict[int, dict], str, int]:
	"""Run the server over stdio. Stdin stays open until every id in `answer_ids` is answered, then it is closed.

	Closing stdin right after writing can end the session before queued requests are answered, so the
	answers are awaited first. Returns (answers by id, stderr, exit code).
	"""
	proc = subprocess.Popen(
		[sys.executable, "-m", "codenav_mcp.server"],
		cwd=workspace,
		env=_stdio_env(workspace),
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
	)
	lines: queue.Queue[str] = queue.Queue()
	threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True).start()
	answers: dict[int, dict] = {}
	try:
		proc.stdin.write("".join(_line(message) for message in messages))
		proc.stdin.flush()
		deadline = time.monotonic() + timeout
		while not answer_ids <= answers.keys():
			message = json.loads(lines.get(timeout=max(0.1, deadline - time.monotonic())))
			if "id" in message:
				answers[message["id"]] = message
		proc.stdin.close()
		code = proc.wait(timeout=timeout)
		return answers, proc.stderr.read(), code
	finally:
		if proc.poll() is None:
			proc.kill()
			proc.wait()


# RC-031 / RC-107 (D19): unknown and mistyped arguments are refused by name, direct and in a batch.


def test_rc_031_unknown_argument_is_refused_by_name_over_mcp(workspace):
	result = _call("edit", {"file_path": "src/a.py", "new_text": "x = 2\n", "aply": True})

	assert result.is_error
	assert _text(result).startswith("unknown argument 'aply' for edit; did you mean 'apply'?; allowed: ")


def test_rc_107_unknown_argument_is_refused_not_ignored(workspace):
	result = _call("symbol_info", {"name": "x", "nope": 1})

	assert result.is_error
	assert "unknown argument 'nope' for symbol_info" in _text(result)


def test_rc_031_wrong_type_is_the_short_hint_not_a_pydantic_dump(workspace):
	result = _call("hover", {"file_path": "src/a.py", "line": "x", "column": 1})

	assert result.is_error
	assert _text(result) == "hover: line must be a whole number (got 'x')"


def test_rc_031_batch_and_direct_call_give_the_same_wording(workspace):
	direct = _text(_call("hover", {"file_path": "src/a.py", "line": "x", "column": 1}))
	batched = _text(
		_call("batch", {"calls": [{"tool": "hover", "args": {"file_path": "src/a.py", "line": "x", "column": 1}}]})
	)

	assert direct == "hover: line must be a whole number (got 'x')"
	assert direct in batched


# RC-150: stop_on_error must be a real boolean; the SDK's coercion of "yes" and 1 is not allowed.


@pytest.mark.parametrize(
	("value", "got"),
	[("yes", "'yes'"), (1, "1")],
)
def test_rc_150_stop_on_error_non_bool_is_refused(value, got, workspace):
	result = _call("batch", {"calls": [{"tool": "outline", "args": {"file_path": "src/a.py"}}], "stop_on_error": value})

	assert result.is_error
	assert _text(result) == f"batch: stop_on_error must be true or false (got {got})"


# RC-041 (D8 A): a call that is itself a result reference (`"$1"`) is refused; the batch has no result passing.


def test_rc_041_whole_value_result_reference_is_refused(workspace):
	result = _call("batch", {"calls": [{"tool": "outline", "args": {"file_path": "$1"}}]})

	assert result.is_error
	assert (
		"batch call #1: result references ($1) are not supported in v1; "
		"make the calls independent or run them one by one"
	) in _text(result)


def test_rc_041_dollar_digit_inside_text_still_runs():
	# A `$5` inside an edit's new text is ordinary text, not a result reference.
	prepared = batch_tools._prepare(
		1, {"tool": "edit", "args": {"file_path": "src/a.py", "new_string": "cost is $5", "new_text": "y"}}
	)

	assert not isinstance(prepared, str), prepared


# RC-106: the batch's static annotation stays destructive (it can write); the description says what is safe.


def test_rc_106_batch_description_and_annotation():
	async def scenario():
		async with Client(server.mcp, mode="legacy") as client:
			return (await client.list_tools()).tools

	batch = next(tool for tool in asyncio.run(scenario()) if tool.name == "batch")

	text = " ".join(batch.description.split())
	assert "cannot refer to an earlier call's result" in text
	assert "possibly destructive" in text
	assert batch.annotations.destructive_hint is True


# RC-146: a blank or dot-only name is refused up front with one message.


def test_rc_146_blank_name_is_an_error_over_mcp(workspace):
	result = _call("symbol_info", {"name": "   "})

	assert result.is_error
	assert _text(result) == "name must be a non-empty identifier or dotted path"


# RC-032: missing-file messages do not echo an absolute path.


def test_rc_032_missing_file_message_has_no_absolute_path(workspace):
	result = _call("hover", {"file_path": "nope.py", "line": 1, "column": 1})

	assert result.is_error
	assert "File not found: nope.py" in _text(result)
	assert str(workspace) not in _text(result)


def test_rc_032_subdirectory_path_is_kept_relative(workspace):
	result = _call("hover", {"file_path": "src/nope.py", "line": 1, "column": 1})

	assert result.is_error
	assert "File not found: src/nope.py" in _text(result).replace("\\", "/")
	assert str(workspace) not in _text(result)


# RC-046: a non-request line is dropped by the SDK; the server says so on stderr and keeps serving.


def test_rc_046_dropped_line_is_reported_on_stderr_and_ping_still_answered(workspace):
	answers, stderr, code = _session(workspace, [INIT, INITIALIZED, "42", PING], {1, 9})

	assert code == 0
	assert "codenav-mcp: ignored a non-request message" in stderr
	assert "Traceback" not in stderr
	assert answers[9]["result"] == {}


# RC-108 (D20 A): a call before `initialize` is refused with the handshake named; calls after it are accepted.


def test_rc_108_call_before_initialize_names_the_handshake(workspace):
	early = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "workspace", "arguments": {}}}
	answers, _, _ = _session(workspace, [early, INIT, INITIALIZED], {2})

	assert answers[2]["error"]["message"] == "the server is not initialized yet; send initialize first"


def test_d20_call_after_initialize_but_before_initialized_notification_is_accepted(workspace):
	call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "workspace", "arguments": {}}}
	answers, _, _ = _session(workspace, [INIT, call], {1, 2})

	assert "error" not in answers[2]


# RC-016: SIGINT with a language server running stops it, exits 0 without a traceback, and leaves no ty child.


def _descendants(pid: int) -> list[int]:
	children: dict[int, list[int]] = {}
	for stat in Path("/proc").glob("[0-9]*/stat"):
		try:
			fields = stat.read_text().rsplit(")", 1)[1].split()
		except (OSError, IndexError):
			continue
		children.setdefault(int(fields[1]), []).append(int(stat.parent.name))
	found, todo = [], [pid]
	while todo:
		for child in children.get(todo.pop(), []):
			found.append(child)
			todo.append(child)
	return found


def _alive(pid: int) -> bool:
	try:
		state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
	except OSError:
		return False
	return state != "Z"


@needs_ty
@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires Linux procfs to inspect child processes")
def test_rc_016_sigint_with_language_server_running_exits_cleanly(workspace):
	proc = subprocess.Popen(
		[sys.executable, "-m", "codenav_mcp.server"],
		cwd=workspace,
		env=_stdio_env(workspace),
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
	)
	lines: queue.Queue[str] = queue.Queue()
	threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True).start()
	hover = {
		"jsonrpc": "2.0",
		"id": 3,
		"method": "tools/call",
		"params": {"name": "hover", "arguments": {"file_path": "src/a.py", "line": 1, "column": 1}},
	}
	try:
		# Stdin stays open: closing it first would end the session before the hover answer (see qa/driver.py).
		proc.stdin.write(_line(INIT) + _line(INITIALIZED) + _line(hover))
		proc.stdin.flush()
		deadline = time.monotonic() + 90
		while True:
			line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
			if '"id":3' in line:
				break
		children = _descendants(proc.pid)
		assert children, "expected the ty language server to be running before SIGINT"

		proc.send_signal(signal.SIGINT)
		code = proc.wait(timeout=5)
		stderr = proc.stderr.read()

		assert code == 0
		assert "Traceback" not in stderr
		deadline = time.monotonic() + 5
		while any(_alive(pid) for pid in children) and time.monotonic() < deadline:
			time.sleep(0.1)
		assert not any(_alive(pid) for pid in children)
	finally:
		if proc.poll() is None:
			proc.kill()
			proc.wait()
