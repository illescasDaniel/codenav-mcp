"""B17: an edit says which importers in excluded directories it did not check (RC-002)."""

from __future__ import annotations

import asyncio

from mcp_nav_shared.diagnostics_delta import DiagnosticsDelta, format_delta
from mcp_nav_shared.edits import EditPlan, FileChange

from codenav_mcp import simulate


def _write(root, files: dict[str, str]) -> None:
	for name, content in files.items():
		path = root / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(content, encoding="utf-8")


def test_rc_002_edit_reports_excluded_importers_not_checked(tmp_path, monkeypatch):
	monkeypatch.delenv("CODENAV_MCP_INCLUDE_DIRS", raising=False)
	_write(
		tmp_path,
		{
			"app/target.py": "def tgt(): ...\n",
			"app/main.py": "from app.target import tgt\n",
			"app/build/inner_user.py": "from app.target import tgt\n",
		},
	)
	target = tmp_path / "app/target.py"
	plan = EditPlan("Edit app/target.py", [FileChange(target, "def tgt(): ...\n", "def tgt(x): ...\n")])
	checked: dict[str, list[str]] = {}

	async def fake_diagnostics_delta(client, workspace, **kwargs):
		checked["paths"] = [path.name for path in kwargs["check"]]
		return DiagnosticsDelta()

	monkeypatch.setattr(simulate, "diagnostics_delta", fake_diagnostics_delta)
	delta = asyncio.run(simulate.simulate_plan(object(), tmp_path, [tmp_path], plan))

	assert checked["paths"] == ["target.py", "main.py"]
	text = format_delta(delta)
	assert "Importers not checked (excluded directories): app/build/ (1 file)" in text, text
