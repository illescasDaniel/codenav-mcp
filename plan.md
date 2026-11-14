# Plan: multi-call tools (batching) for codenav-mcp

Goal: let an agent read or edit several files/symbols in ONE tool call (fewer round-trips,
fewer tokens). Internally the batch is queued and run as if the agent had made separate calls.

## Design
A new generic `batch` tool: `batch(calls=[{"tool": "<name>", "args": {...}}, ...], stop_on_error=None)`.
- Works for every read tool (hover, definition, references, symbol_info, outline, callers,
  search_symbol, diagnostics, implementations, workspace) and every write tool (edit, edit_symbol,
  rename_symbol, change_signature, move, quick_fix, verify_changes, apply_edit, undo_edit).
- Calls run **sequentially in order**, each through the real tool function, so every write keeps
  its own type check / preview / undo id and later calls see earlier writes.
- Output: one numbered section per call (`[1/3] outline(file_path=...)`) + a one-line summary.
- Per-call error isolation: bad tool name / bad args / tool error is reported in that section.
- `stop_on_error`: default auto = stop at the first failed call when the batch contains a write
  tool, keep going for read-only batches.
- Limits: max 25 calls, no nested `batch`.
- Args are validated against the target function's signature/types with actionable hints.

## Checklist
- [x] 1. Branch + plan.md committed
- [ ] 2. Opus review of the design (adjust plan if needed)
- [ ] 3. Implement `batch` (src/codenav_mcp/batch_tools.py) + register in server
- [ ] 4. Unit tests (fake tools: ordering, error isolation, stop_on_error, limits, arg validation)
- [ ] 5. Live/MCP integration tests (multi-file read, multi-file edit via real ty and MCP client)
- [ ] 6. Docs: README section + server instructions mention
- [ ] 7. Full test run, lint, push, open PR
