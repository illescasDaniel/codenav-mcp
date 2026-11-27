# Refactor fuzzer summary (15 cases)

| invariant | pass | fail | n/a |
|---|---|---|---|
| I1 | 13 | 0 | 2 |
| I2 | 8 | 0 | 7 |
| I3 | 15 | 0 | 0 |
| I4 | 1 | 0 | 14 |
| I5 | 15 | 0 | 0 |

| op | cases | applied | held | error | crash | median s |
|---|---|---|---|---|---|---|
| rename | 6 | 5 | 0 | 1 | 0 | 7.5 |
| rename_all | 3 | 3 | 0 | 0 | 0 | 14.2 |
| change_signature | 2 | 2 | 0 | 0 | 0 | 8.9 |
| move | 3 | 3 | 0 | 0 | 0 | 8.2 |
| delete | 1 | 1 | 0 | 0 | 0 | 13.2 |

| project | cases | mean s/case | max s |
|---|---|---|---|
| click | 4 | 6.8 | 9.8 |
| srxy | 7 | 14.7 | 17.1 |
| tomlkit | 4 | 4.1 | 5.2 |

## Failing cases (0)


## Over-rename damage at opted-in sites, not I1 (0)

