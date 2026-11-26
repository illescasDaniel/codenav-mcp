"""The three check modes share the read-only diagnostics entry point."""

from __future__ import annotations

import pytest
from mcp_nav_shared.errors import FailedText

from codenav_mcp import read_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]
loop = make_loop_fixture()


@pytest.fixture
def project(tmp_path, monkeypatch):
	return make_workspace(
		tmp_path,
		monkeypatch,
		{"src/a.py": 'a: int = "a"\nb: int = "b"\n'},
	)


@pytest.mark.parametrize("workspace", [False, True], ids=["file", "workspace"])
def test_check_reports_complete_counts_and_pages_without_writing(project, loop, workspace):
	file_path = None if workspace else "src/a.py"
	before = (project / "src/a.py").read_bytes()
	first = loop.run_until_complete(read_tools.check(file_path=file_path, limit=1))
	second = loop.run_until_complete(read_tools.check(file_path=file_path, limit=1, offset=1))

	assert "2 error(s), 2 diagnostic(s)" in first and "2 error(s), 2 diagnostic(s)" in second
	assert "src/a.py:1:" in first and "src/a.py:2:" not in first
	assert "src/a.py:2:" in second and "src/a.py:1:" not in second
	assert "offset=1" in first and "next:" not in second
	assert (project / "src/a.py").read_bytes() == before
	if workspace:
		assert "ty version:" in first and "Python:" in first and "Source root: src" in first


def test_check_python_summary_ignores_non_table_metadata(tmp_path):
	(tmp_path / "pyproject.toml").write_text('tool = "broken"\nproject = "broken"\n', encoding="utf-8")
	assert read_tools._python_summary(tmp_path) == "Python: inferred by ty"


@pytest.mark.parametrize(
	("arguments", "reason"),
	[
		({"file_path": "src/a.py", "since": "HEAD"}, "give either file_path or since"),
		({"limit": 0}, "limit must be at least 1"),
		({"offset": -1}, "offset must be at least 0"),
	],
)
def test_check_refuses_conflicting_modes_and_invalid_pages(loop, arguments, reason):
	result = loop.run_until_complete(read_tools.check(**arguments))
	assert isinstance(result, FailedText)
	assert reason in result and 'Example: check(since="HEAD")' in result
