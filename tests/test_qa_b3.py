import asyncio
from pathlib import Path

import pytest
from mcp_nav_shared.edits import EditPlan, FileChange
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp.writes import finish_plan, require_workspace_file


def test_rc_109_directory(tmp_path: Path) -> None:
	with pytest.raises(ToolInputError, match="is a directory, not a file"):
		require_workspace_file(tmp_path, ".")


def test_rc_048_outside(tmp_path: Path) -> None:
	with pytest.raises(ToolInputError, match="outside the workspace; give a path inside it"):
		require_workspace_file(tmp_path, "../outside.py")


def test_rc_115_negative(tmp_path: Path) -> None:
	with pytest.raises(ToolInputError, match="max_new_errors must be 0 or more"):
		asyncio.run(
			finish_plan(
				None, workspace=tmp_path, roots=[], plan=EditPlan("x", []), title="x", apply=True, max_new_errors=-1
			)
		)


def test_rc_100_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from mcp_nav_shared.diagnostics_delta import DiagnosticsDelta

	from codenav_mcp import writes

	async def simulate(*args: object, **kwargs: object) -> DiagnosticsDelta:
		return DiagnosticsDelta()

	async def status(*args: object, **kwargs: object) -> str:
		return "After writing: no errors in the edited files."

	monkeypatch.setattr(writes, "simulate_plan", simulate)
	monkeypatch.setattr(writes, "errors_after_write", status)
	path = tmp_path / "a.py"
	path.write_text("x = 1\n")
	before = path.stat().st_mtime_ns
	result = asyncio.run(
		finish_plan(
			None,
			workspace=tmp_path,
			roots=[],
			plan=EditPlan("x", [FileChange(path, "x = 1\n", "x = 1\n")]),
			title="x",
			apply=True,
			max_new_errors=0,
		)
	)
	assert "No changes: the edit leaves a.py as it is." in result.text
	assert result.applied_id is None and path.stat().st_mtime_ns == before


def test_rc_005_unavailable(tmp_path: Path) -> None:
	from mcp_nav_shared.lsp_client import LanguageServerExitedError

	from codenav_mcp.writes import errors_after_write

	class DeadClient:
		async def refresh(self) -> None:
			pass

		async def diagnostics(self, path: str) -> list:
			raise LanguageServerExitedError("dead")

	path = tmp_path / "a.py"
	result = asyncio.run(errors_after_write(DeadClient(), tmp_path, EditPlan("x", [FileChange(path, "a", "b")])))
	assert result == "Not checked (language server unavailable): a.py (1 files)."


def test_rc_058_outside(tmp_path: Path) -> None:
	with pytest.raises(ToolInputError) as error:
		require_workspace_file(tmp_path, "../outside.py")
	assert str(tmp_path) not in str(error.value)


def test_rc_100_mixed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from mcp_nav_shared.diagnostics_delta import DiagnosticsDelta

	from codenav_mcp import writes

	async def simulate(*args: object, **kwargs: object) -> DiagnosticsDelta:
		return DiagnosticsDelta()

	async def status(*args: object, **kwargs: object) -> str:
		return "After writing: no errors in the edited files."

	monkeypatch.setattr(writes, "simulate_plan", simulate)
	monkeypatch.setattr(writes, "errors_after_write", status)
	unchanged, changed = tmp_path / "same.py", tmp_path / "changed.py"
	unchanged.write_text("same")
	changed.write_text("old")
	inode = unchanged.stat().st_ino
	plan = EditPlan("mixed", [FileChange(unchanged, "same", "same"), FileChange(changed, "old", "new")])
	outcome = asyncio.run(
		finish_plan(None, workspace=tmp_path, roots=[], plan=plan, title="mixed", apply=True, max_new_errors=0)
	)
	assert outcome.applied_id
	assert unchanged.stat().st_ino == inode
	assert writes.write_state(tmp_path).journal.history()[0].plan.paths == [changed]


def test_not_applied_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from mcp_nav_shared.diagnostics_delta import DiagnosticsDelta

	from codenav_mcp import writes

	async def simulate(*args: object, **kwargs: object) -> DiagnosticsDelta:
		return DiagnosticsDelta(unchecked=["a.py"])

	async def status(*args: object, **kwargs: object) -> str:
		return "After writing: no errors in the edited files."

	monkeypatch.setattr(writes, "simulate_plan", simulate)
	monkeypatch.setattr(writes, "errors_after_write", status)
	path = tmp_path / "a.py"
	path.write_text("old")
	plan = EditPlan("x", [FileChange(path, "old", "new")])
	result = asyncio.run(
		finish_plan(None, workspace=tmp_path, roots=[], plan=plan, title="x", apply=True, max_new_errors=0)
	)
	assert "\nNOT applied:" in result.text
	assert path.read_text() == "old"


def test_rc_035_ambiguous_moves_report_new(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from mcp_nav_shared.diagnostics_delta import DiagEntry

	from codenav_mcp import simulate

	old1, old2 = tmp_path / "old1.py", tmp_path / "old2.py"
	new1, new2 = tmp_path / "new1.py", tmp_path / "new2.py"
	new1.write_text("bad")
	new2.write_text("bad")
	before = {old1: "bad", old2: "bad", new1: "", new2: ""}
	calls = 0

	async def collect(*args: object, **kwargs: object) -> tuple[list[DiagEntry], list[str]]:
		nonlocal calls
		calls += 1
		paths = ("old1.py", "old2.py") if calls == 1 else ("new1.py", "new2.py")
		return [DiagEntry(path, 1, 1, 1, "code", "bad", "bad") for path in paths], []

	monkeypatch.setattr(simulate, "_collect", collect)
	result = asyncio.run(simulate.diagnostics_delta(None, tmp_path, before=before, after={}, check=before))
	assert len(result.new_errors) == 2
	assert len(result.fixed_errors) == 2


def test_identical_files_are_not_assumed_to_be_renames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from mcp_nav_shared.diagnostics_delta import DiagEntry

	from codenav_mcp import simulate

	old, new = tmp_path / "old.py", tmp_path / "new.py"
	new.write_text("bad")
	calls = 0

	async def collect(*args: object, **kwargs: object) -> tuple[list[DiagEntry], list[str]]:
		nonlocal calls
		calls += 1
		return [DiagEntry("old.py" if calls == 1 else "new.py", 1, 1, 1, "code", "bad", "bad")], []

	monkeypatch.setattr(simulate, "_collect", collect)
	result = asyncio.run(
		simulate.diagnostics_delta(None, tmp_path, before={old: "bad", new: ""}, after={}, check=[old, new])
	)
	assert len(result.new_errors) == 1 and len(result.fixed_errors) == 1


def test_unreadable_importer_blocks_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from contextlib import asynccontextmanager

	from codenav_mcp import simulate
	from codenav_mcp.deps import DependentsReport

	edited, importer = tmp_path / "a.py", tmp_path / "importer.py"
	edited.write_text("x = 1\n")
	importer.write_bytes(b"\xff")
	monkeypatch.setattr(simulate, "find_dependents_report", lambda *args: DependentsReport(files=[importer]))

	class Client:
		@asynccontextmanager
		async def overlay(self, texts: object):
			yield

		async def diagnostics(self, path: str) -> list:
			return []

	plan = EditPlan("edit", [FileChange(edited, "x = 1\n", "x = 2\n")])
	result = asyncio.run(
		finish_plan(Client(), workspace=tmp_path, roots=[], plan=plan, title="Edit", apply=True, max_new_errors=0)
	)
	assert "NOT applied: diagnostics could not be checked" in result.text
	assert "importer.py" in result.text
	assert edited.read_text() == "x = 1\n"


def test_post_write_reports_errors_and_unchecked(tmp_path: Path) -> None:
	from mcp_nav_shared.lsp_client import LanguageServerExitedError

	from codenav_mcp.writes import errors_after_write

	class Client:
		async def refresh(self) -> None:
			pass

		async def diagnostics(self, path: str) -> list:
			if path.endswith("b.py"):
				raise LanguageServerExitedError("dead")
			return [{"severity": 1, "message": "broken", "range": {"start": {"line": 0, "character": 0}}}]

	plan = EditPlan("edit", [FileChange(tmp_path / "a.py", "a", "b"), FileChange(tmp_path / "b.py", "a", "b")])
	result = asyncio.run(errors_after_write(Client(), tmp_path, plan))
	assert "Not checked" in result and "b.py" in result
	assert "1 error" in result and "broken" in result and "a.py" in result


def test_outside_absolute_path_is_not_disclosed(tmp_path: Path) -> None:
	outside = tmp_path.parent / "secret-parent" / "outside.py"
	with pytest.raises(ToolInputError) as error:
		require_workspace_file(tmp_path, str(outside))
	assert str(tmp_path.parent) not in str(error.value)
	assert "outside.py" in str(error.value)


def test_missing_dependent_is_unchecked(tmp_path: Path) -> None:
	from contextlib import asynccontextmanager

	from codenav_mcp.simulate import diagnostics_delta

	class Client:
		@asynccontextmanager
		async def overlay(self, texts: object):
			yield

		async def diagnostics(self, path: str) -> list:
			return []

	missing = tmp_path / "missing.py"
	delta = asyncio.run(diagnostics_delta(Client(), tmp_path, before={}, after={}, check=[missing]))
	assert delta.unchecked == ["missing.py"]
	assert delta.files_checked == 0


def test_verify_changes_reports_missing_historical_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from types import SimpleNamespace

	from mcp_nav_shared.diagnostics_delta import DiagnosticsDelta

	from codenav_mcp import write_tools

	path = tmp_path / "a.py"
	path.write_text("current")

	async def session(ctx: object) -> object:
		return SimpleNamespace(workspace=tmp_path, roots=[], client=None)

	def git(workspace: Path, *args: str) -> str:
		if args[0] == "rev-parse":
			return str(tmp_path)
		if args[0] == "diff":
			return "M\0a.py\0"
		return ""

	async def delta(*args: object, **kwargs: object) -> DiagnosticsDelta:
		assert path in kwargs["skip_before"]
		assert path in kwargs["skip_after"]
		return DiagnosticsDelta(files_checked=1)

	monkeypatch.setattr(write_tools, "open_session", session)
	monkeypatch.setattr(write_tools, "_git", git)
	monkeypatch.setattr(write_tools, "_has_commits", lambda *args: True)
	monkeypatch.setattr(write_tools, "_git_show", lambda *args: None)
	monkeypatch.setattr(write_tools, "files_to_check", lambda *args, **kwargs: ([path], 0))
	monkeypatch.setattr(write_tools, "diagnostics_delta", delta)
	result = asyncio.run(write_tools.verify_changes())
	assert "not checked: a.py" in result
	assert "0 file(s) checked" in result


def test_verify_added_module_does_not_claim_a_complete_comparison(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	import subprocess  # noqa: S404

	from codenav_mcp import server, write_tools
	from tests.live_support import TY, make_workspace

	if not TY.is_file():
		pytest.skip("repo .venv ty binary not available")
	make_workspace(tmp_path, monkeypatch, {"src/user.py": "from newmod import value\nx: int = value\n"})

	def git(*args: str) -> None:
		subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)  # noqa: S603,S607

	git("init", "-q")
	git("add", "-A")
	git("-c", "user.email=test@example.invalid", "-c", "user.name=test", "commit", "-qm", "baseline")
	new_module = tmp_path / "src/newmod.py"
	new_module.write_text('value: str = "incompatible"\n')

	async def run() -> str:
		try:
			return await write_tools.verify_changes()
		finally:
			if server._client is not None:
				await server._client.stop()

	result = asyncio.run(run())
	assert "Cannot verify changes" in result
	assert "src/newmod.py" in result
	assert "cannot hide added Python files" in result
	assert new_module.read_text() == 'value: str = "incompatible"\n'


def test_noop_still_checks_write_guard(tmp_path: Path) -> None:
	outside = tmp_path.parent / "outside-noop.py"
	plan = EditPlan("noop", [FileChange(outside, "same", "same")])
	with pytest.raises(ToolInputError):
		asyncio.run(
			finish_plan(None, workspace=tmp_path, roots=[], plan=plan, title="noop", apply=True, max_new_errors=0)
		)
