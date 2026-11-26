"""QA block B31 regression tests (live, real `ty`): outside file_path guard, overloads, self-import use, lazy cycles."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from codenav_mcp import edit_tools, edits_tools, refactor_tools, write_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()


def _run(loop, coro) -> str:
	return loop.run_until_complete(coro)


@pytest.fixture
def twin(tmp_path, monkeypatch) -> tuple[Path, str]:
	"""A workspace holding `real.py`, and a same-named file in a sibling directory outside the workspace."""
	root = tmp_path / "ws"
	root.mkdir()
	outside = tmp_path / "out"
	outside.mkdir()
	(outside / "real.py").write_text("def real_fn(x: int) -> int:\n\treturn x\n")
	make_workspace(root, monkeypatch, {"real.py": "def real_fn(x: int) -> int:\n\treturn x\n"})
	return root, "../out/real.py"


def _untouched(root: Path) -> bool:
	return (root / "real.py").read_text() == "def real_fn(x: int) -> int:\n\treturn x\n"


def test_rc_092_edit_symbol_outside_file_path_is_refused(twin, loop):
	root, outside = twin
	for kwargs in ({"action": "replace", "source": "def real_fn(x: int) -> int:\n\treturn 2\n"}, {"action": "delete"}):
		text = _run(loop, edit_tools.edit_symbol(name="real_fn", file_path=outside, **cast(dict[str, Any], kwargs)))
		assert "../out/real.py is outside the workspace; give a path inside it" in text
		assert str(root) not in text
		assert "Applied" not in text
	assert _untouched(root)


def test_rc_092_change_signature_outside_file_path_is_refused(twin, loop):
	root, outside = twin
	text = _run(
		loop,
		refactor_tools.change_signature(name="real_fn", file_path=outside, add=[{"name": "flag", "default": "1"}]),
	)
	assert "is outside the workspace; give a path inside it" in text and "Applied" not in text
	assert _untouched(root)


def test_rc_092_absolute_outside_path_is_not_leaked_and_move_is_refused(twin, loop, tmp_path):
	root, _ = twin
	absolute = str(tmp_path / "out" / "real.py")
	text = _run(loop, refactor_tools._move_symbol(name="real_fn", file_path=absolute, to_file="other.py", apply=True))
	assert "<outside workspace>/real.py is outside the workspace" in text
	assert absolute not in text
	assert _untouched(root) and not (root / "other.py").exists()


def test_rc_092_in_workspace_file_path_still_works(twin, loop):
	root, _ = twin
	text = _run(
		loop,
		edit_tools.edit_symbol(
			action="replace", name="real_fn", file_path="real.py", source="def real_fn(x: int) -> int:\n\treturn 2\n"
		),
	)
	assert "Applied as edit" in text
	assert "return 2" in (root / "real.py").read_text()


_OVERLOADED = (
	"from typing import overload\n\n\n@overload\ndef ov(x: int) -> int: ...\n\n\n@overload\ndef ov(x: str) -> str: ...\n\n\n"
	"def ov(x):\n\treturn x\n"
)


def test_bug_sig_r01_overloaded_name_is_refused_naming_the_stubs(tmp_path, monkeypatch, loop):
	root = make_workspace(tmp_path, monkeypatch, {"lib.py": _OVERLOADED})
	text = _run(
		loop, refactor_tools.change_signature(name="ov", file_path="lib.py", add=[{"name": "y", "default": "0"}])
	)
	assert "2 @overload stub(s) (lib.py:5, lib.py:9)" in text
	assert "Applied" not in text
	assert (root / "lib.py").read_text() == _OVERLOADED


def test_bug_sig_r01_plain_function_and_method_still_change(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"lib.py": "def plain(x: int) -> int:\n\treturn x\n\n\nclass K:\n\tdef m(self, x: int) -> int:\n\t\treturn x\n"
		},
	)
	text = _run(
		loop, refactor_tools.change_signature(name="K.m", file_path="lib.py", add=[{"name": "y", "default": "0"}])
	)
	assert "Applied as edit" in text and "def m(self, x: int, *, y: int = 0)" in (root / "lib.py").read_text()


def test_bug_deps_r03_self_import_used_in_the_body_is_kept(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{"selfimp/__init__.py": "", "selfimp/mod.py": "from selfimp import mod\n\n\ndef f():\n\treturn mod\n"},
	)
	text = _run(
		loop,
		refactor_tools._move_module(
			from_file="selfimp/mod.py", to_file="selfimp/mod2.py", apply=True, max_new_errors=None
		),
	)
	moved = (root / "selfimp/mod2.py").read_text()
	assert "unresolved-reference" not in text
	assert "from selfimp import mod2" in moved and "return mod2" in moved
	run = subprocess.run(
		[sys.executable, "-c", "import selfimp.mod2 as m; assert m.f() is m"], cwd=root, capture_output=True, text=True
	)
	assert run.returncode == 0, run.stderr


def test_bug_deps_r04_longer_function_level_cycle_gets_a_note(tmp_path, monkeypatch, loop):
	make_workspace(
		tmp_path,
		monkeypatch,
		{
			"cyc/__init__.py": "",
			"cyc/c1.py": "from cyc.c2 import f2\n\n\ndef f1() -> int:\n\treturn 1\n\n\ndef g1() -> int:\n\treturn f2()\n",
			"cyc/c2.py": "def f2() -> int:\n\tfrom cyc.c3 import f3\n\n\treturn f3()\n",
			"cyc/c3.py": "def f3() -> int:\n\tfrom cyc.c1 import f1\n\n\treturn f1()\n",
		},
	)
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="f2", file_path="cyc/c2.py", to_file="cyc/c4.py", apply=True, max_new_errors=None
		),
	)
	assert "Applied as edit" in text
	assert (
		"Note: the import cycle cyc.c4 -> cyc.c3 -> cyc.c1 -> cyc.c4 goes only through function-level imports" in text
	)


def test_bug_deps_r04_no_cycle_no_note(tmp_path, monkeypatch, loop):
	make_workspace(
		tmp_path,
		monkeypatch,
		{
			"cyc/__init__.py": "",
			"cyc/c1.py": "from cyc.c2 import f2\n\n\ndef g1() -> int:\n\treturn f2()\n",
			"cyc/c2.py": "def f2() -> int:\n\treturn 1\n",
		},
	)
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="f2", file_path="cyc/c2.py", to_file="cyc/c4.py", apply=True, max_new_errors=None
		),
	)
	assert "Applied as edit" in text and "import cycle" not in text


def _plan_id(text: str) -> str:
	return text.split('edits(action="apply", id="', 1)[1].split('"', 1)[0]


def _rename_with_left_out_importer(tmp_path, monkeypatch, loop, extra: dict[str, str]):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/a.py": "def helper() -> int:\n\treturn 1\n",
			"pkg/b.py": "from pkg.a import helper\n\nprint(helper())\n",
			**extra,
		},
	)
	text = _run(loop, write_tools.rename(name="helper", file_path="pkg/a.py", new_name="assist"))
	return root, text


def test_bug_deps_r06_rename_holds_back_for_a_nested_repo_importer_and_lists_it(tmp_path, monkeypatch, loop):
	root, text = _rename_with_left_out_importer(
		tmp_path,
		monkeypatch,
		loop,
		{
			"third_party/lib/.git": "gitdir: x",
			"third_party/lib/inner.py": "from pkg.a import helper\n\nprint(helper())\n",
		},
	)
	# held back: nothing written, the importer is listed, and the plan is kept
	assert "NOT applied: importers that cannot be rewritten would be left broken (third_party/lib/ (1 file))" in text
	assert (
		"Importers not updated (nested git repositories, treated as other projects): third_party/lib/ (1 file)" in text
	)
	assert "def helper" in (root / "pkg/a.py").read_text() and "helper" in (root / "pkg/b.py").read_text()
	# the kept plan writes the rename and leaves the nested repository's file alone
	applied = _run(loop, edits_tools.edits(id=_plan_id(text), action="apply"))
	assert "Applied edit" in applied
	assert "def assist" in (root / "pkg/a.py").read_text() and "assist" in (root / "pkg/b.py").read_text()
	assert "helper" in (root / "third_party/lib/inner.py").read_text()


def test_bug_deps_r06_rename_holds_back_for_an_excluded_dir_importer_and_allow_large_renames(
	tmp_path, monkeypatch, loop
):
	root, text = _rename_with_left_out_importer(
		tmp_path, monkeypatch, loop, {"build/z.py": "from pkg.a import helper\n\nprint(helper())\n"}
	)
	assert "NOT applied: importers that cannot be rewritten would be left broken (build/ (1 file))" in text
	assert "Importers not updated (excluded directories): build/ (1 file)" in text
	assert "def helper" in (root / "pkg/a.py").read_text()
	# a preview says so too
	preview = _run(loop, write_tools.rename(name="helper", file_path="pkg/a.py", new_name="assist", apply=False))
	assert "apply=true would be refused: importers that cannot be rewritten" in preview
	# The held preview remains explicitly applicable.
	applied = _run(loop, edits_tools.edits(action="apply", id=_plan_id(preview), allow_large=True))
	assert "Applied edit" in applied
	assert "helper" in (root / "build/z.py").read_text()


def test_bug_deps_r10_move_into_a_new_package_directory_is_checked_against_the_tree_on_disk(
	tmp_path, monkeypatch, loop
):
	# the destination directory has no `__init__.py` (a namespace package) and does not exist before the move
	files = {"core.py": "X = 1\nY = 2\n"}
	files.update({f"imp/u{i:03d}.py": f"from core import X, Y\n\nv{i} = X, Y\n" for i in range(5)})
	make_workspace(tmp_path, monkeypatch, files)
	text = _run(
		loop,
		refactor_tools._move_module(
			from_file="core.py", to_file="pkg/core.py", apply=True, max_new_errors=None, allow_large=True
		),
	)
	assert "Applied as edit" in text
	assert "Cannot resolve imported module" not in text
	assert "After writing: no errors in the edited files" in text
