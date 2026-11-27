# codenav-mcp

Python code navigation and type-checked refactoring over MCP, powered by Astral's ty.
Use it to follow typed calls through imports and dependency injection, find structural
Protocol implementations, and preview refactors that touch several files. Use grep
for exact-name text lookups and Read for a known file or excerpt.

| I want to… | Tool |
|---|---|
| Find a definition, its signature, references, callers or implementations | `lookup(name="FileSearchPort.execute")` |
| Discover symbols or find names in strings, comments and other text | `search(query="FileSearchPort", scope="mentions")` |
| Map the definitions in a large file | `outline(file_path="src/app.py")` |
| Check a file, workspace or change against git | `check(since="HEAD")` |
| Find dead-code candidates | `unused(path="src")` |
| Inspect what an API change touches | `impact(name="FileSearchPort.execute", change="signature")` |
| Rename a symbol and its linked usages | `rename(name="FileSearchPort.execute", new_name="run", apply=false)` |
| Change parameters and update callers | `change_signature(name="parse", add=[{"name":"strict","default":"False"}])` |
| Move a module or definition and rewrite imports | `move(name="parse", to_file="src/parsing.py", apply=false)` |
| Edit a definition or group several changes into one plan | `edit_symbol(name="old_parser", action="delete", apply=false)` |
| Use a ty quick fix, such as adding a missing import | `fix(file_path="src/app.py", apply=false)` |
| List, show, apply or undo stored changes | `edits(action="list")` |

Start with the name and add `file_path` when several symbols share it. For callers
and structural implementations of a Protocol method, expand the relevant
sections.

```text
lookup(name="FileSearchPort.execute", show=["callers", "implementations"], limit=10)
```

The reply groups Protocol calls and calls to concrete implementations. For
cross-file changes, start with `apply=false`, inspect the preview, then apply
its ID with `edits(action="apply", id="…")`. Preview and undo IDs persist across
server restarts. The write gate checks types only. Run relevant tests and
documentation checks after applying a change.

## Quick start

Install [uv](https://docs.astral.sh/uv/) and register the server with your MCP host.

```bash
claude mcp add codenav -- uvx codenav-mcp
codex mcp add codenav -- uvx codenav-mcp
opencode mcp add codenav -- uvx codenav-mcp
```

Python 3.11 or later is required. ty installs with codenav, and a workspace's
own ty takes priority when present. Python navigation and writes cover
`.py` and `.pyi` files. Positions use 1-based lines and UTF-16 columns.

## How it is tested

A refactoring tool must not break code quietly, so codenav is tested in several ways:

- **About 1,300 tests:** unit tests, plus about 350 integration tests that run the tools against a real ty server.
- **Property tests:** about 190, with Hypothesis and generated Python (hypothesmith), including a state machine for
  preview, apply and undo, and CrossHair symbolic checks.
- **A refactor fuzzer:** random renames, moves and signature changes on real projects (srxy, click, tomlkit), each
  checked against the project's own tests and ty. 187 refactors so far, none silently broke code.
- **Differential checks:** references compared with jedi and renames with rope.
- **Mutation testing** (mutmut) of the modules where a wrong edit would go unnoticed, to find tests that don't check
  enough.
- **Agent pilots:** small models (Claude Haiku, GPT-6 Luna) doing real tasks with and without codenav, graded by hand.

The results and their data are in [Research](docs/research/README.md); how to run each check is in
[Hardening checks](docs/hardening.md).

## Documentation

- [Tool reference](docs/tools.md)
- [Migrating from 1.x](CHANGELOG.md#migrating-from-1x)
- [Host setup, workspace selection and environment variables](docs/configuration.md)
- [Local setup and tests](docs/development.md)
- [Research: measured results and data](docs/research/README.md)
- [Documentation index](docs/INDEX.md)

## License

[MIT](LICENSE)
