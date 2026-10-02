"""Live checks of replace_symbol / insert_symbol / safe_delete / quick_fix against a real `ty`."""

from __future__ import annotations

import ast

import pytest

from codenav_mcp import symbol_tools, write_tools

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]

loop = make_loop_fixture()

_FILES = {
	"src/pkg/__init__.py": "",
	"src/pkg/cart.py": (
		'"""Shopping cart."""\n\n'
		"import functools\n\n\n"
		"def helper(x: int) -> int:\n\treturn x + 1\n\n\n"
		"# The cart.\n"
		"class Cart:\n"
		"\titems: list[int]\n\n"
		"\tdef __init__(self) -> None:\n\t\tself.items = []\n\n"
		"\t@property\n\tdef total(self) -> int:\n\t\treturn sum(self.items)\n\n"
		"\tdef add(self, price: int) -> None:\n\t\tself.items.append(helper(price))\n\n\n"
		"def unused_thing() -> int:\n\treturn 0\n"
	),
	"src/pkg/shop.py": (
		"from pkg.cart import Cart, unused_thing\n\n\n"
		"def checkout(cart: Cart) -> int:\n\tcart.add(3)\n\treturn cart.total\n"
	),
	"src/pkg/empty_class.py": "class Marker:\n\tpass\n",
	"src/pkg/needs_import.py": '"""Doc."""\n\nfrom __future__ import annotations\n\n\ndef where() -> Path:\n\treturn Path(".")\n\n\ndef cwd() -> str:\n\treturn os.getcwd()\n',
}


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(tmp_path, monkeypatch, _FILES)


def _run(loop, coro):
	return loop.run_until_complete(coro)


def _valid(path) -> None:
	ast.parse(path.read_text())


def test_given_method_when_replace_symbol_with_space_indented_source_then_tabs_and_position_kept(project, loop):
	# when
	text = _run(
		loop,
		symbol_tools.replace_symbol(
			name="Cart.add",
			source="    def add(self, price: int, qty: int = 1) -> None:\n        for _ in range(qty):\n            self.items.append(helper(price))\n",
		),
	)
	# then
	cart = project / "src/pkg/cart.py"
	_valid(cart)
	body = cart.read_text()
	assert (
		"\tdef add(self, price: int, qty: int = 1) -> None:\n\t\tfor _ in range(qty):\n\t\t\tself.items.append(helper(price))\n"
		in body
	)
	assert "signature changed" in text
	assert "Applied as edit" in text and "0 new error(s)" in text
	assert body.index("def total") < body.index("def add") < body.index("def unused_thing")


def test_given_decorated_property_when_replace_without_decorator_then_dropped_decorator_reported(project, loop):
	# when
	text = _run(loop, symbol_tools.replace_symbol(name="Cart.total", source="def total(self) -> int:\n    return 0\n"))
	# then
	assert "decorators not in the new source were removed: @property" in text
	assert "new error(s)" in text and "0 new error(s)" not in text  # shop.py uses `cart.total` as a value
	_valid(project / "src/pkg/cart.py")


def test_given_signature_change_when_replace_symbol_with_gate_then_blocked_and_unwritten(project, loop):
	# when
	text = _run(
		loop,
		symbol_tools.replace_symbol(name="helper", source="def helper() -> int:\n    return 1\n", max_new_errors=0),
	)
	# then
	assert "NOT applied" in text
	assert "def helper(x: int)" in (project / "src/pkg/cart.py").read_text()


def test_given_invalid_source_when_replace_symbol_then_rejected_and_file_untouched(project, loop):
	# given
	before = (project / "src/pkg/cart.py").read_text()
	# when
	broken = _run(loop, symbol_tools.replace_symbol(name="helper", source="def helper(:\n    pass\n"))
	two = _run(loop, symbol_tools.replace_symbol(name="helper", source="def a(): ...\ndef b(): ...\n"))
	var = _run(loop, symbol_tools.replace_symbol(name="Cart.items", source="items = []\n"))
	# then
	assert "syntax error" in broken
	assert "exactly one function or class" in two
	assert "not a class, function or method" in var
	assert (project / "src/pkg/cart.py").read_text() == before


def test_given_class_when_replace_symbol_then_comment_above_is_kept_and_imports_added(project, loop):
	# when
	_run(
		loop,
		symbol_tools.replace_symbol(
			name="Cart",
			source="class Cart:\n    total_cache: Decimal | None = None\n",
			imports=["from decimal import Decimal"],
		),
	)
	# then
	body = (project / "src/pkg/cart.py").read_text()
	_valid(project / "src/pkg/cart.py")
	assert "# The cart.\nclass Cart:" in body
	assert "from decimal import Decimal" in body


def test_given_after_symbol_when_insert_symbol_then_spacing_and_indent_follow_pep8(project, loop):
	# when
	_run(
		loop,
		symbol_tools.insert_symbol(
			source="def remove(self, price: int) -> None:\n    self.items.remove(price)\n",
			file_path="src/pkg/cart.py",
			after="Cart.add",
		),
	)
	_run(
		loop,
		symbol_tools.insert_symbol(
			source="def second_helper() -> int:\n    return 2\n", file_path="src/pkg/cart.py", after="helper"
		),
	)
	# then
	body = (project / "src/pkg/cart.py").read_text()
	_valid(project / "src/pkg/cart.py")
	assert (
		"\t\tself.items.append(helper(price))\n\n\tdef remove(self, price: int) -> None:\n\t\tself.items.remove(price)\n\n\n"
		in body
	)
	assert "return x + 1\n\n\ndef second_helper() -> int:\n\treturn 2\n\n\n# The cart." in body


def test_given_into_class_when_insert_symbol_then_appended_to_class_body(project, loop):
	# when
	_run(
		loop,
		symbol_tools.insert_symbol(
			source="def clear(self) -> None:\n    self.items.clear()\n", file_path="src/pkg/cart.py", into="Cart"
		),
	)
	# then
	body = (project / "src/pkg/cart.py").read_text()
	_valid(project / "src/pkg/cart.py")
	assert body.index("def clear") < body.index("def unused_thing")
	assert "\tdef clear(self) -> None:\n\t\tself.items.clear()\n" in body


def test_given_placeholder_class_when_insert_into_then_pass_replaced(project, loop):
	# when
	_run(
		loop,
		symbol_tools.insert_symbol(
			source="def ping(self) -> str:\n    return 'pong'\n", file_path="src/pkg/empty_class.py", into="Marker"
		),
	)
	# then
	assert (
		project / "src/pkg/empty_class.py"
	).read_text() == "class Marker:\n\tdef ping(self) -> str:\n\t\treturn 'pong'\n"


def test_given_no_anchor_when_insert_symbol_then_end_of_file_and_before_works(project, loop):
	# when
	_run(loop, symbol_tools.insert_symbol(source="def last() -> int:\n    return 9\n", file_path="src/pkg/cart.py"))
	_run(
		loop,
		symbol_tools.insert_symbol(
			source="def first_method(self) -> None:\n    pass\n", file_path="src/pkg/cart.py", before="Cart.__init__"
		),
	)
	# then
	body = (project / "src/pkg/cart.py").read_text()
	_valid(project / "src/pkg/cart.py")
	assert body.rstrip().endswith("def last() -> int:\n\treturn 9")
	assert "\titems: list[int]\n\n\tdef first_method(self) -> None:\n\t\tpass\n\n\tdef __init__" in body


def test_given_existing_name_when_insert_symbol_then_refused(project, loop):
	text = _run(loop, symbol_tools.insert_symbol(source="def helper(): ...\n", file_path="src/pkg/cart.py"))
	assert "already defined in that scope" in text
	assert "no definition named 'nope'" in _run(
		loop, symbol_tools.insert_symbol(source="def z(): ...\n", file_path="src/pkg/cart.py", after="nope")
	)


def test_given_used_symbol_when_safe_delete_then_refused_with_users_listed(project, loop):
	# when
	text = _run(loop, symbol_tools.safe_delete(name="helper"))
	# then
	assert text.startswith("Not deleted: helper is still used in 1 file(s)")
	assert "cart.py" in text
	assert "def helper" in (project / "src/pkg/cart.py").read_text()


def test_given_only_imports_remaining_when_safe_delete_then_symbol_and_import_removed(project, loop):
	# when
	text = _run(loop, symbol_tools.safe_delete(name="unused_thing"))
	# then
	assert "Applied as edit" in text and "removed its import from src/pkg/shop.py" in text
	assert "unused_thing" not in (project / "src/pkg/cart.py").read_text()
	assert (project / "src/pkg/shop.py").read_text().startswith("from pkg.cart import Cart\n")
	assert (project / "src/pkg/cart.py").read_text().endswith("self.items.append(helper(price))\n")
	_valid(project / "src/pkg/cart.py")
	# and it is reversible
	_run(loop, write_tools.undo_edit())
	assert "def unused_thing" in (project / "src/pkg/cart.py").read_text()


def test_given_only_member_when_safe_delete_then_pass_left_behind(project, loop):
	# given
	(project / "src/pkg/solo.py").write_text("class Solo:\n\tdef only(self) -> int:\n\t\treturn 1\n", encoding="utf-8")
	# when
	_run(loop, symbol_tools.safe_delete(name="Solo.only"))
	# then
	assert (project / "src/pkg/solo.py").read_text() == "class Solo:\n\tpass\n"


def test_given_force_when_safe_delete_used_symbol_then_deleted_and_breakage_reported(project, loop):
	# when
	text = _run(loop, symbol_tools.safe_delete(name="helper", force=True))
	# then
	assert "Applied as edit" in text
	assert "new error(s)" in text and "0 new error(s)" not in text


def test_given_string_mention_when_safe_delete_then_held_as_preview(project, loop):
	# given
	(project / "src/pkg/dyn.py").write_text("NAME = 'unused_thing'\n", encoding="utf-8")
	# when
	text = _run(loop, symbol_tools.safe_delete(name="unused_thing"))
	# then
	assert "kept as a preview" in text and "dyn.py:1" in text
	assert "def unused_thing" in (project / "src/pkg/cart.py").read_text()


def test_given_missing_imports_when_quick_fix_then_placed_after_docstring_and_future_import(project, loop):
	# when
	text = _run(loop, symbol_tools.quick_fix("src/pkg/needs_import.py", code="unresolved-reference", choice="pathlib"))
	# then — `os` has one preferred fix, `Path` needed `choice`; both addressed? only Path matches the choice
	body = (project / "src/pkg/needs_import.py").read_text()
	_valid(project / "src/pkg/needs_import.py")
	assert "from pathlib import Path" in body
	assert body.index("from __future__") < body.index("from pathlib import Path") < body.index("def where")
	assert "Applied as edit" in text


def test_given_ambiguous_fix_when_quick_fix_without_choice_then_options_listed_and_nothing_written(project, loop):
	# given
	before = (project / "src/pkg/needs_import.py").read_text()
	# when
	text = _run(loop, symbol_tools.quick_fix("src/pkg/needs_import.py", line=6))
	# then
	assert "Several fixes apply" in text and "import pathlib.Path" in text
	assert (project / "src/pkg/needs_import.py").read_text() == before


def test_given_single_fix_when_quick_fix_then_import_added_without_choice(project, loop):
	# when
	text = _run(loop, symbol_tools.quick_fix("src/pkg/needs_import.py", line=11))
	# then
	assert "import os" in (project / "src/pkg/needs_import.py").read_text()
	assert "Applied as edit" in text


def test_given_clean_file_when_quick_fix_then_nothing_to_do(project, loop):
	assert "No diagnostics to fix" in _run(loop, symbol_tools.quick_fix("src/pkg/empty_class.py"))
