# Industry and client adaptation playbook

The pilots are built once and adapted many times. This document shows how the same model serves
architecture, law, accounting, technology and any other firm, and the steps to adapt a pilot for a new client.

## 1. The five adaptation levers

Use them in this order — each step is cheaper and safer than the next.

| # | Lever | What changes | Where | Effort |
|---|---|---|---|---|
| 1 | **Profile** | Which modules, bridges and overlays are deployed | `schemas/profiles.yaml` | Minutes |
| 2 | **Reference data** | Project types, phases, roles, time codes, leave types, expense categories, pipelines, term sets | `seed:` and `termSets:` in an overlay | Hours |
| 3 | **Terminology** | Display names: Project → Matter, Account → Client, column labels | `terminology:` in an overlay | Hours |
| 4 | **Overlay columns and tables** | Industry or client data the base model does not have | `extend:` and `entities:` in an overlay | Days |
| 5 | **App and process configuration** | Forms, views, dashboards, approval policies, branding | The app layer on each platform | Days |

Changing a base layer for one client is never one of the options. If something is genuinely missing for
everyone, add it to the base layer through a normal versioned change.

## 2. Same concept, different words

| Generic (schema) | Architecture | Law | Accounting | Technology | General |
|---|---|---|---|---|---|
| `Account` | Client | Client | Client | Customer | Client / company |
| `Project` | Project / job | **Matter** | **Engagement** | Project / implementation | Project |
| `ProjectType` | Project type (new build, refurb…) | Matter type | Engagement type | Project type | Project type |
| `ProjectPhase` | RIBA stage / AIA phase | Matter stage | Audit / tax phase | Delivery phase / sprint | Phase |
| `Project.projectManager` | Project architect | Responsible lawyer | Engagement manager | Delivery lead | Project manager |
| `Project.sponsor` | Principal / director | Supervising partner | Engagement partner | Product owner / sponsor | Sponsor |
| `ProjectRole` | Architect, technologist… | Partner, associate, paralegal | Partner, manager, senior | Engineer, QA, UX… | Consultant… |
| `TimeCode` | Design, coordination, site | UTBMS task/activity codes | Planning, fieldwork, review | (usually none) | General, meetings |
| `ServiceLine` | Sector studio | **Practice area** | Service line | Product line | Service line |
| `Opportunity` | Bid / competition | Pitch / panel | Proposal | Deal | Opportunity |
| `Proposal` | Fee proposal | Fee proposal | Engagement proposal | Quote | Proposal |
| `CredentialType` | ARB / NCARB registration, LEED AP | Bar admission, practising certificate | CPA, ACCA, ACA, CA | Cloud certifications | PMP, first aid |
| `TravelPurpose` | Site visit | Court hearing, deposition | Audit fieldwork, stock-take | Customer go-live | Client meeting |

## 3. What each industry overlay adds

| Overlay | Adds |
|---|---|
| **Professional services – risk & compliance** (`proserv-compliance`, shared by law and accounting) | Conflict checks and conflict parties, information barriers (ethical walls), client due diligence (KYC/AML) with beneficial owners and sanctions screening; risk and engagement-letter columns on Project and Account |
| **Architecture** (`architecture-*`) | Site address, sector, floor area, construction budget and % fee, procurement route, planning status, sustainability target on Project; drawing register with ISO 19650 suitability codes, revisions and transmittals; RFIs; site visits linked to travel; RIBA 2020 and AIA phases; bids pipeline; architect credentials and design-tool skills |
| **Law** (`legal-*`) | Matter terminology; originating lawyer, billing partner, court, case number, limitation date, amount in dispute, pro bono, e-billing on Project; matter parties; docketed key dates with reminders; litigation and transaction stages; UTBMS codes; panels pipeline; bar admissions; court travel purposes; professional-conduct policy categories |
| **Accounting** (`accounting-*`) | Engagement terminology; entity type, tax references, year end, PIE status on Account; period, reporting framework, recurrence and roll-forward, quality review, materiality on Project; PBC information requests; statutory filing deadlines; independence declarations; audit and tax phases; CPA/ACCA/ACA/CA credentials; independence and AML policy categories |
| **Technology** (`technology-*`) | Customer terminology; ARR, renewal date, customer success manager, health score on Account; subscriptions; products and releases; epic/feature/story/bug types; severity, environment, release and pull-request links on work items; cloud certifications and engineering skills |

The exact columns are in each overlay's page in the [data dictionary](data-dictionary/README.md).

## 4. Adapting a pilot for a new client

| Step | What you do | Output |
|---|---|---|
| 1. **Discovery** (½–1 day) | Walk the client through the demo on the nearest profile; capture their words for things, their reference lists, and gaps | Gap list: terminology, reference data, missing columns, integrations |
| 2. **Pick the profile** | Start from the closest profile (or a starter) | Profile name |
| 3. **Create a client overlay** | `schemas/clients/<client>.yaml`, `kind: client`, depending on the overlays you use | One new layer |
| 4. **Terminology** | Add the client's labels | `terminology:` |
| 5. **Reference data** | Load their project types, phases, rates, leave types, cost centres, offices, approval policies | `seed:` (+ data import for large lists such as clients) |
| 6. **Gaps** | Add only the columns/tables they need; give each a description | `extend:` / `entities:` |
| 7. **Validate and review** | `validate`, `docs`; walk through the client overlay page with them | Signed-off data dictionary |
| 8. **Build and deploy** | Deploy base solutions/templates unchanged, then the client layer on top | Client environment |
| 9. **Migrate data** | Load existing data with `externalId` + `sourceSystem` so reloads are idempotent | Migrated data |
| 10. **Hand over** | Train admins on reference data and approval policies — the parts they will change themselves | Client self-sufficient |

### Example client overlay

```yaml
# schemas/clients/fabrikam.yaml
module:
  id: client-fabrikam
  name: Client – Fabrikam Legal LLP
  kind: client
  version: 1.0.0
  dependsOn: [legal-work, legal-hr]
  description: Fabrikam-specific labels, columns and reference data on top of the law-firm profile.

terminology:
  Project.sponsor: { label: Client Partner }

extend:
  Project:
    attributes:
      billingOffice: lookup(Office)
      panelReference: { type: string(50), description: Reference on the client's legal panel. }

seed:
  ProjectType:
    - { code: FAB_INSOLVENCY, name: Restructuring & insolvency, defaultBillingModel: timeAndMaterials, requiresClient: true, isInternal: false, numberPrefix: "M-" }
  LeaveType:
    - { code: FAB_SABBATICAL, name: Partner sabbatical, isPaid: true }
```

Then add a profile:

```yaml
  fabrikam:
    description: Fabrikam Legal LLP production deployment.
    layers: [core, crm, work, travel, hr, intranet, crm-work, travel-work, hr-work, hr-intranet, intranet-work,
             proserv-compliance, legal-crm, legal-work, legal-hr, legal-travel, legal-intranet, client-fabrikam]
```

Keep real client overlays in the client's own repository (or a private branch) — this repository holds only
the reusable base and fictional examples.

## 5. Adding a new industry

For example engineering consultancies, healthcare clinics or real-estate agencies:

1. List the industry's words for the generic concepts (table in §2) → `terminology`.
2. List its reference data (project types, phases, roles, codes, credentials, travel purposes, policy
   categories) → `seed` and `termSets` with a new code prefix (e.g. `ENG_`).
3. List what it tracks that no other industry does → new entities or `extend:` columns. Check first whether a
   shared overlay already covers it (e.g. `proserv-compliance` for conflicts and KYC).
4. Create `schemas/industries/<industry>.yaml` with one document per module it touches
   (`<industry>-crm`, `<industry>-work`, …), each depending only on the module(s) it extends.
5. Add an `<industry>-firm` profile, run `validate` and `docs`, and review the generated pages.

If two industries need the same addition, put it in a shared overlay (as `proserv-compliance` does) rather
than copying it.
