# Legal – Travel overlay (`legal-travel` v1.0.0)

> Generated from [`schemas/industries/legal.yaml`](../../schemas/industries/legal.yaml) by `tools/schematool.py docs`. Do not edit by hand.

Travel purposes specific to legal work.

| Kind | Depends on | Entities | Extended entities | Enums | Term sets |
|---|---|---|---|---|---|
| industry | [`travel`](travel.md) | 0 | 0 | 0 | 0 |

## Reference data (seed)

Loaded at deployment. Deactivate rows you do not need instead of deleting them.

### TravelPurpose (2 rows)

| code | name | requiresClient | isBillableDefault |
|---|---|---|---|
| LEG_COURT | Court hearing / trial | yes | yes |
| LEG_DEPOSITION | Deposition / witness meeting | yes | yes |
