"""Fast unit tests for `codenav_mcp.signature` (no language server)."""

from __future__ import annotations

import ast

import pytest
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp.pysource import definitions_in
from codenav_mcp.signature import (
	Change,
	ManualCallError,
	Param,
	SourceMap,
	call_edit,
	find_call_at,
	new_params,
	paren_span,
	read_call_args,
	read_signature,
	rebind_call,
	render_params,
	tokenize_source,
)


def _sig(text: str, qualname: str):
	smap = SourceMap(text)
	tokens = tokenize_source(text)
	definition = next(d for d in definitions_in(text) if d.qualname == qualname)
	return read_signature(smap, tokens, definition), definition


def test_given_complex_signature_when_read_then_kinds_defaults_and_annotations_kept():
	# given
	text = "def f(a, /, b: int = 1, *rest, c: str = 'x', d, **kw) -> None:\n\tpass\n"
	# when
	signature, _ = _sig(text, "f")
	# then
	assert [(p.name, p.kind, p.annotation, p.default) for p in signature.params] == [
		("a", "posonly", None, None),
		("b", "normal", "int", "1"),
		("rest", "vararg", None, None),
		("c", "kwonly", "str", "'x'"),
		("d", "kwonly", None, None),
		("kw", "kwarg", None, None),
	]
	assert not signature.has_self and not signature.multiline


def test_given_method_when_read_then_self_is_receiver_and_staticmethod_has_none():
	# given
	text = "class A:\n\tdef m(self, x): ...\n\t@staticmethod\n\tdef s(x): ...\n\t@classmethod\n\tdef c(cls, x): ...\n"
	# when / then
	assert _sig(text, "A.m")[0].has_self and [p.name for p in _sig(text, "A.m")[0].callable_params] == ["x"]
	assert not _sig(text, "A.s")[0].has_self
	assert _sig(text, "A.c")[0].has_self


def test_given_multiline_def_when_render_then_one_param_per_line_with_trailing_comma():
	# given
	text = "def f(\n\ta: int,\n\tb: int = 2,\n) -> int:\n\treturn a\n"
	signature, definition = _sig(text, "f")
	# when
	rendered = render_params(
		[*signature.params, Param("c", "normal", "int", "3")], multiline=signature.multiline, indent="", unit="\t"
	)
	# then
	assert rendered == "\n\ta: int,\n\tb: int = 2,\n\tc: int = 3,\n"
	new_text = text[: signature.span[0]] + rendered + text[signature.span[1] :]
	ast.parse(new_text)


def test_given_keyword_only_params_when_render_then_bare_star_inserted():
	# given / when
	text = render_params([Param("a", "normal"), Param("b", "kwonly", None, "1")], multiline=False, indent="", unit="\t")
	# then
	assert text == "a, *, b=1"


def test_given_add_remove_when_new_params_then_values_removed_and_order(tmp_path):
	# given
	signature, _ = _sig("def f(a, b, c=1, **kw): ...\n", "f")
	change = Change(add=[{"name": "d", "default": "2", "value": "9"}], remove=["b"])
	# when
	params, values, removed = new_params(signature, change, "f")
	# then
	assert [p.name for p in params] == ["a", "c", "d", "kw"]
	assert values == {"d": "9"} and removed == {"b"}


def test_given_add_at_position_and_keyword_only_when_new_params_then_placed():
	# given
	signature, _ = _sig("def f(a, b): ...\n", "f")
	# when
	middle, _, _ = new_params(signature, Change(add=[{"name": "x", "default": "0", "position": 1}]), "f")
	kwonly, _, _ = new_params(signature, Change(add=[{"name": "y", "default": "0", "keyword_only": True}]), "f")
	# then
	assert [p.name for p in middle] == ["a", "x", "b"]
	assert [(p.name, p.kind) for p in kwonly] == [("a", "normal"), ("b", "normal"), ("y", "kwonly")]


@pytest.mark.parametrize(
	("change", "message"),
	[
		(Change(remove=["zzz"]), "no parameter 'zzz'"),
		(Change(add=[{"name": "a", "default": "1"}]), "already has a parameter"),
		(Change(add=[{"name": "x"}]), "give a `default`"),
		(Change(add=[{"name": "not valid", "default": "1"}]), "not a valid parameter name"),
		(Change(reorder=["a"]), "must list exactly"),
		(Change(add=[{"name": "x", "default": "1", "position": 9}]), "out of range"),
	],
)
def test_given_bad_change_when_new_params_then_input_error(change, message):
	# given
	signature, _ = _sig("def f(a, b): ...\n", "f")
	# when / then
	with pytest.raises(ToolInputError, match=message):
		new_params(signature, change, "f")


def _args(call_text: str):
	smap = SourceMap(call_text)
	call = ast.parse(call_text).body[0].value  # type: ignore[attr-defined]
	return smap, call, read_call_args(smap, call)


def _rebind(call_text: str, def_text: str, change: Change, skip: int = 0) -> str:
	signature, _ = _sig(def_text, "f")
	params, values, removed = new_params(signature, change, "f")
	smap, call, args = _args(call_text)
	new_args = rebind_call(args, signature.callable_params, params, values, removed, skip_leading=skip)
	edit = call_edit(smap, tokenize_source(call_text), call, args, new_args)
	if edit is None:
		return call_text
	return call_text[: edit[0]] + edit[2] + call_text[edit[1] :]


def test_given_added_param_with_value_when_rebind_then_keyword_appended():
	# given / when
	result = _rebind("f(1, 2)", "def f(a, b): ...\n", Change(add=[{"name": "c", "default": "0", "value": "7"}]))
	# then
	assert result == "f(1, 2, c=7)"


def test_given_added_param_with_default_only_when_rebind_then_call_untouched():
	assert _rebind("f(1, 2)", "def f(a, b): ...\n", Change(add=[{"name": "c", "default": "0"}])) == "f(1, 2)"


def test_given_param_inserted_in_the_middle_when_rebind_then_later_positionals_become_keywords():
	# given / when
	result = _rebind("f(1, 2)", "def f(a, b): ...\n", Change(add=[{"name": "x", "default": "0", "position": 1}]))
	# then
	assert result == "f(1, b=2)"


def test_given_removed_param_when_rebind_then_argument_dropped_positional_and_keyword():
	# given / when / then
	assert _rebind("f(1, 2, 3)", "def f(a, b, c): ...\n", Change(remove=["b"])) == "f(1, 3)"
	assert _rebind("f(1, c=3, b=2)", "def f(a, b, c): ...\n", Change(remove=["b"])) == "f(1, c=3)"
	assert _rebind("f(1, 2)", "def f(a, b): ...\n", Change(remove=["b"])) == "f(1)"
	assert _rebind("f(1)", "def f(a): ...\n", Change(remove=["a"])) == "f()"


def test_given_reorder_when_rebind_then_positional_arguments_swapped():
	# given / when / then
	assert _rebind("f(1, 2)", "def f(a, b): ...\n", Change(reorder=["b", "a"])) == "f(2, 1)"
	assert _rebind("f(1, b=2)", "def f(a, b): ...\n", Change(reorder=["b", "a"])) == "f(b=2, a=1)"


def test_given_unbound_method_call_when_rebind_with_skip_then_receiver_left_alone():
	# given / when
	result = _rebind("A.f(obj, 1, 2)", "def f(a, b): ...\n", Change(remove=["a"]), skip=1)
	# then
	assert result == "A.f(obj, 2)"


def test_given_starred_or_shifting_extras_when_rebind_then_manual_error():
	# given
	signature, _ = _sig("def f(a, *rest): ...\n", "f")
	change = Change(add=[{"name": "x", "default": "0", "position": 0}])
	params, values, removed = new_params(signature, change, "f")
	smap, call, args = _args("f(1, 2, 3)")
	# when / then
	with pytest.raises(ManualCallError, match="shift"):
		rebind_call(args, signature.callable_params, params, values, removed)
	_, _, starred = _args("f(*xs)")
	with pytest.raises(ManualCallError, match="unpacking"):
		rebind_call(starred, signature.callable_params, params, values, removed)


def test_given_multiline_call_when_append_then_one_argument_per_line_style_kept():
	# given
	call_text = "f(\n\t1,\n\t2,\n)"
	# when
	result = _rebind(call_text, "def f(a, b): ...\n", Change(add=[{"name": "c", "default": "0", "value": "3"}]))
	# then
	assert result == "f(\n\t1,\n\t2, c=3,\n)" or result == "f(\n\t1,\n\t2,\n\tc=3,\n)"
	ast.parse(result)


def test_given_multiline_call_when_remove_middle_then_remaining_lines_realigned():
	# given
	call_text = "f(\n\t1,\n\t2,\n\t3,\n)"
	# when
	result = _rebind(call_text, "def f(a, b, c): ...\n", Change(remove=["b"]))
	# then
	assert result == "f(\n\t1,\n\t3,\n)"


def test_given_call_positions_when_find_call_at_then_matches_callee_end_in_utf16():
	# given — a non-BMP character before the call makes UTF-16 and code point columns differ
	text = "x = '\U0001f600'; y = obj.run(1)\n"
	smap = SourceMap(text)
	tree = ast.parse(text)
	call = tree.body[1].value  # type: ignore[attr-defined]
	end_line, end_col = smap.lsp_end(call.func)
	# when
	found = find_call_at(tree, smap, (end_line, end_col))
	# then
	assert found is call
	assert end_col == text.index("run") + 3 + 1  # +1: the emoji is two UTF-16 units but one character


def test_given_def_with_type_params_when_paren_span_then_skips_brackets():
	# given
	text = "def f[T](a: T) -> T:\n\treturn a\n"
	smap = SourceMap(text)
	# when
	span = paren_span(smap, tokenize_source(text), 0)
	# then
	assert text[span[0] : span[1]] == "a: T"
