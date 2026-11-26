"""QA block B28: search_symbol skips excluded directories (RC-002 search side, decision D1-A).

The exclusion list is the one every other tool uses (`CODENAV_MCP_EXCLUDE_DIRS` / `CODENAV_MCP_INCLUDE_DIRS`).
The LSP client is a fake that answers `workspace_symbol` from a fixed list; ty is never started.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from codenav_mcp import read_tools, server


_FILES = {
	"src/keep.py": "def keep_symbol():\n\tpass\n",
	"build/built.py": "def build_symbol():\n\tpass\n",
	"env/envmod.py": "def env_symbol():\n\tpass\n",
	"dist/distmod.py": "def dist_symbol():\n\tpass\n",
}


_SYMBOL_NAMES = {
	"src/keep.py": "keep_symbol",
	"build/built.py": "build_symbol",
	"env/envmod.py": "env_symbol",
	"dist/distmod.py": "dist_symbol",
}


class _Client:
	"""Answers `workspace_symbol` with one function symbol per file in `_FILES`, matched by name."""

	def __init__(self, root: Path) -> None:
		self.symbols: list[dict[str, Any]] = [
			{
				"name": name,
				"kind": 12,
				"location": {
					"uri": (root / rel).as_uri(),
					"range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 14}},
				},
			}
			for rel, name in _SYMBOL_NAMES.items()
		]

	async def workspace_symbol(self, query: str) -> list[dict[str, Any]]:
		return [sym for sym in self.symbols if query in sym["name"]]


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
	root = (tmp_path / "ws").resolve()
	for rel, text in _FILES.items():
		path = root / rel
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(text, encoding="utf-8")
	monkeypatch.delenv("CODENAV_MCP_EXCLUDE_DIRS", raising=False)
	monkeypatch.delenv("CODENAV_MCP_INCLUDE_DIRS", raising=False)
	monkeypatch.setattr(server, "WORKSPACE_ROOT", root)
	monkeypatch.setattr(server, "SOURCE_ROOT", root)
	monkeypatch.setattr(server, "EXTRA_SOURCE_ROOTS", [])

	async def _get_client() -> _Client:
		return _Client(root)

	monkeypatch.setattr(server, "get_client", _get_client)
	return root


def _search(query: str) -> str:
	result = asyncio.run(read_tools.search(query=query))
	return result


def test_rc_002_search_skips_build_env_dist_directories(ws):
	# when
	texts = {name: _search(name) for name in ("build_symbol", "env_symbol", "dist_symbol")}
	# then — no file in an excluded directory is listed; the note says which directories were skipped
	for name, text in texts.items():
		assert text.startswith(f"0 symbols match {name!r}.\n"), text
		assert "build/built.py" not in text
	assert texts["build_symbol"].endswith(
		"Symbols in excluded directories were not shown: build/ (1). Pass CODENAV_MCP_INCLUDE_DIRS=build to include them."
	)
	assert "dist/ (1)" in texts["dist_symbol"]


def test_rc_002_search_still_lists_source_files_with_relative_paths(ws):
	# when
	text = _search("keep_symbol")
	# then — the non-excluded file is listed with its workspace-relative path, unchanged
	assert "src/keep.py" in text
	assert str(ws) not in text


def test_rc_002_include_dirs_un_excludes_build(ws, monkeypatch):
	# given
	monkeypatch.setenv("CODENAV_MCP_INCLUDE_DIRS", "build")
	# when
	build_text = _search("build_symbol")
	env_text = _search("env_symbol")
	# then — `build` is included by the override; `env` stays excluded
	assert "build/built.py" in build_text
	assert env_text.startswith("0 symbols match 'env_symbol'.")
	assert "env/ (1)" in env_text


def test_rc_002_exclude_dirs_adds_names_to_search_skip(ws, monkeypatch):
	# given
	monkeypatch.setenv("CODENAV_MCP_EXCLUDE_DIRS", "src")
	# when
	text = _search("keep_symbol")
	# then — a name added to the shared list is skipped by search too
	assert text.startswith("0 symbols match 'keep_symbol'.")
	assert "src/ (1)" in text
