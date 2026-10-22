# Accounting – Travel overlay (`accounting-travel` v1.0.0)

> Generated from [`schemas/industries/accounting.yaml`](../../schemas/industries/accounting.yaml) by `tools/schematool.py docs`. Do not edit by hand.

Travel purposes specific to audit and accounting work.

| Kind | Depends on | Entities | Extended entities | Enums | Term sets |
|---|---|---|---|---|---|
| industry | [`travel`](travel.md) | 0 | 0 | 0 | 0 |

## Reference data (seed)

Loaded at deployment. Deactivate rows you do not need instead of deleting them.

### TravelPurpose (2 rows)

| code | name | requiresClient | isBillableDefault |
|---|---|---|---|
| ACC_FIELDWORK | Audit fieldwork at client site | yes | yes |
| ACC_STOCKTAKE | Stock-take attendance | yes | yes |
