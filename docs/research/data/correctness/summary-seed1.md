# Refactor fuzzer summary (80 cases)

| invariant | pass | fail | n/a |
|---|---|---|---|
| I1 | 63 | 1 | 16 |
| I2 | 40 | 0 | 40 |
| I3 | 80 | 0 | 0 |
| I4 | 12 | 0 | 68 |
| I5 | 80 | 0 | 0 |

| op | cases | applied | held | error | crash | median s |
|---|---|---|---|---|---|---|
| rename | 24 | 23 | 0 | 1 | 0 | 7.2 |
| rename_all | 17 | 17 | 0 | 0 | 0 | 14.1 |
| change_signature | 14 | 13 | 0 | 1 | 0 | 13.4 |
| move | 13 | 12 | 0 | 1 | 0 | 3.1 |
| delete | 12 | 12 | 0 | 0 | 0 | 6.4 |

| project | cases | mean s/case | max s |
|---|---|---|---|
| click | 24 | 6.6 | 9.9 |
| srxy | 32 | 14.6 | 16.6 |
| tomlkit | 24 | 3.4 | 5.5 |

## Failing cases (1)

- `srxy-1-13` rename `QNodeType` (src/srxy/domain/models.py) -> I1: new failures not traced to a listed site: unlisted=['test tests.unit.test_public_api::test_given_public_api_when_rendering_reference_then_matches_committed_docs'] silent=[]

## Over-rename damage at opted-in sites, not I1 (2)

- `srxy-1-14` InstallOptions.srxy_spec: ['test tests.unit.test_installer_engine_integration::test_given_english_install_when_headless_then_progress_matches_language', 'test tests.unit.test_installer_engine_integration::test_given_spanish_install_when_headless_then_progress_is_spanish_ascii', 'test tests.unit.test_installer_main::test_given_headless_install_when_invoked_then_calls_install_srxy']
- `click-1-19` open_stream: ['test no-junit::tests']
