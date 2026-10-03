"""`edit`, `edit_symbol` and `move` reject parameter combinations before touching anything."""

from __future__ import annotations

import asyncio

import pytest

from codenav_mcp import edit_tools


def _run(coro):
	return asyncio.run(coro)


@pytest.mark.parametrize(
	("kwargs", "message"),
	[
		({"action": "replace", "name": "f"}, "needs `name` and `source`"),
		({"action": "replace", "name": "f", "source": "def f(): ...", "position": "after"}, "`position` do not apply"),
		({"action": "insert", "source": "def f(): ..."}, "needs `source` and `file_path`"),
		({"action": "insert", "source": "def f(): ...", "file_path": "a.py", "position": "after"}, "needs `name`"),
		(
			{"action": "insert", "source": "def f(): ...", "file_path": "a.py", "name": "g", "position": "end"},
			"drop `name`",
		),
		({"action": "delete"}, "needs `name`"),
		({"action": "delete", "name": "f", "source": "def f(): ..."}, "`source` do not apply"),
	],
)
def test_given_inconsistent_edit_symbol_params_when_called_then_error_names_the_problem(kwargs, message):
	# when
	text = _run(edit_tools.edit_symbol(**kwargs))
	# then
	assert message in text


def test_given_neither_name_nor_file_when_move_then_error():
	assert "pass `name`" in _run(edit_tools.move(to_file="b.py"))


def test_given_module_move_with_keep_reexport_when_move_then_error():
	assert "only applies when moving a symbol" in _run(
		edit_tools.move(to_file="b.py", file_path="a.py", keep_reexport=True)
	)
