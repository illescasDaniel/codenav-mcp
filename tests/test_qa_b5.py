"""B5: importer discovery and exclusion policy."""

from __future__ import annotations

import os

import pytest

from codenav_mcp import deps
from codenav_mcp.deps import find_dependents, find_dependents_report, format_skipped, primary_module_name
from codenav_mcp.mentions import find_mentions


def _write(root, files: dict[str, str | bytes]) -> None:
	for name, content in files.items():
		path = root / name
		path.parent.mkdir(parents=True, exist_ok=True)
		if isinstance(content, bytes):
			path.write_bytes(content)
		else:
			path.write_text(content, encoding="utf-8")


def test_rc_002_build_dir_importer_reported(tmp_path, monkeypatch):
	monkeypatch.delenv("CODENAV_MCP_INCLUDE_DIRS", raising=False)
	_write(
		tmp_path,
		{
			"app/target.py": "def tgt(): ...\n",
			"app/main.py": "from app.target import tgt\n",
			"app/build/inner_user.py": "from app.target import tgt\n",
			"app/build/other.py": "import os\n",
		},
	)
	report = find_dependents_report(tmp_path, [tmp_path / "app/target.py"], [tmp_path])
	assert [p.name for p in report.files] == ["main.py"]
	assert [(s.path_or_dir, s.reason, s.count) for s in report.skipped] == [("app/build/", "excluded dir", 1)]
	lines = format_skipped(report.skipped)
	assert lines == [
		"Importers not updated (excluded directories): app/build/ (1 file: app/build/inner_user.py). Pass CODENAV_MCP_INCLUDE_DIRS=build to include them."
	]
	monkeypatch.setenv("CODENAV_MCP_INCLUDE_DIRS", "build")
	included = find_dependents_report(tmp_path, [tmp_path / "app/target.py"], [tmp_path])
	assert sorted(p.name for p in included.files) == ["inner_user.py", "main.py"]
	assert included.skipped == []


def test_rc_006_cap(tmp_path):
	files: dict[str, str | bytes] = {"core.py": "X = 1\n"}
	files.update({f"u{i:03d}.py": "from core import X\n" for i in range(250)})
	_write(tmp_path, files)
	assert len(find_dependents(tmp_path, [tmp_path / "core.py"], [tmp_path])) == 250
	report = find_dependents_report(tmp_path, [tmp_path / "core.py"], [tmp_path], limit=200)
	assert len(report.files) == 200
	assert [(s.reason, s.count) for s in report.skipped] == [("limit", 50)]
	assert format_skipped(report.skipped) == ["Importers not updated (more than 200 importers): 50 files more."]
	assert deps.MAX_DEPENDENTS == 2000


def test_rc_007_bom_importer_found(tmp_path):
	_write(
		tmp_path,
		{"a.py": "def helper(): ...\n", "b.py": b"\xef\xbb\xbffrom a import helper\n", "c.py": "import os\n"},
	)
	assert [p.name for p in find_dependents(tmp_path, [tmp_path / "a.py"], [tmp_path])] == ["b.py"]


def test_rc_007_bom_file_mentions_found(tmp_path):
	_write(tmp_path, {"b.py": b"\xef\xbb\xbfx = helper\n"})
	mentions = find_mentions(tmp_path, "helper", {})
	assert [(m.path, m.line, m.kind) for m in mentions] == [("b.py", 1, "code")]


def test_rc_071_unparsable_listed(tmp_path):
	_write(
		tmp_path,
		{
			"a.py": "def helper(): ...\n",
			"b_syntax.py": "from a import helper\ndef (:\n",
			"b_latin1.py": b"# \xe9\nfrom a import helper\n",
			"c.py": "def (:\n",  # unparsable but never mentions `a`... except by word below
		},
	)
	report = find_dependents_report(tmp_path, [tmp_path / "a.py"], [tmp_path])
	assert report.files == []
	by_path = {s.path_or_dir: s for s in report.skipped}
	assert set(by_path) == {"b_syntax.py", "b_latin1.py"}
	assert by_path["b_syntax.py"].reason == "unparsable"
	assert by_path["b_syntax.py"].detail == "b_syntax.py: syntax error at line 2"
	assert by_path["b_latin1.py"].reason == "unreadable"
	assert format_skipped(report.skipped)[0].startswith("Importers not updated (could not be parsed or read): ")


def test_rc_013_most_specific_root(tmp_path):
	_write(tmp_path, {"libs/ext/ext_mod.py": "e = 1\n", "tools/t.py": "from ext.ext_mod import e\n"})
	roots = [tmp_path, tmp_path / "libs"]
	assert primary_module_name(tmp_path / "libs/ext/ext_mod.py", roots) == "ext.ext_mod"
	assert primary_module_name(tmp_path / "libs/ext/ext_mod.py", list(reversed(roots))) == "ext.ext_mod"
	assert primary_module_name(tmp_path / "tools/t.py", roots) == "tools.t"
	assert primary_module_name(tmp_path / "x/y.py", [tmp_path / "libs"]) is None


def test_rc_040_nested_repo(tmp_path):
	_write(
		tmp_path,
		{
			"a.py": "def helper(): ...\n",
			"vend/.git": "gitdir: x",
			"vend/u.py": "from a import helper\n",
			"ok.py": "from a import helper\n",
		},
	)
	assert list(deps.iter_python_files(tmp_path)) == [tmp_path.resolve() / "a.py", tmp_path.resolve() / "ok.py"]
	report = find_dependents_report(tmp_path, [tmp_path / "a.py"], [tmp_path])
	assert [p.name for p in report.files] == ["ok.py"]
	assert [(s.path_or_dir, s.reason, s.count) for s in report.skipped] == [("vend/", "nested repo", 1)]
	assert "nested git repositories" in format_skipped(report.skipped)[0]


def test_unreadable_importer_is_reported_even_when_content_cannot_be_read(tmp_path, monkeypatch):

	_write(tmp_path, {"a.py": "x = 1\n", "hidden.py": "from a import x\n"})
	original = deps._read_no_symlink

	def read_bytes(path):
		if path.name == "hidden.py":
			raise PermissionError("denied")
		return original(path)

	monkeypatch.setattr(deps, "_read_no_symlink", read_bytes)
	report = find_dependents_report(tmp_path, [tmp_path / "a.py"], [tmp_path])
	assert any(s.path_or_dir == "hidden.py" and s.reason == "unreadable" for s in report.skipped)


def test_unscanned_excluded_and_truncated_directories_are_reported(tmp_path, monkeypatch):
	_write(
		tmp_path,
		{
			"a.py": "x = 1\n",
			".venv/lib.py": "from a import x\n",
			"build/one.py": "pass\n",
			"build/two.py": "from a import x\n",
			"dist/three.py": "from a import x\n",
		},
	)
	monkeypatch.setattr(deps, "_MAX_SKIPPED_SCAN_FILES", 1)
	report = find_dependents_report(tmp_path, [tmp_path / "a.py"], [tmp_path])
	assert any(s.path_or_dir == ".venv/" and "not scanned" in s.detail for s in report.skipped)
	assert any(s.path_or_dir == "build/" and "limit reached" in s.detail for s in report.skipped)
	assert any(s.path_or_dir == "dist/" and "not scanned" in s.detail for s in report.skipped)


def test_unreadable_directory_is_reported(tmp_path, monkeypatch):
	_write(tmp_path, {"a.py": "x = 1\n", "private/hidden.py": "from a import x\n"})
	original = deps.os.scandir

	def scandir(path):
		if path.name == "private":
			raise PermissionError("denied")
		return original(path)

	monkeypatch.setattr(deps.os, "scandir", scandir)
	report = find_dependents_report(tmp_path, [tmp_path / "a.py"], [tmp_path])
	assert any(s.path_or_dir == "private/" and s.reason == "unreadable" for s in report.skipped)


def test_symlinked_importers_are_not_read_or_returned(tmp_path):
	root = tmp_path / "workspace"
	root.mkdir()
	_write(root, {"a.py": "x = 1\n"})
	external = tmp_path / "outside.py"
	external.write_text("from a import x\n")
	(root / "alias.py").symlink_to(external)
	(root / "build").mkdir()
	(root / "build" / "alias.py").symlink_to(external)
	report = find_dependents_report(root, [root / "a.py"], [root])
	assert not report.files
	assert {"alias.py", "build/alias.py"} <= {item.path_or_dir for item in report.skipped}
	assert str(external) not in str(report)


def test_unreadable_excluded_file_is_reported(tmp_path, monkeypatch):
	_write(tmp_path, {"a.py": "x = 1\n", "build/hidden.py": "from a import x\n"})
	original = deps._read_no_symlink

	def read(path):
		if path.name == "hidden.py":
			raise PermissionError("denied")
		return original(path)

	monkeypatch.setattr(deps, "_read_no_symlink", read)
	report = find_dependents_report(tmp_path, [tmp_path / "a.py"], [tmp_path])
	assert any(item.path_or_dir == "build/hidden.py" and item.reason == "unreadable" for item in report.skipped)


def test_cached_imports_change_with_same_mtime_and_size(tmp_path):
	_write(tmp_path, {"target.py": "x = 1\n", "user.py": "import absent\n"})
	path = tmp_path / "user.py"
	info = path.stat()
	assert not find_dependents_report(tmp_path, [tmp_path / "target.py"], [tmp_path]).files
	path.write_text("import target\n")
	os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
	assert find_dependents_report(tmp_path, [tmp_path / "target.py"], [tmp_path]).files == [path]


def test_symlinked_directories_reported_without_traversal(tmp_path):
	root = tmp_path / "workspace"
	root.mkdir()
	_write(root, {"target.py": "x = 1\n"})
	external = tmp_path / "external"
	_write(external, {"user.py": "import target\n"})
	(root / "link").symlink_to(external, target_is_directory=True)
	(root / "build").mkdir()
	(root / "build" / "link").symlink_to(external, target_is_directory=True)
	report = find_dependents_report(root, [root / "target.py"], [root])
	assert not report.files
	assert {"link/", "build/link/"} <= {item.path_or_dir for item in report.skipped}
	assert str(external) not in str(report)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable")
def test_special_python_file_reported_without_blocking(tmp_path):
	_write(tmp_path, {"target.py": "x = 1\n"})
	os.mkfifo(tmp_path / "pipe.py")
	report = find_dependents_report(tmp_path, [tmp_path / "target.py"], [tmp_path])
	assert any(item.path_or_dir == "pipe.py" and item.reason == "unreadable" for item in report.skipped)
