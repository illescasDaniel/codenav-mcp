"""Fast unit tests for `codenav_mcp.deps`."""

from __future__ import annotations

from codenav_mcp.deps import find_dependents, module_names_for


def _write(root, files: dict[str, str]) -> None:
	for name, content in files.items():
		path = root / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_text(content, encoding="utf-8")


def test_given_roots_when_module_names_for_then_one_name_per_containing_root(tmp_path):
	# given
	_write(tmp_path, {"src/pkg/__init__.py": "", "src/pkg/mod.py": ""})
	# when / then
	assert module_names_for(tmp_path / "src/pkg/mod.py", [tmp_path / "src", tmp_path]) == {"pkg.mod", "src.pkg.mod"}
	assert module_names_for(tmp_path / "src/pkg/__init__.py", [tmp_path / "src"]) == {"pkg"}
	assert module_names_for(tmp_path / "elsewhere.py", [tmp_path / "src"]) == set()


def test_given_various_import_styles_when_find_dependents_then_all_importers_found(tmp_path):
	# given
	_write(
		tmp_path,
		{
			"pkg/__init__.py": "from .core import thing\n",
			"pkg/core.py": "thing = 1\n",
			"pkg/a.py": "from pkg.core import thing\n",
			"pkg/b.py": "from pkg import core\n",
			"pkg/c.py": "import pkg.core\n",
			"pkg/d.py": "from .core import thing\n",
			"pkg/e.py": "from . import core\n",
			"pkg/unrelated.py": "import os\n",
			"tests/test_x.py": "from pkg.core import thing\n",
			".venv/lib/x.py": "from pkg.core import thing\n",
		},
	)
	# when
	found = find_dependents(tmp_path, [tmp_path / "pkg/core.py"], [tmp_path])
	# then
	names = sorted(p.relative_to(tmp_path).as_posix() for p in found)
	assert names == ["pkg/__init__.py", "pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/d.py", "pkg/e.py", "tests/test_x.py"]


def test_given_target_file_when_find_dependents_then_it_is_not_its_own_dependent(tmp_path):
	# given
	_write(tmp_path, {"m.py": "import m\n"})
	# when / then
	assert find_dependents(tmp_path, [tmp_path / "m.py"], [tmp_path]) == []


def test_given_edited_file_when_find_dependents_then_cache_notices_new_imports(tmp_path):
	# given
	_write(tmp_path, {"m.py": "x = 1\n", "user.py": "y = 1\n"})
	assert find_dependents(tmp_path, [tmp_path / "m.py"], [tmp_path]) == []
	# when
	(tmp_path / "user.py").write_text("from m import x\n# longer so the size changes\n", encoding="utf-8")
	# then
	assert [p.name for p in find_dependents(tmp_path, [tmp_path / "m.py"], [tmp_path])] == ["user.py"]


def test_given_syntax_error_file_when_find_dependents_then_skipped(tmp_path):
	# given
	_write(tmp_path, {"m.py": "x = 1\n", "broken.py": "def (:\n", "ok.py": "import m\n"})
	# when / then
	assert [p.name for p in find_dependents(tmp_path, [tmp_path / "m.py"], [tmp_path])] == ["ok.py"]


def test_given_limit_when_find_dependents_then_capped(tmp_path):
	# given
	_write(tmp_path, {"m.py": "x = 1\n", **{f"u{i}.py": "import m\n" for i in range(5)}})
	# when / then
	assert len(find_dependents(tmp_path, [tmp_path / "m.py"], [tmp_path], limit=2)) == 2
