# Technology – HR overlay (`technology-hr` v1.0.0)

> Generated from [`schemas/industries/technology.yaml`](../../schemas/industries/technology.yaml) by `tools/schematool.py docs`. Do not edit by hand.

Cloud and engineering certifications and a starter technical skills catalogue.

| Kind | Depends on | Entities | Extended entities | Enums | Term sets |
|---|---|---|---|---|---|
| industry | [`hr`](hr.md) | 0 | 0 | 0 | 0 |

## Reference data (seed)

Loaded at deployment. Deactivate rows you do not need instead of deleting them.

### CredentialType (7 rows)

| code | name | category | issuingBody | requiresRenewal | renewalPeriodMonths | requiresCpd |
|---|---|---|---|---|---|---|
| TEC_AWS_SA | AWS Certified Solutions Architect | certification | Amazon Web Services | yes | 36 |  |
| TEC_AZURE_SA | Microsoft Certified Azure Solutions Architect Expert | certification | Microsoft | yes | 12 |  |
| TEC_PP_DEV | Microsoft Certified Power Platform Developer Associate | certification | Microsoft | yes | 12 |  |
| TEC_GCP_ARCH | Google Cloud Professional Cloud Architect | certification | Google Cloud | yes | 24 |  |
| TEC_CKA | Certified Kubernetes Administrator | certification | CNCF | yes | 24 |  |
| TEC_CISSP | CISSP | certification | ISC2 | yes | 36 | yes |
| TEC_SCRUM | Professional Scrum Master | certification | Scrum.org |  |  |  |

### Skill (9 rows)

| code | name | category |
|---|---|---|
| TEC_CSHARP | C# / .NET | Technical |
| TEC_TYPESCRIPT | TypeScript | Technical |
| TEC_PYTHON | Python | Technical |
| TEC_REACT | React | Technical |
| TEC_SQL | SQL | Technical |
| TEC_AZURE | Microsoft Azure | Technical |
| TEC_AWS | Amazon Web Services | Technical |
| TEC_K8S | Kubernetes | Technical |
| TEC_SPFX | SharePoint Framework (SPFx) | Technical |
