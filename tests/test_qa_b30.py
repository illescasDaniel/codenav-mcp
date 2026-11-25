"""QA block B30: message wording and call-site fixes (BUG-BATCH-R02, BUG-DEPS-005 note, env overrides in server scans)."""

from __future__ import annotations

import pytest

from codenav_mcp import server as codenav_server
from codenav_mcp.deps import Skipped, format_skipped
from codenav_mcp.pysource import PythonSyntaxError, definitions_in


def test_rc_b30_batch_r02_syntax_refusal_names_the_relative_file() -> None:
	with pytest.raises(PythonSyntaxError) as info:
		definitions_in("def f(:\n\tpass\n", filename="pkg/mod.py")
	text = str(info.value)
	assert text.startswith("pkg/mod.py:")
	assert "<source>" not in text


def test_rc_b30_deps_005_excluded_note_names_the_importer_files() -> None:
	skipped = [
		Skipped("app/build/", "excluded dir", "2 files import it", 2, ("app/build/a.py", "app/build/sub/b.py")),
	]
	note = format_skipped(skipped)[0]
	assert "app/build/a.py" in note
	assert "app/build/sub/b.py" in note
	assert "CODENAV_MCP_INCLUDE_DIRS=build" in note


def test_rc_b30_excluded_note_caps_long_file_lists() -> None:
	files = tuple(f"app/build/m{i}.py" for i in range(12))
	note = format_skipped([Skipped("app/build/", "excluded dir", "12 files import it", 12, files)])[0]
	assert "app/build/m9.py" in note
	assert "app/build/m10.py" not in note
	assert "and 2 more" in note


def test_rc_b30_g_candidate_scan_applies_env_exclude_overrides(tmp_path, monkeypatch) -> None:
	(tmp_path / "vendored_x").mkdir()
	(tmp_path / "vendored_x" / "m.py").write_text("class A: ...\n", encoding="utf-8")
	(tmp_path / "src.py").write_text("class B: ...\n", encoding="utf-8")
	monkeypatch.setenv("CODENAV_MCP_EXCLUDE_DIRS", "vendored_x")
	assert [p.name for p in codenav_server._candidate_files(tmp_path)] == ["src.py"]
	monkeypatch.setenv("CODENAV_MCP_INCLUDE_DIRS", "vendored_x")
	assert sorted(p.name for p in codenav_server._candidate_files(tmp_path)) == ["m.py", "src.py"]
