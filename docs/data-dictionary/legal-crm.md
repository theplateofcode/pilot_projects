# Legal – CRM overlay (`legal-crm` v1.0.0)

> Generated from [`schemas/industries/legal.yaml`](../../schemas/industries/legal.yaml) by `tools/schematool.py docs`. Do not edit by hand.

Law-firm language and reference data for business development.

| Kind | Depends on | Entities | Extended entities | Enums | Term sets |
|---|---|---|---|---|---|
| industry | [`crm`](crm.md) | 0 | 0 | 0 | 1 |

## Terminology

Display names this layer applies on top of the generic model.

| Generic name | Shown as |
|---|---|
| `Account` | Client / Clients |
| `Opportunity` | Pitch / Pitches |
| `Proposal` | Fee Proposal / Fee Proposals |
| `ServiceLine` | Practice Area / Practice Areas |

## Term sets

Stored in the SharePoint term store; mirrored to the Term table on Dataverse/SQL.

### ServiceLine (adds to [ServiceLine](core.md#serviceline))

- Litigation & dispute resolution
- Corporate & M&A
- Employment
- Real estate
- Intellectual property
- Banking & finance
- Private client
- Regulatory & compliance
- Tax

## Reference data (seed)

Loaded at deployment. Deactivate rows you do not need instead of deleting them.

### LeadSource (3 rows)

| code | name |
|---|---|
| LEG_REF_FIRM | Referral – other law firm |
| LEG_DIRECTORY | Legal directory listing |
| LEG_PANEL | Panel appointment |

### SalesPipeline (1 rows)

| code | name |
|---|---|
| LEG_PANELS | Panels & tenders |

### PipelineStage (5 rows)

| pipeline | code | name | sequence | defaultProbability | isClosed | isWon |
|---|---|---|---|---|---|---|
| LEG_PANELS | LEG_P_EOI | Expression of interest | 1 | 10 | no | no |
| LEG_PANELS | LEG_P_RFP | RFP response | 2 | 30 | no | no |
| LEG_PANELS | LEG_P_PITCH | Pitch / interview | 3 | 50 | no | no |
| LEG_PANELS | LEG_P_APPOINTED | Appointed | 4 | 100 | yes | yes |
| LEG_PANELS | LEG_P_UNSUCCESSFUL | Unsuccessful | 5 | 0 | yes | no |
