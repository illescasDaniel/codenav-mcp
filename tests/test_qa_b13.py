"""QA block B13: workspace and environment validation (RC-017, 044, 095, 096, 097, 128, 155, 156)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp_nav_shared.workspace import resolve_extra_source_roots

from codenav_mcp import server


def _use_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
	"""Point the server's module state at `root` for this test only (monkeypatch restores it)."""
	for name in ("CODENAV_MCP_SOURCE_ROOT", "CODENAV_MCP_EXTRA_SOURCE_ROOTS", "CODENAV_MCP_READ_ONLY"):
		monkeypatch.delenv(name, raising=False)
	for name in ("WORKSPACE_ROOT", "SOURCE_ROOT", "EXTRA_SOURCE_ROOTS", "_workspace_source", "_probe_cache_signature"):
		monkeypatch.setattr(server, name, getattr(server, name))
	monkeypatch.setattr(server, "_client", None)
	monkeypatch.setattr(server, "WORKSPACE_ROOT", root)
	monkeypatch.setattr(server, "SOURCE_ROOT", root)
	monkeypatch.setattr(server, "EXTRA_SOURCE_ROOTS", [])
	monkeypatch.setattr(server, "_workspace_source", "cwd")


@pytest.fixture
def ws(tmp_path, monkeypatch):
	"""An empty workspace folder, with the server's module state pointed at it."""
	root = (tmp_path / "ws").resolve()
	root.mkdir()
	_use_root(monkeypatch, root)
	return root


def _workspace_text() -> str:
	return asyncio.run(server.workspace())


def _import_server(cwd: Path, **env: str) -> subprocess.CompletedProcess[str]:
	"""Import codenav_mcp.server in a fresh interpreter; the import validates the workspace settings."""
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


def test_rc_095_given_filesystem_root_as_workspace_when_import_then_refused(tmp_path):
	result = _import_server(tmp_path, CODENAV_MCP_WORKSPACE="/")

	assert result.returncode != 0
	assert "refusing to use / as the workspace; set CODENAV_MCP_WORKSPACE to your project directory" in result.stderr


def test_rc_155_given_pinned_workspace_that_does_not_exist_when_import_then_refused(tmp_path):
	result = _import_server(tmp_path, CODENAV_MCP_WORKSPACE=str(tmp_path / "qa-nope-proj"))

	assert result.returncode != 0
	assert "CODENAV_MCP_WORKSPACE=" in result.stderr
	assert "is not an existing directory" in result.stderr


def test_rc_096_given_source_root_outside_workspace_when_import_then_refused(tmp_path):
	workspace = tmp_path / "ws"
	workspace.mkdir()
	outside = tmp_path / "outside"
	outside.mkdir()

	result = _import_server(tmp_path, CODENAV_MCP_WORKSPACE=str(workspace), CODENAV_MCP_SOURCE_ROOT=str(outside))

	assert result.returncode != 0
	assert f"CODENAV_MCP_SOURCE_ROOT={outside} is not a directory inside the workspace" in result.stderr


def test_rc_096_given_missing_source_root_when_import_then_refused(tmp_path):
	workspace = tmp_path / "ws"
	workspace.mkdir()

	result = _import_server(tmp_path, CODENAV_MCP_WORKSPACE=str(workspace), CODENAV_MCP_SOURCE_ROOT="nope")

	assert result.returncode != 0
	assert "CODENAV_MCP_SOURCE_ROOT=nope is not a directory inside the workspace" in result.stderr


def test_rc_096_given_extra_roots_outside_or_missing_when_resolve_then_only_inside_dirs_kept(tmp_path, monkeypatch):
	workspace = tmp_path / "ws"
	(workspace / "tests").mkdir(parents=True)
	outside = tmp_path / "outside"
	outside.mkdir()
	monkeypatch.setenv("CODENAV_MCP_EXTRA_SOURCE_ROOTS", f"{outside},tests,missing")

	roots = resolve_extra_source_roots("CODENAV_MCP_EXTRA_SOURCE_ROOTS", workspace)

	assert roots == [(workspace / "tests").resolve()]


def test_rc_096_given_skipped_extra_root_when_workspace_then_notice_names_it(ws, monkeypatch, tmp_path):
	outside = tmp_path / "outside"
	outside.mkdir()
	monkeypatch.setenv("CODENAV_MCP_EXTRA_SOURCE_ROOTS", f"{outside},missing")

	asyncio.run(server._configure_workspace(ws, "cwd"))
	text = _workspace_text()

	assert f"Ignored extra source root: {outside} (not an existing folder inside the workspace)" in text
	assert "Ignored extra source root: missing" in text


def test_rc_044_given_read_only_env_when_workspace_then_writes_state_shown(ws, monkeypatch):
	monkeypatch.setenv("CODENAV_MCP_READ_ONLY", "1")

	assert "Writes: disabled (CODENAV_MCP_READ_ONLY)" in _workspace_text()


def test_rc_044_given_default_when_workspace_then_writes_enabled_shown(ws):
	assert "Writes: enabled" in _workspace_text()


def test_rc_017_given_parent_has_pyproject_when_workspace_then_warns_with_parent_name(tmp_path, monkeypatch):
	mono = tmp_path / "mono"
	sub = mono / "sub"
	sub.mkdir(parents=True)
	(mono / "pyproject.toml").write_text('[project]\nname = "mono"\n', encoding="utf-8")
	_use_root(monkeypatch, sub)

	text = _workspace_text()

	assert "Warning: no pyproject.toml here but mono/ has one" in text
	assert "set CODENAV_MCP_WORKSPACE to that folder" in text


def test_rc_097_given_sub_packages_with_pyproject_when_workspace_then_warns_with_names(ws):
	for name in ("pkga", "pkgb"):
		(ws / name).mkdir()
		(ws / name / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")

	text = _workspace_text()

	assert "no pyproject.toml here, but these sub-folders have one: pkga/, pkgb/" in text


def test_b13_guard_given_root_pyproject_when_workspace_then_no_warning(ws):
	(ws / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
	(ws / "pkga").mkdir()
	(ws / "pkga" / "pyproject.toml").write_text('[project]\nname = "a"\n', encoding="utf-8")

	assert "Warning:" not in _workspace_text()


def test_rc_156_given_unparseable_pyproject_when_workspace_then_notes_ty_defaults(ws):
	(ws / "pyproject.toml").write_text("[project]\nname = 'x'\n\n[tool.ty\n", encoding="utf-8")

	text = _workspace_text()

	assert "Note: pyproject.toml could not be parsed (" in text
	assert "); ty runs with its defaults." in text


def test_rc_128_given_src_layout_when_configured_then_source_root_is_src(ws, monkeypatch):
	(ws / "src" / "pkg").mkdir(parents=True)
	(ws / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")

	asyncio.run(server._configure_workspace(ws, "cwd"))
	text = _workspace_text()

	assert server.SOURCE_ROOT == (ws / "src").resolve()
	assert "Source root: src (auto-detected)" in text
