"""Fix C4: imports of a module the edit is about to create are sorted as first-party, as ruff will see them later."""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404
from pathlib import Path

import pytest

from codenav_mcp.tidy import find_ruff, sort_imports


pytestmark = pytest.mark.skipif(shutil.which("ruff") is None and find_ruff(Path.cwd()) is None, reason="needs ruff")

PYPROJECT = '[tool.ruff.lint]\nselect = ["I"]\n\n[tool.ruff.lint.isort]\ncombine-as-imports = true\n'
HEADER = "from __future__ import annotations\n\nimport os\n\nimport pytest\n\n"


def _project(tmp_path: Path, extra: str = "") -> Path:
	(tmp_path / "pyproject.toml").write_text(PYPROJECT + extra)
	for package in ("src/pkg", "tests"):
		(tmp_path / package).mkdir(parents=True)
		(tmp_path / package / "__init__.py").write_text("")
	(tmp_path / "src/pkg/core.py").write_text("X = 1\n")
	(tmp_path / "tests/helpers.py").write_text("def load(): ...\n")
	return tmp_path


def _ruff_clean(workspace: Path, rel: str, text: str) -> bool:
	ruff = find_ruff(workspace) or ["ruff"]
	done = subprocess.run(  # noqa: S603
		[*ruff, "check", "--select", "I001", "--stdin-filename", rel, "--quiet", "-"],
		input=text,
		capture_output=True,
		text=True,
		cwd=workspace,
		check=False,
	)
	return done.returncode == 0


def test_given_an_import_of_a_module_that_does_not_exist_yet_then_it_is_sorted_with_first_party(tmp_path):
	workspace = _project(tmp_path)
	original = HEADER + "from pkg.core import X\nfrom tests.helpers import load\n\nprint(X, os, pytest, load)\n"
	edited = original.replace("import pytest\n", "import pytest\nfrom pkg.created import Y\n")
	rel = "tests/test_a.py"
	assert _ruff_clean(workspace, rel, original)
	result = sort_imports(workspace, original, edited, rel)
	assert result != edited, "the new import stayed among the third-party imports"
	(workspace / "src/pkg/created.py").write_text("Y = 1\n")  # what the move writes
	assert _ruff_clean(workspace, rel, result)


def test_given_a_project_with_its_own_known_first_party_then_it_stays_in_force(tmp_path):
	workspace = _project(tmp_path, 'known-first-party = ["tests"]\n')
	original = "import os\n\nimport pytest\n\nfrom pkg.core import X\nfrom tests.helpers import load\n\nprint(os, pytest, X, load)\n"
	edited = original.replace("import pytest\n", "import pytest\nfrom pkg.created import Y\n")
	rel = "tests/test_a.py"
	assert _ruff_clean(workspace, rel, original)
	result = sort_imports(workspace, original, edited, rel)
	(workspace / "src/pkg/created.py").write_text("Y = 1\n")
	assert _ruff_clean(workspace, rel, result)


def test_given_a_new_import_of_a_third_party_package_then_nothing_is_forced_first_party(tmp_path):
	workspace = _project(tmp_path)
	original = "import os\n\nimport pytest\n\nprint(os, pytest)\n"
	edited = original.replace("import pytest\n", "import pytest\nimport zzz_unknown\n")
	assert sort_imports(workspace, original, edited, "tests/test_a.py") == edited


def test_given_a_file_the_edit_creates_then_its_imports_are_sorted(tmp_path):
	workspace = _project(tmp_path)
	edited = "import pytest\nimport os\n\nprint(os, pytest)\n"
	assert (
		sort_imports(workspace, "", edited, "tests/test_new.py") == "import os\n\nimport pytest\n\nprint(os, pytest)\n"
	)


def test_given_crlf_text_then_sorting_keeps_the_line_endings(tmp_path):
	workspace = _project(tmp_path)
	original = "import os\r\n\r\nprint(os)\r\n"
	edited = "import pytest\r\nimport os\r\n\r\nprint(os, pytest)\r\n"
	assert sort_imports(workspace, "", edited, "tests/test_new.py") == (
		"import os\r\n\r\nimport pytest\r\n\r\nprint(os, pytest)\r\n"
	)
	assert sort_imports(workspace, original, edited, "tests/test_new.py") == (
		"import os\r\n\r\nimport pytest\r\n\r\nprint(os, pytest)\r\n"
	)
