# Tool reference

Start with a symbol name such as `parse`, `Cart.total` or `pkg.parser.parse`.
Use `file_path` to narrow an ambiguous name. Read replies list exact retries
when possible. Positions use a 1-based line and a 1-based UTF-16 column. A tab
counts as one character. Paths are relative to the selected workspace unless an
absolute path inside it is needed.

## Navigation and analysis

| Tool | Parameters and result |
|---|---|
| `lookup` | `name` and optional `file_path`, or `file_path` with `line` and `column`. The default shows the signature, location, brief documentation and usage counts. Use `show` for specific sections. `limit=20`, `offset=0` apply to each section. |
| `search` | `query`, optional `kind` and `path`, `scope="symbols"`, `limit=30`, `offset=0`. Dotted queries narrow symbols by owner. A path can be a directory prefix, a file or a glob. `scope="mentions"` searches for names in strings, comments and other text. |
| `outline` | `file_path`, optional `depth`. Shows definitions and their line numbers. `depth=1` shows the top level. |
| `check` | `file_path` for one file, `since` for a git baseline, or neither for workspace settings and current ty diagnostics. `limit=30`, `offset=0`. For git comparisons, `include_dependents=false` limits the check to changed files. |
| `unused` | Optional `path` and `kind`, `include_private=true`, `limit=30`, `offset=0`. Finds definitions without linked uses and distinguishes symbols used only by tests, mentioned in strings or exported from the project. |
| `impact` | `name`, optional `file_path`, `change="any"`, `depth=1`, `limit=30`. Change kinds include `signature`, `rename`, `delete` and `type`. Groups callers, argument styles, value uses, related definitions, importers, tests and text mentions. A depth above one follows callers transitively. |

`lookup` accepts `signature`, `doc`, `source`, `references`, `callers`,
`implementations`, `subclasses`, `overrides` and `mentions` in `show`. The
signature and location are always included. `source` requests the full
definition. Protocol callers through the Protocol and through concrete
implementations appear in separate groups. The source root and configured extra
roots determine which structural implementations can be found. To scan test
doubles outside the source root, add their directories to
`CODENAV_MCP_EXTRA_SOURCE_ROOTS`.

```text
lookup(name="StorePort.save", show=["callers", "implementations"], limit=5)
search(query="StorePort.save", scope="mentions", path="tests")
check(file_path="src/store.py")
check(since="HEAD", limit=20)
```

`check()` runs the workspace's ty CLI and shows its version, roots, Python
configuration and diagnostic count. The file and git modes use the running
language server. Git comparison includes changed files and importers. Added
Python files have no baseline and are reported separately. When a ty
configuration file changed, the result shows diagnostics under the current
configuration because it cannot compare against the old configuration.

## Refactors and fixes

| Tool | Parameters and result |
|---|---|
| `rename` | `name`, `new_name`, optional `file_path`, `apply=true`, optional `sites`. Updates typed references, linked overrides, implementations and exact patch-target strings. Review uncertain sites by ID, then opt in with `sites=["u1"]` or `sites=["all"]`. |
| `change_signature` | `name`, optional `file_path`, `add`, `remove`, `reorder`, `include_overrides=true`, `apply=true`, `max_new_errors=0`, `allow_large=false`. Rewrites typed callers and reports call sites that require manual attention. |
| `move` | `to_file` and either `name` with optional `file_path` or just the module's `file_path`. Supports `keep_reexport` and `allow_import_cycle` for symbol moves, plus `apply`, `max_new_errors` and `allow_large`. |
| `edit_symbol` | For one definition, use `name`, `action`, `source` and optional `file_path`. Use `edits` for a group of definition edits and literal replacements. Options include `apply`, `max_new_errors`, `force`, `imports`, `position` and `prune_imports`. |
| `fix` | `file_path`, optional `line`, `code` and `choice`, `allow_suppress=false`, `apply=true`, `max_new_errors=0`. Applies ty quick fixes and merges missing imports where possible. |
| `edits` | `action="list"`, optional `id`, `sites`, `allow_large=false` and `limit=10`. Lists previews and applied edits, shows a diff by ID, applies a preview or chosen sites, and undoes an applied edit. |

### Signature changes

Each `add` item takes `name` and at least one of `default` and `value`. It
also accepts `annotation`, `position` and `keyword_only`. A `default`
appears in the definition. A `value` is inserted at each call site. A
defaulted parameter is appended as keyword-only unless you supply a position
or `keyword_only=false`. Positions are 0-based and exclude `self` and
`cls`. `remove` drops the named parameters and their corresponding
arguments. `reorder` takes the complete new parameter order.

The planner holds a preview when it cannot rewrite a call or value use. Review
the listed sites before applying the preview. Optional site IDs are currently
available for `rename`.

### Moves

A symbol move handles module-level functions and classes, carries required
imports, and rewrites importers. `keep_reexport=true` keeps imports from the
original module working and preserves the export in `__all__`. Circular
imports can hold a move. `allow_import_cycle=true` explicitly accepts the
cycle for a symbol move. A module move rewrites imports for the module's new
path. References through module attributes, strings and documentation can
require manual edits.

### Definition edits

- `replace` replaces the complete definition, including its decorators.
- `replace_body` changes the function body and preserves its signature and decorators.
- `insert` takes a `file_path` and `source`. Its `position` can be `before`
  or `after` a named definition, `into` a class or `end` of file.
  `insert_before` and `insert_after` are shortcuts.
- `delete` reports remaining usages and defaults to pruning imports used
  only by the deleted definition. `force=true` authorizes deletion despite
  usage findings. The type gate still applies.
- `imports` adds missing import statements when replacing or inserting code.

For several edits, send a list of definition edits or text items shaped as
`{file_path, old_string, new_string}`. Text items can use `replace_all=true`
or supply `new_text` for the whole file. Items match the current on-disk
source, must have disjoint changes, and combine into one plan, one type
check and one undo ID.

```text
edit_symbol(edits=[{"file_path":"src/a.py","old_string":"x = 1","new_string":"x = 2"},{"name":"legacy","action":"delete"}], apply=false)
```

### Fix selection

Narrow fixes using `line` or a diagnostic `code`. If several fixes are
available, repeat the call with a 1-based `choice` or a substring of a
fix title. Suppression fixes require `allow_suppress=true`.

### Preview, apply and undo

Use `apply=false` to inspect a change. Writes default to a type check that
allows zero new errors. A held plan can be reviewed with `edits(action="show",
id="…")`. `edits(action="apply", id="…")` is explicit approval to write
that plan, including any type errors reported in its preview. `allow_large=true`
permits applying a stored plan that exceeds the file-count limit.
`edits(action="undo")` rolls back the newest applied edit, or pass an ID
to choose one. Applying or undoing refuses to overwrite later changes.

The last 50 previews and applied edits persist per workspace under
`$XDG_STATE_HOME/codenav-mcp/` (default `~/.local/state/codenav-mcp/`). A preview
that creates a file is held for explicit approval because checking it needs a
temporary file. A crash during a write can leave partial changes. The type
gate checks types only. Run relevant tests and documentation checks.

## Arguments and errors

Aliases include `symbol`, `symbol_name`, `qualified_name` and `query` for
`name`; `name` for search's `query`; `file`, `filepath` and `path` for
`file_path`; `col` for `column`; and `line_number` and `lineno` for
`line`. Text edit items accept `old_str` and `new_str`. A tool's actual
parameter takes precedence over an alias. Conflicting aliases are refused.

## Output limit

Replies use workspace-relative diagnostic and symbol paths. A paginated
section shows its remaining count and an exact continuation call. The
server caps each reply at `CODENAV_MCP_MAX_OUTPUT_CHARS` (30,000 by default).
Use `limit`, `offset`, `path` or a narrower name when a large result is cut.

## Directories never scanned or written

Scanner and write exclusions include `.git`, `.venv`, `node_modules`, `vendor`,
`dist`, `build`, cache directories and nested repositories. Configure directory
names with `CODENAV_MCP_EXCLUDE_DIRS` and `CODENAV_MCP_INCLUDE_DIRS`.
[Configuration](configuration.md#environment)

## Known limits

Navigation and writes target Python files, and mention search can scan other
text files. Static analysis cannot resolve every dynamic reference, mock,
framework registration or library extension. Check runtime behavior and
review optional sites. Files outside the workspace and files with more than
one hard link are protected from writes.
