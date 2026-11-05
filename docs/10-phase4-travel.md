# Phase 4 — Travel Portal

The third pilot: travel policy, trip requests and approval, bookings, duty of care, expense
claims and corporate card reconciliation, for **Northwind Group** on the `travel-starter`
profile.

Phase 4 is where the accelerator stops being tested on variations of the same shape. Phase 2
was a second module for a different industry, but still a Dataverse app with an approval
chain and a pipeline. Travel is a different *tier*: the roadmap puts it on SharePoint lists
and a canvas app, because the firms it is aimed at have Microsoft 365 licences and nothing
else. So the question this phase answers is not "do the generators cope with another module"
— it is **does the same schema survive a change of platform**, and what the generators have
to tell you when it does not.

- The plan this phase belongs to → [01-plan-and-roadmap.md](01-plan-and-roadmap.md)
- The stack tiers → [02-tech-stack-strategy.md](02-tech-stack-strategy.md)
- The generators → [07-generators.md](07-generators.md)
- Phases 1 and 2, for comparison → [06-phase1-core-hr.md](06-phase1-core-hr.md),
  [08-phase2-crm.md](08-phase2-crm.md)
- The tables → [data-dictionary/travel.md](data-dictionary/travel.md)

---

## 1. What Phase 4 delivers

```bash
python tools/build.py --profile travel-starter --prefix nwd \
  --solution NorthwindTravel --firm northwind
```

One command, twelve steps, and:

| | |
|---|---|
| **Dataverse** | 32 tables, 332 columns, 79 relationships, 17 alternate keys, 23 choice sets — no manual metadata steps |
| **Security** | 3 roles over 25 / 25 / 34 tables, with a generated access matrix |
| **App** | 5 areas, 27 forms, 27 views; 5 forms with a Sensitive section |
| **Flows** | the shared approval engine, covering TripRequest and ExpenseReport in this profile |
| **Power BI** | 21-table model, 26 measures, 42 relationships, 4 pages |
| **SQL** | 38 tables, 272 foreign keys, 27 reference tables |
| **SharePoint** | 35 lists, 334 site columns, and 15 limits worth knowing about |
| **Solution** | 533 components in 123 files |
| **Demo data** | Northwind Group — 583 rows over 16 tables, 64 staff |

Phase 4 needed **no new generator**. It needed three declarative specs
(`security/travel-roles.yaml`, `apps/travel-desk.yaml`, `powerbi/travel-dashboard.yaml`),
and the profile already existed. What it cost in tool changes is
[§7](#7-what-the-third-pilot-actually-cost) — and this time, unlike Phase 2, every one of
them was a defect in how a generator *reported* its work rather than in what it generated.

### The profile

`travel-starter` is `core` + `travel`. No bridges, because a travel pilot that needs the work
module to exist is not a pilot a firm can buy on its own — charging a trip to a project is
what the `travel-work` bridge is for, and it is deliberately not in this profile.

---

## 2. Two platforms, one schema

The roadmap's claim is that the schema does not care which stack it lands on. Phase 4 is the
first phase where both targets are generated from the same model in the same build, so the
claim is now checkable rather than asserted:

| | SharePoint (Tier A, what the pilot ships on) | Dataverse (Tier B, the Phase 7 re-platforming) |
|---|---|---|
| Tables / lists | 35 lists, 35 content types | 32 tables |
| Columns | 334 site columns, 34 shared across lists | 332 columns |
| Choices | inline per column | 23 global choice sets |
| Relationships | 74 lookup fields | 77 one-to-many, 2 many-to-many |
| Security | list permissions, inheritance broken on 2 lists | 3 roles, 281 privileges, 1 column profile |
| Limits to know about | **15** | 0 |

The list count exceeds the table count because SharePoint has no many-to-many: each one
becomes its own list. The interesting column is the last one.

### The fifteen limits are the deliverable

`tools/spgen.py` writes `build/sharepoint/travel-starter/limits.md`, and it is not a warning
log — it is the evidence behind the stack decision. For travel it reports three kinds:

**Five lists will pass the 5,000-item view threshold** (`CardTransaction`, `ExpenseLine`,
`ApprovalStep`, `Comment`, `DocumentReference`). The generator indexes the filter and sort
columns and filters the views, which is the correct mitigation, and then says plainly that
this is a reason to consider Dataverse. A corporate card feed is the clearest case in the
whole repo: it is append-only and fed by a bank, so unlike a trip request it is never
archived and never stops growing. The generated demo firm of 64 staff produces 110
transactions across a single year of travel; the threshold is a question of how many years
the pilot survives, not of whether it is reached, and nobody is watching on the day it is.

**Eight columns are classified above their list** — `Account.taxNumber`,
`CorporateRate.rateCode`, `TravelBooking.confirmationCode` and five PII columns on `Contact`
and `Employee`. SharePoint has no column-level security, so there is no mitigation at all:
the only options are a separate list with broken inheritance, or Dataverse. Dataverse gets
this for free, which is why `secgen` emits a column profile for exactly these columns and
`spgen` emits a limit for them.

**Two lists are restricted outright** (`TravellerProfile`, `TravelDocument`). A passport
number and a date of birth are not "internal". The generator says to break inheritance and
grant only the owning group, which is the same boundary the Dataverse role draws with depth.

That symmetry is the point. One sensitivity classification in the YAML becomes a column
profile on one platform and a `limits.md` row on the other, and neither is hand-written.

---

## 3. Security: an approver is not a travel agent

Three roles, and the boundary that matters is not between a traveller and an approver — it is
between **seeing somebody's trip** and **seeing somebody's passport**.

| | Traveller | Approver | Travel Desk |
|---|---|---|---|
| `TripRequest` | CRUD own | R unit | CRUD unit+ |
| `ExpenseReport`, `ExpenseLine` | CRUD own | CRUD unit | CRUD unit+ |
| `CardTransaction` | RU own | RU unit | CRUD unit+ |
| `TravelBooking` | R own | R unit | CRUD unit+ |
| **`TravellerProfile`** | RU **own** | RU **own** | CRUD unit+ |
| **`TravelDocument`** | RU **own** | RU **own** | CRUD unit+ |
| **`LoyaltyMembership`** | RU **own** | RU **own** | CRUD unit+ |
| `TravelPolicy`, `PerDiemRate`, `DestinationAdvisory` | R all | R all | CRUD all |
| `CorporateRate` | — | — | CRUD all |

Read the approver column downward and the shape is clear: an approver's depth widens to their
unit for everything operational and stays at **own** for the three tables that hold identity
documents. An approver needs to know where their team is going and what it costs. They do not
need a passport number, a date of birth or a frequent-flyer account, and a system that gives
them one because it was easier to grant the whole module is how a travel system becomes a
GDPR problem.

The travel desk does hold those tables, because somebody has to: an airline booking needs the
name as printed on the passport. That is why the desk is a small team and why its role is the
one with 169 privileges.

`CorporateRate` is granted to nobody but the desk. A negotiated rate code is commercially
confidential — it is what a competitor would want from a leaked spreadsheet — so it is
`confidential` in the schema, which is also why it turns up in the SharePoint limits above.

### The roles file is also the SharePoint spec

This pilot ships on SharePoint, where none of the above exists as a role. So
`security/travel-roles.yaml` carries a `businessUnitStrategy` note saying what the depths mean
for a single-legal-entity firm, and the two restricted tables are the two lists `spgen` tells
you to break inheritance on. The same file is the Dataverse specification for Phase 7 and the
SharePoint permission design for Phase 4, and it cannot drift between them because there is
only one of it.

---

## 4. The app is the desk's day, not the traveller's journey

A traveller raises a trip on their phone in the canvas app. The desk works in the
model-driven one, so `apps/travel-desk.yaml` is ordered by what the desk does, and the first
area is **Duty of care**.

That ordering is a deliberate argument. Every travel product demo opens with booking a trip,
because booking a trip is the fun part. But the question a travel system is actually bought to
answer is "who is abroad right now, and is any of them somewhere dangerous" — and on the day
that question gets asked, nobody navigates a menu. So `TripRequest` is the first table in the
first group of the whole app.

That decision had a consequence the generator caught: `TripRequest` was originally in both
the duty-of-care area and the trips area, and `appgen` failed the build with
`'TripRequest' appears more than once in the site map`. A table belongs in exactly one place
in a site map; the fix was to leave it in duty of care and point the approvals group at
`ApprovalRequest` instead, which is what an approver actually opens.

The 27 forms carry **5 Sensitive sections** — the generator puts a column classified above its
table into a separate collapsed section rather than inline, so that a form is self-documenting
about what it is exposing.

---

## 5. The approval engine, unchanged again

`flowgen.py` derives what is approvable from the model: any table with an `approvalRequest`
writeback column and a status enum carrying submitted-like and approved-like values. For
`travel-starter` that is **TripRequest** and **ExpenseReport**, found without being told, and
four flows come out — start, decision, reminders, escalation — the same four as Phase 1 and
Phase 2.

So a trip approval and a leave approval are the same flow with different data, which is the
whole reason the engine was built once in [Phase 1 §5](06-phase1-core-hr.md). Three pilots in,
it has needed no changes for any of them.

What is *not* built is the policy content: a trip over £2,000 or to a high-risk destination
should route differently from a £300 domestic trip, and that is an `ApprovalPolicy` row rather
than a code change. The demo data generates the trips; the policy rows for travel are still to
write.

---

## 6. The dashboard, and the tile that has to not be zero

`powerbi/travel-dashboard.yaml` is 26 measures over 21 tables in four pages, and the page
order is the same argument as the app's: **Spend**, then **Duty of care**, then **Policy and
channel**, then **Cards and process**. Spend is first because the finance director is who asks
for the report. Duty of care is second because it is the one that matters.

Two measures are worth calling out, because they are the ones a travel team judges a system
by and the ones a vendor demo never shows:

- **Avg Advance Booking Days** — days between booking and departure. It is the single biggest
  lever on air fare, and it is the number a policy's minimum-advance rule exists to move. A
  report without it cannot tell you whether the policy is working.
- **Avg Days To Reimburse** — submission to payment. Travellers judge the whole system by it,
  and a finance team never volunteers it.

### Every tile is checked against generated data

A dashboard whose tiles read zero demonstrates nothing, and three of the Travel tiles read
zero on the first run. The whole measure set was evaluated against the generated rows — the
label literals resolved through the exported model the same way the TDS endpoint resolves
them, so the check sees what Power BI will see — and the results are now asserted by tests:

| Tile | Was | Now |
|---|---|---|
| Travellers Away Now | 0 | 8 |
| Travellers To High Risk Now | 0 | 1 |
| Out Of Policy Expense Lines | 0 | 25 |
| Out Of Policy % | 45.3% | 14.1% |

The first two were a bad check, not bad data: `TripStatus` has `inProgress` with the label
`Travelling`, and the first version of the check compared against the label rather than the
stored key. That is exactly the mistake a hand-written verification makes, and the reason the
committed version resolves labels through the model.

The third and fourth were real. Expense lines were drawn from one range and judged against a
single hotel cap, so almost nothing could ever breach it; and a trip was out of policy if it
was booked inside fourteen days, drawn uniformly from a list where three of eight options were
late — which made nearly half the firm's travel non-compliant. Now every expense category
carries its typical value and its limit, the limits sit at about 1.5× typical and the
multiplier is drawn from [0.45, 1.65), so roughly one line in eight breaches; and lead times
are weighted so late booking is uncommon. A cabin above the policy's class for that haul is a
comparison against `CABIN_RANK`, not a hardcoded list of offending values.

**The duty-of-care tile is guaranteed by construction.** "Somebody high-risk, away right now"
is the product of two small probabilities, so left to the draw it reads zero more often than
not. The first generated trip is that case deliberately, and the test asserts it across four
seeds — because the one tile the product exists for must never be empty in a demo.

---

## 7. What the third pilot actually cost

Phase 2's lesson was that a second pilot surfaces latent defects. Phase 4's is narrower and
more interesting: **every defect this phase found was in how a generator reported or persisted
its work, not in what it generated.** The artefacts were right; three of them described
themselves wrongly, and that is worse, because a wrong description is believed.

| # | What broke | Why it mattered | Fix |
|---|---|---|---|
| 1 | Three effective-dated travel reference tables had no `validFrom` | Caught by the demo-data validator before anything was written | The rows carry the start of the current travel year |
| 2 | `DESTINATIONS` gained a country (`NG`) that the `Country` seed does not contain | A `destinationCountry` is a lookup, so every trip to it was a dangling reference | Destinations use seeded countries only, and a test enforces it |
| 3 | `secgen` left role files from an earlier run in its output directory, and `solgen` packs that directory wholesale | **Three HR roles shipped inside the travel solution** — six roles in a solution that specifies three, granting privileges on tables the travel roles never mention | `secgen` owns its output directory and deletes what it did not write; `solgen` cross-checks the directory against `report.json` and errors on a role the report does not name |
| 4 | `security/travel-roles.yaml` declared `Currency` out of scope and then granted it to two of three roles | The generated matrix said the travel desk could not read a currency every traveller could | A table both `outOfScope` and granted is now a `secgen` **error** |
| 5 | `appgen` warned that nobody could read `ExchangeRate` | A standard Dataverse table has no custom privilege to grant, so an empty grant is the expected state — and a warning nobody can act on trains the reader to ignore the ones that matter | `appgen` reads the roles file's `outOfScope` declarations and reports those as notes with the reason |
| 6 | The travel Power BI project shipped a theme file called `WoodgroveHR.json`, and `pages.md` said it was generated from `hr-dashboard.yaml` | Both were hardcoded from the first report the tool ever generated. The instruction to apply the theme named a file for a different firm | The theme is named after the report, `pages.md` cites the spec it came from, and a theme left by an earlier report is removed |

Findings 3 and 6 are the same bug in two tools, and it is the same bug as the one Phase 2
found in `appgen` and `demodata` sharing an output directory: **a generator that does not own
its output directory will eventually ship somebody else's work.** Three instances is a
pattern, so all three are now guarded by tests that fail when the stale file is reintroduced.

Finding 3 is the one worth dwelling on. Nothing failed. The build was green, the solution
packed, the manifest was internally consistent — and the artefact granted HR privileges in a
travel deployment. It was found by reading the manifest's role list while writing this
document, which is not a control.

Every fix above is asserted by a test, and every test was verified by reintroducing its bug
and watching the suite fail.

---

## 8. Still to do

| | Needs |
|---|---|
| The canvas app (request, approve, expenses with receipt photo) | the maker portal; the roadmap's Phase 4 exit criterion |
| Provision the SharePoint site from the generated template | a tenant, and `tools/spgen.py`'s output applied with PnP |
| Break inheritance on the two restricted lists | the site, once provisioned — the generator says which |
| `ApprovalPolicy` rows for travel | written, not generated: amount and risk thresholds are a business judgement |
| The report canvas | Power BI Desktop, from the generated `pages.md` |
| The `travel-work` bridge (charge a trip to a project) | Phase 3, which is not built |
| Delegation-aware canvas design against SharePoint | the five lists that will cross the 5,000-item threshold |

The limits report already says which of these will bite first.
