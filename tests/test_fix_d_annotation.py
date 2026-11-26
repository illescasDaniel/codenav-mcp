"""D3 (F1): an added parameter without an annotation is typed from its literal default, or the gap is stated."""

from __future__ import annotations

import pytest

from codenav_mcp import refactor_tools
from codenav_mcp.signature import Change, infer_annotations

from .live_support import make_loop_fixture, make_workspace, needs_ty


@pytest.mark.parametrize(
	("spec", "annotation"),
	[
		({"name": "a", "default": "False"}, "bool"),
		({"name": "a", "default": False}, "bool"),
		({"name": "a", "default": "0"}, "int"),
		({"name": "a", "default": "-3"}, "int"),
		({"name": "a", "default": "1.5"}, "float"),
		({"name": "a", "default": "'x'"}, "str"),
		({"name": "a", "default": "None", "value": "'x'"}, "str | None"),
		({"name": "a", "default": "None", "value": "3"}, "int | None"),
		({"name": "a", "value": "True"}, "bool"),
		({"name": "a", "default": "False", "annotation": "Flag"}, "Flag"),
	],
)
def test_given_a_literal_then_the_annotation_is_inferred(spec, annotation):
	change = Change(add=[spec])
	infer_annotations(change)
	assert change.add[0]["annotation"] == annotation


@pytest.mark.parametrize(
	"spec",
	[{"name": "a", "default": "None"}, {"name": "a", "default": "DEFAULT"}, {"name": "a", "default": "[]"}],
)
def test_given_no_inferable_type_then_it_stays_unannotated_and_one_note_says_so(spec):
	change = Change(add=[spec, {"name": "b", "default": "1"}])
	notes = infer_annotations(change)
	assert "annotation" not in change.add[0]
	assert any("without a type annotation" in note and "a" in note for note in notes)
	assert len([n for n in notes if "without a type annotation" in n]) == 1


loop = make_loop_fixture()


@pytest.mark.integration
@needs_ty
def test_given_change_signature_add_with_a_bool_default_then_the_definition_is_typed(tmp_path, monkeypatch, loop):
	project = make_workspace(
		tmp_path, monkeypatch, {"src/pkg/__init__.py": "", "src/pkg/m.py": "def f(a: int) -> int:\n\treturn a\n"}
	)
	text = loop.run_until_complete(
		refactor_tools.change_signature(
			name="f",
			add=[{"name": "strict", "default": "False"}, {"name": "tag", "default": "DEFAULT"}],
			apply=False,
			max_new_errors=None,
		)
	)
	assert "+\tstrict: bool = False" in text or "strict: bool = False" in text
	assert "tag=DEFAULT" in text and "without a type annotation" in text
	assert "strict: bool" not in (project / "src/pkg/m.py").read_text()  # a preview writes nothing
