"""Live smoke: ty workspace/symbol → name column usable for hover.

Skipped when `ty` cannot be resolved. Marked integration so default fast
unit runs can exclude it via `-m 'not integration'` if desired.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from mcp_nav_shared.format import format_workspace_symbol, workspace_symbol_position
from mcp_nav_shared.lsp_client import LspClient

from codenav_mcp.ty_command import resolve_ty_command


pytestmark = pytest.mark.integration

# tests/test_ty_search_smoke.py -> repo root, where the .venv (and its `ty` binary) lives.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _ty_available(workspace: Path) -> bool:
	cmd = resolve_ty_command(workspace)
	if cmd[0] in {"uv", "ty"} or cmd[0].endswith(("ty", "ty.exe")):
		if cmd[0] == "uv":
			return shutil.which("uv") is not None
		if cmd[0] == "ty":
			return shutil.which("ty") is not None
		return Path(cmd[0]).is_file()
	return shutil.which(cmd[0]) is not None


@pytest.mark.skipif(
	not _ty_available(_REPO_ROOT),
	reason="ty not available for live LSP smoke",
)
def test_given_ty_workspace_when_search_class_then_name_column_hovers(tmp_path):
	# given — tiny workspace so indexing stays fast
	mod = tmp_path / "demo.py"
	mod.write_text("class SmokeTarget:\n\tpass\n", encoding="utf-8")
	client = LspClient(
		workspace_root=tmp_path,
		command=resolve_ty_command(_REPO_ROOT),
		language_id="python",
	)

	async def _run() -> None:
		await client.start()
		try:
			await client.ensure_open(str(mod))
			# when
			symbols = await client.workspace_symbol("SmokeTarget")
			assert symbols, "ty returned no symbols for SmokeTarget"
			sym = next(s for s in symbols if s.get("name") == "SmokeTarget")
			formatted = format_workspace_symbol(sym, tmp_path)
			_uri, line, col = workspace_symbol_position(sym)
			# then — column must be on the name (not `class` at col 0)
			assert "[Class]" in formatted or "[Struct]" in formatted or "[" in formatted
			assert f"demo.py:{line + 1}:{col + 1}" in formatted
			assert col == 6, f"expected name column 6, got {col} from {sym!r}"
			hover = await client.hover(str(mod), line + 1, col + 1)
			assert hover, f"hover empty at name column; symbol was {sym!r}"
		finally:
			await client.stop()

	asyncio.run(_run())


@pytest.mark.skipif(
	not _ty_available(_REPO_ROOT),
	reason="ty not available for live LSP smoke",
)
def test_given_ty_decorated_class_when_search_then_name_column_hovers(tmp_path):
	# given — decorator line is where ty often places SymbolInformation.start
	mod = tmp_path / "demo.py"
	mod.write_text(
		"from dataclasses import dataclass\n\n@dataclass\nclass SmokeDecorated:\n\tpass\n",
		encoding="utf-8",
	)
	client = LspClient(
		workspace_root=tmp_path,
		command=resolve_ty_command(_REPO_ROOT),
		language_id="python",
	)

	async def _run() -> None:
		await client.start()
		try:
			await client.ensure_open(str(mod))
			# when
			symbols = await client.workspace_symbol("SmokeDecorated")
			assert symbols, "ty returned no symbols for SmokeDecorated"
			sym = next(s for s in symbols if s.get("name") == "SmokeDecorated")
			formatted = format_workspace_symbol(sym, tmp_path)
			_uri, line, col = workspace_symbol_position(sym)
			# then — not the `@dataclass` line; name column on `class SmokeDecorated`
			assert f"demo.py:{line + 1}:{col + 1}" in formatted
			assert line >= 2, f"expected class line, got line {line} from {sym!r}"
			assert col == 6, f"expected name column 6, got {col} from {sym!r}"
			hover = await client.hover(str(mod), line + 1, col + 1)
			assert hover, f"hover empty at name column; symbol was {sym!r}"
		finally:
			await client.stop()

	asyncio.run(_run())
