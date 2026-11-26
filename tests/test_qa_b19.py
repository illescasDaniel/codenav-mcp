"""QA block B19: output cap on refusals, importer limit as a note, BOM `__all__` renames, file modes on move/undo,
move previews that render when held back, one-line startup refusals, and small dead-code and wording fixes."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codenav_mcp import args, edits_tools, refactor_tools, server, write_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


loop = make_loop_fixture()


def _text(result) -> str:
	return result.content[0].text


def _refuse(tool: str, args: dict):
	return asyncio.run(server.mcp.call_tool(tool, args))


# (1) Output cap: no refusal reply may exceed the cap, whatever the caller sent.


def test_rc_b19_unknown_arguments_refusal_is_capped_and_names_few(tmp_path, monkeypatch):
	monkeypatch.delenv("CODENAV_MCP_MAX_OUTPUT_CHARS", raising=False)
	args = {f"key{i}": 1 for i in range(20_000)}

	result = _refuse("lookup", args)

	text = _text(result)
	assert result.is_error
	assert len(text) <= 30_000
	assert text.startswith("unknown arguments ")
	assert "... and 19995 more" in text


def test_rc_b19_one_huge_unknown_argument_name_is_cut_short(tmp_path):
	result = _refuse("lookup", {"k" * 400_000: 1})

	text = _text(result)
	assert result.is_error
	assert len(text) < 300
	assert text.startswith("unknown argument '")


def test_rc_b19_unknown_tool_name_reply_is_capped_and_does_not_echo_it(tmp_path):
	name = "t" * 500_000

	result = _refuse(name, {})

	text = _text(result)
	assert result.is_error
	assert len(text) <= 30_000
	assert name not in text
	assert text.startswith("Unknown tool ")


def test_rc_b19_batch_refusal_for_unknown_arguments_is_short(tmp_path):
	fn = args.signature_of("lookup")
	assert fn is not None

	problem = args.argument_problem("lookup", fn, {f"k{i}": 1 for i in range(20_000)})

	assert problem is not None
	assert len(problem) < 600
	assert "... and 19995 more" in problem


def test_rc_b19_batch_unknown_tool_and_names_are_short_in_a_call_shape_refusal(tmp_path):
	message = args.unknown_tool_message("x" * 400_000)

	assert len(message) < 400
	assert message.startswith("Unknown tool '")


# (6) Startup refusals: one line on stderr, no traceback, non-zero exit.


def _import_server(cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
	base = {k: v for k, v in os.environ.items() if not k.startswith("CODENAV_MCP_") and k != "CLAUDE_PROJECT_DIR"}
	return subprocess.run(  # noqa: S603
		[sys.executable, "-c", "import codenav_mcp.server"],
		cwd=cwd,
		env={**base, **env},
		capture_output=True,
		text=True,
		timeout=120,
		check=False,
	)


@pytest.mark.parametrize(
	("env", "expected"),
	[
		({"CODENAV_MCP_WORKSPACE": "/"}, "as the workspace"),
		({"CODENAV_MCP_WORKSPACE": "qa-b19-missing-dir"}, "is not an existing directory"),
	],
)
def test_rc_b19_startup_refusal_prints_one_line_without_traceback(tmp_path, env, expected):
	result = _import_server(tmp_path, **env)

	assert result.returncode != 0
	assert "Traceback" not in result.stderr
	assert expected in result.stderr
	assert len(result.stderr.strip().splitlines()) == 1


def test_rc_b19_cwd_root_without_workspace_setting_prints_one_line(tmp_path):
	result = _import_server(Path("/"))

	assert result.returncode != 0
	assert "Traceback" not in result.stderr
	assert "refusing to use " in result.stderr
	assert "as the workspace" in result.stderr


# (2) The importer limit is a note: an apply past 60 importers is not gated on it.


@needs_ty
def test_rc_b19_importer_limit_is_a_note_and_does_not_block_apply(tmp_path, monkeypatch, loop):
	files = {"src/pkg/__init__.py": "", "src/pkg/util.py": "def helper() -> int:\n\treturn 1\n"}
	for i in range(70):
		files[f"src/pkg/user{i:02d}.py"] = "from pkg.util import helper\n\n\ndef f() -> int:\n\treturn helper()\n"
	project = make_workspace(tmp_path, monkeypatch, files)

	text = loop.run_until_complete(
		write_tools._edit_text(file_path="src/pkg/util.py", old_string="return 1", new_string="return 2", apply=True)
	)

	assert "NOT applied" not in text
	assert "Applied as edit" in text
	assert "beyond the 60-file limit" in text
	assert "return 2" in (project / "src/pkg/util.py").read_text(encoding="utf-8")


# (3) A first-line `__all__` rename in a BOM + CRLF file is applied.


@needs_ty
def test_rc_b19_first_line_dunder_all_rename_in_bom_crlf_file_is_applied(tmp_path, monkeypatch, loop):
	source = '﻿__all__ = ["helper"]\r\n\r\n\r\ndef helper() -> int:\r\n\treturn 1\r\n'
	project = make_workspace(tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/lib.py": source})

	text = loop.run_until_complete(
		write_tools.rename(name="helper", file_path="src/pkg/lib.py", new_name="renamed", apply=True)
	)

	assert "NOT applied" not in text
	after = (project / "src/pkg/lib.py").read_bytes()
	assert after.startswith(b'\xef\xbb\xbf__all__ = ["renamed"]\r\n')
	assert b"def renamed()" in after
	assert b"\r\n" in after and b"\n" not in after.replace(b"\r\n", b"")


# (4) Permission bits: a moved file keeps its source's mode; undo restores the deleted file's mode.


@needs_ty
@pytest.mark.skipif(os.name == "nt", reason="Windows does not support POSIX executable mode bits")
def test_rc_b19_moved_module_keeps_mode_and_undo_restores_it(tmp_path, monkeypatch, loop):
	project = make_workspace(
		tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/tool.py": "def main() -> int:\n\treturn 0\n"}
	)
	source = project / "src/pkg/tool.py"
	source.chmod(0o755)

	loop.run_until_complete(
		refactor_tools._move_module(
			from_file="src/pkg/tool.py", to_file="src/pkg/tool2.py", apply=True, max_new_errors=None
		)
	)

	moved = project / "src/pkg/tool2.py"
	assert moved.is_file() and not source.exists()
	assert (moved.stat().st_mode & 0o777) == 0o755

	loop.run_until_complete(edits_tools.edits(action="undo"))

	assert source.is_file() and not moved.exists()
	assert (source.stat().st_mode & 0o777) == 0o755


# (5) A move preview with an unparsable importer renders and says the move would be held back.


@needs_ty
def test_rc_b19_move_preview_creating_the_destination_is_refused(tmp_path, monkeypatch, loop):
	# B23 decision: previews never create files, so a move preview whose destination does not
	# exist is refused (it used to simulate and report "Held back" importers).
	project = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/util.py": "def helper() -> int:\n\treturn 1\n",
			"src/pkg/bad.py": "from pkg.util import helper\n\ndef broken(:\n",
		},
	)

	text = loop.run_until_complete(
		refactor_tools._move_module(from_file="src/pkg/util.py", to_file="src/pkg/common.py", apply=False)
	)

	assert "Preview refused" in text and "src/pkg/common.py" in text
	assert "previews never create files" in text
	assert (project / "src/pkg/util.py").is_file()
	assert not (project / "src/pkg/common.py").exists()


# (7) Wording and dead code: the match list names each line once.


@needs_ty
def test_rc_b19_multiple_matches_on_one_line_list_the_line_once(tmp_path, monkeypatch, loop):
	project = make_workspace(tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/m.py": "x = 'a a a'\n"})

	text = loop.run_until_complete(write_tools._edit_text(file_path="src/pkg/m.py", old_string="a", new_string="b"))

	assert "matches 3 times (lines 1)" in text
	assert "lines 1, 1" not in text
	assert (project / "src/pkg/m.py").read_text(encoding="utf-8") == "x = 'a a a'\n"


def test_rc_b19_dead_helpers_are_gone():
	from codenav_mcp import rename, symbol_tools

	assert not hasattr(rename, "update_dunder_all")
	assert not hasattr(symbol_tools, "_names_in_scope")
