"""Rename notes only scan this workspace, not nested checkouts or linked worktrees."""

from __future__ import annotations

from pathlib import Path

from codenav_mcp.mentions import Mention, find_mentions, format_mentions


def test_given_nested_worktree_docs_when_finding_mentions_then_they_are_skipped(tmp_path: Path) -> None:
	(tmp_path / "docs").mkdir()
	(tmp_path / "docs" / "guide.md").write_text("call greet here\n", encoding="utf-8")
	nested = tmp_path / ".claude" / "worktrees" / "feature" / "docs"
	nested.mkdir(parents=True)
	(nested.parent / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
	(nested / "guide.md").write_text("call greet here\n", encoding="utf-8")

	mentions = find_mentions(tmp_path, "greet", {})

	assert [m.path for m in mentions] == ["docs/guide.md"]


def _reveal_project(tmp_path: Path) -> None:
	(tmp_path / "port.py").write_text(
		"class DesktopPort:\n\tdef reveal_path(self, p): ...\n\n\ndef reveal_path(p): ...\n", encoding="utf-8"
	)
	for i in range(8):  # more bare-name uses than the cap, sorted before the test file
		(tmp_path / f"a{i}.py").write_text("from port import reveal_path\n\nreveal_path(1)\n", encoding="utf-8")
	(tmp_path / "test_x.py").write_text(
		"def t(mock):\n\tmock.reveal_path.assert_called_once_with(1)\n", encoding="utf-8"
	)


def test_given_attribute_access_when_finding_mentions_then_it_is_kind_attr_and_sorted_first(tmp_path: Path) -> None:
	_reveal_project(tmp_path)

	mentions = find_mentions(tmp_path, "reveal_path", {})

	assert mentions[0].kind == "attr" and (mentions[0].path, mentions[0].line) == ("test_x.py", 2)
	assert {m.kind for m in mentions[1:]} == {"code"}


def test_given_many_bare_names_when_formatting_then_attribute_line_is_listed_first_and_not_cut(tmp_path: Path) -> None:
	_reveal_project(tmp_path)
	mentions = find_mentions(tmp_path, "reveal_path", {})

	notes = format_mentions(mentions, "reveal_path", member=True)

	assert notes[0].startswith("`reveal_path` still accessed as an attribute")
	assert "Probably the renamed member" in notes[0]
	assert "test_x.py:2: mock.reveal_path.assert_called_once_with(1)" in notes[0]
	assert "bare name" in notes[1] and "more" in notes[1]
	assert "test_x.py" not in notes[1]


def test_given_non_member_when_formatting_then_attribute_heading_is_softer() -> None:
	mentions = [Mention("a.py", 1, "attr", "mod.f()")]

	assert "may or may not" in format_mentions(mentions, "f", member=False)[0]
	assert "Probably" not in format_mentions(mentions, "f", member=False)[0]


def test_given_more_attributes_than_cap_when_formatting_then_excess_is_counted() -> None:
	mentions = [Mention("a.py", i, "attr", "m.f") for i in range(1, 56)]

	note = format_mentions(mentions, "f", member=True)[0]

	assert note.endswith("... 5 more") and note.count("a.py:") == 50
