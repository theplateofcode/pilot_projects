# Architecture – HR overlay (`architecture-hr` v1.0.0)

> Generated from [`schemas/industries/architecture.yaml`](../../schemas/industries/architecture.yaml) by `tools/schematool.py docs`. Do not edit by hand.

Architect registration and sustainability accreditations.

| Kind | Depends on | Entities | Extended entities | Enums | Term sets |
|---|---|---|---|---|---|
| industry | [`hr`](hr.md) | 0 | 0 | 0 | 0 |

## Reference data (seed)

Loaded at deployment. Deactivate rows you do not need instead of deleting them.

### CredentialType (5 rows)

| code | name | category | requiresRenewal | renewalPeriodMonths | requiresCpd | issuingBody |
|---|---|---|---|---|---|---|
| ARC_REGISTERED | Registered architect (ARB / NCARB / CoA) | professionalLicence | yes | 12 | yes |  |
| ARC_RIBA_AIA | RIBA / AIA membership | professionalMembership | yes | 12 | yes |  |
| ARC_LEED_AP | LEED AP | certification | yes | 24 | yes | GBCI |
| ARC_PASSIVHAUS | Certified Passivhaus Designer | certification |  |  |  |  |
| ARC_SITE_SAFETY | Construction site safety card | certification | yes | 60 |  |  |

### Skill (5 rows)

| code | name | category |
|---|---|---|
| ARC_REVIT | Autodesk Revit | Tools & software |
| ARC_AUTOCAD | AutoCAD | Tools & software |
| ARC_RHINO | Rhino / Grasshopper | Tools & software |
| ARC_RENDERING | Rendering (Enscape / V-Ray) | Tools & software |
| ARC_BIM | BIM coordination (ISO 19650) | Industry / domain |
