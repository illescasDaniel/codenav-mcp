"""QA block B11 regression tests: move semantics (live, real `ty` where the gate matters)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from codenav_mcp import deps, moves, refactor_tools, tidy

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()


def _run(loop, coro) -> str:
	return loop.run_until_complete(coro)


def _all_names(path: Path) -> list[str]:
	for node in ast.parse(path.read_text(encoding="utf-8")).body:
		if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
			return [str(item) for item in ast.literal_eval(node.value)]
	return []


def test_rc_002_excluded_dir_importer_is_reported(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/target.py": "def tgt() -> int:\n\treturn 1\n",
			"pkg/main.py": "from pkg.target import tgt\n\n\ndef go() -> int:\n\treturn tgt()\n",
			"pkg/build/__init__.py": "",
			"pkg/build/inner_user.py": "from pkg.target import tgt\n\n\ndef use() -> int:\n\treturn tgt()\n",
		},
	)
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="tgt", file_path="pkg/target.py", to_file="pkg/target2.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert "Importers not updated (excluded directories)" in text
	assert "CODENAV_MCP_INCLUDE_DIRS" in text
	assert "from pkg.target import tgt" in (root / "pkg/build/inner_user.py").read_text(encoding="utf-8")


def test_rc_004_import_cycle_is_refused_without_flag(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/source.py": "from pkg.dest import consumer\n\nLIMIT = 3\n\n\ndef moved() -> int:\n\treturn LIMIT\n\n\ndef other() -> int:\n\treturn consumer()\n",
			"pkg/dest.py": "def consumer() -> int:\n\treturn 1\n",
		},
	)
	before = (root / "pkg/source.py").read_text(encoding="utf-8")
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="moved", file_path="pkg/source.py", to_file="pkg/dest.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert "creates an import cycle" in text and "allow_import_cycle=true" in text
	assert (root / "pkg/source.py").read_text(encoding="utf-8") == before


def test_rc_004_import_cycle_moves_with_flag(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/source.py": "from pkg.dest import consumer\n\nLIMIT = 3\n\n\ndef moved() -> int:\n\treturn LIMIT\n\n\ndef other() -> int:\n\treturn consumer()\n",
			"pkg/dest.py": "def consumer() -> int:\n\treturn 1\n",
		},
	)
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="moved",
			file_path="pkg/source.py",
			to_file="pkg/dest.py",
			apply=True,
			max_new_errors=None,
			allow_import_cycle=True,
		),
	)
	# then
	assert "Applied as edit" in text
	assert "def moved" in (root / "pkg/dest.py").read_text(encoding="utf-8")


def test_rc_004_function_level_cycle_only_notes(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/source.py": (
				"LIMIT = 3\n\n\ndef moved() -> int:\n\treturn LIMIT\n\n\n"
				"def other() -> int:\n\tfrom pkg.dest import consumer\n\n\treturn consumer()\n"
			),
			"pkg/dest.py": "def consumer() -> int:\n\treturn 1\n",
		},
	)
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="moved", file_path="pkg/source.py", to_file="pkg/dest.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert "Applied as edit" in text
	assert "Note: the import cycle pkg.dest -> pkg.source -> pkg.dest goes only through function-level imports" in text
	assert "def moved" in (root / "pkg/dest.py").read_text(encoding="utf-8")


def test_rc_006_importer_cap_is_reported(tmp_path, monkeypatch, loop):
	# the real cap is 2000 importers; a small limit makes the same path reachable with a few files
	real = deps.find_dependents_report
	monkeypatch.setattr(
		moves,
		"find_dependents_report",
		lambda workspace, targets, roots: real(workspace, targets, roots, limit=5),
		raising=False,
	)
	files = {"pkg/__init__.py": "", "pkg/core.py": "def X() -> int:\n\treturn 1\n\n\ndef Y() -> int:\n\treturn 2\n"}
	for i in range(10):
		files[f"pkg/u{i:03d}.py"] = "from pkg.core import X\n\n\ndef f() -> int:\n\treturn X()\n"
	root = make_workspace(tmp_path, monkeypatch, files)
	# when the default refuses: the importers past the cap would be left broken
	held = _run(
		loop,
		refactor_tools._move_symbol(
			name="X", file_path="pkg/core.py", to_file="pkg/core2.py", apply=True, max_new_errors=None
		),
	)
	assert "NOT applied" in held and "(more than 5 importers)" in held
	assert "def X" in (root / "pkg/core.py").read_text(encoding="utf-8")
	# when the caller accepts it: the move applies and says what it left alone
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="X", file_path="pkg/core.py", to_file="pkg/core2.py", apply=True, max_new_errors=None, allow_large=True
		),
	)
	assert "Importers not updated (more than 5 importers): 5 files more." in text


def test_rc_007_bom_importer_is_rewritten(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"a.py": "def helper() -> int:\n\treturn 1\n",
			"b.py": "﻿from a import helper\n\n\ndef use() -> int:\n\treturn helper()\n",
		},
	)
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(name="helper", file_path="a.py", to_file="c.py", apply=True, max_new_errors=None),
	)
	# then
	assert "Applied as edit" in text
	b_text = (root / "b.py").read_text(encoding="utf-8")
	assert b_text.startswith("﻿") and "from c import helper" in b_text


def test_rc_012_inserted_lines_keep_crlf(tmp_path, monkeypatch, loop):
	root = make_workspace(tmp_path, monkeypatch, {"pkg/__init__.py": ""})
	(root / "pkg/source.py").write_bytes(
		b"def moved() -> int:\r\n\treturn 1\r\n\r\n\r\ndef keep() -> int:\r\n\treturn 2\r\n"
	)
	# when
	_run(
		loop,
		refactor_tools._move_symbol(
			name="moved", file_path="pkg/source.py", to_file="pkg/dest.py", apply=True, max_new_errors=None
		),
	)
	# then
	dest = (root / "pkg/dest.py").read_bytes()
	assert dest.count(b"\r\n") > 0 and b"\n" not in dest.replace(b"\r\n", b"")


def test_rc_026_moved_global_mutation_is_refused(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/source.py": "COUNT = 0\n\n\ndef bump() -> int:\n\tglobal COUNT\n\tCOUNT += 1\n\treturn COUNT\n",
		},
	)
	before = (root / "pkg/source.py").read_text(encoding="utf-8")
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="bump", file_path="pkg/source.py", to_file="pkg/bump.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert "bump assigns the module global COUNT of pkg.source" in text
	assert "NameError" in text
	assert (root / "pkg/source.py").read_text(encoding="utf-8") == before


def test_rc_027_bare_module_attribute_users_follow_the_move(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"a.py": "def helper() -> int:\n\treturn 1\n",
			"b_attr.py": "import a\n\n\ndef go() -> int:\n\treturn a.helper()\n",
		},
	)
	# when
	_run(loop, refactor_tools._move_module(from_file="a.py", to_file="a2.py", apply=True, max_new_errors=None))
	# then
	text = (root / "b_attr.py").read_text(encoding="utf-8")
	assert "import a2" in text and "a2.helper()" in text
	assert "a.helper()" not in text


def test_rc_042_type_checking_import_is_retargeted_not_broken(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/source.py": "def money() -> int:\n\treturn 1\n",
			"pkg/m.py": (
				"from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n\tfrom pkg.source import money\n\n\n"
				"def f(x: 'money') -> None:\n\treturn None\n"
			),
		},
	)
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="money", file_path="pkg/source.py", to_file="pkg/dest.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert "syntax error" not in text
	assert "if TYPE_CHECKING:\n\tfrom pkg.dest import money\n" in (root / "pkg/m.py").read_text(encoding="utf-8")


def test_rc_053_function_local_import_is_rewritten(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/a.py": "def helper() -> int:\n\treturn 1\n",
			"pkg/b_func.py": "from pkg.a import helper\n\n\ndef f() -> int:\n\tfrom pkg.a import helper as h\n\treturn h() + helper()\n",
		},
	)
	# when
	_run(
		loop,
		refactor_tools._move_symbol(
			name="helper", file_path="pkg/a.py", to_file="pkg/c.py", apply=True, max_new_errors=None
		),
	)
	# then
	text = (root / "pkg/b_func.py").read_text(encoding="utf-8")
	assert "from pkg.a import" not in text
	assert "from pkg.c import helper as h" in text


def test_rc_054_star_importer_is_listed(tmp_path, monkeypatch, loop):
	make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/models.py": "class Cart:\n\tpass\n",
			"pkg/star.py": "from pkg.models import *\n\nx = Cart()\n",
		},
	)
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="Cart", file_path="pkg/models.py", to_file="pkg/cart.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert 'Importers using "from pkg.models import *" (not rewritten, names resolve through the module)' in text
	assert "pkg/star.py" in text


def test_rc_055_non_python_or_unimportable_destination_refused(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{"pkg/__init__.py": "", "pkg/lonely.py": "def alone() -> int:\n\treturn 1\n"},
	)
	# when
	stub = _run(loop, refactor_tools._move_symbol(name="alone", file_path="pkg/lonely.py", to_file="pkg/stub.pyi"))
	bad = _run(loop, refactor_tools._move_symbol(name="alone", file_path="pkg/lonely.py", to_file="pkg/bad-name.py"))
	# then
	assert "destination must be a .py file" in stub
	assert "every part of its name must be a valid Python identifier" in bad
	assert not (root / "pkg/stub.pyi").exists() and not (root / "pkg/bad-name.py").exists()


def test_rc_067_future_annotations_travel_with_the_move(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/source.py": (
				"from __future__ import annotations\n\n\nclass Cart:\n\tlater: Later | None = None\n\n\n"
				"class Later:\n\tpass\n"
			),
		},
	)
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="Cart", file_path="pkg/source.py", to_file="pkg/cart.py", apply=True, max_new_errors=0
		),
	)
	# then
	assert "Applied as edit" in text
	assert "from __future__ import annotations" in (root / "pkg/cart.py").read_text(encoding="utf-8")


def test_rc_068_string_annotation_names_are_imported(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/models.py": "class Item:\n\tpass\n",
			"pkg/source.py": "from pkg.models import Item\n\n\ndef f(x: 'Item') -> 'list[Item]':\n\treturn [x]\n",
		},
	)
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="f", file_path="pkg/source.py", to_file="pkg/f_dest.py", apply=True, max_new_errors=0
		),
	)
	# then
	assert "Applied as edit" in text
	assert "from pkg.models import Item" in (root / "pkg/f_dest.py").read_text(encoding="utf-8")


def test_rc_069_all_entry_moves_with_the_symbol(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/source.py": "__all__ = ['Cart', 'keep']\n\n\nclass Cart:\n\tpass\n\n\ndef keep() -> int:\n\treturn 1\n",
			"pkg/dest.py": "__all__ = ['existing']\n\n\ndef existing() -> int:\n\treturn 2\n",
		},
	)
	# when
	_run(
		loop,
		refactor_tools._move_symbol(
			name="Cart", file_path="pkg/source.py", to_file="pkg/dest.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert _all_names(root / "pkg/source.py") == ["keep"]
	assert _all_names(root / "pkg/dest.py") == ["existing", "Cart"]


def test_rc_070_self_import_is_removed_from_moved_module(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{"selfimp/__init__.py": "", "selfimp/mod.py": "from selfimp import mod\n\nVALUE = 1\n"},
	)
	# when
	_run(
		loop,
		refactor_tools._move_module(
			from_file="selfimp/mod.py", to_file="selfimp/mod2.py", apply=True, max_new_errors=None
		),
	)
	# then
	moved = (root / "selfimp/mod2.py").read_text(encoding="utf-8")
	assert "import mod" not in moved and "VALUE = 1" in moved


def test_rc_071_unparsable_and_unreadable_importers_are_reported(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/a.py": "def helper() -> int:\n\treturn 1\n",
			"pkg/b_syntax.py": "from pkg.a import helper\n\n\ndef broken(:\n",
		},
	)
	(root / "pkg/b_latin1.py").write_bytes(b"from pkg.a import helper\n# caf\xe9\n")
	# when the default refuses: unreadable and unparsable importers hold the move back
	held = _run(
		loop,
		refactor_tools._move_symbol(
			name="helper", file_path="pkg/a.py", to_file="pkg/c.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert "NOT applied: importers that cannot be checked would be left broken" in held
	assert "b_syntax.py" in held and "b_latin1.py" in held
	assert "def helper" in (root / "pkg/a.py").read_text(encoding="utf-8")
	# when the caller accepts the skipped importers
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="helper", file_path="pkg/a.py", to_file="pkg/c.py", apply=True, max_new_errors=None, allow_large=True
		),
	)
	# then
	assert "Importers not updated (could not be parsed or read)" in text
	assert "b_syntax.py" in text and "b_latin1.py" in text


def test_rc_075_side_effect_import_stays_in_source(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/side.py": "import json  # registers\n\n\ndef doomed() -> str:\n\treturn json.dumps(1)\n",
		},
	)
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="doomed", file_path="pkg/side.py", to_file="pkg/target.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert "import json  # registers" in (root / "pkg/side.py").read_text(encoding="utf-8")
	assert "kept in pkg/side.py: side-effect import (json)" in text


def test_rc_098_new_destination_is_reported(tmp_path, monkeypatch, loop):
	make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/core.py": "NAME = 'x'\n\n\ndef mover() -> str:\n\treturn NAME\n\n\ndef stays() -> int:\n\treturn 1\n",
		},
	)
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="mover", file_path="pkg/core.py", to_file="pkg/dest.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert "pkg/dest.py was created by this move" in text


def test_rc_126_no_blank_lines_left_at_top_of_source(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/source.py": (
				"import os\n\n\n# about Cart\nclass Cart:\n\tdef where(self) -> str:\n\t\treturn os.sep\n\n\n"
				"def keep() -> int:\n\treturn 1\n"
			),
		},
	)
	# when
	_run(
		loop,
		refactor_tools._move_symbol(
			name="Cart", file_path="pkg/source.py", to_file="pkg/dest.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert (root / "pkg/source.py").read_text(encoding="utf-8").startswith("def keep() -> int:")


def test_rc_127_relative_importer_keeps_relative_form(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/a_in_pkg.py": "def thing() -> int:\n\treturn 1\n",
			"pkg/sub/__init__.py": "",
			"pkg/sub/rel2.py": "from ..a_in_pkg import thing\n\n\ndef use() -> int:\n\treturn thing()\n",
		},
	)
	# when
	_run(
		loop,
		refactor_tools._move_module(
			from_file="pkg/a_in_pkg.py", to_file="pkg/b_in_pkg.py", apply=True, max_new_errors=None
		),
	)
	# then
	text = (root / "pkg/sub/rel2.py").read_text(encoding="utf-8")
	assert "from ..b_in_pkg import thing" in text


def test_rc_159_nested_function_refused_with_cause(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/outer.py": "def outer() -> int:\n\tdef inner() -> int:\n\t\treturn 1\n\n\treturn inner()\n",
		},
	)
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="inner", file_path="pkg/outer.py", to_file="pkg/inner.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert "nested functions cannot be moved; move the enclosing top-level function or class" in text
	assert "def inner" in (root / "pkg/outer.py").read_text(encoding="utf-8")


def test_rc_159_def_under_if_refused_without_syntax_wording(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/typing_shim.py": (
				"from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n\tdef other_impl() -> int:\n\t\treturn 1\n"
				"else:\n\tdef final() -> int:\n\t\treturn 2\n"
			),
		},
	)
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="final", file_path="pkg/typing_shim.py", to_file="pkg/final_qa.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert "syntax error" not in text and "<source>" not in text
	assert "is inside an if/try/with block" in text
	assert "def final" in (root / "pkg/typing_shim.py").read_text(encoding="utf-8")


def test_rc_handoff_sort_failure_is_printed(tmp_path, monkeypatch, loop):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"pkg/__init__.py": "",
			"pkg/source.py": "import os\n\n\ndef keep() -> str:\n\treturn os.sep\n\n\ndef moved() -> int:\n\treturn 1\n",
		},
	)
	monkeypatch.setattr(tidy, "find_ruff", lambda workspace: None)
	# when
	text = _run(
		loop,
		refactor_tools._move_symbol(
			name="moved", file_path="pkg/source.py", to_file="pkg/dest.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert "pkg/source.py: imports were not sorted (ruff unavailable or failed)" in text
	assert "def moved" in (root / "pkg/dest.py").read_text(encoding="utf-8")
