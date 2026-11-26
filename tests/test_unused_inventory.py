import asyncio
from types import SimpleNamespace

from codenav_mcp import unused
from codenav_mcp.unused import inventory


def test_inventory_counts_identifiers_and_top_level_candidates(tmp_path):
	(tmp_path / "example.py").write_text(
		"LIMIT = 1\nclass Example:\n    def method(self):\n        return LIMIT\n"
		'def outer():\n    def nested(): pass\n    return "LIMIT"\n',
		encoding="utf-8",
	)
	candidates, counts = inventory(tmp_path)
	assert [(c.name, c.kind) for c in candidates] == [
		("LIMIT", "constant"),
		("Example", "class"),
		("method", "method"),
		("outer", "function"),
	]
	assert counts["LIMIT"] == 2


def test_inventory_uses_identifier_positions_and_counts_f_string_references(tmp_path):
	(tmp_path / "example.py").write_text(
		'async def résumé(): pass\nclass Example:\n\tdef method(self): return f"{résumé()}"\n',
		encoding="utf-8",
	)
	candidates, counts = inventory(tmp_path)
	assert [(c.qualname, c.position) for c in candidates] == [
		("résumé", (1, 11)),
		("Example", (2, 7)),
		("Example.method", (3, 6)),
	]
	assert counts["résumé"] >= 2


def test_find_unused_skips_ty_for_unique_tokens_and_reports_failures(tmp_path, monkeypatch):
	(tmp_path / "example.py").write_text("def singleton(): pass\ndef repeated(): return repeated()\n")
	calls = []

	async def references(path, line, column):
		calls.append((path, line, column))
		raise OSError("language server unavailable")

	monkeypatch.setattr(unused, "conforms_probe", lambda session: None)
	session = SimpleNamespace(workspace=tmp_path, roots=[tmp_path], client=SimpleNamespace(references=references))
	findings, notes = asyncio.run(unused.find_unused(session, scope=tmp_path, kind=None, include_private=True))
	assert calls == [("example.py", 2, 5)]
	assert [finding.candidate.name for finding in findings] == ["singleton"]
	assert notes == ["ty could not check 1 candidate(s), including example.py:2."]
