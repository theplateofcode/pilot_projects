# Accounting – HR overlay (`accounting-hr` v1.0.0)

> Generated from [`schemas/industries/accounting.yaml`](../../schemas/industries/accounting.yaml) by `tools/schematool.py docs`. Do not edit by hand.

Accountancy qualifications and CPE/CPD tracking.

| Kind | Depends on | Entities | Extended entities | Enums | Term sets |
|---|---|---|---|---|---|
| industry | [`hr`](hr.md) | 0 | 0 | 0 | 1 |

## Term sets

Stored in the SharePoint term store; mirrored to the Term table on Dataverse/SQL.

### TrainingCategory (adds to [TrainingCategory](hr.md#trainingcategory))

- CPE / CPD – technical accounting
- CPE / CPD – ethics

## Reference data (seed)

Loaded at deployment. Deactivate rows you do not need instead of deleting them.

### CredentialType (7 rows)

| code | name | category | requiresRenewal | renewalPeriodMonths | requiresCpd | issuingBody |
|---|---|---|---|---|---|---|
| ACC_CPA | CPA licence | professionalLicence | yes | 24 | yes |  |
| ACC_ACCA | ACCA membership | professionalMembership | yes | 12 | yes | ACCA |
| ACC_ACA | ICAEW Chartered Accountant (ACA) | professionalMembership | yes | 12 | yes | ICAEW |
| ACC_ICAI | Chartered Accountant (ICAI) | professionalMembership | yes | 12 | yes | ICAI |
| ACC_CIMA | CIMA / CGMA | professionalMembership | yes | 12 | yes | CIMA |
| ACC_CTA | Chartered Tax Adviser | professionalMembership | yes | 12 | yes |  |
| ACC_EA | Enrolled Agent | professionalLicence | yes | 36 | yes | IRS |
