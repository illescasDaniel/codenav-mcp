"""QA block B7: rename guards and the implementations probe, against a real `ty`.

Every test drives the public tools, so each one fails by assertion on the base code and passes with the fix.
"""

from __future__ import annotations

import pytest

from codenav_mcp import server as codenav_server, write_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()


def _run(loop, coro):
	return loop.run_until_complete(coro)


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/stubbed.py": "def stubfn(x):\n\treturn x\n",
			"src/pkg/stubbed.pyi": "def stubfn(x: int) -> int: ...\n",
			"src/pkg/uses_stub.py": "from pkg.stubbed import stubfn\n\nprint(stubfn(1))\n",
		},
	)


def test_rc_003_stub_twin_is_renamed_with_the_source(project, loop):
	# when
	text = _run(loop, write_tools.rename_symbol(name="stubfn", file_path="src/pkg/stubbed.py", new_name="stubbed_fn"))
	# then — the .pyi declaration and its importer follow the rename, nothing is left behind
	assert "Applied as edit" in text
	assert "def stubbed_fn(x: int) -> int" in (project / "src/pkg/stubbed.pyi").read_text()
	assert "from pkg.stubbed import stubbed_fn" in (project / "src/pkg/uses_stub.py").read_text()


def test_rc_009_same_named_class_outside_protocol_is_not_renamed(tmp_path, monkeypatch, loop):
	# given — `legacy.Repo` has the port's member names, but a different save() signature
	project = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/ports.py": (
				"from typing import Protocol\n\n\nclass Repo(Protocol):\n"
				"\tdef save(self, x: str) -> str: ...\n\tdef load(self, x: int) -> str: ...\n"
			),
			"src/pkg/legacy.py": (
				"class Repo:\n\tdef save(self, a: int, b: int, c: int) -> int:\n\t\treturn a\n"
				"\tdef load(self, x: int) -> str:\n\t\treturn ''\n"
			),
		},
	)
	# when
	text = _run(
		loop,
		write_tools.rename_symbol(name="Repo.save", file_path="src/pkg/ports.py", new_name="persist", apply=False),
	)
	ports = _run(loop, codenav_server.implementations(port_name="Repo", file_path="src/pkg/ports.py"))
	# then
	assert "renamed too" not in text
	assert "0 implementation(s) of Repo in 0 file(s)" in ports
	assert (project / "src/pkg/legacy.py").read_text().count("def save") == 1


def test_rc_010_candidate_in_the_protocols_module_is_verified(tmp_path, monkeypatch, loop):
	# given
	make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/sinks.py": (
				"from typing import Protocol\n\n\nclass Sink(Protocol):\n\tdef write(self, s: str) -> None: ...\n\n\n"
				"class FileSink:\n\tdef write(self, s: str) -> None:\n\t\tpass\n"
			),
		},
	)
	# when
	text = _run(loop, codenav_server.implementations(port_name="Sink", file_path="src/pkg/sinks.py"))
	# then
	assert "1 implementation(s) of Sink in 1 file(s) (type-verified)" in text
	assert "FileSink  (src/pkg/sinks.py)" in text
	assert "could not verify" not in text


def test_rc_015_locals_in_protocol_method_bodies_are_not_members(tmp_path, monkeypatch, loop):
	# given — `tmp` is a local of Store.save, not a member the implementers must have
	make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/store.py": (
				"from typing import Protocol\n\n\nclass Store(Protocol):\n\tdef save(self, x: str) -> None:\n"
				"\t\ttmp = x\n\n\tdef load(self, x: str) -> None: ...\n\n\n"
				"class FileStore:\n\tdef save(self, x: str) -> None:\n\t\tpass\n\n\tdef load(self, x: str) -> None:\n"
				"\t\tpass\n"
			),
		},
	)
	# when
	text = _run(loop, codenav_server.implementations(port_name="Store", file_path="src/pkg/store.py"))
	# then
	assert "1 implementation(s) of Store in 1 file(s) (type-verified)" in text
	assert "FileStore  (src/pkg/store.py)" in text
	assert "tmp" not in text


def test_rc_024_override_of_library_base_is_refused(project, loop):
	# given — `Worker.run` overrides `threading.Thread.run`, which the workspace does not own
	(project / "src/pkg/worker.py").write_text(
		"import threading\n\n\nclass Worker(threading.Thread):\n\tdef run(self) -> None:\n\t\tpass\n"
	)
	# when
	text = _run(loop, write_tools.rename_symbol(name="Worker.run", new_name="execute", apply=True))
	# then
	assert "run overrides Thread.run from threading (outside the workspace)" in text
	assert "Applied as edit" not in text
	assert "def run(self)" in (project / "src/pkg/worker.py").read_text()


def test_rc_025_importer_binding_of_the_new_name_is_refused(project, loop):
	# given — b.py imports `helper` from a.py and already binds `fetch` at module level
	(project / "src/pkg/a.py").write_text("def helper() -> int:\n\treturn 1\n")
	(project / "src/pkg/b.py").write_text(
		"from pkg.a import helper\n\n\ndef fetch() -> int:\n\treturn 2\n\n\ndef use() -> int:\n\treturn helper() + fetch()\n"
	)
	# when
	text = _run(loop, write_tools.rename_symbol(name="helper", file_path="src/pkg/a.py", new_name="fetch", apply=False))
	# then
	assert "fetch is already bound in src/pkg/b.py (line 4)" in text


def test_rc_037_source_root_messages_show_no_absolute_paths(project, loop):
	# given — no class in the workspace covers the protocol's members
	(project / "src/pkg/store.py").write_text(
		"from typing import Protocol\n\n\nclass Store(Protocol):\n\tdef save(self) -> None: ...\n"
	)
	# when
	text = _run(loop, codenav_server.implementations(port_name="Store", file_path="src/pkg/store.py"))
	# then
	assert "No classes under src cover Store's methods: save." in text
	assert str(project) not in text


def test_rc_063_all_export_list_follows_the_rename(project, loop):
	# given — a literal __all__ that exports the function
	(project / "src/pkg/a.py").write_text(
		'__all__ = ["helper", "Cart"]\n\n\ndef helper() -> int:\n\treturn 1\n\n\nclass Cart:\n\tpass\n'
	)
	(project / "src/pkg/b.py").write_text("from pkg.a import helper\n\nprint(helper())\n")
	# when
	text = _run(loop, write_tools.rename_symbol(name="helper", file_path="src/pkg/a.py", new_name="assist"))
	# then
	assert "Applied as edit" in text
	assert '__all__ = ["assist", "Cart"]' in (project / "src/pkg/a.py").read_text()
	# the export line is already renamed, so it is not listed as a string mention to review
	assert '__all__ = ["helper"' not in text


def test_rc_064_new_name_equal_after_nfkc_is_refused(project, loop):
	# given — `ﬁx` (U+FB01) is `fix` after NFKC normalisation, which Python applies to identifiers
	(project / "src/pkg/nfkc.py").write_text("def fix() -> int:\n\treturn 1\n\n\ndef other() -> int:\n\treturn 2\n")
	# when
	text = _run(
		loop,
		write_tools.rename_symbol(name="other", file_path="src/pkg/nfkc.py", new_name="\ufb01x", apply=False),
	)
	# then
	assert "new name \ufb01x collides with fix after Unicode normalisation" in text
	assert "Rename other" not in text


def test_rc_065_special_method_rename_is_refused(project, loop):
	# given
	(project / "src/pkg/dunder.py").write_text(
		"class P:\n\tdef __eq__(self, other: object) -> bool:\n\t\treturn True\n"
	)
	# when
	text = _run(loop, write_tools.rename_symbol(name="P.__eq__", new_name="same", apply=True))
	# then
	assert "__eq__ is a special method name" in text
	assert "def __eq__" in (project / "src/pkg/dunder.py").read_text()


def test_rc_081_stub_only_class_is_listed_and_marked(project, loop):
	# given — `StubStore` exists only as a stub declaration
	(project / "src/pkg/ports.py").write_text(
		"from typing import Protocol\n\n\nclass Repo(Protocol):\n\tdef save(self, x: str) -> str: ...\n"
	)
	(project / "src/pkg/impl.pyi").write_text("class StubStore:\n\tdef save(self, x: str) -> str: ...\n")
	# when
	text = _run(loop, codenav_server.implementations(port_name="Repo", file_path="src/pkg/ports.py"))
	# then
	assert "StubStore  (src/pkg/impl.pyi, stub)" in text


def test_rc_092_file_path_outside_the_workspace_is_refused(project, loop, tmp_path):
	# given — an outside file declares the same name as one inside the workspace
	outside = tmp_path.parent / f"{tmp_path.name}-outside"
	outside.mkdir()
	(outside / "real.py").write_text("def real_fn() -> int:\n\treturn 1\n")
	(project / "real.py").write_text("def real_fn() -> int:\n\treturn 1\n")
	# when
	text = _run(
		loop,
		write_tools.rename_symbol(
			name="real_fn", file_path=f"../{outside.name}/real.py", new_name="hijacked", apply=True
		),
	)
	# then
	assert "outside the workspace" in text
	assert "Applied as edit" not in text
	assert "def real_fn" in (project / "real.py").read_text()


def test_rc_123_name_and_position_pointing_at_different_symbols_is_refused(project, loop):
	# given — line 5, column 5 is `helper`, not `fix`
	(project / "src/pkg/core.py").write_text(
		"def fix() -> int:\n\treturn 1\n\n\ndef helper(x: int = 1) -> int:\n\treturn x\n"
	)
	# when
	text = _run(
		loop,
		write_tools.rename_symbol(
			name="fix", file_path="src/pkg/core.py", line=5, column=5, new_name="zz", apply=False
		),
	)
	# then
	assert "name and line/column point to different symbols; give one of them" in text
	assert "Rename fix" not in text
