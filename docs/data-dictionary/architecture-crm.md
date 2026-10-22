# Architecture – CRM overlay (`architecture-crm` v1.0.0)

> Generated from [`schemas/industries/architecture.yaml`](../../schemas/industries/architecture.yaml) by `tools/schematool.py docs`. Do not edit by hand.

Bids, competitions and frameworks pipeline for architecture practices.

| Kind | Depends on | Entities | Extended entities | Enums | Term sets |
|---|---|---|---|---|---|
| industry | [`crm`](crm.md) | 0 | 0 | 0 | 0 |

## Reference data (seed)

Loaded at deployment. Deactivate rows you do not need instead of deleting them.

### LeadSource (3 rows)

| code | name |
|---|---|
| ARC_COMPETITION | Design competition |
| ARC_FRAMEWORK | Public framework / tender portal |
| ARC_REF_CONSULTANT | Referral – engineer / contractor |

### SalesPipeline (1 rows)

| code | name |
|---|---|
| ARC_BIDS | Bids & competitions |

### PipelineStage (6 rows)

| pipeline | code | name | sequence | defaultProbability | isClosed | isWon |
|---|---|---|---|---|---|---|
| ARC_BIDS | ARC_B_EOI | Expression of interest | 1 | 10 | no | no |
| ARC_BIDS | ARC_B_PQQ | Pre-qualification / shortlist | 2 | 25 | no | no |
| ARC_BIDS | ARC_B_TENDER | Tender / competition entry | 3 | 40 | no | no |
| ARC_BIDS | ARC_B_INTERVIEW | Interview | 4 | 60 | no | no |
| ARC_BIDS | ARC_B_AWARDED | Awarded | 5 | 100 | yes | yes |
| ARC_BIDS | ARC_B_UNSUCCESSFUL | Unsuccessful | 6 | 0 | yes | no |
