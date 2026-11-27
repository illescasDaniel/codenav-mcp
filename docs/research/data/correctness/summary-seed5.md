# Refactor fuzzer summary (16 cases)

| invariant | pass | fail | n/a |
|---|---|---|---|
| I1 | 16 | 0 | 0 |
| I2 | 10 | 0 | 6 |
| I3 | 16 | 0 | 0 |
| I4 | 0 | 0 | 16 |
| I5 | 16 | 0 | 0 |

| op | cases | applied | held | error | crash | median s |
|---|---|---|---|---|---|---|
| rename | 7 | 7 | 0 | 0 | 0 | 14.0 |
| rename_all | 3 | 3 | 0 | 0 | 0 | 7.4 |
| move | 6 | 6 | 0 | 0 | 0 | 4.8 |

| project | cases | mean s/case | max s |
|---|---|---|---|
| click | 5 | 8.2 | 10.1 |
| srxy | 7 | 14.5 | 15.8 |
| tomlkit | 4 | 3.4 | 5.0 |

## Failing cases (0)


## Over-rename damage at opted-in sites, not I1 (0)

