"""Fix round B1: the type gate checks importers reached through package re-exports and says what it left out."""

from __future__ import annotations

from pathlib import Path

import pytest

from codenav_mcp import deps, edit_tools, refactor_tools
from codenav_mcp.simulate import Scope, files_to_check, scope_clauses
from codenav_mcp.ty_command import ty_coverage

from .live_support import make_loop_fixture, make_workspace, needs_ty, write_files


loop = make_loop_fixture()

PKG = {
	"src/pkg/__init__.py": "from .base import Base as Base\n",
	"src/pkg/base.py": "class Base:\n\tdef run(self, x: int) -> int:\n\t\treturn x\n",
}


def _ws(tmp_path: Path, files: dict[str, str], pyproject: str = "") -> Path:
	tmp_path.mkdir(parents=True, exist_ok=True)
	(tmp_path / "pyproject.toml").write_text(f'[project]\nname = "demo"\nversion = "0"\n{pyproject}', encoding="utf-8")
	write_files(tmp_path, files)
	return tmp_path


def _roots(root: Path) -> list[Path]:
	return [root / "src", root]


def test_reexport_importers_are_dependents(tmp_path):
	root = _ws(
		tmp_path,
		{
			**PKG,
			"tests/test_it.py": "import pkg\n\n\nclass Sub(pkg.Base):\n\tpass\n",
			"tests/test_unrelated.py": "import pkg\n\nVALUE = pkg.something\n",
			"tests/test_star.py": "from pkg import *\n\nclass S(Base):\n\tpass\n",
		},
	)
	paths, _ = files_to_check(root, _roots(root), [root / "src/pkg/base.py"])
	names = {p.relative_to(root).as_posix() for p in paths}
	assert "tests/test_it.py" in names
	assert "tests/test_star.py" in names
	assert "tests/test_unrelated.py" not in names  # imports the package but never mentions a re-exported name


def test_reexport_through_nested_packages(tmp_path):
	root = _ws(
		tmp_path,
		{
			"src/pkg/__init__.py": "from .sub import Base as Base\n",
			"src/pkg/sub/__init__.py": "from .base import Base as Base\n",
			"src/pkg/sub/base.py": "class Base:\n\tpass\n",
			"tests/test_it.py": "import pkg\n\n\nclass Sub(pkg.Base):\n\tpass\n",
		},
	)
	found = deps.reexport_dependents(
		root, [root / "src/pkg/sub/base.py"], [root / "src/pkg/sub/__init__.py"], _roots(root)
	)
	assert {p.relative_to(root).as_posix() for p in found} == {"src/pkg/__init__.py", "tests/test_it.py"}


def test_ty_coverage_follows_the_config(tmp_path):
	root = _ws(tmp_path, {}, '[tool.ty.src]\nexclude = ["scripts/**", "docs/", "*_gen.py"]\n')
	cover = ty_coverage(root)
	assert cover.excludes(root / "scripts/a.py", root)
	assert cover.excludes(root / "scripts/deep/a.py", root)
	assert cover.excludes(root / "docs/conf.py", root)
	assert cover.excludes(root / "src/pkg/x_gen.py", root)
	assert not cover.excludes(root / "src/pkg/base.py", root)
	assert not cover.excludes(root / "tests/test_a.py", root)
	include = ty_coverage(_ws(tmp_path / "inc", {}, '[tool.ty.src]\ninclude = ["src"]\n'))
	assert include.excludes(tmp_path / "inc/tests/t.py", tmp_path / "inc")
	assert not include.excludes(tmp_path / "inc/src/a.py", tmp_path / "inc")


def test_files_to_check_reports_what_ty_config_excludes(tmp_path):
	root = _ws(
		tmp_path,
		{**PKG, "scripts/run.py": "from pkg.base import Base\n", "examples/e.py": "from pkg.base import Base\n"},
		'[tool.ty.src]\nexclude = ["scripts/**", "examples/**"]\n',
	)
	scope = Scope()
	paths, _ = files_to_check(root, _roots(root), [root / "src/pkg/base.py"], scope=scope)
	assert {p.name for p in paths} == {"base.py", "__init__.py"}
	assert scope_clauses(scope, root) == ["scripts/, examples/ (ty config excludes)"]


def test_files_to_check_ranks_files_mentioning_the_changed_names_first(tmp_path):
	files = {
		**PKG,
		"src/pkg/a_other.py": "from pkg.base import Base\n",
		"src/pkg/z_user.py": "from pkg.base import Base\n\nBase.run\n",
	}
	root = _ws(tmp_path, files)
	paths, left_out = files_to_check(root, _roots(root), [root / "src/pkg/base.py"], limit=2, names={"run"})
	assert [p.name for p in paths] == ["base.py", "z_user.py"] and left_out == 2


@needs_ty
@pytest.mark.integration
def test_gate_line_names_ty_excluded_importers(tmp_path, monkeypatch, loop):
	"""scripts/ is outside the ty config: its importer must not be counted as checked, and the line says so."""
	make_workspace(tmp_path, monkeypatch, PKG)
	(tmp_path / "pyproject.toml").write_text(
		'[project]\nname = "demo"\nversion = "0"\n[tool.ty.environment]\nroot = ["src", "."]\n'
		'[tool.ty.src]\nexclude = ["scripts/**"]\n',
		encoding="utf-8",
	)
	write_files(tmp_path, {"scripts/run.py": "from pkg.base import Base\n\nBase().run('x')\n"})
	reply = str(
		loop.run_until_complete(
			edit_tools.edit_symbol(
				name="Base.run", action="replace_body", source="return x + 1", file_path="src/pkg/base.py", apply=False
			)
		)
	)
	line = next(ln for ln in reply.splitlines() if ln.startswith("Type check:"))
	assert "not checked: scripts/ (ty config excludes)" in line, reply
	changed = str(
		loop.run_until_complete(
			refactor_tools.change_signature(
				name="Base.run", file_path="src/pkg/base.py", add=[{"name": "extra", "default": "None"}]
			)
		)
	)
	line = next(ln for ln in changed.splitlines() if ln.startswith("Type check:"))
	assert "not checked: scripts/ (ty config excludes)" in line, changed
