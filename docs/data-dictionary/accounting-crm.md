# Accounting – CRM overlay (`accounting-crm` v1.0.0)

> Generated from [`schemas/industries/accounting.yaml`](../../schemas/industries/accounting.yaml) by `tools/schematool.py docs`. Do not edit by hand.

Client profile data accounting firms need (entity type, year end, tax references).

| Kind | Depends on | Entities | Extended entities | Enums | Term sets |
|---|---|---|---|---|---|
| industry | [`crm`](crm.md) | 0 | 1 | 1 | 1 |

## Terminology

Display names this layer applies on top of the generic model.

| Generic name | Shown as |
|---|---|
| `Account` | Client / Clients |
| `Proposal` | Engagement Proposal / Engagement Proposals |

## Extensions to entities from other layers

### Account (+10)

Adds columns to [Account](core.md#account) from `core`.

| Column | Type | Req | Dataverse | SharePoint | SQL | Notes |
|---|---|:-:|---|---|---|---|
| `entityType` | choice([ClientEntityType](#cliententitytype)) |  | Choice (global: ClientEntityType) | Choice | int |  |
| `taxReference` | string(50) |  | Text (50) | Single line of text | nvarchar(50) | _restricted_ |
| `salesTaxNumber` | string(30) |  | Text (30) | Single line of text | nvarchar(30) | VAT / GST / sales tax registration. |
| `yearEndMonth` | integer |  | Whole number | Number (0 dp) | int |  |
| `yearEndDay` | integer |  | Whole number | Number (0 dp) | int |  |
| `isPublicInterestEntity` | boolean |  | Yes/No | Yes/No | bit |  |
| `isListed` | boolean |  | Yes/No | Yes/No | bit |  |
| `isAuditExempt` | boolean |  | Yes/No | Yes/No | bit |  |
| `accountingSoftware` | string(100) |  | Text (100) | Single line of text | nvarchar(100) | Client's bookkeeping system, e.g. Xero, QuickBooks, Tally, Sage. |
| `payrollHeadcount` | integer |  | Whole number | Number (0 dp) | int |  |

## Choices (enums)

Codes are stable forever. Dataverse option value = publisher choice prefix + code.

### ClientEntityType

Legal form of a client.

| Code | Key | Label |
|--:|---|---|
| 1 | `individual` | Individual |
| 2 | `soleTrader` | Sole Trader |
| 3 | `partnership` | Partnership |
| 4 | `llp` | LLP |
| 5 | `privateCompany` | Private Company |
| 6 | `publicCompany` | Public Company |
| 7 | `trust` | Trust |
| 8 | `charity` | Charity |
| 9 | `publicBody` | Public Body |

## Term sets

Stored in the SharePoint term store; mirrored to the Term table on Dataverse/SQL.

### ServiceLine (adds to [ServiceLine](core.md#serviceline))

- Audit & assurance
- Tax
- Advisory
- Accounts & bookkeeping
- Payroll services
- Forensic accounting

## Reference data (seed)

Loaded at deployment. Deactivate rows you do not need instead of deleting them.

### LeadSource (2 rows)

| code | name |
|---|---|
| ACC_REF_BANK | Referral – bank |
| ACC_REF_LAWYER | Referral – law firm |
