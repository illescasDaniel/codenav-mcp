# Refactor fuzzer summary (60 cases)

| invariant | pass | fail | n/a |
|---|---|---|---|
| I1 | 43 | 0 | 17 |
| I2 | 30 | 0 | 30 |
| I3 | 58 | 2 | 0 |
| I4 | 10 | 0 | 50 |
| I5 | 60 | 0 | 0 |

| op | cases | applied | held | error | crash | median s |
|---|---|---|---|---|---|---|
| rename | 21 | 16 | 0 | 5 | 0 | 5.6 |
| rename_all | 14 | 14 | 0 | 0 | 0 | 14.0 |
| change_signature | 10 | 10 | 0 | 0 | 0 | 3.4 |
| move | 3 | 3 | 0 | 0 | 0 | 13.3 |
| delete | 12 | 10 | 0 | 2 | 0 | 6.4 |

| project | cases | mean s/case | max s |
|---|---|---|---|
| click | 18 | 7.1 | 9.8 |
| srxy | 24 | 13.2 | 17.5 |
| tomlkit | 18 | 3.6 | 5.6 |

## Failing cases (2)

- `click-2-12` rename_all `confirmation_option` (src/click/decorators.py) -> I3: edits outside source/test roots or in non-Python files: ['examples/repo/repo.py']
- `click-2-16` rename `ParamType.fail` (src/click/types.py) -> I3: edits outside source/test roots or in non-Python files: ['examples/validation/validation.py']

## Over-rename damage at opted-in sites, not I1 (1)

- `srxy-2-0` Capabilities.ocr: ['test tests.cli.test_cli::test_given_grouped_stream_when_running_cli_then_prints_summary_after_matches', 'test tests.cli.test_cli::test_given_hidden_directory_when_running_cli_with_include_hidden_then_searches_hidden_entries', 'test tests.cli.test_cli::test_given_invalid_boolean_query_when_running_cli_then_exits_with_error']
