"""The `edits` tool (list | apply | undo | show) against a real `ty`: previews and history across a restart."""

from __future__ import annotations

import re

import pytest
from mcp_nav_shared.edits import EditPlan, FileChange, TextEdit

from codenav_mcp import edit_state, edits_tools, write_tools, writes
from codenav_mcp.plan_sites import CodenavPlan, OptionalSite

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/core.py": "def compute(value: int) -> int:\n\treturn value * 2\n",
	"src/pkg/user.py": "from pkg.core import compute\n\n\ndef use() -> int:\n\treturn compute(1)\n",
	"tests/test_core.py": "def test_x():\n\tm = object()\n\tm.compute = 1  # fake\n",
}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(tmp_path, monkeypatch, _FILES)


def _run(loop, coro):
	return loop.run_until_complete(coro)


def _edits(loop, **kwargs) -> str:
	return str(_run(loop, edits_tools.edits(**kwargs)))


def _preview(loop) -> str:
	text = _run(loop, write_tools.rename_symbol(name="compute", new_name="calculate", apply=False))
	return re.search(r'apply_edit\(id="(\w+)"\)', text).group(1)


def _restart() -> None:
	"""A new server process: nothing in memory, only what is on disk."""
	writes.reset_state()


def test_given_no_state_when_list_then_it_says_so(project, loop):
	assert _edits(loop).startswith("No previews or applied edits")


def test_given_a_preview_when_the_server_restarts_then_list_shows_it_and_apply_writes_it(project, loop):
	preview_id = _preview(loop)
	_restart()

	listing = _edits(loop, action="list")
	applied = _edits(loop, action="apply", id=preview_id)

	assert "1 preview waiting, 0 applied edits" in listing
	assert f"{preview_id}  Rename compute" in listing
	assert f'next: edits(action="apply", id="{preview_id}")' in listing
	assert applied.startswith(f"Applied edit {preview_id}: Rename")
	assert "no errors in the edited files" in applied
	assert f'next: edits(action="undo", id="{preview_id}")' in applied
	assert "def calculate(" in (project / "src/pkg/core.py").read_text()
	assert "calculate(1)" in (project / "src/pkg/user.py").read_text()


def test_given_an_applied_edit_when_the_server_restarts_then_undo_reverts_it(project, loop):
	preview_id = _preview(loop)
	_edits(loop, action="apply", id=preview_id)
	_restart()

	listing = _edits(loop)
	undone = _edits(loop, action="undo")

	assert "0 previews waiting, 1 applied edit" in listing
	assert undone.startswith(f"Reverted edit {preview_id}")
	assert "def compute(" in (project / "src/pkg/core.py").read_text()
	assert _edits(loop).startswith("No previews or applied edits")


def test_given_a_file_changed_by_hand_after_apply_when_undo_then_it_refuses_and_names_the_file(project, loop):
	preview_id = _preview(loop)
	_edits(loop, action="apply", id=preview_id)
	user = project / "src/pkg/user.py"
	user.write_text(user.read_text() + "# my note\n")
	_restart()

	text = _edits(loop, action="undo", id=preview_id)

	assert "changed since edit" in text and "src/pkg/user.py" in text
	assert "Nothing was undone" in text
	assert "# my note" in user.read_text()
	assert "def calculate(" in (project / "src/pkg/core.py").read_text()  # nothing was reverted
	assert f'edits(action="show", id="{preview_id}")' in text
	assert "[changed since: src/pkg/user.py]" in _edits(loop)


def test_given_a_stale_preview_when_apply_then_it_refuses_with_the_exact_rerun_call(project, loop):
	token = edit_state.current_call.set('rename(name="compute", new_name="calculate", apply=false)')
	try:
		preview_id = _preview(loop)
	finally:
		edit_state.current_call.reset(token)
	core = project / "src/pkg/core.py"
	core.write_text(core.read_text() + "# hand edit\n")
	_restart()

	text = _edits(loop, action="apply", id=preview_id)

	assert "changed since the preview was made: src/pkg/core.py" in text
	assert "Nothing was written" in text
	assert 'rename(name="compute", new_name="calculate", apply=false)' in text
	assert "def compute(" in core.read_text()
	assert "STALE" in _edits(loop)


def test_given_an_unknown_id_then_the_error_points_at_list(project, loop):
	text = _edits(loop, action="apply", id="deadbeef")

	assert "unknown or expired preview id" in text
	assert 'edits(action="list")' in text


def test_given_no_id_then_apply_uses_the_only_preview_and_asks_when_there_are_several(project, loop):
	assert "no previews are waiting" in _edits(loop, action="apply")
	first = _preview(loop)
	second = _preview(loop)
	asked = _edits(loop, action="apply")
	assert "2 previews are waiting" in asked

	state = writes.write_state(project)
	state.plans.discard(second)
	assert _edits(loop, action="apply").startswith(f"Applied edit {first}")


def test_given_a_preview_when_show_then_the_diff_the_sites_and_the_next_call_are_listed(project, loop):
	state = writes.write_state(project)
	core = project / "src/pkg/core.py"
	old = core.read_text()
	plan = EditPlan("Demo edit", [FileChange(core, old, old.replace("value * 2", "value * 3"))])
	site = OptionalSite("u1", "tests/test_core.py", 3, "attr", "m.compute = 1  # fake", TextEdit(2, 3, 2, 10, "calc"))
	plan_id = state.plans.add(CodenavPlan(plan, [site]))

	text = _edits(loop, action="show", id=plan_id)

	assert text.startswith(f"Preview {plan_id} (not written): Demo edit - 1 file, 1 optional site")
	assert "-\treturn value * 2" in text and "+\treturn value * 3" in text
	assert "u1  tests/test_core.py:3  [attr]  m.compute = 1  # fake" in text
	assert f'next: edits(action="apply", id="{plan_id}", sites=["all"])' in text


def test_given_sites_when_apply_then_only_the_chosen_sites_are_written(project, loop):
	state = writes.write_state(project)
	core = project / "src/pkg/core.py"
	test_file = project / "tests/test_core.py"
	old = core.read_text()
	plan = EditPlan("Demo edit", [FileChange(core, old, old.replace("value * 2", "value * 3"))])
	sites = [
		OptionalSite("u1", "tests/test_core.py", 3, "attr", "m.compute = 1  # fake", TextEdit(2, 3, 2, 10, "calc")),
		OptionalSite("u2", "src/pkg/user.py", 5, "attr", "return compute(1)", TextEdit(4, 8, 4, 15, "compute")),
	]
	plan_id = state.plans.add(CodenavPlan(plan, sites))
	_restart()

	text = _edits(loop, action="apply", id=plan_id, sites=["u1"])

	assert text.startswith(f"Applied edit {plan_id}: Demo edit (+1 optional site)")
	assert "m.calc = 1  # fake" in test_file.read_text()
	assert "value * 3" in core.read_text()
	assert "1 optional site not applied: u2 (src/pkg/user.py:5)" in text


def test_given_all_sites_then_every_one_is_written_and_undo_reverts_them_too(project, loop):
	state = writes.write_state(project)
	test_file = project / "tests/test_core.py"
	before = test_file.read_text()
	core = project / "src/pkg/core.py"
	old = core.read_text()
	plan = EditPlan("Demo edit", [FileChange(core, old, old.replace("value * 2", "value * 3"))])
	site = OptionalSite("u1", "tests/test_core.py", 3, "attr", "m.compute = 1  # fake", TextEdit(2, 3, 2, 10, "calc"))
	plan_id = state.plans.add(CodenavPlan(plan, [site]))

	_edits(loop, action="apply", id=plan_id, sites=["all"])
	assert "m.calc" in test_file.read_text()
	_edits(loop, action="undo", id=plan_id)

	assert test_file.read_text() == before
	assert core.read_text() == old


def test_given_unknown_site_ids_or_a_plan_without_sites_then_nothing_is_written(project, loop):
	state = writes.write_state(project)
	core = project / "src/pkg/core.py"
	old = core.read_text()
	plan = EditPlan("Demo edit", [FileChange(core, old, old.replace("value * 2", "value * 3"))])
	site = OptionalSite("u1", "tests/test_core.py", 3, "attr", "m.compute = 1  # fake", TextEdit(2, 3, 2, 10, "calc"))
	with_sites = state.plans.add(CodenavPlan(plan, [site]))
	without = state.plans.add(plan)

	unknown = _edits(loop, action="apply", id=with_sites, sites=["u9"])
	none_there = _edits(loop, action="apply", id=without, sites=["u1"])

	assert "unknown site id(s) u9; preview" in unknown and "has: u1" in unknown
	assert "has no optional sites" in none_there
	assert core.read_text() == old


def test_given_a_site_whose_line_changed_then_apply_refuses_the_whole_plan(project, loop):
	state = writes.write_state(project)
	core = project / "src/pkg/core.py"
	old = core.read_text()
	plan = EditPlan("Demo edit", [FileChange(core, old, old.replace("value * 2", "value * 3"))])
	site = OptionalSite("u1", "tests/test_core.py", 3, "attr", "m.compute = 1  # fake", TextEdit(2, 3, 2, 10, "calc"))
	plan_id = state.plans.add(CodenavPlan(plan, [site]))
	(project / "tests/test_core.py").write_text("def test_x():\n\tm = object()\n\tm.other = 1\n")

	text = _edits(loop, action="apply", id=plan_id, sites=["u1"])

	assert "no longer matches the file" in text
	assert core.read_text() == old


def test_given_bad_arguments_then_the_error_has_a_corrected_example(project, loop):
	assert 'Example: edits(action="list")' in _edits(loop, action="remove")
	assert "only applies" in _edits(loop, action="undo", sites=["u1"])
	assert "only applies" in _edits(loop, action="list", allow_large=True)


def test_given_more_previews_than_the_limit_then_list_says_how_many_are_older(project, loop):
	state = writes.write_state(project)
	core = project / "src/pkg/core.py"
	old = core.read_text()
	for n in range(4):
		state.plans.add(EditPlan(f"Demo {n}", [FileChange(core, old, old.replace("* 2", f"* {n + 3}"))]))

	text = _edits(loop, action="list", limit=2)

	assert "... 2 older" in text
	assert 'next: edits(action="list", limit=4)' in text


def test_given_a_v1_apply_edit_and_undo_edit_then_they_share_the_persistent_state(project, loop):
	preview_id = _preview(loop)
	_restart()

	applied = _run(loop, write_tools.apply_edit(preview_id))
	_restart()
	undone = _run(loop, write_tools.undo_edit(preview_id))

	assert applied.startswith(f"Applied edit {preview_id}")
	assert undone.startswith(f"Reverted edit {preview_id}")
