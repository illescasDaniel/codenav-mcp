"""Suite-wide fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from codenav_mcp import writes
from codenav_mcp.edit_state import STATE_DIR_ENV


@pytest.fixture(autouse=True)
def _isolated_edit_state(tmp_path_factory, monkeypatch):
	"""Preview and undo state goes to a per-test temporary directory, never to the real state directory.

	Subprocesses a test starts inherit the variable through `os.environ`, so a spawned server honours it too."""
	monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path_factory.mktemp("codenav-state")))
	writes.reset_state()
	yield
	writes.reset_state()


@pytest.fixture(autouse=True)
def _exact_text_writes(monkeypatch):
	"""Files a test writes hold exactly the bytes given, on every platform.

	Text mode without `newline=""` translates `\n` to `\r\n` on Windows, so hashes and round-trip comparisons
	would disagree with the content the tests say they wrote (the tools themselves read and write bytes)."""
	original = Path.write_text

	def write_text(
		self: Path, data: str, encoding: str | None = None, errors: str | None = None, newline: str | None = None
	) -> int:
		return original(self, data, encoding=encoding, errors=errors, newline="")

	monkeypatch.setattr(Path, "write_text", write_text)
