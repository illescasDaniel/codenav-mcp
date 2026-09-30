"""Fast unit tests for `codenav_mcp.server` (no live language servers)."""

from __future__ import annotations

import asyncio

import pytest
from mcp_nav_shared.errors import ToolInputError, format_tool_error
from mcp_nav_shared.lsp_client import LspRequestError

from codenav_mcp import server as codenav_server
from codenav_mcp.server import _check_python_file, _protocol_class_names


def test_given_python_file_when_check_python_file_then_no_error():
	# given / when / then — .py and .pyi are both accepted, no exception raised
	_check_python_file("src/spacemaker/bootstrap/services.py")
	_check_python_file("src/spacemaker/stubs/foo.pyi")


def test_given_non_python_file_when_check_python_file_then_tool_input_error():
	# given — a non-Python file must be rejected before it ever reaches ty,
	# which would otherwise mis-parse it as Python (e.g. diagnostics on a
	# README producing a wall of bogus syntax errors)
	# when
	with pytest.raises(ToolInputError) as caught:
		_check_python_file("README.md")
	# then
	text = format_tool_error(caught.value)
	assert text == "codenav only supports Python files (.py/.pyi), got 'README.md'"


def test_given_query_alias_when_symbol_info_then_resolves_name(monkeypatch):
	# given
	seen: list[str] = []

	class _Resolved:
		name = "start_convert"
		kind = 6
		uri = "file:///jobs.py"
		line = 296
		column = 5

	async def _fake_resolve(client, workspace, name, file_path=None):
		seen.append(name)
		return _Resolved()

	class _FakeClient:
		async def hover(self, *_a, **_k):
			return {"contents": {"value": "def start_convert"}}

		async def definition(self, *_a, **_k):
			return []

		async def references(self, *_a, **_k):
			return []

	async def _fake_get_client():
		return _FakeClient()

	monkeypatch.setattr(codenav_server, "get_client", _fake_get_client)
	monkeypatch.setattr(codenav_server, "resolve_symbol", _fake_resolve)
	monkeypatch.setattr(codenav_server, "uri_to_relative", lambda *_: "jobs.py")
	# when
	import asyncio

	result = asyncio.run(codenav_server.symbol_info(query="start_convert", include_references=False))
	# then
	assert seen == ["start_convert"]
	assert "start_convert" in result


def test_given_name_alias_when_search_symbol_then_uses_query(monkeypatch):
	# given
	seen: list[str] = []

	class _FakeClient:
		async def workspace_symbol(self, query: str) -> list:
			seen.append(query)
			return []

	async def _fake_get_client():
		return _FakeClient()

	monkeypatch.setattr(codenav_server, "get_client", _fake_get_client)
	import asyncio

	# when
	result = asyncio.run(codenav_server.search_symbol(name="start_convert"))
	# then
	assert seen == ["start_convert"]
	assert "No symbols matching" in result


def test_given_neither_when_implementations_then_actionable_error():
	import asyncio

	# when
	result = asyncio.run(codenav_server.implementations())
	# then
	assert "port_name" in result
	assert "alias" in result.lower() or "aliases" in result.lower()


def test_given_plain_protocol_base_when_protocol_class_names_then_included():
	source = "from typing import Protocol\n\nclass Port(Protocol):\n\tdef run(self) -> None: ...\n"
	assert _protocol_class_names(source) == {"Port"}


def test_given_qualified_protocol_base_when_protocol_class_names_then_included():
	source = "import typing\n\nclass Port(typing.Protocol):\n\tdef run(self) -> None: ...\n"
	assert _protocol_class_names(source) == {"Port"}


def test_given_subscripted_protocol_base_when_protocol_class_names_then_included():
	source = "from typing import Protocol\nfrom typing import TypeVar\n\nT = TypeVar('T')\n\nclass Port(Protocol[T]):\n\tpass\n"
	assert _protocol_class_names(source) == {"Port"}


def test_given_non_protocol_class_when_protocol_class_names_then_excluded():
	source = "class AppServices:\n\tdef run(self) -> None: ...\n"
	assert _protocol_class_names(source) == set()


def test_given_nested_protocol_class_when_protocol_class_names_then_found_at_any_depth():
	source = "from typing import Protocol\n\nclass Outer:\n\tclass Inner(Protocol):\n\t\tdef run(self) -> None: ...\n"
	assert _protocol_class_names(source) == {"Inner"}


def test_given_no_source_root_env_when_module_loaded_then_source_root_defaults_to_workspace_root():
	# codenav_mcp.server reads CODENAV_MCP_SOURCE_ROOT once at import time; in
	# a plain test environment (no .mcp.json-injected env) it should fall back
	# to scanning/deriving import paths against the whole workspace, not a
	# hardcoded "src" layout.
	assert codenav_server.SOURCE_ROOT == codenav_server.WORKSPACE_ROOT


def test_given_unchanged_file_when_cached_protocol_names_twice_then_parses_once(tmp_path, monkeypatch):
	# given
	src = tmp_path / "port.py"
	src.write_text("from typing import Protocol\n\nclass P(Protocol):\n\tdef f(self) -> None: ...\n", encoding="utf-8")
	codenav_server._class_infos_cache.clear()
	parsed: list[str] = []
	real = codenav_server._class_infos
	monkeypatch.setattr(codenav_server, "_class_infos", lambda text: parsed.append(text) or real(text))
	# when
	first = codenav_server._cached_protocol_class_names(src)
	second = codenav_server._cached_protocol_class_names(src)
	# then
	assert first == second == {"P"}
	assert len(parsed) == 1


def test_given_edited_file_when_cached_protocol_names_then_reparsed(tmp_path):
	# given
	src = tmp_path / "port.py"
	src.write_text("class P:\n\tpass\n", encoding="utf-8")
	codenav_server._class_infos_cache.clear()
	assert codenav_server._cached_protocol_class_names(src) == set()
	# when
	src.write_text("from typing import Protocol\n\nclass P(Protocol):\n\tpass\n", encoding="utf-8")
	# then
	assert codenav_server._cached_protocol_class_names(src) == {"P"}


def test_given_edited_source_file_when_source_signature_then_differs(tmp_path):
	# given
	a = tmp_path / "a.py"
	a.write_text("x = 1\n", encoding="utf-8")
	before = codenav_server._source_signature([a])
	# when
	a.write_text("x = 12\n", encoding="utf-8")
	# then
	assert codenav_server._source_signature([a]) != before


def test_given_site_packages_change_when_source_signature_then_differs(tmp_path, monkeypatch):
	# given — a venv whose site-packages mtime moves on `uv sync`
	site = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages"
	site.mkdir(parents=True)
	monkeypatch.setattr(codenav_server, "WORKSPACE_ROOT", tmp_path)
	before = codenav_server._source_signature([])
	# when
	(site / "newpkg").mkdir()
	import os

	os.utime(site, ns=(1, 2))
	# then
	assert codenav_server._source_signature([]) != before


def test_given_lockfile_change_when_source_signature_then_differs(tmp_path, monkeypatch):
	# given
	lock = tmp_path / "uv.lock"
	lock.write_text("a\n", encoding="utf-8")
	monkeypatch.setattr(codenav_server, "WORKSPACE_ROOT", tmp_path)
	before = codenav_server._source_signature([])
	# when
	lock.write_text("bb\n", encoding="utf-8")
	# then
	assert codenav_server._source_signature([]) != before


_INHERITANCE_SOURCE = """
from dataclasses import dataclass
from typing import Protocol


class Port(Protocol):
	name: str

	def run(self) -> None: ...


class Mixin:
	def run(self) -> None: ...


@dataclass
class Impl(Mixin):
	name: str


class Selfish:
	def __init__(self) -> None:
		self.name = "x"

	def run(self) -> None: ...
"""


def _infos_by_name(source: str) -> dict[str, list]:
	by_name: dict[str, list] = {}
	for info in codenav_server._class_infos(source):
		by_name.setdefault(info.name, []).append(info)
	return by_name


def test_given_class_body_and_self_attrs_when_class_infos_then_members_include_them():
	# when
	by_name = _infos_by_name(_INHERITANCE_SOURCE)
	# then
	assert by_name["Port"][0].members == {"name", "run"}
	assert by_name["Impl"][0].members == {"name"}
	assert by_name["Selfish"][0].members == {"name", "run"}
	assert by_name["Impl"][0].bases == ("Mixin",)


def test_given_inherited_method_when_effective_members_then_base_members_included():
	# given
	by_name = _infos_by_name(_INHERITANCE_SOURCE)
	# when
	members = codenav_server._effective_members("Impl", {"name"}, by_name)
	# then
	assert {"name", "run"} <= members


def test_given_cyclic_bases_when_effective_members_then_terminates():
	# given
	by_name = _infos_by_name("class A(B):\n\tdef a(self): ...\n\nclass B(A):\n\tdef b(self): ...\n")
	# when
	members = codenav_server._effective_members("A", {"a"}, by_name)
	# then
	assert members == {"a", "b"}


def test_given_property_and_field_symbols_when_symbol_members_then_both_counted():
	# given
	node = {
		"children": [
			{"kind": 7, "name": "prop"},
			{"kind": 8, "name": "field"},
			{"kind": 6, "name": "__init__"},
			{"kind": 6, "name": "meth"},
		]
	}
	# then
	assert codenav_server._symbol_members(node) == {"prop", "field", "meth"}


def test_given_file_path_when_implementations_then_passed_to_resolver(monkeypatch):
	import asyncio

	# given
	seen: dict[str, object] = {}

	async def _fake_client():
		return object()

	async def _fake_resolve(_client, _root, name, **kwargs):
		seen["name"] = name
		seen.update(kwargs)
		raise ToolInputError("stop here")

	monkeypatch.setattr(codenav_server, "get_client", _fake_client)
	monkeypatch.setattr(codenav_server, "resolve_symbol", _fake_resolve)
	# when
	asyncio.run(codenav_server.implementations(port_name="Port", file_path="a/b.py"))
	# then
	assert seen == {"name": "Port", "file_path": "a/b.py"}


def test_given_dead_client_when_get_client_then_stale_one_is_stopped(monkeypatch):
	import asyncio

	# given
	class _Stale:
		is_alive = False
		stopped = False

		async def stop(self) -> None:
			self.stopped = True

	class _Fresh:
		is_alive = True

		def __init__(self, **_kw) -> None:
			pass

		async def start(self) -> None:
			pass

		async def refresh(self) -> None:
			pass

	stale = _Stale()
	codenav_server._probe_cache[("a", "b", "c", "d")] = (True, "old verdict")
	monkeypatch.setattr(codenav_server, "_client", stale)
	monkeypatch.setattr(codenav_server, "LspClient", _Fresh)
	monkeypatch.setattr(codenav_server, "resolve_ty_command", lambda _root: ["ty"])
	# when
	client = asyncio.run(codenav_server.get_client())
	# then
	assert stale.stopped
	assert isinstance(client, _Fresh)
	assert codenav_server._probe_cache == {}
	monkeypatch.setattr(codenav_server, "_client", None)


class _TypeDefinitionClient:
	def __init__(self, locations):
		self.locations = locations
		self.asked = 0

	async def type_definition(self, *_a, **_k):
		self.asked += 1
		return self.locations


def _class_file(tmp_path, source: str):
	path = tmp_path / "core.py"
	path.write_text(source, encoding="utf-8")
	return {"uri": path.as_uri(), "range": {"start": {"line": 1, "character": 6}, "end": {"line": 1, "character": 11}}}


def test_given_bare_type_when_enrich_then_location_header_and_docstring_added(tmp_path, monkeypatch):
	# given
	location = _class_file(tmp_path, 'import os\nclass Widget(Base):\n\t"""A thing.\n\n\tMore detail."""\n')
	monkeypatch.setattr(codenav_server, "WORKSPACE_ROOT", tmp_path)
	client = _TypeDefinitionClient([location])
	# when
	text = asyncio.run(codenav_server._enrich_bare_type("Widget", client, "x.py", 1, 1))
	# then
	assert text == 'Widget\nType defined at core.py:2:7\n  class Widget(Base):\n  """A thing."""'


def test_given_class_without_docstring_when_enrich_then_header_only(tmp_path, monkeypatch):
	# given
	location = _class_file(tmp_path, "import os\nclass Widget:\n\tx = 1\n")
	monkeypatch.setattr(codenav_server, "WORKSPACE_ROOT", tmp_path)
	# when
	text = asyncio.run(codenav_server._enrich_bare_type("Widget", _TypeDefinitionClient([location]), "x.py", 1, 1))
	# then
	assert text == "Widget\nType defined at core.py:2:7\n  class Widget:"


@pytest.mark.parametrize(
	"hover", ["def start(self) -> None", "line one\nline two", "x" * 200, "(variable) x: int", "Return the value"]
)
def test_given_not_a_bare_type_when_enrich_then_unchanged_and_server_not_asked(hover):
	# given
	client = _TypeDefinitionClient([{"uri": "file:///x.py", "range": {}}])
	# when
	text = asyncio.run(codenav_server._enrich_bare_type(hover, client, "x.py", 1, 1))
	# then
	assert text == hover
	assert client.asked == 0


def test_given_builtin_type_without_definition_when_enrich_then_unchanged():
	# given
	client = _TypeDefinitionClient([])
	# when
	text = asyncio.run(codenav_server._enrich_bare_type("list[str] | None", client, "x.py", 1, 1))
	# then
	assert text == "list[str] | None"
	assert client.asked == 1


class _FailingTypeDefinitionClient:
	async def type_definition(self, *_a, **_k):
		raise LspRequestError("textDocument/typeDefinition", -32603, "boom")


def test_given_type_definition_request_fails_when_enrich_then_plain_hover_kept():
	# given / when
	text = asyncio.run(codenav_server._enrich_bare_type("Widget", _FailingTypeDefinitionClient(), "x.py", 1, 1))
	# then
	assert text == "Widget"


def test_given_builtin_type_when_enrich_then_typeshed_noise_skipped():
	# given
	client = _TypeDefinitionClient([{"uri": "file:///cache/stdlib/builtins.pyi", "range": {}}])
	# when
	text = asyncio.run(codenav_server._enrich_bare_type("str", client, "x.py", 1, 1))
	# then
	assert text == "str"


def test_given_union_of_two_types_when_enrich_then_every_definition_listed(tmp_path, monkeypatch):
	# given
	location = _class_file(tmp_path, "import os\nclass Widget:\n\tx = 1\n")
	monkeypatch.setattr(codenav_server, "WORKSPACE_ROOT", tmp_path)
	client = _TypeDefinitionClient([location, location])
	# when
	text = asyncio.run(codenav_server._enrich_bare_type("Widget | Gadget", client, "x.py", 1, 1))
	# then
	assert text.count("Type defined at core.py:2:7") == 2


def test_given_tools_when_listed_then_ctx_is_not_a_parameter_and_notices_wrap_every_tool():
	# given / when: the notice decorator must not hide the `Context` parameter from the framework
	tools = asyncio.run(codenav_server.mcp.list_tools())
	# then
	assert {t.name for t in tools} >= {"hover", "symbol_info", "workspace", "implementations"}
	for tool in tools:
		assert "ctx" not in tool.input_schema.get("properties", {}), tool.name
