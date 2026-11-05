# Phase 3 — Work Management

The fourth pilot: projects, phases, the backlog, resourcing, timesheets and the RAID log, for
**Contoso Technologies** on the `work-starter` profile.

Phase 3 is the volume phase, and the one whose showcase stack is not low-code at all. The
roadmap puts Work Management on **pro-code** — React, an API, Azure SQL — because this is the
module where the data gets big and the interactions get beyond what a form can do. So the
question it answers is not "can the generators do another module" (Phase 2 settled that) nor
"does the schema survive another platform" (Phase 4 settled that). It is **does any of this
hold up at volume, and what does the pro-code stack get from a schema-first approach when it
has no Dataverse to generate into.**

The roadmap's exit criterion for this phase is a number: *10k+ demo time entries perform well*.
That number is now generated, loaded into the generated schema, and queried in the test suite.

- The plan this phase belongs to → [01-plan-and-roadmap.md](01-plan-and-roadmap.md)
- The stack tiers → [02-tech-stack-strategy.md](02-tech-stack-strategy.md)
- The generators → [07-generators.md](07-generators.md)
- The other pilots → [06-phase1-core-hr.md](06-phase1-core-hr.md),
  [08-phase2-crm.md](08-phase2-crm.md), [10-phase4-travel.md](10-phase4-travel.md)
- The tables → [data-dictionary/work.md](data-dictionary/work.md)

---

## 1. What Phase 3 delivers

```bash
python tools/build.py --profile work-starter --prefix ctw \
  --solution ContosoDelivery --firm contoso
```

One command, twelve steps, and:

| | |
|---|---|
| **Azure SQL** | 40 tables, 4 junctions, 294 foreign keys, 29 reference tables — the showcase stack |
| **Dataverse** | 34 tables, 301 columns, 91 relationships, 21 alternate keys, 25 choice sets (the low-code alternative) |
| **Security** | 4 roles over 33 / 36 / 29 / 35 tables, with a generated access matrix |
| **App** | 5 areas, 26 forms, 26 views; 5 forms with a Sensitive section |
| **Flows** | the approval engine; Timesheet is the one approvable table in this profile, and the generator says why |
| **Power BI** | 21-table model, 39 measures, 4 pages |
| **SharePoint** | 37 lists, 298 site columns, and 14 limits — five of them the reason this pilot is not on SharePoint |
| **Solution** | 515 components in 126 files |
| **Demo data** | Contoso Technologies — **14,242 time entries** over 2,186 weekly timesheets, 20 projects, 331 work items, 17,559 rows in all |

Phase 3 needed **no new generator**, same as Phase 4. It needed three declarative specs, a
`work:` section in each firm file, and one new section in the demo-data generator.

### One schema addition, and it was the dashboard that demanded it

`OrgUnit.isDeliveryUnit` is new. Writing the utilisation measure made it obvious that nothing
in the model said which units earn fees — `unitType` says what *shape* a unit is (department,
practice group, studio), not whether its people charge their time. Without it the only
utilisation figure available is billable hours over all hours across the whole firm, which for
Contoso is **37%**: a true number that is not utilisation at all, it is the firm's billable
headcount ratio. With the column, delivery utilisation is **68%** and the firm-wide ratio is
reported beside it as the different thing it is.

Both are on the page, deliberately, because quoting one as the other is the commonest way a
utilisation report misleads.

---

## 2. Volume: the exit criterion, run rather than asserted

The demo firm generates 44 weeks of weekly timesheets for every active member of staff. At the
default headcount of 64 that is **14,242 time entries** — and the shape matters more than the
count:

| | |
|---|---|
| Delivery utilisation | 67.6% against the firm's 72% target |
| Firm-wide billable ratio | 37.4% |
| Support functions | up to 3.4%, not uniformly zero: roughly one client project in six carries a PMO or a finance analyst |
| Hours against budget | 95.2%, with 6 of 20 projects over |
| Write-off rate | 6.1% |
| Unsubmitted hours | 358, so the revenue page can be wrong for a reason |

`tests/test_sqlgen.py` then does the part the roadmap actually asked for. It generates the
rows, creates the table with the indexes `sqlgen` chose, loads all 14,000, and runs the query
the dashboard computes — asserting that the aggregate matches a Python calculation over the
same rows, and that the planner reaches it **through an index rather than by scanning**.

SQLite is not Azure SQL. What transfers is the index set, because that is the same DDL.

### Which found a real gap

`sqlgen` indexed every foreign key — ten of them on `TimeEntry` — and nothing else. An index
on a foreign key serves a *join*. It does nothing for a *report*, and a report is what a
high-volume table exists to feed: the filter is always a period and a state. So the one query
the delivery dashboard runs was a full scan of fourteen thousand rows, and would have been a
full scan of two million in a real deployment's third year.

The generator now also indexes, on any table marked `volume: high`, the row's own **required
date** and the column the model's convention calls **`status`**. Narrow on purpose: not every
date, because a due date is not what a period is filtered by, and not every choice, because
`priority` is too low-cardinality to earn one. For `work-starter` that is four indexes —
`TimeEntry.entryDate`, `TimeEntry.status`, `WorkItem.status`, `ApprovalStep.status` — and the
test asserts both that they exist and that the unwanted ones do not.

---

## 3. Security: utilisation needs hours, not narratives

Four roles. The interesting line in the file is not a grant, it is a denial.

| Table | Team Member | Project Manager | Resource Manager | Finance |
|---|---|---|---|---|
| `Project`, `ProjectPhase`, `Milestone` | R unit | CRUD unit | R all | RU all |
| `WorkItem` | CRU unit | CRUD unit | R all | R all |
| `Timesheet` | CRUD own | RU unit, CD own | **R all** | RU all |
| `TimeEntry` | CRUD own | RU unit, CD own | **— denied** | RU all |
| `ProjectMember` | R unit | CRUD unit | CRUD all | RU all |
| `ResourceRequest` | — | CRUD unit | CRUD all | R all |
| `ProjectRole`, `TimeCode` | R all | R all | R all | CRUD all |
| `ApprovalPolicy` | — | R all | — | CRUD all |

**The resource manager is denied `TimeEntry` outright.** Utilisation is a sum of hours, and the
`Timesheet` row already carries `totalHours` and `billableHours` — so the report this role
exists to produce needs no access to the individual entries at all. A narrative says what a
named person did for a named client on a named day. A resourcing function has no business
reading it, and "the report needs the hours" is exactly how that access gets justified into
existence.

The second boundary is the **cost rate**. `ProjectMember.costRate` and
`ProjectRole.defaultCostRate` are a column profile readable only by finance, because a cost
rate is a salary divided by a year: a delivery person who can see a colleague's cost rate can
work out their pay. The bill rate is not secured — it is on the client's invoice.

A project manager's **create and delete** on time stay at own-row depth while read and write
widen to their unit. They approve their team's time; they do not log it on somebody's behalf,
because then nobody knows who did the work.

### The roles file is the API's specification

On a pro-code stack there are no Dataverse roles. Authorisation is the API's job, and this file
is what its authorisation tests assert against — the same arrangement as Phase 4, where
`travel-roles.yaml` is the specification for SharePoint list permissions. One file, two
targets, and it cannot drift between them because there is only one of it.

---

## 4. One module, two cadences

A software company runs sprints. A law firm runs matter phases. An architecture practice runs
RIBA work stages with a fee percentage against each one. All three are the same tables.

Each firm file now carries a `work:` section with a `cadence` of `sprints` or `phases`, its
project types and their phase names, a rate card and a list of time codes with their
chargeable flag. `demodata` reads it and the difference shows up in the data rather than in the
schema:

| Firm | Cadence | Project types | Story points |
|---|---|---|---|
| Contoso Technologies | sprints | Customer implementation, Platform workstream, Integration, Managed service | yes |
| Fabrikam Legal | phases | the matter types, **with the same codes as the CRM's service offerings** |  — |
| Woodgrove Advisory | phases | Statutory audit, Tax compliance, Advisory | — |
| Adatum Architects | phases | New build (RIBA stages), Refurbishment, Feasibility | — |
| Northwind Group | phases | Client project, Fixed-price, Retained | — |

`Sprint` simply has no rows for a practice, and `WorkItem.storyPoints` is blank. That is the
schema serving both cadences rather than two schemas, and it is checkable rather than claimed
because both come out of the same generator run.

### The hand-off the roadmap asked for

The roadmap's other Phase 3 exit criterion is *a project created from a won opportunity*. That
works, and it works without any Phase 3 code: `build_matters` creates a matter per won pitch
from the CRM, and Phase 3's delivery section builds on top of whatever projects already exist
rather than replacing them. On `law-firm-crm`:

```
18 projects, 8 of them carrying originatingOpportunity
note: 8 project(s) came from won pitches; the delivery data is built on top of
      them rather than replacing them
```

Building it did surface one piece of rot. Fabrikam's matter types were invented independently
of the service offerings the CRM pitches, so the firm ended up with both an `MA` and a
`COR_MA` matter type for the same work. The firm file now uses the service offerings' own
codes, and the note above is what tells a reader the two halves met.

---

## 5. Why this pilot is not on SharePoint

Phase 4's SharePoint limits report was the evidence for putting Travel *on* SharePoint. The
same report for Phase 3 is the evidence for keeping Work Management *off* it:

| Limit | Count | What it means here |
|---|---|---|
| list view threshold | **5** | `TimeEntry`, `WorkItem`, `ApprovalStep`, `Comment` and `DocumentReference` are all `volume: high`. The demo firm alone puts 14,242 rows in one list, against a 5,000-item view threshold |
| lookup threshold | 1 | `Project` has 14 lookup columns; a SharePoint view may show 12 |
| no column security | 8 | including the cost rate, which is the one column this phase is built around protecting |

One list past the threshold is a design to work around. Five, one of which the demo data
exceeds by a factor of three before anybody has used the system, is a stack decision — which is
precisely what the roadmap already made. The report does not argue; it counts.

---

## 6. The dashboard, and the numbers nobody volunteers

39 measures over 21 tables in four pages: **Utilisation**, **Delivery health**, **Revenue and
WIP**, **Resourcing and backlog**. Every one of the 36 headline tiles was evaluated against the
generated rows with the choice labels resolved through the exported model, the same check
Phase 4 introduced, and none reads zero.

Three measures are worth calling out:

- **Work In Progress** — approved or submitted value not yet billed. Cash the firm has earned
  and not asked for, and the first number a finance director wants.
- **Write-off %** — write-offs over everything chargeable. A rising rate on a fixed-fee book
  means the fees are wrong, not that the team is slow. The demo had **no write-offs at all**
  until this was checked, which made a whole page read zero.
- **Over-allocated People** — people whose allocations across live projects exceed 100%.
  Computed rather than stored, because an allocation is only over-committed *relative to the
  others* — the sort of thing a column cannot hold and a measure can.

---

## 7. What the fourth pilot actually cost

Phase 2's lesson was that a second pilot surfaces latent defects. Phase 4's was that a
generator can be right about its output and wrong about its description of it. Phase 3 found
the most serious defect so far, and it is a third kind: **a guard that encoded the bug it was
guarding against.**

| # | What broke | Why it mattered | Fix |
|---|---|---|---|
| 1 | `pbigen` allowed two active relationships between one pair of tables when they pointed at each other | **Every one of the four generated semantic models carried three or four such pairs.** Power BI permits one active relationship between two tables whichever way each points, so none of the four models would load as generated | `_choose_active` keys on the unordered pair, and the structural lookup wins over the pointer back — `Employee.orgUnit` over `OrgUnit.head` |
| 2 | the test that was meant to catch #1 | It keyed on the ordered pair too — the same mistake — so it passed over three ambiguous pairs in the model it was checking. And `TestRealDashboard` only ever loaded the HR spec, so three of the four dashboards were never built by a test at all | The test uses the unordered pair; a new class builds **every** committed dashboard spec against its own profile's model |
| 3 | `secgen`'s matrix showed one depth per cell — the widest | A project manager who reads their unit's time but creates only their own rendered as `CRUD (unit)`, a claim the role does not make. This also means Phase 1's and Phase 4's published matrices overstated two roles, and [this guide's](10-phase4-travel.md#3-security-an-approver-is-not-a-travel-agent) approver table repeated it | Letters are grouped by depth: `RU (unit) CD (own)` |
| 4 | `sqlgen` indexed every foreign key and nothing else | A full scan for the only query shape a high-volume table ever sees | A `volume: high` table also gets its required date and its `status` indexed |
| 5 | `pbigen` applied the three-series colour limit to table visuals | A table's measures are columns of numbers; nothing is identified by colour. The rule argued for splitting a readable four-column table in two | Tables, matrices and cards are exempt, with a separate readability limit of six |
| 6 | fourteen of sixty-four staff logged no time, and billable staff charged admin hours to client projects | An internal project existed only for non-billable departments, so a fee earner's admin and training hours had nowhere true to go | Every department gets an internal project, and its whole membership joins it |
| 7 | 23 of 54 people were over-allocated, and no project had a write-off | Allocations were drawn independently per membership, and no entry ever reached `writtenOff` | Allocation is drawn against what is left of the person; a sixteenth of approved billable time is written off |
| 8 | the over-budget count in the demo generator's own note was inverted, and reported 6 of 20 while 14 were over | `budget = logged / ratio`, so a ratio above 1 means the budget is *smaller* than the hours spent. The note counted the other branch, and the weighting had two projects in three overrunning — a pricing problem, not demo data | The test is the right way round and the weighting is 30% over, which is the number the note now reports and the data agrees with |

Findings 1 and 2 are the ones to dwell on. The models were generated, the manifests were
consistent, the builds were green, and a test named
`test_exactly_one_active_path_between_any_pair_of_tables` was passing — while four shipped
semantic models could not have been opened. A guard that reproduces the generator's own
mistake is worse than no guard, because it is read as evidence.

Every fix above is asserted by a test, and every test was verified by reintroducing its bug and
watching the suite fail.

---

## 8. Still to do

| | Needs |
|---|---|
| The React front end and the API | the pro-code build itself; this phase delivers its schema, its migrations and its authorisation specification |
| Apply the migrations to an Azure SQL database | a subscription. The DDL is executed in tests against SQLite, which is a parser and an executor but not the target |
| The drag-and-drop board | the roadmap's reason for choosing pro-code here, and the one part no generator can produce |
| `ApprovalPolicy` rows for timesheets | written, not generated: the hours and value thresholds are a business judgement |
| The report canvas | Power BI Desktop, from the generated `pages.md` |
| The `travel-work` bridge (charge a trip to a project) | both modules in one profile, which no profile currently combines |
| Capacity, so utilisation can be billable-against-available | a schema addition — contracted hours per person per period. `isDeliveryUnit` was the first half of this question; capacity is the second |

That last row is the honest limit on this phase's headline number, and it is stated on the
measure itself rather than only here.
