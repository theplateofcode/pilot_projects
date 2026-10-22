# Legal – HR overlay (`legal-hr` v1.0.0)

> Generated from [`schemas/industries/legal.yaml`](../../schemas/industries/legal.yaml) by `tools/schematool.py docs`. Do not edit by hand.

Lawyer credentials and CLE/CPD tracking.

| Kind | Depends on | Entities | Extended entities | Enums | Term sets |
|---|---|---|---|---|---|
| industry | [`hr`](hr.md) | 0 | 0 | 0 | 1 |

## Term sets

Stored in the SharePoint term store; mirrored to the Term table on Dataverse/SQL.

### TrainingCategory (adds to [TrainingCategory](hr.md#trainingcategory))

- Ethics & professional responsibility
- CLE / CPD – legal

## Reference data (seed)

Loaded at deployment. Deactivate rows you do not need instead of deleting them.

### JobFamily (2 rows)

| code | name |
|---|---|
| LEG_FEE_EARNER | Fee earners (lawyers) |
| LEG_BUS_SERVICES | Business services |

### CredentialType (3 rows)

| code | name | category | requiresRenewal | renewalPeriodMonths | requiresCpd |
|---|---|---|---|---|---|
| LEG_BAR | Bar admission / practising licence | professionalLicence | yes | 12 | yes |
| LEG_SOLICITOR | Solicitor practising certificate | professionalLicence | yes | 12 | yes |
| LEG_NOTARY | Notary public commission | professionalLicence | yes |  |  |
