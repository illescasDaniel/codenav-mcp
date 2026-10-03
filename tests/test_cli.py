"""The console script answers `--help` / `--version` instead of silently waiting on stdin."""

from __future__ import annotations

import subprocess
import sys


def _run(*args: str) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		[sys.executable, "-c", "from codenav_mcp.server import main; main()", *args],
		capture_output=True,
		text=True,
		stdin=subprocess.DEVNULL,
		timeout=60,
		check=False,
	)


def test_given_help_flag_when_running_then_usage_is_printed_and_server_does_not_start() -> None:
	result = _run("--help")

	assert result.returncode == 0
	assert "usage: codenav-mcp" in result.stdout
	assert "CODENAV_MCP_" in result.stdout


def test_given_version_flag_when_running_then_the_version_is_printed() -> None:
	result = _run("--version")

	assert result.returncode == 0
	assert result.stdout.startswith("codenav-mcp ")


def test_given_unknown_flag_when_running_then_it_is_rejected() -> None:
	result = _run("--nope")

	assert result.returncode == 2
