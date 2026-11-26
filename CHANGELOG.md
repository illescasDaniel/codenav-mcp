# Changelog

## Unreleased

### Fixed

- Shorten the package and MCP Registry descriptions to meet the registry schema limit.

## 1.0.2

### Changed

- Tool replies are text-only: `structuredContent` is no longer emitted and tools no longer advertise an output schema. Agents and hosts read the same text as before; a programmatic client that parsed `structuredContent` should parse the text instead. A typed payload alongside the report is a candidate for a future minor version.

### Improved

- Refusals for an unknown argument suggest the closest real name (`unknown argument 'file' for outline; did you mean 'file_path'?; allowed: file_path, depth`), so an agent gets there on the first retry. The surface stays strict: no aliases were added.
- A `server.json` manifest (MCP Registry format) ships in the repository root, kept in sync with the package metadata and documented settings by a test.
- The README gained an Examples section with real tool output (navigation, references, a refused unsafe edit, a type-checked rename).

## 1.0.1

### Fixed

- A workspace with a huge number of Python files (for example a whole home directory, 700k+ files) no longer exhausts memory: the server counts Python files before starting the language server and refuses a tree over the cap (startup exit code 2 with a one-line message; a client-driven switch to such a tree is refused the same way).

### Added

- `CODENAV_MCP_MAX_FILES` (default 250,000) sets that cap. See [configuration](docs/configuration.md).

## 1.0.0

Release summary by area. Details and examples are in the [tool reference](docs/tools.md); settings are in [configuration](docs/configuration.md).

### Added

- Settings: `CODENAV_MCP_MAX_OUTPUT_CHARS` (output cap, default 30,000 characters), `CODENAV_MCP_EXCLUDE_DIRS` and `CODENAV_MCP_INCLUDE_DIRS` (directory names never scanned or written, adjusted), and `CODENAV_MCP_ALLOW_READONLY_FILES` (let write tools edit read-only Python files).
- Tool parameters: `references(limit=)` and `callers(limit=)` (cap the list; the first line is the exact count), `outline(depth=)` (top-level definitions only), and `move(allow_import_cycle=)`.
- The shared package gained the `mcp_nav_shared.output` module (output cap helpers).

### Output

- Every tool result is capped at the output budget, and `batch` shares one budget among its calls. Shortened results say so and name the calls.
- `references` and `callers` always print the exact count, and `search_symbol` says when it shows fuzzy matches only.

### Writes and safety

- Hard-linked files are refused, because writing them would change names outside the workspace.
- Read-only files are refused unless `CODENAV_MCP_ALLOW_READONLY_FILES` is set. Case-only name collisions are refused.
- Nested git checkouts, excluded directories and symlinks that leave the workspace are never written.
- Failed writes name the file and the reason, roll back what was written, and report `COULD NOT RESTORE` if rollback fails.
- Undo keeps the last 20 applied edits and removes the files and directories an edit created.
- `verify_changes` checks modified Python files and reports added ones as not checked; it refuses only when every changed Python file is new.

### Language server

- Previews no longer leak unsaved text into concurrent reads.
- Hung `uvx` and ty processes are stopped with their whole process group.
- Same-size, same-mtime rewrites are detected through ctime.
- A missing ty or uvx gives a clear message. The exit message no longer shows the command line or raw stderr.
- ty restarts are automatic, and only a restart caused by your own edit of a config file is reported.

### Text model

- Line endings follow Python and the language server: only LF, CRLF or a lone CR end a line. Form feed, NEL, U+2028 and U+2029 are ordinary characters.
- A UTF-8 byte-order mark is kept by every write tool and never counted as a column.
- The syntax gate uses the compiler's checks and names the file and Python version. NUL bytes and stray byte-order marks are refused with short messages.
- Text inserted into a CRLF file uses CRLF. Plan summaries list at most 60 files.

### Imports and moves

- Importer discovery no longer skips importers silently. Excluded directories, nested repositories and unreadable or unparsable files are reported by name.
- `move` holds back a move when an importer cannot be parsed or when more than 2000 importers exist, unless `apply=true` and `allow_large=true` are given.
- `move` refuses a module-level import cycle, and a function that assigns a module global.
- Side-effect imports stay in the source module. Relative imports keep their form. `__all__` entries move with the symbol.
- The most specific source root names the moved module. New imports go into the first import block.
- Imports merged into an existing `from` line are sorted case-insensitively, and existing comments are kept.

### Rename and signatures

- `rename_symbol` refuses dunder methods and overrides of base classes outside the workspace, and refuses a new name that is already bound.
- Rename updates a literal `__all__`, and renames `.pyi` stubs together with their `.py` files.
- `change_signature` finds constructor calls, and lists calls it cannot rewrite safely.
- `edit_symbol` deletes keep comments above the definition, and overloads act on the implementation.

### Navigation reads

- Paths outside the workspace are refused before the file is read.
- `definition` shows library locations as `<stdlib>/...` and `<site-packages>/...`, and lists `.pyi` stubs first.
- `symbol_info` matches names after NFKC normalisation, suggests close names, and resolves dotted module paths.
- `hover` explains a `def` or `class` keyword position, and `search_symbol` has a total order.
- `references` and `callers` exclude results outside the workspace and list production files before tests.

### Tool contract and errors

- Refusals and failed calls return `isError: true`. Previews and clean no-op answers do not.
- Unknown or mistyped arguments are refused by name, with the allowed names.
- A batch reports `batch: N/M ok, K failed`, and refuses a result reference such as `$1`.
- Calls before `initialize` are refused.

### Configuration

- The pinned workspace and `CODENAV_MCP_SOURCE_ROOT` are checked at startup.
- A `src/` layout is detected as the source root, and `workspace` reports it.
- Invalid `extra source root` entries are skipped with a note.
- A monorepo root without `pyproject.toml` is documented, with the workaround.

### Known limits

- After SIGKILL, during a preview or in a worker-thread write, an edit can be left partly applied, and preview files are left for manual inspection. See [known limits](docs/tools.md#known-limits).
- A rename of a symbol whose stub or alias twins live in a `.pyi` file or a sibling re-export package (attrs-style) rewrites the module and the package `__all__`, but re-exports in sibling packages can be left unrewritten; they are listed, not silently skipped.
- A pre-existing diagnostic that moves with the code can still count as a new error when `allow_import_cycle=true` is passed.
- A module that is moved out and then back leaves the previous destination file empty (named in the reply).
- Replies larger than about 1 MB hit the language-server timeout (~20 s) and come back as an error; the call can be retried with a `limit=`.
- Raw malformed input on stdin is dropped by the MCP SDK with no reply (by design, see development.md).
- Files using Python 3.13/3.14-only syntax are refused for writes (the bundled parser is older); run the server under a newer Python to edit them.
- A move that would create a module-level import cycle (including one that already exists through the destination) is refused unless `allow_import_cycle=true` is passed; cycles through function-level imports only get a note.
- Changes under excluded directories are not scanned, written or watched; the directories are named in the replies.
- `verify_changes` lists added Python files as not checked (no committed baseline) and refuses only when nothing can be compared.
