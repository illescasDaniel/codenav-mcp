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
