from __future__ import annotations

import asyncio


def test_post_write_created_file_sync_failure_marks_all_files_unchecked(tmp_path):
	from mcp_nav_shared.edits import EditPlan, FileChange

	from codenav_mcp.writes import errors_after_write

	class Client:
		async def refresh(self):
			pass

		async def diagnostics_with_open_files(self, paths, *, open_first):
			raise OSError("private path /hidden")

	plan = EditPlan(
		"create",
		[FileChange(tmp_path / "existing.py", "x=1\n", "x=2\n"), FileChange(tmp_path / "new.py", None, "x=1\n")],
	)
	result = asyncio.run(errors_after_write(Client(), tmp_path, plan))
	assert "Not checked" in result and "existing.py, new.py (2 files)" in result
	assert "/hidden" not in result and str(tmp_path) not in result
