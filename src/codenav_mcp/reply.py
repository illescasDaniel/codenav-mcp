"""v2 reply format shared by every tool: answer line, items, notes, next calls.

    3 callers of FileSearchPort.execute in 3 files
    src/a.py:817  run_plain
    ...
    Notes:
      - static callers only ...
    next: lookup(name="FileSearchPort.execute", show=["callers"], offset=20)

Keep replies small: no source unless asked, workspace-relative paths, one item per line.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


def call(tool: str, **args: Any) -> str:
	"""An exact, copy-pastable call: `lookup(name="X", show=["callers"], offset=20)`. None values are dropped."""
	parts = [f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in args.items() if value is not None]
	return f"{tool}({', '.join(parts)})"


@dataclass
class Page:
	"""One page of a list: the items shown, and how many there are in total."""

	items: list[Any]
	offset: int
	total: int

	@property
	def more(self) -> int:
		return max(0, self.total - self.offset - len(self.items))


def paginate(items: Sequence[Any], limit: int, offset: int = 0) -> Page:
	if limit < 1:
		raise ValueError("limit must be at least 1")
	if offset < 0:
		raise ValueError("offset must be at least 0")
	return Page(items=list(items[offset : offset + limit]), offset=offset, total=len(items))


def next_page_call(page: Page, tool: str, **args: Any) -> str | None:
	"""The call that shows the next page, or None when the page is the last one."""
	if not page.more:
		return None
	return call(tool, **args, offset=page.offset + len(page.items))


@dataclass
class Reply:
	answer: str
	items: list[str] = field(default_factory=list)
	notes: list[str] = field(default_factory=list)
	next_calls: list[str] = field(default_factory=list)

	def add_page(self, page: Page, render: Any, tool: str, **args: Any) -> None:
		"""Add a page's rendered items and, if cut, a line saying so plus the next-page call."""
		self.items.extend(render(item) for item in page.items)
		follow = next_page_call(page, tool, **args)
		if follow:
			self.items.append(f"... {page.more} more")
			self.next_calls.append(follow)

	def render(self) -> str:
		lines = [self.answer, *self.items]
		if self.notes:
			lines.append("Notes:")
			lines.extend(f"  - {note}" for note in self.notes)
		lines.extend(f"next: {follow}" for follow in self.next_calls)
		return "\n".join(lines)
