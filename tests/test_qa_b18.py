"""B18 (QA campaign, wave 3 leftovers): dotted and NFKC search, diagnostics scrubbing, quick-fix import sorting,
line splitting, implementations refusals and candidate skips, excluded importers reported, local-name hints."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from mcp_nav_shared.edits import FileChange

from codenav_mcp import server as codenav_server, symbol_tools, tidy, write_tools
from codenav_mcp.deps import find_dependents_report, format_skipped

from .live_support import make_loop_fixture, make_workspace, needs_ty


loop = make_loop_fixture()


def _text(result) -> str:
	"""Tool results are plain strings on success and a FailedText (or CallToolResult) on refusal."""
	return result if isinstance(result, str) else result.content[0].text


def _is_error(result) -> bool:
	return bool(getattr(result, "is_error", False))


def _run(loop, coro) -> str:
	return _text(loop.run_until_complete(coro))


# -- RC-086: dotted search -------------------------------------------------------------------------------------------

_DOTTED_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/cart.py": "class Cart:\n\tdef total(self) -> int:\n\t\treturn 0\n",
	"src/pkg/other.py": "class Other:\n\tdef total(self) -> int:\n\t\treturn 1\n",
}


@pytest.fixture
def dotted_project(tmp_path, monkeypatch, loop):
	return make_workspace(tmp_path, monkeypatch, _DOTTED_FILES)


@needs_ty
def test_rc_086_dotted_search_finds_the_member_of_that_class(dotted_project, loop):
	text = _run(loop, codenav_server.search_symbol(query="Cart.total"))
	assert "total  [Method]" in text
	assert "cart.py:2:" in text
	assert "other.py" not in text


@needs_ty
def test_rc_086_dotted_search_with_no_such_owner_finds_nothing(dotted_project, loop):
	assert _run(loop, codenav_server.search_symbol(query="Missing.total")) == "No symbols matching 'Missing.total'."


# -- RC-090: NFKC-normalised search ----------------------------------------------------------------------------------


@pytest.fixture
def nfkc_project(tmp_path, monkeypatch, loop):
	return make_workspace(tmp_path, monkeypatch, {"src/pkg/nfkc.py": "ﬁ = 1\nvalue = fi\n"})


@needs_ty
def test_rc_090_search_symbol_finds_the_nfkc_equal_name(nfkc_project, loop):
	text = _run(loop, codenav_server.search_symbol(query="ﬁ"))
	assert "fi  [Variable]" in text
	assert "nfkc.py" in text


# -- RC-039: scrubbing diagnostics text -------------------------------------------------------------------------------


def test_rc_039_scrub_drops_the_bare_workspace_root_and_library_install_paths(tmp_path, monkeypatch):
	monkeypatch.setattr(codenav_server, "WORKSPACE_ROOT", tmp_path)
	text = (
		f"info:   1. {tmp_path} (first-party code)\n"
		f"info:   2. {tmp_path}/src (first-party code)\n"
		"info:   3. /usr/lib/python3.14/site-packages (site-packages)\n"
		"info:   4. /opt/extra/src (extra search path specified on the CLI or in your config file)"
	)
	scrubbed = codenav_server._scrub_text(text)
	assert str(tmp_path) not in scrubbed
	assert "<workspace root> (first-party code)" in scrubbed
	assert "src (first-party code)" in scrubbed
	assert "/usr/lib" not in scrubbed
	assert "/opt/extra" not in scrubbed
	assert "<outside workspace> (extra search path" in scrubbed


@needs_ty
def test_rc_039_diagnostics_output_has_no_absolute_paths(tmp_path, monkeypatch, loop):
	project = make_workspace(tmp_path, monkeypatch, {"needs_late.py": "import late_module\n"})
	text = _run(loop, codenav_server.diagnostics(file_path="needs_late.py"))
	assert "unresolved-import" in text
	assert str(project) not in text
	assert "/usr/lib" not in text


# -- RC-138: quick-fix merged imports are sorted -----------------------------------------------------------------------


@needs_ty
def test_rc_138_quick_fix_sorts_the_merged_import(tmp_path, monkeypatch, loop):
	project = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"models.py": "class Cart:\n\tpass\n\n\nclass Item:\n\tpass\n",
			"uses.py": (
				"from models import Item  # keep this comment\n\n\ndef a() -> Item:\n\treturn Item()\n\n\n"
				"def b():\n\treturn Cart()\n"
			),
		},
	)
	result = loop.run_until_complete(symbol_tools.quick_fix("uses.py"))
	assert "Applied as edit" in result
	assert "from models import Cart, Item  # keep this comment" in (project / "uses.py").read_text()


def test_rc_138_a_sort_that_cannot_run_is_named(tmp_path, monkeypatch):
	monkeypatch.setattr(tidy, "find_ruff", lambda _workspace: None)
	notes: list[str] = []
	original = "from models import Cart\nfrom models import Item\n"
	edited = "from models import Item, Cart\n"
	change = FileChange(tmp_path / "uses.py", original, edited)
	result = symbol_tools._with_sorted_imports(tmp_path, change, notes)
	assert result.new_text == edited
	assert notes == ["uses.py: imports were not sorted (ruff unavailable or failed)"]


# -- splitlines sweep (RC-001): form feeds are ordinary characters in source -----------------------------------------


def test_rc_001_line_text_ignores_form_feed_and_line_separators(tmp_path):
	path = tmp_path / "ff.py"
	path.write_text("a = 1\x0cb = 2\nc = 3\n", encoding="utf-8")
	assert symbol_tools._line_of(path, 2) == "c = 3"


def test_rc_001_definition_keyword_check_ignores_form_feed(tmp_path, monkeypatch):
	monkeypatch.setattr(codenav_server, "WORKSPACE_ROOT", tmp_path)
	(tmp_path / "a.py").write_text("x = 1\x0c\ndef helper():\n\tpass\n", encoding="utf-8")
	assert codenav_server._on_definition_keyword("a.py", 2, 1) is True


# -- RC-144: local-name hint wins over a same-named module-level symbol elsewhere --------------------------------


@pytest.fixture
def local_project(tmp_path, monkeypatch, loop):
	return make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/a.py": "def f() -> int:\n\tlocal_var = 1\n\treturn local_var\n",
			"src/pkg/b.py": "local_var = 3\n",
		},
	)


@needs_ty
def test_rc_144_symbol_info_names_the_local_even_when_a_module_symbol_exists_elsewhere(local_project, loop):
	result = loop.run_until_complete(codenav_server.symbol_info(name="local_var", file_path="src/pkg/a.py"))
	assert _is_error(result)
	assert "local_var is a local variable/parameter" in _text(result)


# -- implementations (RC-002 family, item 6 and 7) -----------------------------------------------------------------


@needs_ty
def test_protocol_without_members_is_an_error_not_a_plain_answer(tmp_path, monkeypatch, loop):
	make_workspace(
		tmp_path,
		monkeypatch,
		{"src/pkg/ports.py": "from typing import Protocol\n\n\nclass Empty(Protocol):\n\tpass\n"},
	)
	result = loop.run_until_complete(codenav_server.implementations(port_name="Empty"))
	assert not isinstance(result, str) and _is_error(result)
	assert "Empty has no members to match candidates against." in _text(result)


@needs_ty
def test_unparsable_candidate_is_skipped_by_implementations(tmp_path, monkeypatch, loop):
	make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/ports.py": "from typing import Protocol\n\n\nclass Port(Protocol):\n\tdef run(self) -> int: ...\n",
			"src/pkg/adapter.py": "class Impl:\n\tdef run(self) -> int:\n\t\treturn 1\n",
			"src/pkg/bad.py": "class Bad:\n\tdef run(self) -> int:\n\t\treturn 2\n",
		},
	)
	real = codenav_server._cached_class_infos

	def failing_for_bad(path):
		if path.name == "bad.py":
			raise ValueError("source code string cannot contain null bytes")
		return real(path)

	monkeypatch.setattr(codenav_server, "_cached_class_infos", failing_for_bad)
	text = _run(loop, codenav_server.implementations(port_name="Port"))
	assert "Impl" in text
	assert "skipped: unreadable" in text


# -- RC-002 sibling: excluded importers are reported; .git is not listed as unreadable ---------------------------


def test_rc_002_excluded_importers_are_reported_as_checked_or_updated(tmp_path):
	(tmp_path / "app").mkdir()
	(tmp_path / "app" / "target.py").write_text("def tgt():\n\treturn 1\n", encoding="utf-8")
	(tmp_path / "build").mkdir()
	(tmp_path / "build" / "user.py").write_text("from app.target import tgt\n\ntgt()\n", encoding="utf-8")
	roots = [tmp_path]
	report = find_dependents_report(tmp_path, [tmp_path / "app" / "target.py"], roots)
	lines = format_skipped([s for s in report.skipped if s.reason == "excluded dir"], verb="checked")
	assert lines and lines[0].startswith("Importers not checked (excluded directories): build/ (1 file: ")


def test_rc_002_vcs_directories_are_not_listed_as_unparsable_or_unreadable(tmp_path):
	(tmp_path / "app").mkdir()
	(tmp_path / "app" / "target.py").write_text("def tgt():\n\treturn 1\n", encoding="utf-8")
	(tmp_path / ".git").mkdir()
	(tmp_path / ".git" / "hook.py").write_text("tgt = 1\n", encoding="utf-8")
	report = find_dependents_report(tmp_path, [tmp_path / "app" / "target.py"], [tmp_path])
	lines = format_skipped(report.skipped)
	assert not any(".git" in line for line in lines)


def test_rc_002_importer_scan_reports_the_skipped_directory(tmp_path):
	(tmp_path / "app").mkdir()
	target = tmp_path / "app" / "target.py"
	target.write_text("def tgt():\n\treturn 1\n", encoding="utf-8")
	(tmp_path / "build").mkdir()
	(tmp_path / "build" / "user.py").write_text("from app.target import tgt\n\ntgt()\n", encoding="utf-8")
	session = SimpleNamespace(workspace=tmp_path, roots=[tmp_path])
	skipped: list = []
	found = symbol_tools._importing_statements(session, target, "tgt", skipped)
	assert found == {}
	assert any(s.reason == "excluded dir" for s in skipped)


def test_rc_144_module_scope_names_ignore_function_locals():
	import ast

	tree = ast.parse("import os.path\nclass C:\n\tx = 1\ndef f():\n\tlocal_var = 2\n")
	assert {"os", "C", "x", "f"} <= codenav_server._outer_bound_names(tree)
	assert "local_var" not in codenav_server._outer_bound_names(tree)


def test_rc_086_split_dotted_separates_owner_and_name():
	assert codenav_server._split_dotted("Cart.total") == ("Cart", "total")
	assert codenav_server._split_dotted("pkg.mod.func") == ("pkg.mod", "func")
	assert codenav_server._split_dotted("plain") == ("", "plain")


def _git(root, *args):
	subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)  # noqa: S603,S607


@needs_ty
def test_rc_002_verify_changes_reports_importers_in_excluded_directories(tmp_path, monkeypatch, loop):
	project = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/core.py": "def compute(value: int) -> int:\n\treturn value\n",
			"build/user.py": "from pkg.core import compute\n\n\ncompute(1)\n",
		},
	)
	_git(project, "init", "-q")
	_git(project, "-c", "user.email=a@b.c", "-c", "user.name=t", "add", "-A")
	_git(project, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init")
	core = project / "src/pkg/core.py"
	core.write_text(core.read_text() + "\n\nEXTRA = 1\n", encoding="utf-8")
	text = _run(loop, write_tools.verify_changes())
	assert "Changed since HEAD: src/pkg/core.py" in text
	assert "Importers not checked (excluded directories): build/ (1 file: " in text


def test_rc_001_snippet_lines_of_a_file_ignore_form_feed(tmp_path):
	from mcp_nav_shared.format import _read_lines

	path = tmp_path / "ff.py"
	path.write_text("a = 1\x0cb = 2\nc = 3\n", encoding="utf-8")
	assert _read_lines(path) == ("a = 1\x0cb = 2", "c = 3")
