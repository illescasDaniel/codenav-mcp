"""Shared fixtures for the live write-tool tests: a throwaway project served by the repo's own `ty`."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codenav_mcp import server as codenav_server, writes


REPO_ROOT = Path(__file__).resolve().parents[1]
TY = REPO_ROOT / ".venv" / "bin" / "ty"

needs_ty = pytest.mark.skipif(not TY.is_file(), reason="repo .venv ty binary not available")


def write_files(root: Path, files: dict[str, str]) -> None:
	for name, content in files.items():
		path = root / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(content, encoding="utf-8")


def make_loop_fixture():
	@pytest.fixture
	def loop():
		event_loop = asyncio.new_event_loop()
		yield event_loop
		client = codenav_server._client
		if client is not None:
			event_loop.run_until_complete(client.stop())
		event_loop.close()

	return loop


def make_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> Path:
	(tmp_path / "pyproject.toml").write_text(
		'[project]\nname = "demo"\nversion = "0"\n[tool.ty.environment]\nroot = ["src", "."]\n', encoding="utf-8"
	)
	write_files(tmp_path, files)
	monkeypatch.setattr(codenav_server, "WORKSPACE_ROOT", tmp_path)
	monkeypatch.setattr(codenav_server, "SOURCE_ROOT", tmp_path / "src")
	monkeypatch.setattr(codenav_server, "EXTRA_SOURCE_ROOTS", [])
	monkeypatch.setattr(codenav_server, "_client", None)
	monkeypatch.setattr(codenav_server, "resolve_ty_command", lambda _root: [str(TY), "server"])
	monkeypatch.delenv(writes.READ_ONLY_ENV, raising=False)
	writes.reset_state()
	return tmp_path
