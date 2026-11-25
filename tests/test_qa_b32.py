"""QA block B32: a workspace with too many Python files is refused before anything is indexed (BUG-ENV-R06)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp import scan_cap, server


def _tree(root: Path, files: int) -> Path:
	"""A workspace folder with `files` tiny Python modules spread over a few packages."""
	root.mkdir(parents=True, exist_ok=True)
	for index in range(files):
		package = root / f"pkg{index % 5}"
		package.mkdir(exist_ok=True)
		(package / f"m{index}.py").write_text("x = 1\n", encoding="utf-8")
	return root


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


def test_b32_given_tree_under_cap_when_import_then_starts(tmp_path):
	workspace = _tree(tmp_path / "ws", 12)

	result = _import_server(tmp_path, CODENAV_MCP_WORKSPACE=str(workspace), CODENAV_MCP_MAX_FILES="20")

	assert result.returncode == 0, result.stderr


def test_b32_given_tree_over_cap_when_import_then_exit_2_with_message_and_language_server_never_runs(tmp_path):
	workspace = _tree(tmp_path / "ws", 12)
	marker = tmp_path / "ty-ran"
	fake_ty = workspace / ".venv" / "bin" / "ty"
	fake_ty.parent.mkdir(parents=True)
	fake_ty.write_text(f"#!/bin/sh\ntouch {marker}\nsleep 60\n", encoding="utf-8")
	fake_ty.chmod(0o755)

	result = _import_server(tmp_path, CODENAV_MCP_WORKSPACE=str(workspace), CODENAV_MCP_MAX_FILES="10")

	assert result.returncode == 2
	assert result.stderr.startswith("codenav-mcp: ")
	assert "holds more than 10 Python files" in result.stderr
	assert "CODENAV_MCP_MAX_FILES" in result.stderr
	assert "Traceback" not in result.stderr
	assert not marker.exists()


def test_b32_given_cap_raised_by_env_when_import_then_same_tree_starts(tmp_path):
	workspace = _tree(tmp_path / "ws", 12)

	refused = _import_server(tmp_path, CODENAV_MCP_WORKSPACE=str(workspace), CODENAV_MCP_MAX_FILES="10")
	allowed = _import_server(tmp_path, CODENAV_MCP_WORKSPACE=str(workspace), CODENAV_MCP_MAX_FILES="12")

	assert refused.returncode == 2
	assert allowed.returncode == 0, allowed.stderr


def test_b32_given_no_env_when_max_files_then_default_250000(monkeypatch):
	monkeypatch.delenv("CODENAV_MCP_MAX_FILES", raising=False)

	assert scan_cap.max_files() == 250_000


@pytest.mark.parametrize("value", ["", "abc", "0", "-5", "1.5"])
def test_b32_given_invalid_env_when_max_files_then_default(monkeypatch, value):
	monkeypatch.setenv("CODENAV_MCP_MAX_FILES", value)

	assert scan_cap.max_files() == 250_000


def test_b32_given_folders_ty_skips_when_count_then_not_counted_but_other_trees_are(tmp_path):
	root = _tree(tmp_path / "ws", 3)
	for skipped in (".venv/lib", "node_modules/x", ".git/x", "dist"):
		_tree(root / skipped, 4)
	for counted in ("env/lib/site-packages", "build", "vendor"):
		_tree(root / counted, 2)
	nested = _tree(root / "other-checkout", 5)
	(nested / ".git").mkdir()

	assert scan_cap.count_python_files(root, 1000) == 3 + 2 * 3 + 5


def test_b32_given_tree_over_limit_when_count_then_walk_stops_at_limit_plus_one(tmp_path):
	root = _tree(tmp_path / "ws", 30)

	assert scan_cap.count_python_files(root, 5) == 6


def test_b32_given_workspace_switch_to_huge_tree_when_configure_then_refused_and_no_language_server(
	tmp_path, monkeypatch
):
	huge = _tree(tmp_path / "huge", 12)
	monkeypatch.setenv("CODENAV_MCP_MAX_FILES", "10")
	started: list[object] = []
	monkeypatch.setattr(server, "LspClient", lambda *a, **k: started.append((a, k)))
	monkeypatch.setattr(server, "WORKSPACE_ROOT", server.WORKSPACE_ROOT)
	before = server.WORKSPACE_ROOT

	with pytest.raises(ToolInputError, match="holds more than 10 Python files"):
		asyncio.run(server._configure_workspace(huge, "test"))

	assert server.WORKSPACE_ROOT == before
	assert not started
