"""QA block B20: wrong-code and data fixes from the black-box re-run (CRLF inserts, quick-fix import cycles,
the move error gate, side-effect imports, signature call rewrites, TYPE_CHECKING renames, namespace importers)."""

from __future__ import annotations

import asyncio
import re
from typing import cast

from mcp.types import CallToolResult, TextContent

from codenav_mcp import edit_tools, edits_tools, refactor_tools, server, write_tools
from codenav_mcp.tool_base import Session

from .live_support import make_loop_fixture, make_workspace, needs_ty


loop = make_loop_fixture()


# (1) ESYM-028 / ESYM-028a: inserting into a CRLF file keeps one CR before each LF.


@needs_ty
def test_rc_esym028_insert_into_crlf_file_keeps_single_cr(tmp_path, monkeypatch, loop):
	project = make_workspace(
		tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/crlf.py": "def f():\r\n    return 1\r\n"}
	)

	text = loop.run_until_complete(
		edit_tools.edit_symbol(
			action="insert", file_path="src/pkg/crlf.py", source="def g2():\n    return 2\n", position="end", apply=True
		)
	)

	raw = (project / "src/pkg/crlf.py").read_bytes()
	assert "Applied as edit" in text
	assert b"\r\r" not in raw
	assert raw.replace(b"\r\n", b"").find(b"\n") == -1
	assert raw.endswith(b"def g2():\r\n    return 2\r\n")


# (2) SIG-024 / REN-027 / BATCH-027: a plan refused by the file-count limit keeps a preview id.


@needs_ty
def test_rc_sig024_large_rename_refusal_keeps_plan_under_an_id(tmp_path, monkeypatch, loop):
	files = {"src/pkg/__init__.py": "", "src/pkg/lib.py": "def helper() -> int:\n\treturn 1\n"}
	for i in range(45):
		files[f"src/pkg/user{i:02d}.py"] = "from pkg.lib import helper\n\n\ndef f() -> int:\n\treturn helper()\n"
	project = make_workspace(tmp_path, monkeypatch, files)

	refused = loop.run_until_complete(
		write_tools.rename(name="helper", file_path="src/pkg/lib.py", new_name="renamed", apply=True)
	)

	assert "NOT applied" in refused
	assert "touches 46 files (limit 40)" in refused
	match = re.search(r'edits\(action="apply", id="([0-9a-f]+)", allow_large=true\)', refused)
	assert match, refused
	assert "def helper" in (project / "src/pkg/lib.py").read_text(encoding="utf-8")

	applied = loop.run_until_complete(edits_tools.edits(id=match.group(1), allow_large=True, action="apply"))

	assert "Applied" in applied or "edit" in applied, applied
	assert "def renamed" in (project / "src/pkg/lib.py").read_text(encoding="utf-8")


# (4) change_signature: comment-bearing calls are refused, alias calls are reported, unreadable call-site files
# are refused with a readable reason, and a malformed `add` entry gets a correct message.

SIG_LIB = "def send(to, cc=None, bcc=None):\n\treturn (to, cc, bcc)\n"


@needs_ty
def test_rc_sig005_change_with_commented_call_is_refused_and_nothing_written(tmp_path, monkeypatch, loop):
	callers = (
		'from pkg.lib import send\n\n\ntotal = send(\n    "a",  # recipient\n    "b",  # copy\n'
		'    # explicit bcc follows\n    bcc="c",\n)\n'
	)
	project = make_workspace(
		tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/lib.py": SIG_LIB, "src/pkg/callers.py": callers}
	)

	text = loop.run_until_complete(refactor_tools.change_signature(name="send", remove=["cc"], apply=True))

	assert "cannot be written safely" in text
	assert "src/pkg/callers.py:4" in text
	assert "apply_edit" not in text
	assert (project / "src/pkg/lib.py").read_text(encoding="utf-8") == SIG_LIB
	assert (project / "src/pkg/callers.py").read_text(encoding="utf-8") == callers


@needs_ty
def test_rc_sig027_alias_call_is_reported_for_manual_review(tmp_path, monkeypatch, loop):
	project = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/lib.py": SIG_LIB,
			"src/pkg/use.py": 'from pkg.lib import send as s\n\n\ndef go() -> None:\n\ts("x", "y")\n',
		},
	)

	text = loop.run_until_complete(
		refactor_tools.change_signature(name="send", remove=["cc"], apply=True, max_new_errors=None)
	)

	assert "requires review" in text
	assert 's("x", "y")' in (project / "src/pkg/use.py").read_text(encoding="utf-8")


@needs_ty
def test_rc_sig004_call_site_file_with_syntax_error_is_refused_readably(tmp_path, monkeypatch, loop):
	project = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/lib.py": SIG_LIB,
			"src/pkg/broken.py": "from pkg.lib import send\n\nsend('b')\n\n\ndef oops(:\n\tpass\n",
		},
	)

	text = loop.run_until_complete(refactor_tools.change_signature(name="send", remove=["cc"], apply=True))

	assert "cannot tokenize" not in text
	assert "('" not in text
	assert "Error executing" not in text
	assert "broken.py" in text
	assert SIG_LIB in (project / "src/pkg/lib.py").read_text(encoding="utf-8")


def test_rc_sig033_add_entry_that_is_not_an_object_gets_a_true_message(tmp_path):
	result = asyncio.run(server.mcp.call_tool("change_signature", {"name": "send", "add": ["x"]}))

	assert isinstance(result, CallToolResult) and result.is_error
	assert isinstance(content := result.content[0], TextContent)
	text = content.text
	assert "add must be a list of objects" in text
	assert "(got ['x'])" in text


# (5) IMP-018: a plain `import` that is unused after a delete may be there for its side effects: it is kept and
# reported, while an unused `from x import name` is still pruned.


@needs_ty
def test_rc_imp018_plain_import_with_side_effects_is_kept_when_its_only_user_is_deleted(tmp_path, monkeypatch, loop):
	source = (
		"import readline\nfrom os.path import join\n\n\ndef legacy() -> str:\n"
		"\treturn readline.__doc__ or join('a', 'b')\n\n\ndef keep() -> int:\n\treturn 1\n"
	)
	project = make_workspace(tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/plug.py": source})

	text = loop.run_until_complete(
		edit_tools.edit_symbol(action="delete", name="legacy", file_path="src/pkg/plug.py", apply=True)
	)

	after = (project / "src/pkg/plug.py").read_text(encoding="utf-8")
	assert "Applied as edit" in text
	assert "import readline" in after
	assert "readline" in text and "side effects" in text
	assert "from os.path import join" not in after
	assert "def keep" in after


# (3) DEPS-005: `from pkg import name` where pkg/__init__.py binds `name` itself keeps that value. A module move
# must not rewrite such an import to the moved module's new name (the import would then return a module).


@needs_ty
def test_rc_deps005_import_of_a_name_the_package_binds_is_not_rewritten_to_the_moved_module(
	tmp_path, monkeypatch, loop
):
	monkeypatch.setenv("CODENAV_MCP_INCLUDE_DIRS", "env")
	main = "from app.env import settings\n\n\ndef run() -> object:\n\treturn settings\n"
	project = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/app/__init__.py": "",
			"src/app/env/__init__.py": "settings = 1\n",
			"src/app/env/settings.py": "VALUE = 1\n",
			"src/app/main.py": main,
		},
	)

	refused = loop.run_until_complete(
		refactor_tools._move_module(
			from_file="src/app/env/settings.py", to_file="src/app/env/settings2.py", apply=True, max_new_errors=None
		)
	)
	assert "Cannot safely determine" in refused and "importing its submodule" in refused
	assert (project / "src/app/env/settings.py").exists()
	assert not (project / "src/app/env/settings2.py").exists()
	assert (project / "src/app/main.py").read_text(encoding="utf-8") == main


@needs_ty
def test_python_module_move_requires_explicit_incomplete_diagnostics_waiver(tmp_path, monkeypatch, loop):
	project = make_workspace(tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/a.py": "VALUE = 1\n"})
	result = loop.run_until_complete(
		refactor_tools._move_module(from_file="src/pkg/a.py", to_file="src/pkg/b.py", apply=True)
	)
	assert "NOT applied" in result and "Deleted Python modules cannot be accurately simulated" in result
	assert (project / "src/pkg/a.py").exists() and not (project / "src/pkg/b.py").exists()
	result = loop.run_until_complete(
		refactor_tools._move_module(from_file="src/pkg/a.py", to_file="src/pkg/b.py", apply=True, max_new_errors=None)
	)
	assert "Applied" in result and (project / "src/pkg/b.py").exists()


def test_quick_fix_warns_if_new_import_creates_direct_cycle(tmp_path):
	from types import SimpleNamespace

	from codenav_mcp.symbol_tools import _quick_fix_cycle_notes

	package = tmp_path / "cyc"
	package.mkdir()
	(package / "__init__.py").write_text("")
	user = package / "user.py"
	user.write_text("def util(): pass\n")
	(package / "models.py").write_text("from cyc.user import util\nclass CycCart: pass\n")
	session = SimpleNamespace(workspace=tmp_path, roots=[tmp_path])
	notes = _quick_fix_cycle_notes(
		cast(Session, session), user, user.read_text(), "from cyc.models import CycCart\n" + user.read_text()
	)
	assert len(notes) == 1 and "import cycle" in notes[0] and "cyc.models" in notes[0]
	assert not _quick_fix_cycle_notes(cast(Session, session), user, user.read_text(), "import os\n" + user.read_text())


def test_parent_binding_distinguishes_literals_submodules_and_conditional_writes(tmp_path):
	import pytest
	from mcp_nav_shared.errors import ToolInputError

	from codenav_mcp.moves import _parent_binds

	package = tmp_path / "app" / "env"
	package.mkdir(parents=True)
	init = package / "__init__.py"
	init.write_text("settings = 1\n")
	with pytest.raises(ToolInputError, match="importing its submodule"):
		_parent_binds("app.env", "settings", [tmp_path])
	init.write_text("from . import settings\n")
	with pytest.raises(ToolInputError, match="Cannot safely determine"):
		_parent_binds("app.env", "settings", [tmp_path])
	init.write_text("if False:\n    settings = 1\n")
	with pytest.raises(ToolInputError, match="Cannot safely determine"):
		_parent_binds("app.env", "settings", [tmp_path])
	init.write_text("from somewhere import settings\n")
	with pytest.raises(ToolInputError, match="Cannot safely determine"):
		_parent_binds("app.env", "settings", [tmp_path])
	for source in (
		"settings = 1\ndel settings\n",
		'settings = 1\nglobals().pop("settings")\n',
		"settings = 1\nimport sys\ndel sys.modules[__name__].settings\n",
		"settings = 1\nimport helper\nhelper.mutate()\n",
	):
		init.write_text(source)
		with pytest.raises(ToolInputError, match="Cannot safely determine"):
			_parent_binds("app.env", "settings", [tmp_path])


@needs_ty
def test_quick_fix_import_cycle_warning_reaches_applied_reply(tmp_path, monkeypatch, loop):
	project = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/cyc/__init__.py": "",
			"src/cyc/user.py": "def util():\n    return 1\n\ndef f() -> object:\n    return CycCart()\n",
			"src/cyc/models.py": "from cyc.user import util\n\nclass CycCart:\n    pass\n",
		},
	)
	from codenav_mcp.symbol_tools import fix

	result = loop.run_until_complete(fix(file_path="src/cyc/user.py", choice="cyc.models", apply=True))
	assert "Applied as edit" in result, result
	assert "import cycle" in result and "cyc.models" in result, result
	assert "from cyc.models import CycCart" in (project / "src/cyc/user.py").read_text()
