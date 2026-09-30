"""Fast unit tests for `codenav_mcp.ty_command` (no ty process is started)."""

from __future__ import annotations

import sys

from codenav_mcp import ty_command
from codenav_mcp.ty_command import resolve_ty_command


def _venv_ty(root):
	bin_dir = root / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
	bin_dir.mkdir(parents=True)
	exe = bin_dir / ("ty.exe" if sys.platform == "win32" else "ty")
	exe.write_text("")
	return exe


def test_given_project_venv_has_ty_when_resolve_then_uses_it(tmp_path, monkeypatch):
	# given — the project pins its own ty; its version matches its config
	exe = _venv_ty(tmp_path)
	monkeypatch.setattr(ty_command, "_bundled_ty", lambda: "/elsewhere/ty")
	# when
	command = resolve_ty_command(tmp_path)
	# then
	assert command == [str(exe), "server"]


def test_given_no_project_ty_when_resolve_then_uses_bundled_ty_not_on_path(tmp_path, monkeypatch):
	# given — installed via uvx/pipx: codenav's own ty exists but its env isn't on PATH
	monkeypatch.setattr(ty_command, "_bundled_ty", lambda: "/tool-env/bin/ty")
	monkeypatch.setattr(ty_command.shutil, "which", lambda _name: None)
	# when
	command = resolve_ty_command(tmp_path)
	# then
	assert command == ["/tool-env/bin/ty", "server"]


def test_given_ty_nowhere_when_resolve_then_falls_back_to_uvx(tmp_path, monkeypatch):
	# given — `uv run ty` would fail in a project that doesn't declare ty; uvx downloads it
	monkeypatch.setattr(ty_command, "_bundled_ty", lambda: None)
	monkeypatch.setattr(ty_command.shutil, "which", lambda _name: None)
	# when
	command = resolve_ty_command(tmp_path)
	# then
	assert command == ["uvx", "ty", "server"]


def test_given_ty_package_installed_when_bundled_ty_then_finds_real_binary():
	# given — `ty` is a declared dependency of codenav-mcp
	# when
	path = ty_command._bundled_ty()
	# then
	assert path is not None and path.endswith(("ty", "ty.exe"))
