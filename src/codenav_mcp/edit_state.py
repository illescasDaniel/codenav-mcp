"""Previews and applied edits that survive a server restart.

`PlanStore` and `EditJournal` (mcp-nav-shared) keep everything in memory. The subclasses here keep their
behaviour and mirror their state to disk, one directory per workspace, so a preview made by one server process
can be applied (and an applied edit undone) by the next one.

Layout: `<state dir>/<hash of the workspace path>/{plans,journal}.json`. The state dir is
`$CODENAV_MCP_STATE_DIR`, else `$XDG_STATE_HOME/codenav-mcp`, else `~/.local/state/codenav-mcp`.
Every operation reloads the file first (another server process may have written it) and holds a file lock
while it modifies and saves, so two processes on one workspace do not lose each other's entries. Locks are
never nested. A state directory that cannot be written leaves the stores working in memory only
(`save_error` says why).
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mcp_nav_shared.edits import EditPlan, FileChange, TextEdit, hash_bytes
from mcp_nav_shared.errors import ToolInputError
from mcp_nav_shared.transaction import AppliedEdit, EditJournal, PlanStore, WriteGuard, echo_text

from codenav_mcp.plan_sites import CodenavPlan, OptionalSite
from codenav_mcp.reply import call


STATE_DIR_ENV = "CODENAV_MCP_STATE_DIR"
MAX_ENTRIES = 50
FORMAT_VERSION = 1

# The tool call being served ("rename(name="a", new_name="b")"), set by the server around every call so that a
# plan stored while serving it can say how to redo it. None for in-process calls (tests).
current_call: contextvars.ContextVar[str | None] = contextvars.ContextVar("codenav_current_call", default=None)


def call_text(tool: str, arguments: dict[str, Any]) -> str:
	return call(tool, **{key: value for key, value in arguments.items() if key != "ctx"})


def state_root() -> Path:
	explicit = os.environ.get(STATE_DIR_ENV, "").strip()
	if explicit:
		return Path(explicit).expanduser()
	xdg = os.environ.get("XDG_STATE_HOME", "").strip()
	base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
	return base / "codenav-mcp"


def workspace_state_dir(workspace: Path) -> Path:
	"""One directory per workspace: the hash of its resolved path."""
	key = hashlib.sha256(str(workspace.resolve()).encode("utf-8")).hexdigest()[:16]
	return state_root() / key


# ---------------------------------------------------------------------------------------------------------------
# JSON form of plans and edits


def _path_out(path: Path, root: Path) -> str:
	try:
		return path.relative_to(root).as_posix()
	except ValueError:
		return str(path)


def _path_in(text: str, root: Path) -> Path:
	path = Path(text)
	return path if path.is_absolute() else root / path


def plan_to_json(plan: EditPlan, root: Path) -> dict[str, Any]:
	return {
		"description": plan.description,
		"notes": list(plan.notes),
		"base_hashes": {_path_out(path, root): digest for path, digest in plan.base_hashes.items()},
		"changes": [
			{
				"path": _path_out(change.path, root),
				"old_text": change.old_text,
				"new_text": change.new_text,
				"old_bom": change.old_bom,
				"new_bom": change.new_bom,
				"old_mode": change.old_mode,
				"new_mode": change.new_mode,
			}
			for change in plan.changes
		],
	}


def plan_from_json(data: dict[str, Any], root: Path) -> EditPlan:
	# Every field is passed explicitly: FileChange would otherwise read BOM and mode from the file as it is now.
	changes = [
		FileChange(
			_path_in(raw["path"], root),
			raw["old_text"],
			raw["new_text"],
			raw["old_bom"],
			raw["new_bom"],
			old_mode=raw["old_mode"],
			new_mode=raw["new_mode"],
		)
		for raw in data["changes"]
	]
	hashes = {_path_in(path, root): digest for path, digest in data["base_hashes"].items()}
	return EditPlan(data["description"], changes, list(data["notes"]), hashes)


def _site_out(site: OptionalSite) -> dict[str, Any]:
	return {
		"id": site.id,
		"path": site.path,
		"line": site.line,
		"kind": site.kind,
		"text": site.text,
		"edit": asdict(site.edit),
	}


def _site_in(raw: dict[str, Any]) -> OptionalSite:
	return OptionalSite(raw["id"], raw["path"], raw["line"], raw["kind"], raw["text"], TextEdit(**raw["edit"]))


# ---------------------------------------------------------------------------------------------------------------
# Files


@contextlib.contextmanager
def _locked(directory: Path) -> Iterator[None]:
	"""An exclusive lock on the workspace's state directory (best effort: without `fcntl` or a writable directory
	the caller simply goes ahead)."""
	try:
		import fcntl

		directory.mkdir(parents=True, exist_ok=True, mode=0o700)
		handle = (directory / "lock").open("a")
	except (ImportError, OSError):
		yield
		return
	with handle:
		fcntl.flock(handle, fcntl.LOCK_EX)
		try:
			yield
		finally:
			fcntl.flock(handle, fcntl.LOCK_UN)


def _read_json(path: Path) -> dict[str, Any]:
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}
	return data if isinstance(data, dict) and data.get("version") == FORMAT_VERSION else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
	temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
	try:
		fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
		with os.fdopen(fd, "w", encoding="utf-8") as handle:
			json.dump({"version": FORMAT_VERSION, **data}, handle)
		os.replace(temp, path)
	except BaseException:
		with contextlib.suppress(OSError):
			temp.unlink()
		raise


def age_text(created: float, now: float | None = None) -> str:
	seconds = max(0, int((time.time() if now is None else now) - created))
	for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
		if seconds >= size:
			return f"{seconds // size}{unit} ago"
	return f"{seconds}s ago"


# ---------------------------------------------------------------------------------------------------------------
# Previews


@dataclass
class StoredPlan:
	id: str
	created: float
	rerun: str | None
	plan: CodenavPlan


class PersistentPlanStore(PlanStore):
	"""Previewed plans waiting to be applied by id; kept on disk. A plan may carry `optional_sites`."""

	def __init__(self, root: Path, directory: Path, max_entries: int = MAX_ENTRIES) -> None:
		super().__init__(max_entries)
		self._root = root
		self._directory = directory
		self._file = directory / "plans.json"
		self._stored: OrderedDict[str, StoredPlan] = OrderedDict()
		self.save_error: str | None = None

	# -- disk

	def _load(self) -> None:
		data = _read_json(self._file)
		self._stored = OrderedDict()
		self._applied = OrderedDict((plan_id, None) for plan_id in data.get("applied", []))
		for raw in data.get("plans", []):
			try:
				plan = CodenavPlan(
					plan_from_json(raw["plan"], self._root), [_site_in(site) for site in raw.get("optional_sites", [])]
				)
				self._stored[raw["id"]] = StoredPlan(raw["id"], raw["created"], raw.get("rerun"), plan)
			except (KeyError, TypeError, ValueError):
				continue  # an entry this version cannot read is dropped, the rest stays usable
		self._plans = OrderedDict((plan_id, stored.plan.plan) for plan_id, stored in self._stored.items())

	def _save(self) -> None:
		data = {
			"applied": list(self._applied),
			"plans": [
				{
					"id": stored.id,
					"created": stored.created,
					"rerun": stored.rerun,
					"plan": plan_to_json(stored.plan.plan, self._root),
					"optional_sites": [_site_out(site) for site in stored.plan.optional_sites],
				}
				for stored in self._stored.values()
			],
		}
		try:
			_write_json(self._file, data)
			self.save_error = None
		except OSError as exc:
			self.save_error = exc.strerror or "could not write the state directory"

	# -- PlanStore API

	def add(self, plan: EditPlan | CodenavPlan, *, rerun: str | None = None) -> str:
		codenav = plan if isinstance(plan, CodenavPlan) else CodenavPlan(plan)
		with _locked(self._directory):
			self._load()
			plan_id = uuid.uuid4().hex[:8]
			while plan_id in self._stored or plan_id in self._applied:
				plan_id = uuid.uuid4().hex[:8]
			self._stored[plan_id] = StoredPlan(plan_id, time.time(), rerun or current_call.get(), codenav)
			while len(self._stored) > self._max_entries:
				self._stored.popitem(last=False)
			self._save()
		return plan_id

	def get_stored(self, plan_id: str) -> StoredPlan:
		self._load()
		stored = self._stored.get(plan_id)
		if stored is not None:
			return stored
		shown = echo_text(plan_id)
		if plan_id in self._applied:
			raise ToolInputError(
				f"preview id {shown} was already applied; nothing was written again. To revert that edit: "
				f"{call('edits', action='undo', id=plan_id)} if it is still in the history"
			)
		known = ", ".join(self._stored) or "none"
		raise ToolInputError(
			f"unknown or expired preview id {shown!r} (pending: {known}). Previews are kept on disk, "
			f"the last {self._max_entries} per workspace: {call('edits', action='list')} shows them; "
			"otherwise rerun the edit with apply=false."
		)

	def get(self, plan_id: str) -> EditPlan:
		return self.get_stored(plan_id).plan.plan

	def pending(self) -> list[StoredPlan]:
		"""Newest first."""
		self._load()
		return list(reversed(self._stored.values()))

	def discard(self, plan_id: str) -> None:
		with _locked(self._directory):
			self._load()
			if self._stored.pop(plan_id, None) is not None:
				self._applied[plan_id] = None
				while len(self._applied) > 100:
					self._applied.popitem(last=False)
				self._save()

	def clear(self) -> None:
		with _locked(self._directory):
			self._stored.clear()
			self._applied.clear()
			self._plans.clear()
			self._save()


# ---------------------------------------------------------------------------------------------------------------
# Applied edits


class PersistentJournal(EditJournal):
	"""Applies plans and remembers the last 50, on disk, so they can be undone after a restart."""

	def __init__(self, guard: WriteGuard, directory: Path, max_entries: int = MAX_ENTRIES) -> None:
		super().__init__(guard, max_entries)
		self._directory = directory
		self._file = directory / "journal.json"
		self.applied_at: dict[str, float] = {}
		self._followups: dict[str, CodenavPlan] = {}
		self._parents: dict[str, str] = {}
		self.save_error: str | None = None

	@property
	def root(self) -> Path:
		return self.guard.root

	def _load(self) -> None:
		data = _read_json(self._file)
		self._entries.clear()
		self._undone.clear()
		self.applied_at = {}
		self._followups = {}
		self._parents = {}
		for raw in data.get("entries", []):
			try:
				plan = plan_from_json(raw["plan"], self.root)
				hashes = {_path_in(path, self.root): digest for path, digest in raw["applied_hashes"].items()}
				self._entries[raw["id"]] = AppliedEdit(raw["id"], plan, hashes)
				self.applied_at[raw["id"]] = raw["created"]
				if followup := raw.get("followup"):
					self._followups[raw["id"]] = CodenavPlan(
						plan_from_json(followup["plan"], self.root),
						[_site_in(site) for site in followup["optional_sites"]],
					)
				if parent := raw.get("parent"):
					self._parents[raw["id"]] = parent
			except (KeyError, TypeError, ValueError):
				continue
		for edit_id in data.get("undone", []):
			self._undone[edit_id] = None

	def _save(self) -> None:
		data = {
			"entries": [
				{
					"id": entry.id,
					"created": self.applied_at.get(entry.id, 0.0),
					"plan": plan_to_json(entry.plan, self.root),
					"applied_hashes": {
						_path_out(path, self.root): digest for path, digest in entry.applied_hashes.items()
					},
					"followup": self._followup_json(entry.id),
					"parent": self._parents.get(entry.id),
				}
				for entry in self._entries.values()
			],
			"undone": list(self._undone),
		}
		try:
			_write_json(self._file, data)
			self.save_error = None
		except OSError as exc:
			self.save_error = exc.strerror or "could not write the state directory"

	def _followup_json(self, edit_id: str) -> dict[str, Any] | None:
		followup = self._followups.get(edit_id)
		if followup is None:
			return None
		return {
			"plan": plan_to_json(followup.plan, self.root),
			"optional_sites": [_site_out(site) for site in followup.optional_sites],
		}

	def followup(self, edit_id: str) -> CodenavPlan | None:
		self._load()
		if self._active_child(edit_id) is not None:
			return None
		return self._followups.get(edit_id)

	def _active_child(self, parent: str) -> str | None:
		return next(
			(child for child, ancestor in self._parents.items() if ancestor == parent and child in self._entries), None
		)

	# -- EditJournal API

	def rebind(self, guard: WriteGuard) -> None:
		"""Another workspace's journal lives in its own directory; nothing is cleared."""
		self.guard = guard
		self._load()

	def history(self) -> list[AppliedEdit]:
		self._load()
		return super().history()

	def entry(self, edit_id: str) -> AppliedEdit | None:
		self._load()
		return self._entries.get(edit_id)

	def was_undone(self, edit_id: str) -> bool:
		self._load()
		return edit_id in self._undone

	def apply(
		self,
		plan: EditPlan,
		*,
		allow_large: bool = False,
		entry_id: str | None = None,
		followup: CodenavPlan | None = None,
		parent_id: str | None = None,
	) -> AppliedEdit:
		with _locked(self._directory):
			self._load()
			if parent_id is not None:
				if parent_id not in self._entries:
					raise ToolInputError(f"edit {parent_id} is no longer in the history")
				if child := self._active_child(parent_id):
					raise ToolInputError(f"edit {parent_id} already has follow-up {child}")
			entry = super().apply(plan, allow_large=allow_large, entry_id=entry_id)
			self.applied_at[entry.id] = time.time()
			if followup is not None:
				self._followups[entry.id] = followup
			if parent_id is not None:
				self._parents[entry.id] = parent_id
			self._save()
		return entry

	def undo(self, edit_id: str | None = None) -> AppliedEdit:
		with _locked(self._directory):
			self._load()
			if edit_id is None and not self._entries:
				raise ToolInputError(
					f"no edits to revert (the history keeps the last {self._max_entries} applied edits per "
					f"workspace); {call('edits', action='list')} shows what is there"
				)
			if edit_id is not None and edit_id not in self._entries:
				shown = echo_text(edit_id)
				if edit_id in self._undone:
					raise ToolInputError(f"edit {shown} was already undone; nothing was changed")
				raise ToolInputError(
					f"edit {shown} is not in the undo history (never applied, already undone, or older than the "
					f"last {self._max_entries}); {call('edits', action='list')} shows the history"
				)
			target = edit_id or next(reversed(self._entries))
			if child := self._active_child(target):
				raise ToolInputError(f"edit {target} has follow-up {child}; undo {child} first")
			entry = super().undo(edit_id)
			self._save()
		return entry


# ---------------------------------------------------------------------------------------------------------------


def changed_files(plan: EditPlan, root: Path) -> list[str]:
	"""Workspace-relative names of the files whose content differs from what the plan was made from."""
	names = []
	for path, expected in plan.base_hashes.items():
		try:
			current: str | None = hash_bytes(path.read_bytes())
		except FileNotFoundError:
			current = None
		if current != expected:
			names.append(_path_out(path, root))
	return names
