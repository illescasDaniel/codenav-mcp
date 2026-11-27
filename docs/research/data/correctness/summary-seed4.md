# Refactor fuzzer summary (16 cases)

| invariant | pass | fail | n/a |
|---|---|---|---|
| I1 | 15 | 0 | 1 |
| I2 | 10 | 0 | 6 |
| I3 | 16 | 0 | 0 |
| I4 | 0 | 0 | 16 |
| I5 | 16 | 0 | 0 |

| op | cases | applied | held | error | crash | median s |
|---|---|---|---|---|---|---|
| rename | 7 | 7 | 0 | 0 | 0 | 13.6 |
| rename_all | 3 | 3 | 0 | 0 | 0 | 9.3 |
| move | 6 | 6 | 0 | 0 | 0 | 5.1 |

| project | cases | mean s/case | max s |
|---|---|---|---|
| click | 5 | 8.2 | 9.3 |
| srxy | 7 | 14.6 | 16.9 |
| tomlkit | 4 | 3.4 | 5.0 |

## Failing cases (0)


## Over-rename damage at opted-in sites, not I1 (1)

- `tomlkit-4-3` loads: ['test ::tests.test_api', 'test ::tests.test_array_appends', 'test ::tests.test_build']
