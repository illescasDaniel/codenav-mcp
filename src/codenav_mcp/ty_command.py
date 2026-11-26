"""Resolves how to launch `ty server` for codenav's LspClient."""

from __future__ import annotations

import logging
import re
import shutil
import sys
import tomllib
from pathlib import Path


logger = logging.getLogger(__name__)


def _bundled_ty() -> str | None:
	"""The `ty` binary installed alongside codenav (it is a declared dependency).

	Hosts typically launch codenav with `uvx`/`pipx`, whose environment's
	scripts directory is not on the server's PATH, so `shutil.which` alone
	misses it."""
	try:
		from ty import find_ty_bin  # absent when running from a bare source tree

		return find_ty_bin()
	except (ImportError, FileNotFoundError):
		return None


def resolve_ty_command(workspace_root: Path) -> list[str]:
	"""The project's own `ty` first (its pinned version matches its config), then
	the one bundled with codenav, then PATH, then `uvx` as a last resort."""
	venv_bin = "Scripts" if sys.platform == "win32" else "bin"
	venv_exe = "ty.exe" if sys.platform == "win32" else "ty"
	candidate = workspace_root / ".venv" / venv_bin / venv_exe
	if candidate.is_file():
		return [str(candidate), "server"]
	bundled = _bundled_ty()
	if bundled:
		return [bundled, "server"]
	on_path = shutil.which("ty")
	if on_path:
		return [on_path, "server"]
	logger.warning(
		"ty not found in %s/.venv, codenav's environment or on PATH; falling back to 'uvx ty server', "
		"which downloads it on first use.",
		workspace_root,
	)
	return ["uvx", "ty", "server"]


def ty_config_note(workspace_root: Path) -> str | None:
	"""Explain why the workspace's ty configuration cannot be parsed (RC-156)."""
	name = next((n for n in ("ty.toml", ".ty.toml") if (workspace_root / n).is_file()), "pyproject.toml")
	path = workspace_root / name
	if not path.is_file():
		return None
	try:
		tomllib.loads(path.read_text(encoding="utf-8"))
	except tomllib.TOMLDecodeError as exc:
		# "Expected ']' ... (at line 4, column 9)" -> "Expected ']' ... on line 4"
		reason = re.sub(r"\s*\(at line (\d+), column \d+\)", r" on line \1", " ".join(str(exc).split()))
	except UnicodeDecodeError:
		reason = "not valid UTF-8"
	except OSError as exc:
		reason = exc.strerror or "unreadable"
	else:
		return None
	return f"{name} could not be parsed ({reason})."
