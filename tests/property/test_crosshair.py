"""The pure core properties again, under the CrossHair backend (symbolic execution through Hypothesis).

CrossHair explores values with an SMT solver instead of sampling, so it can prove small properties (for a bounded
input size) rather than hope to hit them. It cannot run code that is not symbolic-friendly (file I/O, the language
server, `ast` over symbolic text), so this module covers only the pure functions: `reply.paginate`,
`signature.apply_offset_edits` and `args.resolve_aliases`.

Run: `uv run pytest -m crosshair tests/property/test_crosshair.py --timeout=0` (see docs/hardening.md).
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings, strategies as st
from mcp_nav_shared.errors import ToolInputError

from codenav_mcp.args import resolve_aliases
from codenav_mcp.reply import paginate
from codenav_mcp.signature import apply_offset_edits

from .sigcases import cases, run_case


pytestmark = pytest.mark.crosshair

pytest.importorskip("hypothesis_crosshair_provider")

CROSSHAIR = settings(backend="crosshair", max_examples=60, deadline=None, database=None, derandomize=True)


@CROSSHAIR
@given(st.lists(st.integers(), max_size=5), st.integers(1, 6), st.integers(0, 7))
def test_given_a_list_then_a_page_is_the_slice_and_more_counts_what_is_left(items, limit, offset):
	page = paginate(items, limit, offset)
	assert page.items == items[offset : offset + limit]
	assert page.total == len(items)
	assert page.more == max(0, len(items) - offset - len(page.items))
	assert len(page.items) <= limit
	assert page.offset == offset


@CROSSHAIR
@given(st.lists(st.integers(), max_size=5), st.integers(1, 4))
def test_given_pages_taken_one_after_the_other_then_they_partition_the_list(items, limit):
	seen: list[int] = []
	offset = 0
	while True:
		page = paginate(items, limit, offset)
		seen += page.items
		if not page.more:
			break
		assert page.items, "a page with more to come cannot be empty"
		offset += len(page.items)
	assert seen == items


@CROSSHAIR
@given(
	st.text(alphabet="abc", max_size=6),
	st.lists(st.tuples(st.integers(0, 6), st.integers(0, 6), st.text(alphabet="XY", max_size=2)), max_size=3),
)
def test_given_offset_edits_then_the_result_is_the_text_with_each_span_replaced_or_the_edits_overlap(text, raw):
	edits = [(min(a, b, len(text)), min(max(a, b), len(text)), new) for a, b, new in raw]
	try:
		result = apply_offset_edits(text, edits)
	except ToolInputError:
		spans = sorted((s, e) for s, e, _ in edits)
		assert any(
			later_start < earlier_end for (_, earlier_end), (later_start, _) in zip(spans, spans[1:], strict=False)
		)
		return
	# without overlaps the length changes by exactly the sum of (len(new) - len(old))
	assert len(result) == len(text) + sum(len(new) - (end - start) for start, end, new in edits)
	for start, end, _ in edits:
		assert start <= end


@CROSSHAIR
@given(
	st.lists(st.tuples(st.sampled_from(["file", "path", "file_path", "symbol", "name"]), st.integers(0, 2)), max_size=4)
)
def test_given_aliased_arguments_then_every_value_survives_under_a_real_parameter_or_the_call_is_refused(pairs):
	params = {"file_path", "name"}
	args = dict(pairs)
	result, problem = resolve_aliases("tool", params, args)
	if problem is not None:
		assert result == args
		return
	assert set(result) <= params, "every alias used here has its target among the parameters"
	assert set(result.values()) <= set(args.values())
	real = {"file": "file_path", "path": "file_path", "symbol": "name"}
	assert len(result) == len({real.get(k, k) for k in args})


@settings(backend="crosshair", max_examples=20, deadline=None, database=None, derandomize=True)
@given(cases(tight_only=True))
def test_given_any_change_and_call_then_the_rewritten_call_binds_like_the_old_one_under_crosshair(case):
	"""The key call-rewriting property. `ast.parse`/`exec` concretize the symbolic values, so this is guided sampling."""
	run_case(case)
