"""QA block B12b: list reads, paging and ordering in codenav's read tools (RC-017, 043, 049, 082, 083, 084, 085, 087, 089,
142, 145, 147, 157) plus the implementations extra-root handoff. The LSP client is a fake; ty is never started.

Each test checks the new parameter or behaviour first, so it fails on the base code by assertion.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from mcp_nav_shared.resolve import ResolvedSymbol

from codenav_mcp import server


class _Client:
	"""Answers the LSP calls the read tools make; `document_symbol` may be a dict keyed by a path suffix."""

	def __init__(self, **answers: Any) -> None:
		self.answers = answers

	def _answer(self, key: str, default: Any = None) -> Any:
		return self.answers.get(key, [] if default is None else default)

	async def references(self, file_path: str, line: int, column: int, include_declaration: bool = True) -> list:
		return list(self._answer("references"))

	async def workspace_symbol(self, query: str) -> list:
		return list(self._answer("workspace_symbol"))

	async def diagnostics(self, file_path: str) -> list:
		return list(self._answer("diagnostics"))

	async def document_symbol(self, file_path: str) -> list:
		answer = self._answer("document_symbol")
		if isinstance(answer, dict):
			return next((value for key, value in answer.items() if file_path.endswith(key)), [])
		return list(answer)

	async def prepare_call_hierarchy(self, file_path: str, line: int, column: int) -> list:
		return list(self._answer("prepare", [{"name": "target"}]))

	async def incoming_calls(self, item: dict) -> list:
		return list(self._answer("incoming"))

	async def open_scratch_document(self, uri: str, text: str) -> None:
		return None

	async def change_scratch_document(self, uri: str, text: str) -> None:
		return None

	async def pull_diagnostics(self, uri: str) -> list:
		return []

	async def close_scratch_document(self, uri: str) -> None:
		return None


def _ws(tmp_path: Path, monkeypatch) -> Path:
	root = (tmp_path / "ws").resolve()
	root.mkdir()
	monkeypatch.delenv("CODENAV_MCP_EXTRA_SOURCE_ROOTS", raising=False)
	monkeypatch.setattr(server, "WORKSPACE_ROOT", root)
	monkeypatch.setattr(server, "SOURCE_ROOT", root)
	monkeypatch.setattr(server, "EXTRA_SOURCE_ROOTS", [])
	return root


def _use(monkeypatch, client: _Client) -> None:
	async def _get_client() -> _Client:
		return client

	monkeypatch.setattr(server, "get_client", _get_client)


def _write(root: Path, rel: str, text: str = "x = 1\n" * 12) -> None:
	path = root / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding="utf-8")


def _loc(root: Path, rel: str, line: int = 0) -> dict[str, Any]:
	return {
		"uri": (root / rel).as_uri(),
		"range": {"start": {"line": line, "character": 0}, "end": {"line": line, "character": 4}},
	}


def _symbol(uri: str, name: str, line: int = 0) -> dict[str, Any]:
	return {
		"name": name,
		"kind": 12,
		"location": {
			"uri": uri,
			"range": {"start": {"line": line, "character": 4}, "end": {"line": line, "character": 10}},
		},
	}


def _doc_sym(name: str, kind: int, start: int, end: int, children: list | None = None) -> dict[str, Any]:
	rng = {"start": {"line": start, "character": 0}, "end": {"line": end, "character": 0}}
	return {"name": name, "kind": kind, "range": rng, "selectionRange": rng, "children": children or []}


def _resolve_to(monkeypatch, target: ResolvedSymbol) -> None:
	async def _resolve(*args: Any, **kwargs: Any) -> ResolvedSymbol:
		return target

	monkeypatch.setattr(server, "resolve_symbol", _resolve)


def _run(coro: Any) -> str:
	"""The reply text; a refused call comes back as a CallToolResult, so its text is taken."""
	result = asyncio.run(coro)
	return result.content[0].text if hasattr(result, "content") else result


def test_rc_017_hits_outside_the_workspace_are_not_listed(tmp_path, monkeypatch):
	# given
	root = _ws(tmp_path, monkeypatch)
	_write(root, "a.py")
	outside = tmp_path / "lib" / "site.py"
	outside.parent.mkdir()
	outside.write_text("x = 1\n", encoding="utf-8")
	outside_loc = {
		"uri": outside.as_uri(),
		"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 4}},
	}
	_use(
		monkeypatch,
		_Client(references=[_loc(root, "a.py"), outside_loc], workspace_symbol=[_symbol(outside.as_uri(), "helper")]),
	)
	# when
	refs = _run(server.references(file_path="a.py", line=1, column=1))
	symbols = _run(server.search_symbol(query="helper"))
	# then
	assert "1 result(s) outside the workspace were not shown." in refs
	assert "site.py" not in refs
	assert "1 result(s) outside the workspace were not shown." in symbols
	assert "site.py" not in symbols


def test_rc_043_same_name_hits_are_listed_in_path_order(tmp_path, monkeypatch):
	# given
	root = _ws(tmp_path, monkeypatch)
	_write(root, "pkg/a.py")
	_write(root, "pkg/b.py")
	_use(
		monkeypatch,
		_Client(
			workspace_symbol=[
				_symbol((root / "pkg/b.py").as_uri(), "helper", 5),
				_symbol((root / "pkg/a.py").as_uri(), "helper", 9),
			]
		),
	)
	# when
	text = _run(server.search_symbol(query="helper"))
	# then
	assert text.index("pkg/a.py") < text.index("pkg/b.py")


def test_rc_049_references_state_the_exact_total_and_cap_the_list(tmp_path, monkeypatch):
	# given
	assert "limit" in inspect.signature(server.references).parameters
	root = _ws(tmp_path, monkeypatch)
	_write(root, "a.py")
	_use(monkeypatch, _Client(references=[_loc(root, "a.py", 0), _loc(root, "a.py", 1), _loc(root, "a.py", 2)]))
	# when
	full = _run(server.references(file_path="a.py", line=1, column=1))
	capped = _run(server.references(file_path="a.py", line=1, column=1, limit=2))
	refused = _run(server.references(file_path="a.py", line=1, column=1, limit=0))
	# then
	assert full.splitlines()[0] == "3 reference(s) in 1 file(s):"
	assert capped.splitlines()[0] == "3 reference(s) in 1 file(s):"
	assert "1 more omitted; raise limit= to see them" in capped
	assert "limit must be 1 or more" in refused


def test_rc_049_scale_references_limit_50_of_5000(tmp_path, monkeypatch):
	# given
	assert "limit" in inspect.signature(server.references).parameters
	root = _ws(tmp_path, monkeypatch)
	_write(root, "big.py", "x = 1\n" * 6000)
	_use(monkeypatch, _Client(references=[_loc(root, "big.py", i) for i in range(5000)]))
	# when
	text = _run(server.references(file_path="big.py", line=1, column=1, limit=50))
	# then
	assert text.splitlines()[0] == "5000 reference(s) in 1 file(s):"
	assert "4950 more omitted; raise limit= to see them" in text
	assert len(text) < 5000


def test_rc_082_callers_of_a_method_with_subclasses_say_static_only(tmp_path, monkeypatch):
	# given
	root = _ws(tmp_path, monkeypatch)
	_write(root, "base.py", "class Base:\n    def run(self):\n        pass\n")
	_write(root, "sub.py", "from base import Base\n\nclass Sub(Base):\n    pass\n")
	_write(root, "use.py", "from base import Base\n\ndef go(b: Base):\n    b.run()\n")
	_resolve_to(
		monkeypatch,
		ResolvedSymbol(
			name="run", kind=6, uri=(root / "base.py").as_uri(), line=1, column=8, range_start_line=1, range_end_line=2
		),
	)
	tree = [_doc_sym("Base", 5, 0, 2, [_doc_sym("run", 6, 1, 2)])]
	_use(
		monkeypatch, _Client(document_symbol=tree, prepare=[{"name": "run"}], incoming=[_call_from(root, "use.py", 2)])
	)
	# when
	text = _run(server.callers(name="Base.run"))
	# then
	assert "Static callers only; overriding methods are not included." in text


def _call_from(root: Path, rel: str, line: int) -> dict[str, Any]:
	uri = (root / rel).as_uri()
	start = {"line": line, "character": 0}
	return {
		"from": {"name": "go", "kind": 12, "uri": uri, "selectionRange": {"start": start}},
		"fromRanges": [{"start": start}],
	}


def test_rc_145_callers_of_a_value_only_function_say_so(tmp_path, monkeypatch):
	# given
	root = _ws(tmp_path, monkeypatch)
	_write(root, "m.py", "def make_key(x):\n    return x\n\nKEY = make_key\n")
	_resolve_to(
		monkeypatch,
		ResolvedSymbol(
			name="make_key",
			kind=12,
			uri=(root / "m.py").as_uri(),
			line=0,
			column=4,
			range_start_line=0,
			range_end_line=1,
		),
	)
	_use(monkeypatch, _Client(prepare=[{"name": "make_key"}], incoming=[], references=[_loc(root, "m.py", 3)]))
	# when
	text = _run(server.callers(name="make_key"))
	# then
	assert "make_key is used as a value only (not called); see references for those uses." in text


def test_rc_083_outline_of_a_file_with_a_syntax_error_warns_first(tmp_path, monkeypatch):
	# given
	root = _ws(tmp_path, monkeypatch)
	_write(root, "bad.py", "def broken(:\n    pass\n")
	diag = {
		"code": "invalid-syntax",
		"range": {"start": {"line": 0, "character": 11}, "end": {"line": 0, "character": 12}},
		"message": "Expected a parameter",
	}
	_use(monkeypatch, _Client(document_symbol=[], diagnostics=[diag]))
	# when
	text = _run(server.outline(file_path="bad.py"))
	# then
	assert text.splitlines()[0] == "Warning: bad.py has a syntax error at line 1; the outline may be incomplete."


def test_rc_089_outline_refuses_a_file_with_a_nul_byte(tmp_path, monkeypatch):
	# given
	root = _ws(tmp_path, monkeypatch)
	_write(root, "nul.py", "x = 1\x00\n")
	_use(monkeypatch, _Client(document_symbol=[_doc_sym("x", 13, 0, 0)]))
	# when
	text = _run(server.outline(file_path="nul.py"))
	# then
	assert "contains a NUL byte; not a text file" in text


def test_rc_084_absolute_path_inside_the_workspace_filters_like_a_relative_one(tmp_path, monkeypatch):
	# given
	root = _ws(tmp_path, monkeypatch)
	_write(root, "src/a.py", "def helper(): pass\n")
	_use(monkeypatch, _Client(workspace_symbol=[_symbol((root / "src/a.py").as_uri(), "helper")]))
	# when
	text = _run(server.search_symbol(query="helper", path=str(root / "src")))
	# then
	assert "helper" in text
	assert "No symbols" not in text


def test_rc_147_search_symbol_documents_path_as_prefix_or_glob(tmp_path, monkeypatch):
	# given
	doc = " ".join((server.search_symbol.__doc__ or "").split())  # docstring lines wrap
	# then
	assert "prefix" in doc
	assert "glob" in doc
	assert "`*` also matches `/`" in doc
	assert "An absolute path inside the workspace is accepted." in doc
	assert "substring" not in doc.lower()
	# and the documented glob matches a nested file
	root = _ws(tmp_path, monkeypatch)
	_write(root, "src/sub/b.py", "def helper(): pass\n")
	_use(monkeypatch, _Client(workspace_symbol=[_symbol((root / "src/sub/b.py").as_uri(), "helper")]))
	assert "helper" in _run(server.search_symbol(query="helper", path="src/**/*.py"))


def test_rc_085_fuzzy_only_result_says_there_is_no_exact_match(tmp_path, monkeypatch):
	# given
	root = _ws(tmp_path, monkeypatch)
	_write(root, "a.py", "x = 1\n")
	_use(monkeypatch, _Client(workspace_symbol=[_symbol((root / "a.py").as_uri(), "unrelated")]))
	# when
	text = _run(server.search_symbol(query="helper"))
	# then
	assert "No exact match" in text and "fuzzy=true" in text


def test_rc_087_diagnostic_lines_name_their_file(tmp_path, monkeypatch):
	# given
	root = _ws(tmp_path, monkeypatch)
	_write(root, "a.py", "x = 1\n")
	diag = {
		"range": {"start": {"line": 11, "character": 4}, "end": {"line": 11, "character": 9}},
		"severity": 1,
		"code": "unresolved-reference",
		"message": "boom",
	}
	_use(monkeypatch, _Client(diagnostics=[diag]))
	# when
	text = _run(server.diagnostics(file_path="a.py"))
	# then
	assert text.splitlines()[0].startswith("a.py:12:5 ")


def test_rc_142_test_file_is_listed_after_production_files(tmp_path, monkeypatch):
	# given
	root = _ws(tmp_path, monkeypatch)
	_write(root, "tests/test_x.py")
	_write(root, "src/x.py")
	_use(monkeypatch, _Client(references=[_loc(root, "tests/test_x.py"), _loc(root, "src/x.py")]))
	# when
	text = _run(server.references(file_path="src/x.py", line=1, column=1))
	# then
	assert text.index("src/x.py") < text.index("tests/test_x.py")


def test_rc_157_outline_depth_one_lists_only_the_top_level(tmp_path, monkeypatch):
	# given
	assert "depth" in inspect.signature(server.outline).parameters
	root = _ws(tmp_path, monkeypatch)
	_write(root, "m.py", "class Big:\n    pass\n")
	_use(monkeypatch, _Client(document_symbol=[_doc_sym("Big", 5, 0, 3, [_doc_sym("method_0", 6, 1, 1)])]))
	# when
	text = _run(server.outline(file_path="m.py", depth=1))
	# then
	assert "Big" in text
	assert "method_0" not in text


def test_rc_157_scale_5000_members_at_depth_one_is_one_line(tmp_path, monkeypatch):
	# given
	assert "depth" in inspect.signature(server.outline).parameters
	root = _ws(tmp_path, monkeypatch)
	_write(root, "m.py", "class Big:\n    pass\n")
	members = [_doc_sym(f"m{i}", 6, i + 1, i + 1) for i in range(5000)]
	_use(monkeypatch, _Client(document_symbol=[_doc_sym("Big", 5, 0, 5001, members)]))
	# when
	text = _run(server.outline(file_path="m.py", depth=1))
	# then
	assert len(text.splitlines()) == 1
	assert "Big" in text


def test_handoff_implementations_finds_a_protocol_in_an_extra_root(tmp_path, monkeypatch):
	# given
	root = _ws(tmp_path, monkeypatch)
	src = root / "src"
	monkeypatch.setattr(server, "WORKSPACE_ROOT", root)
	monkeypatch.setattr(server, "SOURCE_ROOT", src)
	monkeypatch.setattr(server, "EXTRA_SOURCE_ROOTS", [root / "tests"])
	monkeypatch.setattr(server, "_probe_cache", {})
	monkeypatch.setattr(server, "_probe_cache_signature", None)
	_write(
		root,
		"tests/ports.py",
		"from typing import Protocol\n\n\nclass Store(Protocol):\n    def save(self, item: str) -> None: ...\n",
	)
	_write(root, "src/adapters.py", "class FileStore:\n    def save(self, item: str) -> None:\n        pass\n")
	port_tree = {"tests/ports.py": [_doc_sym("Store", 5, 3, 4, [_doc_sym("save", 6, 4, 4)])]}
	adapter_tree = {"src/adapters.py": [_doc_sym("FileStore", 5, 0, 2, [_doc_sym("save", 6, 1, 2)])]}
	_resolve_to(
		monkeypatch,
		ResolvedSymbol(
			name="Store",
			kind=5,
			uri=(root / "tests/ports.py").as_uri(),
			line=3,
			column=6,
			range_start_line=3,
			range_end_line=4,
		),
	)
	_use(monkeypatch, _Client(document_symbol={**port_tree, **adapter_tree}))
	# when
	text = _run(server.implementations(port_name="Store"))
	# then
	assert "outside the source root" not in text
	assert "FileStore" in text
