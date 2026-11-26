"""The v2 unused tool through MCP and the real ty language server."""

from __future__ import annotations

from textwrap import dedent

import pytest
from mcp import Client

from codenav_mcp import server as codenav_server

from .live_support import make_loop_fixture, make_workspace, needs_ty, write_files


pytestmark = [pytest.mark.integration, needs_ty]
loop = make_loop_fixture()


@pytest.fixture
def project(tmp_path, monkeypatch):
	root = make_workspace(
		tmp_path,
		monkeypatch,
		{
			"src/pkg/__init__.py": dedent("""
				from .api import public_alias as external
				__all__ = ("external",)
			"""),
			"src/pkg/api.py": dedent("""
				from .core import export_me as public_alias
			"""),
			"src/pkg/core.py": dedent("""
				from threading import Thread
				from typing import Protocol as Port
				from pytest import fixture as register_fixture
				from unittest.mock import patch

				__all__: list[str] = ["direct_export", "used_export"]
				__all__.append("append_export")

				CONSTANT = 42
				_PRIVATE_CONSTANT = 21
				USED_CONSTANT = 3
				assert USED_CONSTANT > 0

				if True:
					def conditional() -> None: pass

				def direct_export() -> None: pass
				def append_export() -> None: pass
				def export_me() -> None: pass
				def used_export() -> None: pass
				used_export()
				def test_only() -> None: pass
				def patch_only() -> None: pass
				def _private() -> None: pass
				def duplicate() -> None: pass
				def cli() -> None: pass
				def gui() -> None: pass
				def plugin() -> None: pass
				async def async_orphan() -> None: pass
				def recursive() -> None: recursive()
				def format_used() -> int: return 1
				FORMATTED = f"{format_used()}"
				patch("pkg.core.patch_only")
				# conditional is mentioned in a comment only.

				@register_fixture
				def fixture_hook() -> None: pass

				class Contract(Port):
					def contract_method(self, value: int) -> None: ...

				class Adapter:
					def contract_method(self, value: int) -> None: pass
					def unrelated(self) -> None: pass
					def __str__(self) -> str: return ""

				class Incompatible:
					def contract_method(self, value: str) -> None: pass

				class FullContract(Port):
					def inherited_method(self) -> None: ...
					def complete(self) -> None: ...

				class Mixin:
					def inherited_method(self) -> None: pass

				class CompleteAdapter(Mixin):
					def complete(self) -> None: pass

				class LibraryOverride(Thread):
					def run(self) -> None: pass
					def orphan(self) -> None: pass

				class Parent:
					def hook(self) -> None: pass

				class Child(Parent):
					def hook(self) -> None: pass
					def by_name(self) -> None: pass

				getattr(Child(), "by_name")()
			"""),
			"src/pkg/decoy.py": "def duplicate() -> None: pass\nduplicate()\n",
			"tests/test_core.py": dedent("""
				from pkg.core import test_only
				def test_entry() -> None:
					test_only()
			"""),
			".agents/tooling.py": "from pkg.core import conditional\nconditional()\n",
			"broken.py": "def broken(:\n",
		},
	)
	with (root / "pyproject.toml").open("a") as file:
		file.write(
			'\n[project.scripts]\ncli = "pkg.core:cli"\n'
			'[project.gui-scripts]\nwin = "pkg.core:gui"\n'
			'[project.entry-points."demo.plugins"]\nplugin = "pkg.core:plugin"\n'
		)
	monkeypatch.setenv("CODENAV_MCP_WORKSPACE", str(root))
	monkeypatch.setattr(codenav_server, "_selector", codenav_server.WorkspaceSelector("CODENAV_MCP_WORKSPACE"))
	return root


def _text(result):
	return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def test_unused_labels_methods_on_test_doubles_with_linked_class_uses(project, loop):
	write_files(
		project,
		{
			"tests/test_double.py": dedent("""
				class InMemoryQueue:
					def pull(self) -> int: return 1

				class UnusedQueue:
					def pull(self) -> int: return 2

				def test_with_double(monkeypatch) -> None:
					monkeypatch.setattr("pkg.core.make_queue", InMemoryQueue)
			"""),
		},
	)

	async def scenario():
		async with Client(codenav_server.mcp, mode="legacy") as client:
			return _text(await client.call_tool("unused", {"path": "tests/test_double.py", "kind": "method"}))

	output = loop.run_until_complete(scenario())
	assert "InMemoryQueue.pull (method, only in tests)" in output
	assert "UnusedQueue.pull (method, unused)" in output


def test_unused_distinguishes_linked_references_and_runtime_hooks(project, loop):
	async def scenario():
		async with Client(codenav_server.mcp, mode="legacy") as client:
			result = await client.call_tool("unused", {"limit": 100})
			assert not result.is_error
			return _text(result)

	output = loop.run_until_complete(scenario())
	for name in (
		"conditional",
		"_private",
		"duplicate",
		"async_orphan",
		"recursive",
	):
		assert f"  {name} (function, unused)" in output
	for name in ("CONSTANT", "_PRIVATE_CONSTANT"):
		assert f"  {name} (constant, unused)" in output
	for name in ("Adapter.unrelated", "Incompatible.contract_method", "LibraryOverride.orphan"):
		assert f"  {name} (method, unused)" in output
	for name in ("direct_export", "export_me", "append_export"):
		assert f"  {name} (function, exported, unused inside the project)" in output
	assert "  test_only (function, only in tests)" in output
	assert "  patch_only (function, only in strings)" in output
	assert "  Child.by_name (method, only in strings)" in output
	for name in (
		"used_export",
		"USED_CONSTANT",
		"format_used",
		"test_entry",
		"fixture_hook",
		"cli",
		"gui",
		"plugin",
		"Contract.contract_method",
		"Adapter.contract_method",
		"LibraryOverride.run",
		"Mixin.inherited_method",
		"Child.hook",
		"Adapter.__str__",
	):
		assert f"  {name} (" not in output
	assert "src/pkg/decoy.py" not in output
	assert "ty could not check" not in output
	assert "Could not scan 1 Python file(s), including broken.py" in output


def test_unused_filters_and_paginates_across_edits(project, loop):
	async def scenario():
		async with Client(codenav_server.mcp, mode="legacy") as client:
			tools = {t.name: t for t in (await client.list_tools()).tools}
			assert tools["unused"].annotations is not None
			assert tools["unused"].annotations.read_only_hint is True
			args = {"path": "src/pkg/core.py", "kind": "function", "include_private": False, "limit": 1}
			first = _text(await client.call_tool("unused", args))
			second = _text(await client.call_tool("unused", {**args, "offset": 1}))
			full = _text(await client.call_tool("unused", {**args, "limit": 100}))
			invalid = await client.call_tool("unused", {"path": ".."})
			write_files(project, {"src/pkg/consumer.py": "from pkg.core import conditional\nconditional()\n"})
			updated = _text(await client.call_tool("unused", {**args, "limit": 100}))
			return first, second, full, invalid, updated

	first, second, full, invalid, updated = loop.run_until_complete(scenario())
	assert "conditional (function, unused)" in first
	assert "duplicate (function, unused)" in second
	assert 'next: unused(path="src/pkg/core.py", kind="function", include_private=false, limit=1, offset=1)' in first
	assert "_private (" not in full
	assert "CONSTANT (" not in full
	assert "test_only (function, only in tests)" in full
	assert invalid.is_error and 'Example: unused(kind="function")' in _text(invalid)
	assert "conditional (function," not in updated


def test_unused_output_cap_preserves_the_exact_next_page(project, loop, monkeypatch):
	monkeypatch.setenv("CODENAV_MCP_MAX_OUTPUT_CHARS", "2000")
	write_files(
		project,
		{
			"src/pkg/many.py": "".join(f"def orphan_function_{i:03}(): pass\n" for i in range(100)),
		},
	)

	async def scenario():
		async with Client(codenav_server.mcp, mode="legacy") as client:
			args = {"path": "src/pkg/many.py", "limit": 100}
			first = _text(await client.call_tool("unused", args))
			offset = first.count("(function, unused)")
			second = _text(await client.call_tool("unused", {**args, "offset": offset}))
			return first, offset, second

	first, offset, second = loop.run_until_complete(scenario())
	assert 0 < offset < 100
	assert len(first) <= 2000
	assert first.endswith(f'next: unused(path="src/pkg/many.py", include_private=true, limit=100, offset={offset})')
	assert f"orphan_function_{offset:03} (function, unused)" in second


def test_unused_joins_references_across_stub_and_implementation(project, loop):
	write_files(
		project,
		{
			"src/pkg/with_stub.py": "def used(): return 1\ndef dead(): return 2\n",
			"src/pkg/with_stub.pyi": "def used() -> int: ...\ndef dead() -> int: ...\n",
			"src/pkg/user.py": "from pkg.with_stub import used\nprint(used())\n",
		},
	)

	async def scenario():
		async with Client(codenav_server.mcp, mode="legacy") as client:
			full = _text(await client.call_tool("unused", {"limit": 1000}))
			stub = _text(await client.call_tool("unused", {"path": "src/pkg/with_stub.pyi"}))
			return full, stub

	full, stub = loop.run_until_complete(scenario())
	assert "src/pkg/with_stub.py:2  dead (function, unused)" in full
	assert "src/pkg/with_stub.pyi:2  dead (function, unused)" in stub
	assert "src/pkg/with_stub.pyi:" not in full
	assert " used (function," not in full + stub
