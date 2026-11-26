from __future__ import annotations

import ast

from codenav_mcp import imports, tidy


def test_rc_028_headers():
	out = imports.add_import("#!/usr/bin/python\n# coding: latin-1\nx = 1\n", "os")
	assert out.index("import os") > out.index("coding:")


def test_rc_042_empty_block():
	out = imports.remove_names("if TYPE_CHECKING:\n    from x import y\n", {"y"})
	ast.parse(out)
	assert "pass" in out
	out = imports.remove_names(
		"try:\n    import a\n    import b\nexcept ImportError:\n    import c\n    import d\n", {"a", "b", "c", "d"}
	)
	ast.parse(out)
	assert out.count("pass") == 2


def test_rc_053_nested():
	tree = ast.parse("def f():\n    from a import helper\n")
	assert imports.module_imports(tree) == []
	assert imports.module_imports(tree, include_nested=True)[0].scope == "function"


def test_rc_074_comments():
	out = imports.add_from_import(
		"from pathlib import (\n    PurePath,  # keep: used by plugins\n    PosixPath,\n)\n", "pathlib", ["Path"]
	)
	assert "# keep: used by plugins" in out
	assert "import (" in out
	out = imports.remove_names(
		"from os import (\n    sep,  # separator\n    getcwd,  # cwd\n    linesep,\n)  # noqa\n", {"getcwd"}
	)
	assert all(comment in out for comment in ("# separator", "# cwd", "# noqa"))
	assert ")  # noqa" in out
	ast.parse(out)


def test_rc_129_failure(tmp_path, monkeypatch):
	monkeypatch.setattr(tidy, "find_ruff", lambda _: None)
	assert tidy.sort_imports(tmp_path, "import a\n", "import b\n", "a.py") == "import b\n"
	assert tidy.sort_failure() == "imports were not sorted (ruff unavailable or failed)"


def test_rc_134_type_comment_limit():
	assert "type comments" in (imports.module_imports.__doc__ or "")


def test_rc_138_sorted():
	out = imports.add_from_import("from models import Item  # keep\n", "models", ["Cart"])
	assert out == "from models import Cart, Item  # keep\n"


def test_shared_line_refused_without_losing_code():
	import pytest
	from mcp_nav_shared.errors import ToolInputError

	for text, op in [
		('from x import a; print("hello")\n', lambda text: imports.add_from_import(text, "x", ["z"])),
		('from x import a; print("hello")\n', lambda text: imports.remove_names(text, {"a"})),
		("if TYPE_CHECKING: from x import y\n", lambda text: imports.remove_names(text, {"y"})),
		("try: import a; import b\nexcept ImportError: pass\n", lambda text: imports.remove_names(text, {"a", "b"})),
	]:
		with pytest.raises(ToolInputError, match="sharing line"):
			op(text)


def test_cookie_only_in_comments():
	text = 'print("coding: utf-8", os.name)\n'
	out = imports.add_import(text, "os")
	assert out.index("import os") < out.index("print(")
	text = "print(os.name)\n# coding: utf-8\n"
	assert imports.add_import(text, "os").index("import os") < text.index("print(") + 1


def test_import_rewrite_preserves_crlf():
	text = "from x import (\r\n    a,\r\n    b,\r\n)\r\n"
	for out in (imports.add_from_import(text, "x", ["z"]), imports.remove_names(text, {"a"})):
		assert "\n" not in out.replace("\r\n", "")
		ast.parse(out)


def test_conditional_import_does_not_satisfy_runtime_import():
	text = "if TYPE_CHECKING:\n    from x import Item\n"
	out = imports.add_from_import(text, "x", ["Item"])
	tree = ast.parse(out)
	assert any(isinstance(node, ast.ImportFrom) and node.module == "x" for node in tree.body)


def test_comment_association_uses_exact_alias_position():
	text = "from x import (\n    a,  # a comment\n    ab,  # ab comment\n)\n"
	out = imports.add_from_import(text, "x", ["c"])
	assert "a,  # a comment" in out
	assert "ab,  # ab comment" in out
	assert out.count("# a comment") == out.count("# ab comment") == 1


def test_conditional_plain_import_does_not_satisfy_runtime_import():
	text = "if TYPE_CHECKING:\n    import os\n"
	out = imports.add_import(text, "os")
	assert any(isinstance(node, ast.Import) for node in ast.parse(out).body)


def test_shared_alias_comment_refused():
	import pytest
	from mcp_nav_shared.errors import ToolInputError

	text = "from x import (\n    a, b,  # both\n)\n"
	with pytest.raises(ToolInputError, match="preserve comments"):
		imports.add_from_import(text, "x", ["c"])


def test_duplicate_comments_preserved():
	text = "from x import (\n    a,  # same\n    # same\n    b,\n)\n"
	out = imports.add_from_import(text, "x", ["c"])
	assert out.count("# same") == 2
