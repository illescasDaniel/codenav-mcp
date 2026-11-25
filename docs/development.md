---
id: DOC-003
title: Development
summary: Local setup, test commands, language server lifecycle and shutdown behaviour
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

## Language server

- Previews (`apply=false` simulations) hold an overlay lock: an LSP call already in flight is answered for the disk's text before the preview shows its text, and other calls wait until the preview ends.
- A preview of a new file writes it to disk for the duration only, and records it in a private temporary manifest. If preview creation fails after the file was created, the file is kept and its workspace-relative path is reported. If the server is killed (SIGKILL) during a preview, the next start reports the leftover path once and leaves the file for manual inspection; nothing is deleted automatically.
- The server and its wrapper (`uvx`) run in their own process group. On a start timeout or on stop, the whole group is terminated, so no orphan `sleep` or `ty` process stays behind.
- A file rewritten with the same size and the same mtime is detected through its ctime. `LspClient.restarts` counts restarts.
- Calls from child tasks inside an active preview are refused, to avoid bypassing isolation or deadlocking. Request the operation from the preview-owning task. File-change detection relies on filesystem ctime and may miss same-size, same-mtime changes on filesystems with unsuitable or coarse ctime.

## Signals and shutdown

On SIGINT or SIGTERM the server stops its ty language server and exits with status 0, without a traceback. The process ends from the signal handler (`os._exit`), because the MCP SDK reads stdin in a thread that cannot be cancelled while stdin stays open.

## Dropped input

A line on stdin that is not a JSON-RPC request (for example `42`, `[]`, or a response to no request of the server) gets no reply, as the MCP SDK requires. The server writes `codenav-mcp: ignored a non-request message (...); no reply is sent` to stderr. A lone UTF-16 surrogate inside a JSON string is not reported this way, because the SDK drops it at a point this code does not see. The public MCP SDK exposes no interception point for raw malformed input, so codenav cannot guarantee its own error text for such frames (decision: BY DESIGN, QA PROTO-005/007).
