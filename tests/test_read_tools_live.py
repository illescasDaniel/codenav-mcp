"""The v2 read tools over real ty data and a throwaway workspace."""

from __future__ import annotations

import pytest
from mcp_nav_shared.lsp_client import LspClient

from codenav_mcp import read_tools, server

from .live_support import make_loop_fixture, make_workspace, needs_ty


pytestmark = [pytest.mark.integration, needs_ty]
loop = make_loop_fixture()


@pytest.fixture
def project(tmp_path, monkeypatch, loop):
	return make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": "",
			"src/pkg/port.py": (
				"from abc import ABC, abstractmethod\n"
				"from dataclasses import dataclass\n"
				"from typing import Protocol\n"
				"\n"
				"@dataclass\n"
				"class Result:\n"
				"\tlines: list[str]\n"
				"\n"
				"class Port(Protocol):\n"
				"\tdef execute(self, count: int) -> Result:\n"
				'\t\t"""Return results.\n'
				"\n"
				"\t\tAdditional details.\n"
				'\t\t"""\n'
				"\n"
				"class Framework(ABC):\n"
				"\t@abstractmethod\n"
				"\tdef start(self) -> None: ...\n"
			),
			"src/pkg/app.py": (
				"from sqlite3 import Connection\n"
				"from unittest.mock import Mock\n"
				"from pkg.port import Framework, Port, Result\n"
				"\n"
				"class Service:\n"
				"\tdef execute(self, count: int) -> Result:\n"
				"\t\treturn Result(lines=[str(count)])\n"
				"\n"
				"class Other:\n"
				"\tdef execute(self, count: str) -> str:\n"
				"\t\treturn count\n"
				"\n"
				"class Child(Framework):\n"
				"\tdef start(self) -> None: pass\n"
				"\n"
				"class Grandchild(Child):\n"
				"\tdef start(self) -> None: pass\n"
				"\n"
				"def run(p: Port, service: Service, connection: Connection, mock: Mock) -> None:\n"
				"\tp.execute(1)\n"
				"\tp.execute(2)\n"
				"\tservice.execute(3)\n"
				"\tconnection.execute('SELECT 1')\n"
				"\tmock.execute(4)\n"
				'\tpatch_target = "pkg.port.Port.execute"\n'
				"\t# execute here\n"
			),
			"src/pkg/another.py": "class Another:\n\tdef execute(self) -> None: pass\n",
			"tests/__init__.py": "",
			"tests/fakes.py": (
				"from pkg.port import Result\n"
				"class Fake:\n"
				"\tdef execute(self, count: int) -> Result:\n"
				"\t\treturn Result(lines=[])\n"
			),
			"docs/usage.md": "Call execute here.\n",
		},
	)


def test_lookup_default_and_position_resolve_one_symbol(project, loop, monkeypatch):
	async def unexpected_scan(*args):
		pytest.fail("default lookup must defer the structural implementation scan")

	monkeypatch.setattr(server, "_protocol_matches", unexpected_scan)
	result = loop.run_until_complete(read_tools.lookup(name="Port.execute"))
	assert result.splitlines()[0] == "Port.execute  src/pkg/port.py:10:6"
	assert "def execute(" in result
	assert "Return results." in result and "Additional details." not in result
	assert '3 references (next: lookup(name="Port.execute", show=["references"]))' in result.splitlines()[-1]
	assert '2 direct callers (next: lookup(name="Port.execute", show=["callers"]))' in result.splitlines()[-1]
	assert 'implementations (next: lookup(name="Port.execute", show=["implementations"]))' in result.splitlines()[-1]

	from_use = loop.run_until_complete(
		read_tools.lookup(file_path="src/pkg/app.py", line=20, column=4, show=["signature"])
	)
	assert from_use.startswith("Port.execute  src/pkg/port.py:10:6\n")
	assert "Return results." not in from_use and "next:" not in from_use
	explicit_doc = loop.run_until_complete(read_tools.lookup(name="Port.execute", show=["doc"]))
	assert "Return results.\n\nAdditional details." in explicit_doc


def test_lookup_source_returns_decorated_full_definition(project, loop):
	result = loop.run_until_complete(
		read_tools.lookup(name="pkg.port.Result", file_path="src/pkg/port.py", show=["source"])
	)
	assert result.startswith("pkg.port.Result  src/pkg/port.py:6:7\n")
	assert "@dataclass\nclass Result:\n\tlines: list[str]" in result


def test_multiple_sections_paginate_and_reference_list_includes_constructor_keywords(project, loop):
	result = loop.run_until_complete(read_tools.lookup(name="Result.lines", show=["references", "mentions"], limit=1))
	assert "References (3)" in result
	assert "src/pkg/app.py:7  column 17  keyword lines=" in result
	assert 'next: lookup(name="Result.lines", show=["references"], limit=1, offset=1)' in result
	assert "Mentions (0)" in result

	later = loop.run_until_complete(read_tools.lookup(name="Result.lines", show=["references"], limit=2, offset=1))
	assert "src/pkg/port.py:7  column 2  declaration" in later
	assert "tests/fakes.py:4  column 17  keyword lines=" in later
	assert "next:" not in later


def test_protocol_callers_group_sites_and_paginate_across_groups(project, loop):
	first = loop.run_until_complete(read_tools.lookup(name="Port.execute", show=["callers"], limit=2))
	assert "Callers (3)" in first and "Through Port.execute (2)" in first
	assert "src/pkg/app.py:20  run" in first and "src/pkg/app.py:21  run" in first
	assert 'next: lookup(name="Port.execute", show=["callers"], limit=2, offset=2)' in first
	last = loop.run_until_complete(read_tools.lookup(name="Port.execute", show=["callers"], limit=2, offset=2))
	assert "To Service.execute (1)" in last and "src/pkg/app.py:22  run" in last
	assert "src/pkg/app.py:23  run" not in last and "src/pkg/app.py:24  run" not in last
	assert "next:" not in last


def test_protocol_implementations_follow_source_roots(project, loop, monkeypatch):
	primary = loop.run_until_complete(read_tools.lookup(name="Port", show=["implementations"]))
	assert "Implementations (1)" in primary and "src/pkg/app.py:5  Service" in primary
	assert "Fake" not in primary and "tests/ not scanned" in primary
	monkeypatch.setattr(server, "EXTRA_SOURCE_ROOTS", [project / "tests"])
	all_roots = loop.run_until_complete(read_tools.lookup(name="Port.execute", show=["implementations"]))
	assert "Implementations (2)" in all_roots and "src/pkg/app.py:6  Service.execute" in all_roots
	assert "tests/fakes.py:3  Fake.execute" in all_roots
	assert "Other.execute" not in all_roots


def test_nominal_hierarchy_has_transitive_subclasses_and_overrides(project, loop):
	result = loop.run_until_complete(
		read_tools.lookup(name="Framework.start", show=["subclasses", "overrides", "implementations"], limit=1)
	)
	assert "Subclasses (2)" in result and "src/pkg/app.py:13  Child" in result
	assert "Overrides (2)" in result and "src/pkg/app.py:14  Child.start" in result
	assert "Implementations (2)" in result
	for section in ("subclasses", "overrides", "implementations"):
		assert f'next: lookup(name="Framework.start", show=["{section}"], limit=1, offset=1)' in result


def test_lookup_mentions_excludes_resolved_decoys_and_flags_exact_dotted_string(project, loop):
	result = loop.run_until_complete(read_tools.lookup(name="Port.execute", show=["mentions"]))
	assert "Mentions (3)" in result
	assert "src/pkg/app.py:24  unlinked attribute" in result
	assert "src/pkg/app.py:25  exact dotted string  pkg.port.Port.execute" in result
	assert "src/pkg/app.py:26  comment" in result
	assert "src/pkg/app.py:23" not in result  # typed SQLite decoy


def test_ambiguity_returns_exact_selectors_preserving_requested_section(project, loop):
	result = loop.run_until_complete(read_tools.lookup(name="execute", show=["signature"], limit=2))
	assert "5 symbols match 'execute'." in result
	assert 'lookup(name="Another.execute", file_path="src/pkg/another.py", show=["signature"])' in result
	assert 'next: lookup(name="execute", show=["signature"], limit=2, offset=2)' in result


def test_search_symbols_supports_dotted_queries_filters_and_exact_paging(project, loop):
	dotted = loop.run_until_complete(read_tools.search(query="Port.execute", kind="method", path="src/"))
	assert "1 symbols match 'Port.execute'." in dotted
	assert "src/pkg/port.py:10  execute [Method]" in dotted
	page = loop.run_until_complete(read_tools.search(query="execute", kind="method", path="src/**/*.py", limit=2))
	assert "4 symbols match 'execute'." in page
	assert (
		'next: search(query="execute", kind="method", path="src/**/*.py", scope="symbols", limit=2, offset=2)' in page
	)
	last = loop.run_until_complete(
		read_tools.search(query="execute", kind="method", path="src/**/*.py", limit=2, offset=2)
	)
	assert "src/pkg/port.py:10" in last and "src/pkg/another.py:" not in last and "next:" not in last


def test_search_mentions_groups_string_comment_and_text_and_paginates(project, loop):
	first = loop.run_until_complete(read_tools.search(query="execute", scope="mentions", limit=2))
	assert "3 mentions of 'execute'." in first
	assert "String\nsrc/pkg/app.py:25  string  pkg.port.Port.execute" in first
	assert "Comment\nsrc/pkg/app.py:26  comment" in first
	assert 'next: search(query="execute", scope="mentions", limit=2, offset=2)' in first
	last = loop.run_until_complete(read_tools.search(query="execute", scope="mentions", limit=2, offset=2))
	assert "Text\ndocs/usage.md:1  text" in last and "next:" not in last
	scoped = loop.run_until_complete(read_tools.search(query="execute", scope="mentions", kind="text", path="src/"))
	assert scoped == "0 mentions of 'execute'."


def test_constructor_keywords_are_recovered_when_only_the_callee_is_linked(project, loop, monkeypatch):
	references = LspClient.references

	async def omit_keyword(client, file_path, line, column, **kwargs):
		hits = await references(client, file_path, line, column, **kwargs)
		if file_path == "src/pkg/port.py" and line == 7:
			return [
				hit
				for hit in hits
				if not (hit["uri"].endswith("/src/pkg/app.py") and hit["range"]["start"]["line"] == 6)
			]
		return hits

	monkeypatch.setattr(LspClient, "references", omit_keyword)
	result = loop.run_until_complete(read_tools.lookup(name="Result.lines", show=["references"]))
	assert "References (3)" in result
	assert "src/pkg/app.py:7  column 17  keyword lines=" in result
