"""Properties of argument alias resolution (`args.py`): idempotent, conflicts refused, nothing silently dropped."""

from __future__ import annotations

import inspect

import pytest
from hypothesis import assume, given, strategies as st

from codenav_mcp import args as args_module
from codenav_mcp.args import apply_aliases, argument_problem, resolve_aliases


ALIASES = args_module._ARGUMENT_ALIASES
TARGETS = sorted({target for targets in ALIASES.values() for target in targets})
OTHERS = ["limit", "depth", "show", "junk", "other_junk"]
UNIVERSE = sorted(set(ALIASES) | set(TARGETS) | set(OTHERS))
values = st.none() | st.sampled_from(["a", "b", "c"]) | st.integers(0, 3)
param_sets = st.sets(st.sampled_from(UNIVERSE), min_size=1, max_size=8)
arg_dicts = st.dictionaries(st.sampled_from(UNIVERSE), values, max_size=6)


def target_of(key: str, params: set[str]) -> str:
	"""Where `key` ends up: itself when the tool has it, else the first alias target the tool has."""
	if key in params:
		return key
	return next((t for t in ALIASES.get(key, ()) if t in params), key)


@given(param_sets, arg_dicts)
def test_given_resolved_arguments_then_resolving_again_changes_nothing(params, args):
	result, problem = resolve_aliases("t", params, args)
	assume(problem is None)
	again, problem_again = resolve_aliases("t", params, result)
	assert problem_again is None
	assert again == result


@given(param_sets, arg_dicts)
def test_given_any_arguments_then_every_key_is_kept_renamed_to_a_real_parameter_or_refused(params, args):
	result, problem = resolve_aliases("t", params, args)
	if problem is not None:
		assert result == args, "a refusal must hand the arguments back untouched"
		return
	assert set(result) == {target_of(key, params) for key in args}, "a key was lost or invented"
	for key in args:
		if key in params or target_of(key, params) == key:
			continue
		assert target_of(key, params) in params and key not in result
	# values: nothing but an explicit null may vanish
	for key, value in args.items():
		if value is not None:
			assert result[target_of(key, params)] == value or result[target_of(key, params)] is not None


@st.composite
def alias_in_play(draw: st.DrawFn) -> tuple[set[str], str, str]:
	"""(parameters, an alias the tool lacks, the target it resolves to there)."""
	alias = draw(st.sampled_from(sorted(ALIASES)))
	target = draw(st.sampled_from(sorted(ALIASES[alias])))
	extra = draw(st.sets(st.sampled_from([name for name in UNIVERSE if name != alias]), max_size=5))
	params = extra | {target}
	assume(alias not in params and target_of(alias, params) == target)
	return params, alias, target


@given(alias_in_play(), st.sampled_from(["a", "b", "c"]), st.sampled_from(["a", "b", "c"]), st.booleans())
def test_given_an_alias_and_its_target_with_different_values_then_it_is_refused_in_either_order(
	play, first, second, flip
):
	params, alias, target = play
	assume(first != second)
	args = {alias: first, target: second}
	if flip:
		args = dict(reversed(args.items()))
	result, problem = resolve_aliases("t", params, args)
	assert problem is not None and repr(alias) in problem and repr(target) in problem
	assert result == args


@given(alias_in_play(), st.sampled_from(["a", "b", "c"]), st.booleans())
def test_given_an_alias_and_its_target_with_the_same_value_or_a_null_then_they_merge(play, value, flip):
	params, alias, target = play
	args = {alias: value, target: value}
	if flip:
		args = dict(reversed(args.items()))
	assert resolve_aliases("t", params, args) == ({target: value}, None)
	nulled = {alias: None, target: value} if not flip else {target: value, alias: None}
	assert resolve_aliases("t", params, nulled) == ({target: value}, None)


@given(st.sampled_from(sorted(ALIASES)), param_sets)
def test_given_a_tool_that_has_the_alias_as_its_own_parameter_then_the_alias_is_left_alone(alias, params):
	params = params | {alias}
	assert resolve_aliases("t", params, {alias: "v"}) == ({alias: "v"}, None)


async def _fake_tool(
	name: str | None = None,
	file_path: str | None = None,
	line: int | None = None,
	scope: str | None = None,
	limit: int = 10,
) -> str:
	"""A tool with a few real parameters. Example: `fake(name="x")`."""
	return ""


@given(
	st.dictionaries(
		st.sampled_from([*sorted(ALIASES), "name", "file_path", "line", "scope", "limit", "bogus", "bogus2"]),
		st.sampled_from(["a", "b", 1, 2, None]),
		max_size=5,
	)
)
def test_given_a_tool_then_a_key_it_cannot_take_is_never_silently_dropped(args):
	resolved, problem = apply_aliases("fake", _fake_tool, args)
	if problem is not None:
		return
	params = set(inspect.signature(_fake_tool).parameters)
	unknown = [key for key in resolved if key not in params]
	refusal = argument_problem("fake", _fake_tool, resolved)
	if unknown:
		assert refusal is not None and "unknown argument" in refusal
		assert all(repr(key) in refusal or key[:5] in refusal for key in unknown[:5])
	# every key the caller gave either survives (maybe renamed) or merged with an equal alias; none vanishes
	assert len(resolved) >= len({target_of(key, params) for key in args}) - 0
	assert {target_of(key, params) for key in args} == set(resolved)


@pytest.mark.xfail(strict=True, reason="FINDINGS-h3 #7")
def test_given_an_alias_true_and_its_target_one_then_they_are_a_conflict_not_a_merge():
	resolved, problem = resolve_aliases("t", {"line"}, {"line": 1, "lineno": True})
	assert problem is not None, resolved
