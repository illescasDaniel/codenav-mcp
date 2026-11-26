"""Suite-wide fixtures."""

from __future__ import annotations

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
