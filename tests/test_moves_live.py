"""Live checks of move_symbol and move_module against a real `ty`."""

from __future__ import annotations

import ast

import pytest

from codenav_mcp import refactor_tools, write_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/dates.py": (
		'"""Date helpers."""\n\n'
		"import re\nimport os\nfrom datetime import date\nfrom pkg.consts import FORMAT\n\n\n"
		"SEPARATOR = '-'\n\n\n"
		"# Parse a date.\n"
		"def parse_date(text: str) -> date:\n"
		"\tif not re.match(FORMAT, text):\n\t\traise ValueError(text)\n"
		"\tyear, month, day = text.split(SEPARATOR)\n"
		"\treturn date(int(year), int(month), int(day))\n\n\n"
		"def today_path() -> str:\n\treturn os.getcwd() + SEPARATOR\n\n\n"
		"def render(value: date) -> str:\n\treturn parse_date(value.isoformat()).isoformat()\n"
	),
	"src/pkg/consts.py": "FORMAT = r'\\d{4}-\\d{2}-\\d{2}'\n",
	"src/pkg/uses.py": "from pkg.dates import parse_date, render\n\n\ndef go() -> str:\n\treturn render(parse_date('2020-01-02'))\n",
	"src/pkg/uses_mod.py": "from pkg import dates\n\n\ndef go() -> str:\n\treturn dates.today_path()\n",
	"tests/__init__.py": "",
	"tests/test_dates.py": "from pkg.dates import parse_date\n\n\ndef test_it():\n\tassert parse_date('2020-01-02').year == 2020\n",
}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(tmp_path, monkeypatch, _FILES)


def _run(loop, coro):
	return loop.run_until_complete(coro)


def _valid(root) -> None:
	for path in root.rglob("*.py"):
		ast.parse(path.read_text())


def test_given_function_when_move_symbol_then_code_imports_and_importers_follow(project, loop):
	# when
	text = _run(
		loop, refactor_tools.move_symbol(name="parse_date", to_file="src/pkg/parsing.py", apply=True, max_new_errors=0)
	)
	# then
	_valid(project)
	parsing = (project / "src/pkg/parsing.py").read_text()
	assert parsing.startswith("import re\nfrom datetime import date\n") or "import re" in parsing
	assert "from pkg.consts import FORMAT" in parsing
	assert "from pkg.dates import SEPARATOR" in parsing
	assert "# Parse a date.\ndef parse_date(text: str) -> date:" in parsing
	dates = (project / "src/pkg/dates.py").read_text()
	assert "def parse_date" not in dates
	assert "from pkg.parsing import parse_date" in dates  # render() still uses it
	assert "import re" not in dates and "FORMAT" not in dates  # only the moved code used them
	assert "import os" in dates and "from datetime import date" in dates
	assert "from pkg.parsing import parse_date" in (project / "src/pkg/uses.py").read_text()
	assert "from pkg.dates import render" in (project / "src/pkg/uses.py").read_text()
	assert "from pkg.parsing import parse_date" in (project / "tests/test_dates.py").read_text()
	assert "Applied as edit" in text and "0 new error(s)" in text
	assert "circular import" in text  # parsing imports SEPARATOR from dates, dates imports parse_date back


def test_given_move_symbol_when_undo_then_new_file_removed_and_originals_back(project, loop):
	# given
	originals = {p: p.read_text() for p in project.rglob("*.py")}
	_run(
		loop,
		refactor_tools.move_symbol(name="parse_date", to_file="src/pkg/parsing.py", apply=True, max_new_errors=None),
	)
	# when
	_run(loop, write_tools.undo_edit())
	# then
	assert {p: p.read_text() for p in project.rglob("*.py")} == originals


def test_given_symbol_unused_in_old_file_when_move_then_no_import_back_unless_reexport(project, loop):
	# when
	_run(
		loop, refactor_tools.move_symbol(name="today_path", to_file="src/pkg/paths.py", apply=True, max_new_errors=None)
	)
	# then
	dates = (project / "src/pkg/dates.py").read_text()
	assert "today_path" not in dates and "import os" not in dates
	assert (
		"dates.today_path()" in (project / "src/pkg/uses_mod.py").read_text()
	)  # module-attribute use: reported, not rewritten


def test_given_attribute_user_when_move_symbol_then_listed_for_manual_update(project, loop):
	# when
	text = _run(loop, refactor_tools.move_symbol(name="today_path", to_file="src/pkg/paths.py"))
	# then
	assert "accessed through the old module" in text and "uses_mod.py:5" in text
	assert "new error(s)" in text and "0 new error(s)" not in text  # and ty confirms it breaks


def test_given_reexport_flag_when_move_symbol_then_old_import_kept(project, loop):
	# when
	_run(
		loop,
		refactor_tools.move_symbol(
			name="today_path", to_file="src/pkg/paths.py", keep_reexport=True, apply=True, max_new_errors=None
		),
	)
	# then
	assert "from pkg.paths import today_path" in (project / "src/pkg/dates.py").read_text()


def test_given_existing_destination_name_or_nested_symbol_when_move_then_refused(project, loop):
	# given
	(project / "src/pkg/taken.py").write_text("def parse_date(): ...\n", encoding="utf-8")
	(project / "src/pkg/nested.py").write_text("class A:\n\tdef m(self): ...\n", encoding="utf-8")
	# when / then
	assert "already defines 'parse_date'" in _run(
		loop, refactor_tools.move_symbol(name="parse_date", to_file="src/pkg/taken.py", file_path="src/pkg/dates.py")
	)
	assert "only module-level symbols" in _run(loop, refactor_tools.move_symbol(name="A.m", to_file="src/pkg/other.py"))
	assert "already in" in _run(
		loop, refactor_tools.move_symbol(name="parse_date", to_file="src/pkg/dates.py", file_path="src/pkg/dates.py")
	)
	assert "must be a .py file" in _run(
		loop, refactor_tools.move_symbol(name="parse_date", to_file="notes.md", file_path="src/pkg/dates.py")
	)


def test_given_existing_destination_when_move_symbol_then_appended_and_merged_imports(project, loop):
	# given
	(project / "src/pkg/parsing.py").write_text(
		"import re\n\n\ndef other() -> bool:\n\treturn bool(re.compile('x'))\n", encoding="utf-8"
	)
	# when
	_run(
		loop,
		refactor_tools.move_symbol(name="parse_date", to_file="src/pkg/parsing.py", apply=True, max_new_errors=None),
	)
	# then
	parsing = (project / "src/pkg/parsing.py").read_text()
	_valid(project)
	assert parsing.count("import re") == 1
	assert parsing.index("def other") < parsing.index("def parse_date")


def test_given_module_when_move_module_then_all_import_styles_rewritten(project, loop):
	# when
	text = _run(
		loop,
		refactor_tools.move_module(
			from_file="src/pkg/dates.py", to_file="src/pkg/time/calendar.py", apply=True, max_new_errors=None
		),
	)
	# then
	_valid(project)
	assert not (project / "src/pkg/dates.py").exists()
	assert (project / "src/pkg/time/calendar.py").read_text().startswith('"""Date helpers."""')
	assert "from pkg.time.calendar import parse_date, render" in (project / "src/pkg/uses.py").read_text()
	assert "from pkg.time.calendar import parse_date" in (project / "tests/test_dates.py").read_text()
	uses_mod = (project / "src/pkg/uses_mod.py").read_text()
	assert uses_mod == "from pkg.time import calendar\n\n\ndef go() -> str:\n\treturn calendar.today_path()\n"
	assert "Applied as edit" in text


def test_given_in_place_rename_when_move_module_then_from_package_imports_and_uses_follow(project, loop):
	# when
	_run(
		loop,
		refactor_tools.move_module(
			from_file="src/pkg/dates.py", to_file="src/pkg/calendar_utils.py", apply=True, max_new_errors=None
		),
	)
	# then
	uses_mod = (project / "src/pkg/uses_mod.py").read_text()
	assert uses_mod == "from pkg import calendar_utils\n\n\ndef go() -> str:\n\treturn calendar_utils.today_path()\n"


def test_given_relative_imports_when_move_module_then_made_absolute_on_both_sides(project, loop):
	# given
	(project / "src/pkg/rel_user.py").write_text(
		"from .dates import render\n\n\ndef r() -> str:\n\treturn render\n", encoding="utf-8"
	)
	(project / "src/pkg/rel_dep.py").write_text("from .consts import FORMAT\n\nX = FORMAT\n", encoding="utf-8")
	# when
	_run(
		loop,
		refactor_tools.move_module(
			from_file="src/pkg/rel_dep.py", to_file="src/pkg/sub/rel_dep.py", apply=True, max_new_errors=None
		),
	)
	_run(
		loop,
		refactor_tools.move_module(
			from_file="src/pkg/dates.py", to_file="src/pkg/sub/dates.py", apply=True, max_new_errors=None
		),
	)
	# then
	assert (project / "src/pkg/sub/rel_dep.py").read_text().startswith("from pkg.consts import FORMAT")
	assert "from pkg.sub.dates import render" in (project / "src/pkg/rel_user.py").read_text()
	_valid(project)


def test_given_bad_module_moves_when_requested_then_refused(project, loop):
	assert "already exists" in _run(
		loop, refactor_tools.move_module(from_file="src/pkg/dates.py", to_file="src/pkg/uses.py")
	)
	assert "not an existing .py file" in _run(
		loop, refactor_tools.move_module(from_file="src/pkg/nope.py", to_file="src/pkg/x.py")
	)
	assert "__init__.py is not supported" in _run(
		loop, refactor_tools.move_module(from_file="src/pkg/__init__.py", to_file="src/pkg/y.py")
	)


def test_given_move_module_preview_when_undo_after_apply_then_tree_restored(project, loop):
	# given
	originals = {p: p.read_text() for p in project.rglob("*.py")}
	preview = _run(loop, refactor_tools.move_module(from_file="src/pkg/dates.py", to_file="src/pkg/renamed.py"))
	assert "Preview only" in preview and (project / "src/pkg/dates.py").exists()
	# when
	_run(
		loop,
		refactor_tools.move_module(
			from_file="src/pkg/dates.py", to_file="src/pkg/renamed.py", apply=True, max_new_errors=None
		),
	)
	_run(loop, write_tools.undo_edit())
	# then
	assert {p: p.read_text() for p in project.rglob("*.py")} == originals
