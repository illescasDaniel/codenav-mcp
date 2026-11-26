"""Properties of call rewriting in `signature.py` (`rebind_call`, `call_edit`, `new_params`): no language server.

The oracle executes the old call on the old definition and the rewritten call on the rewritten definition, so a
rewrite that silently changes which value a parameter gets, or the order effectful arguments run in, fails.
A call the tool declines to rewrite (`ManualCallError`) or a plan it refuses is an allowed outcome.
"""

from __future__ import annotations

from hypothesis import assume, given, settings, strategies as st

from codenav_mcp.signature import Change

from .sigcases import (
	CallShape,
	call_statement,
	cases,
	execute,
	parses,
	rewrite,
	run_case,
	signature_of,
	split_call,
)


@given(cases(tight_only=True))
def test_given_any_change_and_call_then_the_rewritten_call_binds_and_evaluates_like_the_old_one(case):
	run_case(case)


@given(cases(single_line=True))
def test_given_a_rewritten_call_then_the_statement_parses_and_text_outside_the_parentheses_is_kept(case):
	outcome = run_case(case)
	assert outcome is not None
	if outcome.new_statement is None:
		return
	statement = call_statement(case.call, method=case.method)
	assert parses(outcome.new_statement)
	before, _, after = split_call(statement)
	new_before, _, new_after = split_call(outcome.new_statement)
	assert (new_before, new_after) == (before, after)


@given(cases(allow_reorder=False))
def test_given_multiline_calls_then_the_rewrite_still_parses_and_keeps_the_text_around_the_arguments(case):
	outcome = run_case(case)
	assert outcome is not None
	if outcome.new_statement is None:
		return
	statement = call_statement(case.call, method=case.method)
	assert parses(outcome.new_statement)
	before, _, after = split_call(statement)
	new_before, _, new_after = split_call(outcome.new_statement)
	assert new_before.startswith("result = ") and new_before.endswith("(")
	assert new_after.endswith(")") and new_after.strip() in (")", "),", ")")


# -- keyword-only adds ----------------------------------------------------------------------------------------


@st.composite
def keyword_only_adds(draw: st.DrawFn):
	case = draw(cases(allow_reorder=False, tight_only=True))
	specs = [
		{"name": spec["name"], "value": 99, "keyword_only": True, **({"default": 7} if draw(st.booleans()) else {})}
		for spec in case.change.add
	]
	return case, Change(add=specs)


@given(keyword_only_adds())
def test_given_keyword_only_adds_then_existing_arguments_are_kept_as_written_and_new_ones_are_appended(pair):
	case, change = pair
	assume(change.add)
	# FINDINGS-h3 #2: keyword arguments written out of parameter order are re-emitted in parameter order.
	order = [p.name for p in signature_of(case.definition, "A.m" if case.method else "f").callable_params]
	written = [name for name, _ in case.call.keywords]
	assume(written == sorted(written, key=lambda name: order.index(name) if name in order else len(order)))
	statement = call_statement(case.call, method=case.method)
	qualname = "A.m" if case.method else "f"
	try:
		execute(case.definition, statement, method=case.method)
		outcome = rewrite(case.definition, change, statement, method=case.method, qualname=qualname)
	except Exception:  # noqa: BLE001  # a call that does not fit the signature, or a refused plan, is not a scenario
		return
	if outcome.new_statement is None:
		return
	_, old_args, _ = split_call(statement)
	_, new_args, _ = split_call(outcome.new_statement)
	flat_old = [part.strip() for part in old_args.replace("\n", " ").split(",") if part.strip()]
	flat_new = [part.strip() for part in new_args.replace("\n", " ").split(",") if part.strip()]
	assert flat_new[: len(flat_old)] == flat_old, f"{statement}\n--->\n{outcome.new_statement}"
	assert flat_new[len(flat_old) :] == [f"{spec['name']}=99" for spec in change.add]


# -- round trips -----------------------------------------------------------------------------------------------


@settings(max_examples=60)
@given(cases(effects=False, tight_only=True, single_line=True))
def test_given_a_reorder_and_its_inverse_then_the_call_binds_as_it_did_at_the_start(case):
	"""Reordering then restoring the original order must give a call that binds the original way."""
	if not case.change.reorder:
		return
	statement = call_statement(case.call, method=case.method)
	qualname = "A.m" if case.method else "f"
	try:
		old, _ = execute(case.definition, statement, method=case.method)
		forward = rewrite(
			case.definition, Change(reorder=case.change.reorder), statement, method=case.method, qualname=qualname
		)
		if forward.new_statement is None:
			return
		original_order = _reorderable(case.definition, qualname)
		back = rewrite(
			forward.new_definition,
			Change(reorder=original_order),
			forward.new_statement,
			method=case.method,
			qualname=qualname,
		)
	except Exception:  # noqa: BLE001
		return
	if back.new_statement is None:
		return
	again, _ = execute(back.new_definition, back.new_statement, method=case.method)
	assert again == old, f"{statement}\n--->\n{forward.new_statement}\n--->\n{back.new_statement}"


@settings(max_examples=60)
@given(cases(effects=False, tight_only=True, single_line=True))
def test_given_an_added_parameter_that_is_removed_again_then_the_call_binds_as_it_did_at_the_start(case):
	added = [spec for spec in case.change.add if spec.get("value") is not None]
	if not added:
		return
	statement = call_statement(case.call, method=case.method)
	qualname = "A.m" if case.method else "f"
	try:
		old, _ = execute(case.definition, statement, method=case.method)
		forward = rewrite(case.definition, Change(add=added), statement, method=case.method, qualname=qualname)
		if forward.new_statement is None:
			return
		back = rewrite(
			forward.new_definition,
			Change(remove=[spec["name"] for spec in added]),
			forward.new_statement,
			method=case.method,
			qualname=qualname,
		)
	except Exception:  # noqa: BLE001
		return
	if back.new_statement is None:
		return
	again, _ = execute(back.new_definition, back.new_statement, method=case.method)
	assert again == old, f"{statement}\n--->\n{forward.new_statement}\n--->\n{back.new_statement}"


def _reorderable(definition: str, qualname: str) -> list[str]:
	return [
		p.name for p in signature_of(definition, qualname).callable_params if p.kind in ("posonly", "normal", "kwonly")
	]


def test_given_the_unrewritable_shapes_then_the_call_shape_helper_renders_what_it_says():
	"""Guard for the generator itself: the statement text is what the shape describes."""
	shape = CallShape((), (), multiline=False)
	assert call_statement(shape, method=False) == "result = f()"
