"""Live checks against a real `ty` server on a throwaway project: what the
mocked unit tests can't show — that answers track files changed on disk, and
that `implementations` reports test doubles from extra roots."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codenav_mcp import server as codenav_server


_REPO_ROOT = Path(__file__).resolve().parents[1]
_TY = _REPO_ROOT / ".venv" / "bin" / "ty"

pytestmark = [
	pytest.mark.integration,
	pytest.mark.skipif(not _TY.is_file(), reason="repo .venv ty binary not available"),
]


@pytest.fixture
def loop():
	# One loop for the whole test: the server keeps its LSP client (and its locks) across calls.
	event_loop = asyncio.new_event_loop()
	yield event_loop
	client = codenav_server._client
	if client is not None:
		event_loop.run_until_complete(client.stop())
	event_loop.close()


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	src = tmp_path / "src" / "pkg"
	src.mkdir(parents=True)
	(tmp_path / "tests").mkdir()
	(tmp_path / "pyproject.toml").write_text(
		'[project]\nname = "demo"\nversion = "0"\n[tool.ty.environment]\nroot = ["src", "."]\n', encoding="utf-8"
	)
	(src / "__init__.py").write_text("", encoding="utf-8")
	(src / "core.py").write_text("def target() -> int:\n\treturn 1\n", encoding="utf-8")
	(src / "port.py").write_text(
		"from typing import Protocol\n\n\nclass StorePort(Protocol):\n\tdef save(self, key: str) -> None: ...\n",
		encoding="utf-8",
	)
	(src / "adapter.py").write_text(
		"class DiskStore:\n\tdef save(self, key: str) -> None:\n\t\tpass\n", encoding="utf-8"
	)
	(tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
	(tmp_path / "tests" / "fakes.py").write_text(
		"class FakeStore:\n\tdef save(self, key: str) -> None:\n\t\tpass\n", encoding="utf-8"
	)
	monkeypatch.setattr(codenav_server, "WORKSPACE_ROOT", tmp_path)
	monkeypatch.setattr(codenav_server, "SOURCE_ROOT", tmp_path / "src")
	monkeypatch.setattr(codenav_server, "EXTRA_SOURCE_ROOTS", [])
	monkeypatch.setattr(codenav_server, "_client", None)
	monkeypatch.setattr(codenav_server, "resolve_ty_command", lambda _root: [str(_TY), "server"])
	return tmp_path


def test_given_files_changed_on_disk_when_callers_then_answers_track_the_disk(project, loop):
	async def scenario() -> list[str]:
		out = [await codenav_server.callers(name="target")]
		(project / "src" / "pkg" / "user.py").write_text(
			"from pkg.core import target\n\n\ndef uses() -> int:\n\treturn target()\n", encoding="utf-8"
		)
		out.append(await codenav_server.callers(name="target"))
		(project / "src" / "pkg" / "user.py").write_text("def uses() -> int:\n\treturn 2\n", encoding="utf-8")
		out.append(await codenav_server.callers(name="target"))
		(project / "src" / "pkg" / "user.py").unlink()
		out.append(await codenav_server.callers(name="target"))
		return out

	# when
	before, created, edited, deleted = loop.run_until_complete(scenario())
	# then
	assert "No callers" in before
	assert "uses" in created
	assert "uses" not in edited
	assert "uses" not in deleted


def test_given_extra_root_when_implementations_then_test_double_listed_separately(project, loop, monkeypatch):
	# given
	monkeypatch.setattr(codenav_server, "EXTRA_SOURCE_ROOTS", [project / "tests"])
	# when
	text = loop.run_until_complete(codenav_server.implementations(port_name="StorePort"))
	# then
	head, _, extra = text.partition("more in extra roots")
	assert "DiskStore" in head and "FakeStore" not in head
	assert "FakeStore" in extra and "DiskStore" not in extra


def test_given_no_extra_root_when_implementations_then_test_double_not_scanned(project, loop):
	# when
	text = loop.run_until_complete(codenav_server.implementations(port_name="StorePort"))
	# then
	assert "DiskStore" in text
	assert "FakeStore" not in text


def test_given_config_edited_when_next_call_then_ty_restarted_and_result_says_so(project, loop):
	match_file = project / "src" / "pkg" / "matcher.py"
	match_file.write_text(
		"def f(x: int) -> int:\n\tmatch x:\n\t\tcase 1:\n\t\t\treturn 1\n\treturn 0\n", encoding="utf-8"
	)

	async def scenario() -> tuple[str, str, str]:
		before = await codenav_server.diagnostics(file_path="src/pkg/matcher.py")
		# `match` needs Python 3.10: a version pin is read by ty only when it starts
		(project / "pyproject.toml").write_text(
			'[project]\nname = "demo"\nversion = "0"\n[tool.ty.environment]\nroot = ["src", "."]\npython-version = "3.9"\n',
			encoding="utf-8",
		)
		after_edit = await codenav_server.diagnostics(file_path="src/pkg/matcher.py")
		after_restart = await codenav_server.diagnostics(file_path="src/pkg/matcher.py")
		return before, after_edit, after_restart

	# when
	before, after_edit, after_restart = loop.run_until_complete(scenario())
	# then
	assert "3.9" not in before
	assert "3.9" in after_edit
	assert "restarted the language server because pyproject.toml changed" in after_edit
	assert "restarted" not in after_restart  # one-shot
