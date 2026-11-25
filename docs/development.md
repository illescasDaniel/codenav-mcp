---
id: DOC-003
title: Development
summary: Local setup and test commands
type: reference
status: current
updated: 2026-11-25
verified:
covers: [pyproject.toml, tests/**, scripts/test_package.sh]
related: []
supersedes:
superseded_by:
---

# Development

Use a checkout to run the server and its tests locally.

```bash
uv sync --group dev
uv run codenav-mcp
```

Run the test suite with `uv run pytest`, or lint and check formatting with `uv run task lint`. The suite includes live language-server tests; results depend on the available environment. `uv run task test-package` installs the published package from PyPI in a fresh environment and runs a smoke test; it requires network access.
