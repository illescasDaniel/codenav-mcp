"""Rename notes only scan this workspace, not nested checkouts or linked worktrees."""

from __future__ import annotations

from pathlib import Path

from codenav_mcp.mentions import find_mentions


def test_given_nested_worktree_docs_when_finding_mentions_then_they_are_skipped(tmp_path: Path) -> None:
	(tmp_path / "docs").mkdir()
	(tmp_path / "docs" / "guide.md").write_text("call greet here\n", encoding="utf-8")
	nested = tmp_path / ".claude" / "worktrees" / "feature" / "docs"
	nested.mkdir(parents=True)
	(nested.parent / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
	(nested / "guide.md").write_text("call greet here\n", encoding="utf-8")

	mentions = find_mentions(tmp_path, "greet", {})

	assert [m.path for m in mentions] == ["docs/guide.md"]
