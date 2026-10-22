# Schema principles — how the schemas stay futureproof

These rules are why the schemas can survive new industries, new clients, new platforms and years of change.
Most of them are enforced by `tools/schematool.py validate`; the rest are review checklist items.

## 1. Structure

**1.1 One canonical model, many physical builds.**
The YAML in `schemas/` is the only place a table or column is defined. Dataverse solutions, SharePoint
templates, SQL migrations and documentation are all derived from it. When a platform changes or a new one
appears, you write a new generator — not a new data model.

**1.2 Layers depend downwards only.**
`core` ← modules ← bridges ← industry overlays ← client overlays. A layer can only reference tables, enums
and term sets from itself and its dependencies. *(Enforced.)*

**1.3 Modules stand alone; bridges connect them.**
Travel does not depend on Work Management. When both are deployed, the `travel-work` bridge adds
`TripRequest.project`. Each pilot can be sold and installed on its own. *(Enforced through dependencies.)*

**1.4 Extend, never modify.**
Industry and client differences are added in their own layer (`extend:` adds columns, new entities add
tables, `seed:` adds reference data). Base layers are never edited for one client. This mirrors Dataverse
solution layering and means base upgrades never overwrite customisations. Two layers may not add the same
column to the same table. *(Enforced.)*

**1.5 Generic names, industry labels.**
Core names are industry-neutral (`Project`, `Account`, `ServiceLine`). Industries rename them through
`terminology` (Matter, Client, Practice Area). Never put an industry word into a base table or column name.

## 2. Identity and keys

**2.1 Every row has a surrogate GUID `id`.** Never reused, never meaningful, never shown to users.

**2.2 Business keys are separate.** Human-friendly numbers (`PRJ-000123`, `TRP-000045`) are autonumber
columns declared as alternate keys. They can change format per client without breaking relationships.

**2.3 Integration keys are explicit.** Tables that sync with other systems carry the `integration` trait
(`externalId`, `sourceSystem`, `lastSyncedOn`). Migrations and integrations upsert on these, never on `id`.

**2.4 People are keyed on Entra Object ID.** `Employee.entraObjectId` links the directory record to Entra
ID, Dataverse users and SharePoint Person columns. Email addresses and UPNs change (marriage, rebrand,
domain move) — never key on them.

**2.5 Reference data has a natural key (`code`).** Seeds and lookups between seeds use codes such as
`LEG_LITIGATION` or `USD`, so reference data can be loaded into any environment without GUIDs.
*(Enforced: seeded tables need a natural key; seed lookups must resolve.)*

## 3. Naming

| Item | Convention | Example |
|---|---|---|
| Layer id | kebab-case | `legal-work` |
| Entity | PascalCase, singular, ≤ 40 chars | `TripRequest` |
| Column | camelCase, ≤ 32 chars, no abbreviations | `estimatedCloseDate` |
| Enum | PascalCase; keys camelCase | `LeaveRequestStatus.submitted` |
| Seed code | UPPER_SNAKE, prefixed per overlay | `ARC_NEW_BUILD`, `ACC_AUDIT` |
| Booleans | `is…`, `has…`, `requires…`, `allow…` | `isBillable` |
| Lookups | Named after the role, not the table | `projectManager`, not `employee2` |

Names that clash with built-in platform columns (`title` except as the primary column, `author`, `editor`,
`created`, `modified`, `id`, `version`, `path`, `order`, `key`…) are rejected. *(Enforced.)*
Rename before building — after a table exists, renaming is expensive on every platform.

## 4. Choices, reference tables and term sets

| Use | When | Examples |
|---|---|---|
| **Enum (choice)** | The values drive logic and rarely change; same for every client | `TripStatus`, `ApprovalStatus`, `BillingModel` |
| **Reference table** | Clients will add, rename or retire values, or the values carry extra data (rates, limits, GL codes) | `ProjectType`, `LeaveType`, `ExpenseCategory`, `TimeCode`, `CredentialType` |
| **Term set** | Hierarchical or tagging vocabularies, shared with SharePoint metadata | `ServiceLine`, `Industry`, `NewsCategory`, `PolicyCategory` |

Enum rules:
- Every value has an explicit numeric **code** that never changes and is never reused. Retire a value with
  `deprecated: true` instead of deleting it. *(Codes and keys are checked for uniqueness.)*
- Dataverse option values are generated as the publisher's choice prefix + code, so they are stable
  across environments.
- Apps and flows compare **keys or codes**, never labels — labels get translated and relabelled.

## 5. Status and lifecycle

Three different things, three different columns:

| Column | Meaning | Example |
|---|---|---|
| `stateCode` (base trait) | Is the record active? Deactivate instead of delete | Retired leave type |
| `status` (per entity) | Where the business process is | Trip `submitted` → `approved` → `booked` |
| `approvalRequest` (lookup) | The latest approval run, from the shared approval engine | Expense report approval |

## 6. Money, numbers and dates

- **Money** is `decimal(19,4)` plus a `currency` lookup on the same row — never floating point, never a
  currency-less amount. Where two currencies meet (expense lines), each amount names its currency column.
  *(Enforced: every money column needs a Currency lookup.)*
- **Dates** that are business days (leave, due dates, deadlines) are **date-only** so they never shift across
  time zones. Moments in time (`submittedOn`) are date-time stored in UTC.
- **Durations** are whole minutes; **hours** for time entries are `decimal(6,2)`.

## 7. History and evidence

- **Effective dating** (`effectiveDated` trait: `validFrom`, `validTo`) for anything whose history matters:
  job assignments, compensation, policies, rates. Changes add a row; they never overwrite one.
- **Read models**: `Employee` is the current snapshot projected from HR's `JobAssignment`. Directory
  consumers read the snapshot; HR owns the history.
- **Append-only evidence** for compliance: `ConsentRecord`, `PolicyAttestation`, `ApprovalStep`. Corrections
  are new rows.
- **Audit**: Dataverse auditing and SharePoint versioning natively; `AuditEvent` for SQL builds.

## 8. Security and privacy by design

- Every entity declares **sensitivity** (`public`, `internal`, `confidential`, `restricted`, aligned to
  Purview labels) and **ownership** (user-owned for row-level security, or organisation-owned).
  Ownership is irreversible in Dataverse, so it is decided in the schema.
- Columns holding personal data are tagged **`pii: true`**. This drives column security, retention, masking
  in demo data and data-subject access exports.
- **Sensitive data lives in separate tables** (`EmployeePersonalDetails`, `Compensation`, `TravellerProfile`,
  `TravelDocument`, `PerformanceReview`) so it can be secured on every platform, including SharePoint.
- **Data minimisation is a feature.** The schema deliberately does not store bank details, health data,
  ethnicity, religion or full national ID numbers. Each exclusion is noted on the relevant table.
- Time-limited retention is modelled where the law requires it (`Candidate.retainUntil`, consent expiry).
- Secrets never go in tables — `AppSetting` is for non-secret configuration only.

## 9. Shared services instead of copies

Approvals (`ApprovalPolicy`, `ApprovalRequest`, `ApprovalStep`), documents (`DocumentReference`), comments
and audit are defined once in `core`. Business records hold a lookup to their latest `ApprovalRequest`;
shared records point back with a generic `regarding` (`polylookup(*)`), so new modules plug in without
changing `core`.

## 10. Documents outside the database

Files live in SharePoint, Blob storage or the client's DMS. The database stores metadata and a link
(`DocumentReference`, `*Url` columns). This keeps database capacity low and lets law firms keep iManage or
NetDocuments as the system of record.

## 11. Volume awareness

Every entity declares `volume: low | medium | high`. High-volume tables (`TimeEntry`, `WorkItem`,
`ExpenseLine`, `CardTransaction`, `ApprovalStep`, `PolicyAttestation`) get indexes, archiving plans and a
platform that can handle them. The data dictionary flags high-volume tables mapped to SharePoint lists.

## 12. Portable types only

The canonical model uses only types that every platform can represent (see the type mapping in the
[data dictionary](data-dictionary/README.md#logical-type--platform-mapping)). No JSON blobs for business
data, no platform-only column types. Composite types (`address`) and multi-valued types (`multilookup`,
`multichoice`, `multiterm`) have a defined expansion on each platform (columns, N:N or junction tables).

## 13. Descriptions everywhere (AI- and catalogue-ready)

Every module, entity, enum and term set has a description, and non-obvious columns do too. These flow into
Dataverse table/column descriptions, which Copilot and Microsoft Purview use to understand the data, and
into this repo's data dictionary. *(Descriptions on entities, enums and term sets are enforced.)*

## 14. Localisation

Labels are separate from keys. `Language` is a reference table, content tables have a `language` column,
and terminology overrides are display labels only. Translating an app never changes the schema.

## 15. Multi-entity and multi-tenant ready

`LegalEntity` sits on financial and employment records so groups with several companies, countries and
currencies work from day one. A SQL/SaaS build can add a `tenantId` column through a generator option
without changing the canonical model.

## 16. Versioning and change control

- Each layer has a **semantic version**. Additive changes (new optional column, new table, new enum
  value) are **minor**; renames, removals and type changes are **major** and need a migration note.
- **Deprecate, don't delete**: mark columns and enum values `deprecated: true` for at least one major
  version before removing them.
- Every change goes through a pull request; CI runs `validate`, `docs --check` and the unit tests, so the
  YAML, the dictionary and the rules can never drift apart.

## Review checklist for a new table

- [ ] Generic name? Industry wording only in `terminology`
- [ ] Right layer? Does it really belong in core/module/bridge/overlay?
- [ ] Description written; non-obvious columns described
- [ ] Ownership, volume and sensitivity decided
- [ ] Primary column chosen (or `auto`)
- [ ] Alternate key for anything users or integrations look up by
- [ ] Enum vs reference table vs term set decided per column
- [ ] Money has currency; business dates are `date`
- [ ] PII tagged; sensitive data split out if needed
- [ ] `integration` trait if it syncs with another system
- [ ] `validate` and `docs` run; dictionary reviewed
