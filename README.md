# codenav-mcp

[![PyPI](https://img.shields.io/pypi/v/codenav-mcp)](https://pypi.org/project/codenav-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/codenav-mcp)](https://pypi.org/project/codenav-mcp/)
[![CI](https://github.com/illescasDaniel/codenav-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/illescasDaniel/codenav-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Python code navigation and type-checked refactoring over MCP, powered by Astral's ty.
Use it to follow typed calls through imports and dependency injection, find structural
Protocol implementations, and preview refactors that touch several files. Use grep
for exact-name text lookups and Read for a known file or excerpt.

![An agent asks codenav for a symbol by name; ty resolves definitions, references, callers and implementations. For edits, codenav builds the change in memory, offers text mentions as optional sites, checks types with ty, and shows a preview that can be applied and undone.](assets/how-it-works.svg)

An agent asks by name, and ty follows the types to answer. For an edit, codenav builds the change in memory, offers
names it found in strings, docs and shell heredocs as optional sites, and writes only if ty reports no new type
errors. Every change has a preview ID to apply and an undo ID.

## What it can do

- **Follow typed code:** find callers and Protocol implementations across imports and dependency injection.
- **Preview refactors:** rename symbols, change signatures and move definitions with their linked usages.
- **Catch overlooked sites:** surface mentions in strings, docs and shell heredocs for review.
- **Review and undo:** inspect type-checked previews before applying them, then undo stored changes.

For agent workflows, exact calls and parameters, see the [agent tool guide](docs/tools.md#agent-quick-reference).

## Quick start

Install [uv](https://docs.astral.sh/uv/) and [Node.js 22+](https://nodejs.org/), then run this from your project folder. [mcps](https://github.com/illescasDaniel/mcps) prompts you to choose agents and registers CodeNav locally:

```bash
npx @danielxir96/mcps add illescasDaniel/codenav-mcp
```

<details>
<summary>Register directly with an individual MCP host</summary>

```bash
claude mcp add codenav -- uvx codenav-mcp
codex mcp add codenav -- uvx codenav-mcp
opencode mcp add codenav -- uvx codenav-mcp
```

</details>

Some models don't pick up new tools on their own (see [What has been measured](#what-has-been-measured)). One line in
the project's `AGENTS.md` or `CLAUDE.md` is enough:

```text
For Python code navigation and refactors (finding callers or references, renaming, changing signatures, moving code),
prefer the codenav MCP tools over grep, sed and hand edits: they follow the types.
```

## Requirements and limits

- Python 3.11 or later. ty installs with codenav, and a workspace's own ty takes priority when present.
- Python navigation and writes cover `.py` and `.pyi` files. Positions use 1-based lines and UTF-16 columns.
- The write gate checks types only. Run the relevant tests and documentation checks after applying a change.
- Static analysis can't resolve every dynamic reference, mock, framework registration or library extension; review
  the optional sites a rename offers. [Known limits](docs/tools.md#known-limits)

## What has been measured

Small models (Claude Haiku 5.5, GPT-6 Luna) did real tasks on one project with and without codenav, and every run was
graded against a hand-checked key. These are pilots with a few runs per task: read them as direction, not effect
sizes.

- **Completeness on a big rename:** a 35-file rename where the tests can't see 4 of the files (an example, a doc page,
  two shell scripts). Luna with codenav updated all 4 in 4 of 5 runs, without it in 0 of 4.
- **Effort:** Haiku was usually complete either way, but with codenav it used 161k-359k input tokens against
  368k-519k.
- **Adoption:** Haiku used codenav unprompted in 17 of 18 runs. Luna used it in 0 of 12 runs without the hint line
  above, and 12 of 13 with it.
- **Where it didn't help:** on tasks where names are distinct and the tests thorough, careful grep was just as
  correct, and codenav sometimes cost more tokens.

Findings, data and the harness to repeat the runs: [Research](docs/research/README.md).

## How it is tested

A refactoring tool must not break code quietly, so codenav is tested in several ways:

- **About 1,300 tests:** unit tests, plus about 350 integration tests that run the tools against a real ty server.
- **Property tests:** about 190, with Hypothesis and generated Python (hypothesmith), including a state machine for
  preview, apply and undo, and CrossHair symbolic checks.
- **A refactor fuzzer:** random renames, moves and signature changes on real projects (srxy, click, tomlkit), each
  checked against the project's own tests and ty. 187 refactors so far, none silently broke code.
- **Differential checks:** references agree with jedi on 40 of 40 symbols, and renames with rope on 38 of 40; the 2
  differences are call sites rope misses.
- **Mutation testing** (mutmut) of the modules where a wrong edit would go unnoticed, to find tests that don't check
  enough. The suite catches about 84% of the mutants there; the gaps that mattered got tests.
- **Agent pilots:** the small-model runs above, graded by hand.

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
