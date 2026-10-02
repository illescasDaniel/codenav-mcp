"""Live checks of the write tools (rename, check_edit, verify_changes, apply/undo) against a real `ty`."""

from __future__ import annotations

import subprocess  # noqa: S404

import pytest

from codenav_mcp import write_tools, writes

from .live_support import REPO_ROOT, make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/core.py": (
		"def compute(value: int, scale: int = 2) -> int:\n\treturn value * scale\n\n\n"
		"class Base:\n\tdef run(self) -> int:\n\t\treturn 1\n\n\n"
		"class Child(Base):\n\tdef run(self) -> int:\n\t\treturn super().run() + 1\n"
	),
	"src/pkg/user.py": (
		"from pkg.core import Base, Child, compute\n\n\n"
		"def use(base: Base) -> int:\n\treturn compute(1, scale=3) + base.run() + Child().run()\n"
	),
	"src/pkg/port.py": "from typing import Protocol\n\n\nclass StorePort(Protocol):\n\tdef save(self, key: str) -> None: ...\n",
	"src/pkg/adapter.py": "class DiskStore:\n\tdef save(self, key: str) -> None:\n\t\tpass\n",
	"src/pkg/app.py": (
		"from pkg.port import StorePort\n\n\ndef persist(store: StorePort) -> None:\n\tstore.save('k')\n"
	),
	"tests/__init__.py": "",
	"tests/test_core.py": "from pkg.core import compute\n\n\ndef test_compute():\n\tassert compute(2) == 4\n",
	"README.md": "Call `compute` to scale values.\n",
}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(tmp_path, monkeypatch, _FILES)


def _run(loop, coro):
	return loop.run_until_complete(coro)


def test_given_function_when_rename_preview_then_nothing_written_and_diff_shown(project, loop):
	# when
	text = _run(loop, write_tools.rename_symbol(name="compute", new_name="calculate"))
	# then
	assert "Preview only, nothing written" in text
	assert "0 new error(s)" in text
	assert "+def calculate(" in text
	assert (project / "src/pkg/core.py").read_text().startswith("def compute(")
	assert "appears in non-Python files" in text  # README mention
	assert "README.md:1" in text


def test_given_preview_when_apply_edit_then_all_files_written_and_undo_restores(project, loop):
	# given
	preview = _run(loop, write_tools.rename_symbol(name="compute", new_name="calculate"))
	edit_id = preview.split('apply_edit(id="')[1].split('"')[0]
	originals = {
		p: p.read_text()
		for p in (project / "src/pkg/core.py", project / "src/pkg/user.py", project / "tests/test_core.py")
	}
	# when
	applied = _run(loop, write_tools.apply_edit(edit_id))
	# then
	assert "Applied edit" in applied and "no errors in the edited files" in applied
	assert "calculate(1, scale=3)" in (project / "src/pkg/user.py").read_text()
	assert "from pkg.core import calculate" in (project / "tests/test_core.py").read_text()
	# when
	reverted = _run(loop, write_tools.undo_edit())
	# then
	assert "Reverted edit" in reverted
	assert {p: p.read_text() for p in originals} == originals


def test_given_apply_true_when_rename_clean_then_written_in_one_call(project, loop):
	# when
	text = _run(loop, write_tools.rename_symbol(name="compute", new_name="calculate", apply=True))
	# then
	assert text.splitlines()[2].startswith("Applied as edit")
	assert "def calculate(" in (project / "src/pkg/core.py").read_text()


def test_given_override_when_rename_base_method_then_override_and_super_call_follow(project, loop):
	# when
	text = _run(loop, write_tools.rename_symbol(name="Base.run", new_name="execute", apply=True))
	# then
	core = (project / "src/pkg/core.py").read_text()
	assert "def execute(self)" in core and "def run(" not in core
	assert "super().execute() + 1" in core
	assert "Child.run renamed too" in text
	assert "base.execute() + Child().execute()" in (project / "src/pkg/user.py").read_text()
	assert "0 new error(s)" in text


def test_given_linked_false_when_rename_base_method_then_override_left_and_error_reported(project, loop):
	# when
	text = _run(loop, write_tools.rename_symbol(name="Base.run", new_name="execute", linked=False))
	# then — the override is left alone, and ty's check shows what that breaks (super().run())
	assert "Child.run renamed too" not in text
	assert "1 new error(s)" in text or "2 new error(s)" in text


def test_given_protocol_member_when_rename_then_implementer_renamed_too(project, loop):
	# when
	text = _run(loop, write_tools.rename_symbol(name="StorePort.save", new_name="persist_key", apply=True))
	# then
	assert "DiskStore.save renamed too" in text
	assert "def persist_key(self, key: str)" in (project / "src/pkg/adapter.py").read_text()
	assert "store.persist_key('k')" in (project / "src/pkg/app.py").read_text()
	assert "0 new error(s)" in text


def test_given_parameter_when_rename_then_keyword_arguments_follow(project, loop):
	# when
	text = _run(loop, write_tools.rename_symbol(name="compute", parameter="scale", new_name="factor", apply=True))
	# then
	assert "def compute(value: int, factor: int = 2)" in (project / "src/pkg/core.py").read_text()
	assert "compute(1, factor=3)" in (project / "src/pkg/user.py").read_text()
	assert "0 new error(s)" in text


def test_given_existing_name_when_rename_then_refused_before_any_edit(project, loop):
	# when
	text = _run(loop, write_tools.rename_symbol(name="Child.run", new_name="__init__"))
	collision = _run(loop, write_tools.rename_symbol(name="compute", parameter="scale", new_name="value"))
	invalid = _run(loop, write_tools.rename_symbol(name="compute", new_name="not valid"))
	keyword = _run(loop, write_tools.rename_symbol(name="compute", new_name="class"))
	# then
	assert "already has a parameter named 'value'" in collision
	assert "not a valid Python identifier" in invalid and "not a valid Python identifier" in keyword
	assert "Preview" in text or "error" in text.lower()  # __init__ is legal; just must not crash


def test_given_builtin_when_rename_then_explained(project, loop):
	# given — position on `int` in the signature
	text = _run(loop, write_tools.rename_symbol(file_path="src/pkg/core.py", line=1, column=23, new_name="integer"))
	# then
	assert "cannot be renamed" in text


def test_given_file_changed_after_preview_when_apply_edit_then_stale_refusal(project, loop):
	# given
	preview = _run(loop, write_tools.rename_symbol(name="compute", new_name="calculate"))
	edit_id = preview.split('apply_edit(id="')[1].split('"')[0]
	(project / "src/pkg/user.py").write_text("# somebody else edited this\n", encoding="utf-8")
	# when
	text = _run(loop, write_tools.apply_edit(edit_id))
	# then
	assert "changed since the preview was made: src/pkg/user.py" in text
	assert "def compute(" in (project / "src/pkg/core.py").read_text()


def test_given_unknown_id_when_apply_edit_then_helpful_error(project, loop):
	assert "unknown or expired preview id" in _run(loop, write_tools.apply_edit("deadbeef"))


def test_given_read_only_env_when_apply_then_refused_but_preview_works(project, loop, monkeypatch):
	# given
	monkeypatch.setenv(writes.READ_ONLY_ENV, "1")
	# when
	preview = _run(loop, write_tools.rename_symbol(name="compute", new_name="calculate"))
	applied = _run(loop, write_tools.rename_symbol(name="compute", new_name="calculate", apply=True))
	# then
	assert "Preview only" in preview
	assert "write tools are disabled" in applied
	assert "def compute(" in (project / "src/pkg/core.py").read_text()


def test_given_breaking_edit_when_check_edit_then_new_errors_in_importers_reported(project, loop):
	# when — drop a parameter that user.py and the tests still pass
	text = _run(
		loop,
		write_tools.check_edit(
			"src/pkg/core.py",
			old_string="def compute(value: int, scale: int = 2)",
			new_string="def compute(value: int)",
		),
	)
	# then
	assert "new error(s)" in text and "0 new error(s)" not in text
	assert "src/pkg/user.py" in text
	assert "Preview only" in text
	assert "scale: int = 2" in (project / "src/pkg/core.py").read_text()


def test_given_clean_edit_when_check_edit_apply_then_written_and_undoable(project, loop):
	# when
	text = _run(
		loop,
		write_tools.check_edit(
			"src/pkg/core.py",
			old_string="return value * scale",
			new_string="return scale * value",
			apply=True,
			max_new_errors=0,
		),
	)
	# then
	assert "Applied as edit" in text
	assert "return scale * value" in (project / "src/pkg/core.py").read_text()
	_run(loop, write_tools.undo_edit())
	assert "return value * scale" in (project / "src/pkg/core.py").read_text()


def test_given_error_gate_when_check_edit_apply_then_blocked(project, loop):
	# when
	text = _run(
		loop,
		write_tools.check_edit(
			"src/pkg/core.py",
			old_string="def compute(value: int, scale: int = 2)",
			new_string="def compute(value: int)",
			apply=True,
			max_new_errors=0,
		),
	)
	# then
	assert "NOT applied" in text
	assert "scale: int = 2" in (project / "src/pkg/core.py").read_text()


def test_given_invalid_python_when_check_edit_then_rejected_without_simulation(project, loop):
	# when
	text = _run(loop, write_tools.check_edit("src/pkg/core.py", new_text="def broken(:\n"))
	# then
	assert "syntax error at line 1" in text


def test_given_ambiguous_old_string_when_check_edit_then_asks_for_unique_match(project, loop):
	assert "matches 3 times" in _run(
		loop, write_tools.check_edit("src/pkg/core.py", old_string="return", new_string="return")
	)
	assert "was not found" in _run(loop, write_tools.check_edit("src/pkg/core.py", old_string="zzz", new_string="y"))


def test_given_new_file_when_check_edit_apply_then_created(project, loop):
	# when
	text = _run(loop, write_tools.check_edit("src/pkg/fresh.py", new_text="X = 1\n", apply=True))
	# then
	assert "(new file)" in text
	assert (project / "src/pkg/fresh.py").read_text() == "X = 1\n"
	_run(loop, write_tools.undo_edit())
	assert not (project / "src/pkg/fresh.py").exists()


def test_given_non_python_file_when_check_edit_then_rejected(project, loop):
	assert "only edits Python files" in _run(loop, write_tools.check_edit("README.md", new_text="x"))


def _git(root, *args):
	subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)  # noqa: S603,S607


def test_given_working_tree_changes_when_verify_changes_then_reports_errors_introduced_since_head(project, loop):
	# given — a committed clean tree, then an edit made outside the tools that breaks an importer
	_git(project, "init", "-q")
	_git(project, "-c", "user.email=a@b.c", "-c", "user.name=t", "add", "-A")
	_git(project, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init")
	core = project / "src/pkg/core.py"
	core.write_text(
		core.read_text().replace("def compute(value: int, scale: int = 2)", "def compute(value: int)"), encoding="utf-8"
	)
	# when
	text = _run(loop, write_tools.verify_changes())
	# then
	assert "Changed since HEAD: src/pkg/core.py" in text
	assert "new error(s)" in text and "0 new error(s)" not in text
	assert "src/pkg/user.py" in text


def test_given_clean_tree_when_verify_changes_then_no_differences(project, loop):
	# given
	_git(project, "init", "-q")
	_git(project, "-c", "user.email=a@b.c", "-c", "user.name=t", "add", "-A")
	_git(project, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init")
	# when / then
	assert "No Python files differ from HEAD" in _run(loop, write_tools.verify_changes())
	assert "invalid revision" in _run(loop, write_tools.verify_changes(since="--output=x"))


def test_given_repo_root_marker():
	assert REPO_ROOT.name
