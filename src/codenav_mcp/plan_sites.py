"""v2 contract between refactor planners (rename, change_signature) and edit state (`edits`).

`EditPlan` comes from mcp-nav-shared and cannot change; codenav wraps it. A plan writes its certain
edits (`plan`); the uncertain ones (mock attribute accesses, `Mock(old=...)` keywords, comments,
docstrings, same-named test fakes) are `optional_sites` the agent opts in by id (`sites=["u1"]`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcp_nav_shared.edits import EditPlan, TextEdit


@dataclass(frozen=True)
class OptionalSite:
	id: str  # "u1", "u2", ... stable within one plan
	path: str  # workspace-relative
	line: int  # 1-based
	kind: str  # "attr" | "keyword" | "def" | "string" | "comment" | "docstring" | "text"
	text: str  # the source line, stripped, for display
	edit: TextEdit  # the edit to add if the agent opts in


@dataclass
class CodenavPlan:
	plan: EditPlan
	optional_sites: list[OptionalSite] = field(default_factory=list)

	def site(self, site_id: str) -> OptionalSite | None:
		return next((site for site in self.optional_sites if site.id == site_id), None)
