"""Unit tests of the refactor fuzzer's pure helpers (hardening/fuzz_checks.py); no language server involved."""

from __future__ import annotations

import random
from pathlib import Path

from hardening.fuzz_checks import (
	Diag,
	Symbol,
	attribute_failures,
	check_reply_format,
	discover_symbols,
	fresh_name,
	junit_test_file,
	new_diagnostics,
	parse_junit,
	parse_reply,
	parse_ty_output,
	pick_symbol,
	validate_call,
)


SCHEMAS = {"rename": {"name", "new_name", "file_path", "apply", "sites"}, "edits": {"action", "id", "sites"}}

APPLIED = """Rename f -> g (f): 2 edit(s) in 1 file(s) (+3 optional sites). Applied as edit ab12cd34.
After writing: no errors in the edited files (type check only; tests and docs not checked).
3 optional site(s), not changed (add with sites):
  u1  tests/test_a.py:10  attr  m.f()
  u2  src/pkg/b.py:4  comment  # f
Notes:
  - 7 attribute access(es) of `f` could not be checked and are not listed
next: edits(action="undo", id="ab12cd34")
"""


def test_discover_symbols_covers_every_category(tmp_path: Path) -> None:
	(tmp_path / "src").mkdir()
	(tmp_path / "src" / "m.py").write_text(
		"from dataclasses import dataclass\nfrom typing import Protocol\n\n"
		"LIMIT = 3\n__all__ = ['func']\n\n"
		"def func(): ...\n\n"
		"@dataclass\nclass Data:\n\tx_val: int = 0\n\tdef meth(self): ...\n\n"
		"class Port(Protocol):\n\tdef run(self): ...\n\n"
		"class Impl(Port):\n\tdef run(self): ...\n\n"
		"def dup(): ...\ndef dup(): ...\n",
		encoding="utf-8",
	)
	found = {(s.name, s.category) for s in discover_symbols(tmp_path, ("src",))}
	assert ("func", "function") in found
	assert ("LIMIT", "constant") in found
	assert ("Data", "class") in found
	assert ("Data.x_val", "field") in found
	assert ("Data.meth", "method") in found
	assert ("Port.run", "protocol_member") in found
	assert ("Impl.run", "method") in found
	assert not any(name in ("dup", "__all__") for name, _ in found)


def test_pick_symbol_is_seeded_and_respects_the_operation() -> None:
	symbols = [Symbol("f", "a.py", 1, "function"), Symbol("C", "a.py", 3, "class"), Symbol("C.m", "a.py", 4, "method")]
	assert pick_symbol(random.Random("x"), symbols, "rename") == pick_symbol(random.Random("x"), symbols, "rename")
	assert all(getattr(pick_symbol(random.Random(i), symbols, "move"), "name", None) == "f" for i in range(10))
	assert all(
		getattr(pick_symbol(random.Random(i), symbols, "change_signature"), "category", "") != "class"
		for i in range(10)
	)


def test_fresh_name_is_an_identifier_matching_the_style() -> None:
	assert fresh_name("Cls.method", 3).isidentifier()
	assert fresh_name("Cls", 3).startswith("ZzFuzz")
	assert fresh_name("_private", 3) == "zzfuzz3_private"


def test_parse_reply_reads_sites_and_notes() -> None:
	reply = parse_reply(APPLIED, is_error=False)
	assert reply.applied and reply.edit_id == "ab12cd34"
	assert reply.optional_sites == [("tests/test_a.py", 10, "attr"), ("src/pkg/b.py", 4, "comment")]
	assert reply.unlisted_note
	assert {"tests/test_a.py", "src/pkg/b.py"} <= reply.listed_files


def test_validate_call_accepts_real_calls_and_rejects_bad_ones() -> None:
	assert validate_call('rename(name="a", new_name="b", sites=["all"])', SCHEMAS) is None
	assert "unknown tool" in (validate_call('rename_symbol(name="a")', SCHEMAS) or "")
	assert "no parameter" in (validate_call('rename(nome="a")', SCHEMAS) or "")
	assert "parse" in (validate_call("rename(name=", SCHEMAS) or "")
	assert "literal" in (validate_call("rename(name=foo)", SCHEMAS) or "")
	assert "positional" in (validate_call('rename("a")', SCHEMAS) or "")


def test_check_reply_format_flags_malformed_replies() -> None:
	assert check_reply_format(APPLIED, False, SCHEMAS) == []
	assert check_reply_format("Traceback (most recent call last):\n  boom", False, SCHEMAS)
	assert check_reply_format('  indented first line\nnext: edits(action="list")', False, SCHEMAS)
	assert check_reply_format("Done.\nnext: frobnicate(x=1)", False, SCHEMAS)
	assert check_reply_format("No symbol found matching 'x'.", True, SCHEMAS)  # an error without a corrected call
	assert check_reply_format("Refused: cycle; pass allow_import_cycle=true to move anyway", True, SCHEMAS) == []
	assert check_reply_format('No symbol found.\nExample: `rename(name="a", new_name="b")`', True, SCHEMAS) == []


def test_ty_diff_ignores_lines_and_renamed_identifiers() -> None:
	before = parse_ty_output("a.py:3:1: error[unresolved-attribute] no attribute `old`\nb.py:1:1: warning[x] w\n")
	after = parse_ty_output(
		"a.py:9:1: error[unresolved-attribute] no attribute `new`\nc.py:2:2: error[invalid-return-type] r\n"
	)
	fresh = new_diagnostics(before, after, {"new": "old"})
	assert [d.rule for d in fresh] == ["invalid-return-type"]
	assert new_diagnostics(before, before) == []
	assert Diag("a", 1, "r", "m").key == ("r", "m")


def test_junit_and_test_file_mapping(tmp_path: Path) -> None:
	xml = tmp_path / "j.xml"
	xml.write_text(
		'<testsuites><testsuite><testcase classname="tests.test_a.TestX" name="t1"/>'
		'<testcase classname="tests.test_a.TestX" name="t2"><failure message="x"/></testcase>'
		'<testcase classname="" name="tests/test_b.py"><error message="collect"/></testcase>'
		'<testcase classname="tests.test_a" name="t3"><skipped/></testcase></testsuite></testsuites>',
		encoding="utf-8",
	)
	failed, passed, total = parse_junit(xml)
	assert failed == {("tests.test_a.TestX", "t2"), ("", "tests/test_b.py")}
	assert (passed, total) == (1, 4)
	(tmp_path / "tests").mkdir()
	(tmp_path / "tests" / "test_a.py").write_text("", encoding="utf-8")
	assert junit_test_file("tests.test_a.TestX", "t2", tmp_path) == "tests/test_a.py"
	assert junit_test_file("", "tests/test_b.py", tmp_path) == "tests/test_b.py"


def test_attribute_failures_separates_traced_unlisted_and_silent() -> None:
	reply = parse_reply(APPLIED, is_error=False)
	result = attribute_failures({"t1": "tests/test_a.py", "t2": "tests/other.py"}, reply)
	assert result.traced == ["t1"] and result.unlisted == ["t2"] and not result.ok
	quiet = parse_reply(
		'Rename f -> g: 1 edit(s) in 1 file(s). Applied as edit ab12cd34.\nnext: edits(action="undo", id="ab12cd34")',
		False,
	)
	assert attribute_failures({"t": "tests/x.py"}, quiet).silent == ["t"]
	held = parse_reply("Rename f -> g: NOT applied: 1 new error (held back).", False)
	assert held.held and attribute_failures({"t": "tests/x.py"}, held).ok


def test_check_reply_format_flags_removed_tool_names_and_non_call_retry_entries() -> None:
	stale = "'X' is not a function (found a variable); edit it with `edit` instead."
	assert any("removed tool `edit`" in p for p in check_reply_format(stale, True, SCHEMAS))
	assert not any("removed tool" in p for p in check_reply_format("Use `rename` instead.", True, SCHEMAS))
	bad = "2 symbols match 'X':\nNothing was changed. Retry with one of (other arguments unchanged):\n  [Constant] a.py:1:5\n  [Constant] a.py:9:1\n"
	assert any("retry entry" in p for p in check_reply_format(bad, True, SCHEMAS))
	good = 'Ambiguous.\nRetry with one of (other arguments unchanged):\n  rename(name="X", file_path="a.py")  [Function, a.py:1]\n'
	assert check_reply_format(good, True, SCHEMAS) == []


def test_parse_reply_detects_held_plans_and_opted_in_sites() -> None:
	held = parse_reply("Rename a -> b (a): 2 edit(s)\n\nNOT applied: 1 new error (allowed: 0).\n", is_error=True)
	assert held.held and not held.applied
	assert (
		parse_reply(
			"Rename a -> b (a): 2 edit(s) in 1 file(s) (+316 optional sites). Applied as edit ab12cd34.", False
		).optional_applied
		== 316
	)
