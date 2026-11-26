[![version](https://img.shields.io/pypi/v/codenav-mcp)](https://pypi.org/project/codenav-mcp/)

# codenav-mcp

Give AI agents Python code navigation and refactoring through the Model Context Protocol. codenav-mcp uses [ty](https://github.com/astral-sh/ty)'s language server to resolve definitions, references and call sites, and checks proposed edits for type diagnostics before writing them.

![Infographic showing an MCP client requesting navigation or an edit, ty resolving code, and edits being checked before preview or application.](assets/workflow.svg)

An MCP client sends a request to codenav-mcp. Navigation uses ty to resolve code; edits are simulated and checked before they are previewed or applied. Applied edits can be undone while the server retains its edit journal, if the files have not changed since.

## Examples

Real tool calls on a small sample project (`app/user_service.py` defines `UserService` with `create` and `save`; `app/main.py` and `tests/test_user_service.py` use it):

> symbol_info(name: "UserService.create")

```text
create  [Method]  (app/user_service.py:14:6)

def create(
    self,
    name: str
) -> User
---------------------------------------------
Create a user and save it.

Definition:
app/user_service.py:14:6
   12 | 		self._next_id = 1
   13 |
   14 | 	def create(self, name: str) -> User:
   15 | 		"""Create a user and save it."""
   16 | 		user = User(id=self._next_id, name=name)

References:
3 reference(s) in 3 file(s):
app/main.py: L4
app/user_service.py: L14
tests/test_user_service.py: L5
```

> references(file_path: "app/user_service.py", line: 14, column: 6)

```text
3 reference(s) in 3 file(s):
app/main.py:4:16
    2 |
    3 | service = UserService()
    4 | user = service.create("Ada")
    5 | print(user.name)

app/user_service.py:14:6
   12 | 		self._next_id = 1
   13 |
   14 | 	def create(self, name: str) -> User:
   15 | 		"""Create a user and save it."""
   16 | 		user = User(id=self._next_id, name=name)

tests/test_user_service.py:5:23
    3 |
    4 | def test_create_returns_user() -> None:
    5 | 	user = UserService().create("Grace")
    6 | 	assert user.name == "Grace"
```

> edit(file_path: "app/user_service.py", old_string: "...", new_string: "...", apply: false) — an edit that would introduce a bug is caught before anything is written:

```text
Edit app/user_service.py
Replaced 1 occurrence(s).

Preview only, nothing written. apply=true would be refused: 1 new error (allowed: 0). Review the diagnostics below; adjust and rerun, or write the plan anyway with apply_edit.
The plan is kept: apply_edit(id="a59fa0ec") writes it anyway.

Files:
  app/user_service.py: +1 -1

Diagnostics: 1 new error(s), 0 fixed (3 file(s) checked, 0 unchanged)
New:
  + app/user_service.py:16:15 error [unknown-argument]: Argument `active` does not match any known parameter

--- a/app/user_service.py
+++ b/app/user_service.py
@@ -14,5 +14,5 @@
 	def create(self, name: str) -> User:
 		"""Create a user and save it."""
-		user = User(id=self._next_id, name=name)
+		user = User(active=True, id=self._next_id, name=name)
 		self._next_id += 1
 		self.save(user)
```

> rename_symbol(name: "UserService.save", new_name: "persist", apply: false) — a type-checked rename, with call sites found through the language server:

```text
Rename save -> persist (UserService.save): 2 edit(s) in 1 file(s)

Preview only, nothing written.
Write it with apply_edit(id="9fd895aa") or rerun with apply=true.

Files:
  app/user_service.py: +2 -2

Notes:
  - `save` appears in strings (getattr/patch targets, __all__, messages): 1
    app/user_service.py:15: """Create a user and save it."""

Diagnostics: 0 new error(s), 0 fixed (3 file(s) checked, 0 unchanged)

--- a/app/user_service.py
+++ b/app/user_service.py
@@ -16,8 +16,8 @@
 		user = User(id=self._next_id, name=name)
 		self._next_id += 1
-		self.save(user)
+		self.persist(user)
 		return user

-	def save(self, user: User) -> None:
+	def persist(self, user: User) -> None:
 		self._users = getattr(self, "_users", {})
 		self._users[user.id] = user
```

All tools answer plain text like this; the full catalogue with parameters and safety rules is in the [tool reference](docs/tools.md).

## Quick start

Install [uv](https://docs.astral.sh/uv/), then register the server with your MCP host from the project root. The host starts it when needed.

**Claude Code:**

```bash
claude mcp add codenav -- uvx codenav-mcp
```

**Codex** (user configuration):

```bash
codex mcp add codenav -- uvx codenav-mcp
```

**OpenCode** (project configuration):

```bash
opencode mcp add codenav -- uvx codenav-mcp
```

See [configuration](docs/configuration.md) for Claude Code and Cursor configuration, workspace selection and environment variables.

## Requirements and limits

- Python 3.11 or later; the package installs ty and its other dependencies.
- Navigation and edits target Python (`.py` and `.pyi`). Positions are 1-indexed; columns use UTF-16 offsets.
- Edit tools normally check type diagnostics and allow no new errors by default. Use `apply=false` to preview; `apply_edit` can explicitly apply a preview despite its diagnostic limit. Read-only mode disables writes. Checks are not a substitute for tests.
- Batch calls run in order and are not atomic. Tool results are capped at 30,000 characters by default (`CODENAV_MCP_MAX_OUTPUT_CHARS`). See the [tool reference](docs/tools.md) for safety rules, output limits and [known limits](docs/tools.md#known-limits).

## Environment

See [configuration](docs/configuration.md#environment) for environment variables.

## Tests

From a checkout, run `uv sync --group dev` and `uv run pytest`. See [development](docs/development.md) for lint and package smoke-test commands. No evaluation score is reported here.

## Documentation

- [Tool reference](docs/tools.md): navigation, edits, safety and batching.
- [Configuration](docs/configuration.md): host setup, workspace and ty selection.
- [Development](docs/development.md): local setup and tests.
- [Documentation index](docs/INDEX.md).

Related: [webnav-ts-mcp](https://www.npmjs.com/package/webnav-ts-mcp) provides navigation for JS/TS/HTML/CSS. [Design notes](https://github.com/illescasDaniel/SpaceMaker/blob/main/docs/agent-tooling.md) cover Protocol conformance, positioning and output formats.

## License

[MIT](LICENSE).
