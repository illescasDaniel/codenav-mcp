# codenav A/B results (run 20261126-1330)

Repo commit 953d27c, Claude Code 2.1.332, sonnet subagents. Baseline: ty clean; unit+cli 1170 passed / 7 skipped; GUI subset 6 passed.

| | Arm 1: codenav | Arm 2: plain |
|---|---|---|
| Task 1 verdict | success (only search_files.py:599; decoys named as non-callers) | success (same; also listed decoy imports as same-name) |
| Task 2 verdict | success (8 sites/7 files, decoys untouched; mock line fixed by hand with sed) | success (same 8 sites via line-targeted sed; decoys untouched) |
| Task 3 verdict | success (ty clean, all 4 places handle None; plus docs/api-reference.md) | success (ty clean, all 4 places handle None; plus docs/api-reference.md; hint reads "--max-file-size a larger value") |
| ty regressions | none (exit 0) | none (exit 0) |
| Tests (my re-run) | unit/cli 1170 passed, 7 skipped; GUI 6 passed | unit/cli 1170 passed, 7 skipped; GUI 6 passed |
| Tool calls (total, incl. final hand-back) | 15 | 11 (+1 hand-back counted in metrics) |
| codenav | 6 (callers 2, search_symbol 1, symbol_info 1, rename_symbol 1, references 1); 3 returned errors (wrong args) | 0 |
| Read | 1 | 0 |
| Edit/Write | 0 | 0 |
| Bash | 8 | 11 |
| other | 1 (hand-back) | 1 (hand-back) |
| Bash that look like search/edit fallbacks | 6 (grep, sed, python3 edit script) | 10 (grep, sed, python3 edit script) |
| Total tokens | 40,712 | 22,266 |
| Duration | 90.9 s | 106.2 s |
| Diff | 10 files, +26/-16 | 10 files, +18/-17 |

## Agents' own reviews
- **codenav-agent:** 4/5. `rename_symbol` saved the most work (port, 3 implementations, caller, one test in one type-checked edit; it flagged the unlinkable mock line). `callers` answered task 1 directly. Three calls failed on wrong argument names. It did not use `edit`/`edit_symbol` and fell back to sed/python for Python edits, which it called unjustified.
- **plain-agent:** grep gave every textual match so real call sites had to be separated by hand; the rename relied on grep line numbers; ty cannot see the docs mismatch, which only a failing test revealed. It wanted a type-resolved references tool and a symbol-aware rename, and said it never tried codenav even though it was visible in its environment (it had no MCP tools per its agent definition, so this is as reported).

## Verdict
Both arms completed all three tasks correctly, with identical test and ty results and no decoy renamed. On this single run the plain arm used fewer tokens and was not slower (the 15 s time gap is within what one run can show), so no efficiency advantage for codenav is demonstrated. Codenav's visible benefit was a type-checked, undoable rename that flagged what it could not link; whether that matters beyond a task this size is not shown here.

## Limits
One run per arm, one repo, one model. Arm 1 was told it MUST use codenav; arm 2 was not. Order and prompt-cache effects are uncontrolled (arm 1 ran first). Token and time figures are what the harness stored. Differences of this size are within what one run can show; no percentages claimed.

## Unexpected / notes
- events.jsonl held stale entries from an earlier session; moved aside as pre-arm1-stale.* and between-arms.* (my own verification calls), so arm logs start clean. Logs/scoreboard still include the orchestrator's own collection Bash call(s) (one extra Bash in each).
- codenav-agent did not follow its own rule: sed/python edits and grep cross-checks, 3 failed codenav calls.
- plain-agent briefly ran `git stash` / `stash pop` itself to compare against baseline; it restored cleanly and no stray stash remained.
- Both agents also had to edit docs/api-reference.md (a test compares it to the dataclass signature).
- Real project untouched: status before and after is `?? .claude/demo-hooks/`.
- Stashes: arm1-codenav, arm2-plain (clone is clean).
