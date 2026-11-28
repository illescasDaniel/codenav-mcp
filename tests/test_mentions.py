"""Rename notes only scan this workspace, not nested checkouts or linked worktrees."""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_given_dot_directories_when_finding_mentions_then_python_and_text_files_in_them_are_skipped(
	tmp_path: Path,
) -> None:
	(tmp_path / "app.py").write_text("x.greet()\n", encoding="utf-8")
	for hidden in (".agents/skills", ".cursor/skills", ".github"):
		(tmp_path / hidden).mkdir(parents=True)
		(tmp_path / hidden / "tool.py").write_text("thing.greet()\n", encoding="utf-8")
		(tmp_path / hidden / "README.md").write_text("call greet\n", encoding="utf-8")

	mentions = find_mentions(tmp_path, "greet", {})

	assert [m.path for m in mentions] == ["app.py"]


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


def test_given_skipped_files_first_when_finding_mentions_then_later_files_are_still_scanned(tmp_path: Path) -> None:
	# Each skip (symlink, oversized, unreadable, path filter, shell script) moves on to the next file; none ends the scan.
	outside = tmp_path / "outside"
	outside.mkdir()
	(outside / "real.py").write_text("x.greet()\n", encoding="utf-8")
	(outside / "real.md").write_text("call greet\n", encoding="utf-8")
	ws = tmp_path / "ws"
	ws.mkdir()
	(ws / "a_link.py").symlink_to(outside / "real.py")
	(ws / "z.py").write_text("y.greet()\n", encoding="utf-8")
	(ws / "a_link.md").symlink_to(outside / "real.md")
	(ws / "b_big.md").write_text("greet\n" + "x" * 600_000, encoding="utf-8")
	(ws / "c_skip.md").write_text("greet\n", encoding="utf-8")
	(ws / "c_unreadable.md").write_text("greet\n", encoding="utf-8")
	(ws / "c_unreadable.md").chmod(0)
	try:
		(ws / "c_unreadable.md").read_text()
	except OSError:
		pass
	else:
		pytest.skip("chmod(0) does not make files unreadable on this platform")
	(ws / "d.sh").write_text("python3 <<'PY'\nz.greet()\nPY\n", encoding="utf-8")
	(ws / "e.md").write_text("then greet\n", encoding="utf-8")

	mentions = find_mentions(ws, "greet", {}, path_matches=lambda rel: rel != "c_skip.md")

	assert sorted((m.path, m.line) for m in mentions) == [("d.sh", 2), ("e.md", 1), ("z.py", 1)]


def test_given_nested_checkout_in_a_plain_folder_when_finding_mentions_then_it_is_skipped(tmp_path: Path) -> None:
	(tmp_path / "guide.md").write_text("call greet\n", encoding="utf-8")
	nested = tmp_path / "third_party" / "lib"
	nested.mkdir(parents=True)
	(nested / ".git").mkdir()
	(nested / "README.md").write_text("call greet\n", encoding="utf-8")

	assert [m.path for m in find_mentions(tmp_path, "greet", {})] == ["guide.md"]


def test_given_strings_of_three_and_four_words_when_finding_mentions_then_four_words_is_prose(tmp_path: Path) -> None:
	(tmp_path / "app.py").write_text('a = "greet them now"\nb = "then greet them now"\n', encoding="utf-8")

	mentions = find_mentions(tmp_path, "greet", {})

	assert [(m.line, m.kind, m.prose) for m in mentions] == [(1, "string", False), (2, "string", True)]
