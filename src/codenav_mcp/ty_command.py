"""Resolves how to launch `ty server` for codenav's LspClient."""

from __future__ import annotations

import logging
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass
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


def _glob_regex(pattern: str) -> re.Pattern[str]:
	"""A gitignore-style glob (the form ty's `src.include`/`src.exclude` take) as a regex over `a/b/c.py` paths."""
	pattern = pattern.removeprefix("./")
	anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
	body = pattern.strip("/")
	out: list[str] = []
	i = 0
	while i < len(body):
		if body.startswith("**/", i):
			out.append("(?:.*/)?")
			i += 3
		elif body.startswith("/**", i) and i + 3 == len(body):
			out.append("/.*")
			i += 3
		elif body.startswith("**", i):
			out.append(".*")
			i += 2
		elif body[i] == "*":
			out.append("[^/]*")
			i += 1
		elif body[i] == "?":
			out.append("[^/]")
			i += 1
		else:
			out.append(re.escape(body[i]))
			i += 1
	prefix = "" if anchored else "(?:.*/)?"
	return re.compile(prefix + "".join(out))


def _read_ty_src(workspace_root: Path) -> dict[str, list[str]]:
	"""`include` / `exclude` of the workspace's ty config (`ty.toml`, `.ty.toml` or `[tool.ty.src]`); empty when none."""
	for name in ("ty.toml", ".ty.toml"):
		path = workspace_root / name
		if path.is_file():
			section = _toml_section(path, ("src",))
			break
	else:
		section = _toml_section(workspace_root / "pyproject.toml", ("tool", "ty", "src"))
	result: dict[str, list[str]] = {}
	for key in ("include", "exclude"):
		value = section.get(key)
		if isinstance(value, list):
			result[key] = [item for item in value if isinstance(item, str)]
	return result


def _toml_section(path: Path, keys: tuple[str, ...]) -> dict[str, object]:
	try:
		data: object = tomllib.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
		return {}
	for key in keys:
		data = data.get(key) if isinstance(data, dict) else None
	return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class TyCoverage:
	"""Which files the workspace's ty config checks. ty's language server answers `[]` for a file its
	`src.include`/`src.exclude` leaves out, which must never be reported as "checked, no errors"."""

	include: tuple[re.Pattern[str], ...] = ()
	exclude: tuple[tuple[re.Pattern[str], bool], ...] = ()  # (pattern, is_negation)

	def covers(self, relative: str) -> bool:
		parts = relative.split("/")
		prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
		if self.include and not any(p.fullmatch(prefix) for p in self.include for prefix in prefixes):
			return False
		excluded = False
		for pattern, negation in self.exclude:
			if any(pattern.fullmatch(prefix) for prefix in prefixes):
				excluded = not negation
		return not excluded

	def excludes(self, path: Path, workspace_root: Path) -> bool:
		try:
			relative = path.resolve().relative_to(workspace_root.resolve()).as_posix()
		except ValueError:
			return False  # outside the workspace: not ours to judge
		return not self.covers(relative)


def ty_coverage(workspace_root: Path) -> TyCoverage:
	src = _read_ty_src(workspace_root)
	return TyCoverage(
		include=tuple(_glob_regex(p) for p in src.get("include", ())),
		exclude=tuple(
			(_glob_regex(p.removeprefix("!")), p.startswith("!")) for p in src.get("exclude", ()) if p.strip()
		),
	)
