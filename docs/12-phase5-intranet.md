# Phase 5 — Intranet

The fifth and last pilot: a hub, news, events, policies with attestation, a knowledge base, a
service desk and site governance, for **Adatum Architects** on the `intranet-starter` profile.

Phase 5 is the pilot whose showcase stack is the most native and the least generated. The
roadmap puts it on **SharePoint hub sites, SPFx web parts and Viva Connections**, so almost
nothing a reader sees comes out of this repo: they open the hub, and the hub is SharePoint. What
the schema contributes is everything SharePoint is *bad* at — a site inventory with owners and
review dates, an attestation report, a service desk queue measured against an agreed target —
and the one feature the whole platform is judged on: **audience targeting**.

With Phase 5 all five pilots are built from source.

- The plan this phase belongs to → [01-plan-and-roadmap.md](01-plan-and-roadmap.md)
- The stack tiers → [02-tech-stack-strategy.md](02-tech-stack-strategy.md)
- The generators → [07-generators.md](07-generators.md)
- The other pilots → [06-phase1-core-hr.md](06-phase1-core-hr.md),
  [08-phase2-crm.md](08-phase2-crm.md), [11-phase3-work.md](11-phase3-work.md),
  [10-phase4-travel.md](10-phase4-travel.md)
- The tables → [data-dictionary/intranet.md](data-dictionary/intranet.md)

---

## 1. What Phase 5 delivers

```bash
python tools/build.py --profile intranet-starter --prefix adi \
  --solution AdatumIntranet --firm adatum
```

One command, twelve steps, and:

| | |
|---|---|
| **SharePoint** | 32 lists, 278 site columns, **10 term sets**, and 12 limits — the showcase stack |
| **Dataverse** | 29 tables, 257 columns, 92 relationships (**15 of them many-to-many**), 18 choice sets |
| **Security** | 6 roles over 29 / 29 / 29 / 22 / 22 / 29 tables, with a generated access matrix |
| **App** | 5 areas, 23 forms, 23 views; 4 forms with a Sensitive section |
| **Flows** | the approval engine; news and policy versions are the two approvable things here |
| **Power BI** | 17-table model, 36 measures, 4 pages |
| **SQL** | 35 tables, **16 junction tables**, 269 foreign keys |
| **Solution** | 458 components in 112 files |
| **Demo data** | Adatum Architects — 1,192 rows: 356 event registrations, 320 attestations, 220 service requests |

Phase 5 needed **no new generator** — the third phase running. It needed three declarative
specs, an `intranet:` section in each firm file, and one section in the demo-data generator.

### The shape is different from every other pilot

Three numbers above say what makes this module unlike the other four:

- **15 many-to-many relationships**, against 2 or 3 in the others. Audience targeting is a
  multi-value lookup, and six content types carry two of them each.
- **16 junction tables** in SQL, against 3 or 4. The same fact, on the relational target.
- **10 term sets**, against 3 or 4. Nearly all of this module's reference data is managed
  metadata rather than a choice set, because an intranet's vocabulary is edited by the
  communications team rather than deployed by a developer.

Each of those turned out to be a gap in the tooling rather than a property of the module, which
is [§6](#6-what-the-fifth-pilot-actually-cost).

---

## 2. Audience targeting, which is the whole platform

Every piece of content carries `audienceOrgUnits` and `audienceOffices`, and **empty means
everyone**. That is the feature an intranet is bought for and the one most intranets get wrong,
in a specific way worth naming:

> Audience targeting narrows what is **shown**, not what may be **read**.

It is a relevance feature. Treating it as a security boundary is how a firm comes to believe a
policy is confidential because it was not on somebody's home page — and the roles file says so
in as many words, because the belief is common enough that leaving it implicit is a risk.

The demo data makes the distinction visible: about 40% of news, events and quick links are
addressed to named departments or studios and the rest to everyone. A demo where everything is
addressed to everyone cannot show the feature works at all; one where nothing is would be a
firm that talks to nobody.

### Nothing checked it

The demo-data validator checked choices, lookups and polylookups. It did not check
`multilookup` or `multiterm` **at all** — so an audience naming a department that does not
exist would have passed silently, and a dangling audience is worse than an empty one: it
narrows who sees a policy while the policy still looks published.

It also did not check single `term` columns, which matters more here than anywhere else: this is
the first module whose reference data is mostly managed metadata. Both are now validated, with
open term sets (`open: true`, meaning the set grows in use) accepted as the exception they are
declared to be.

---

## 3. Policy attestation, which is the exit criterion

The roadmap's exit criteria for Phase 5 are *audience targeting works*, *a policy attestation
report*, and *mobile-friendly*. The third is SPFx and Viva Connections, not data. The second is
the one with teeth:

| | |
|---|---|
| Attestations in scope | 320 |
| Signed | 270 (87.7% of everyone who had to sign, exemptions excluded from both halves) |
| Outstanding | 38 — of which **29 overdue** and 9 still inside their window |
| People behind on at least one policy | 30 |
| Policies past their own review date | 2 |

The distinction between **pending** and **overdue** is the point. A report whose only state is
"overdue" cannot tell somebody who is late from somebody who simply has not got to it yet, and
the first version of the demo data produced exactly that — every policy was long-standing, so
every unsigned attestation was late. A third of policies are now issued recently enough that
their window is still open.

The due date is the policy's **own** window: `effectiveDate + attestationDueDays`, so "overdue"
is a fact about the data rather than a status somebody typed, and a test asserts the arithmetic.

An attestation row is evidence that a named person was told a named version of a policy on a
named date. It is the most valuable row in this profile the day after an incident, which is why
**no role may delete one** — not even the administrator.

---

## 4. Security: an intranet's problem is who can publish

Six roles, which is more than any other pilot, because an intranet has more distinct jobs than
it looks like it does. Everything is readable by everyone; the roles are about the authoring
boundary.

| Table | Reader | Site Owner | Comms Editor | Policy Owner | Service Desk | Admin |
|---|---|---|---|---|---|---|
| `NewsArticle` | R (all) | R (all) CUD (unit) | CRUD (all) | R (all) | R (all) | R (all) |
| `Announcement` | R (all) | R (all) | **CRUD (all)** | R (all) | R (all) | R (all) |
| `CompanyEvent` | R (all) | R (all) CUD (unit) | CRUD (all) | R (all) | R (all) | R (all) |
| `QuickLink` | R (all) | CRUD (all) | CRUD (all) | R (all) | R (all) | R (all) |
| `EventRegistration` | CRU (own) | RU (unit) C (own) | RU (unit) C (own) | — | — | R (all) |
| `PolicyDocument` | R (all) | R (all) | R (all) | **CRUD (all)** | R (all) | R (all) |
| `PolicyAttestation` | CRU (own) | CRU (own) | CRU (own) | **CRU (all)** | — | R (all) |
| `ServiceRequest` | CRU (own) | CRU (own) | CRU (own) | — | **CRU+a (all)** | R (all) |
| `KnowledgeArticle` | R (all) | R (all) CUD (unit) | R (all) CUD (unit) | R (all) | **CRUD (all)** | R (all) |
| `Site` | R (all) | RU (all) | RU (all) | R (all) | R (all) | **CRUD+a+s (all)** |
| `Term` | R (all) | R (all) | R (all) | R (all) | R (all) | **CRUD (all)** |
| `TermSet` | R (all) | R (all) | R (all) | R (all) | R (all) | **CRUD (all)** |
| `Recognition` | CR (all) | CR (all) | **CRU (all)** | — | — | R (all) |

Generated by `tools/secgen.py`, so this table is the output rather than the intent — the first
draft of it, written by hand, overstated four cells. Where a row reads `R (all) CUD (unit)`,
read widens through inheritance from the Reader while authoring stays in the role's own unit,
which is exactly the distinction the grouped depths exist to show.

Three deliberate choices:

**The administrator publishes nothing.** It owns the estate — sites, external sharing, the
taxonomy, the governance review — and reads everything without authoring any of it. Combining
platform ownership with publishing is how an intranet ends up with one person who is the only
one who can do anything, and then with nothing happening when they are on leave.

**Nobody may delete a recognition post.** An editor can unpublish one, which leaves a trace.
Deleting somebody's thank-you is not a thing an intranet should be able to do quietly, and
editing your own after the fact makes the wall unreliable.

**The service desk agent can `assign`.** It is the only role in any phase granted that
privilege, and it matters: a ticket moving between agents is the normal case, and a desk that
cannot reassign is a desk where tickets die with whoever first touched them.

And one correction the matrix forced. `PolicyAttestation` was declared organization-owned in the
schema, so own-row depth had no meaning and `secgen` raised every role asking for it to the
whole firm — which made **every member of staff able to read every colleague's compliance
status**, three roles wide, while the roles file plainly asked for own. `EventRegistration` is
the same kind of row, one person's response to something, and was already user-owned. The
inconsistency was the defect; the table is now user-owned too, and the row above shows the
boundary the file asked for in the first place.

### Dietary requirements are special-category data

`secgen` refused to generate these roles until a column profile covered
`EventRegistration.dietaryRequirements`. That looks pedantic until you read the values:
"coeliac", "halal", "no nuts". Innocuous on a catering sheet, and under the UK GDPR a dietary
requirement can reveal a religion or a medical condition. It is classified above its table in
the schema, and the check that would not let the roles build is the check doing its job.

---

## 5. The service desk measures something the firm agreed to

`ServiceCategory` carries `targetResponseHours` and `targetResolutionHours` — per category, not
per firm, because a password reset and a new laptop are not the same promise. The dashboard
compares each request against its own category's target:

| | |
|---|---|
| Response SLA met | 83.0% |
| Resolution SLA met | 83.8% |
| Avg hours to first response | 6.2 |
| Avg hours to resolve | 42.8 |
| Self-service share | 70.5% (portal, Teams or the virtual agent) |
| Avg satisfaction | 4.06 of 5 |

### Which needed a column the schema did not have

Those measures first read from `createdOn`, the platform's audit column. That is wrong, and
wrong in a way that flatters the service desk: a ticket phoned in or emailed is typed into the
system by an agent, sometimes the next morning, so `createdOn` measures **the agent's typing**
rather than the firm's responsiveness. In the demo it is worse than wrong — every row is created
at import, so every SLA would read 100%.

So `ServiceRequest.raisedOn` is new, required, and the thing every SLA measure starts from. Its
description in the schema says why, because the next person to write a measure will reach for
`createdOn` too.

---

## 6. What the fifth pilot actually cost

Each pilot has found a different *kind* of defect. Phase 2 found latent bugs, Phase 4 found
generators describing their own output wrongly, Phase 3 found a guard that encoded the bug it
guarded against. Phase 5's theme is **the thing nobody validated because nothing used it yet**.

| # | What broke | Why it mattered | Fix |
|---|---|---|---|
| 1 | `secgen` resolved inheritance only one level deep | A role inheriting a role that itself inherits lost the grandparent's grants entirely. The Communications Editor inherits Site Owner, which inherits Reader, and came out with **10 tables instead of 29** — unable to read the staff directory it is written to publish about. Every earlier phase used a single level, so nothing exposed it | Inheritance resolves transitively, in dependency order, with cycle detection and an order-independent result |
| 2 | the demo-data validator ignored `multilookup` and `multiterm` | **Audience targeting is carried entirely by those columns**, so the headline feature of this phase was the one thing nothing checked. A dangling audience silently narrows who sees a policy | Each element has to resolve, duplicates are an error, and a bare string where a list belongs is caught |
| 3 | it ignored single `term` columns too | This is the first module whose reference data is mostly managed metadata — ten term sets against three or four elsewhere | Terms are checked against their set, with `open: true` sets accepting new values as declared |
| 4 | the SLA measures read from `createdOn` | A ticket phoned in is typed up later, so that measures the agent's typing. In the demo every row is created at import, so every SLA would have read 100% | `ServiceRequest.raisedOn` is new and required, and says in its own description why it exists |
| 5 | event waitlist position was counted across every event, not within one | After the first event every registration was a waitlisting: **453 of 524**. Which is not an event, it is a queue | Position is per event; 6 of 356 are waitlisted, on genuinely oversubscribed events |
| 6 | the attestation report had no pending state, only overdue | Every policy was long-standing, so every unsigned attestation was late — and a report that cannot tell late from not-yet-done is not a chase list | A third of policies are issued inside their own window |
| 7 | nothing was stale, and no policy was past its own review date | Two governance tiles read zero. News was generated over 270 days, so nothing could be a year old | News spans 620 days, and every fourth policy is overdue for review by construction |
| 8 | `PolicyAttestation` was organization-owned while the near-identical `EventRegistration` was user-owned | Own-row depth has no meaning on an organization-owned table, so `secgen` raised it — and three roles that asked for own-row access got the whole firm's compliance status instead. The generator said so in a note, and the note was the only sign | The table is user-owned, and the matrix now shows `CRU (own)` for everyone but the policy owner |
| 9 | `test_a_step_with_no_spec_for_this_profile_is_skipped` named the next pilot due to be built | It failed the day that pilot got its roles file — a test about the build driver, broken by the project making progress | It picks a profile `SPECS` does not map, and a companion test asserts every mapped profile does run its own specs |

Finding 1 is the one to dwell on. The role was declared, the build was green, the matrix
rendered, and the Communications Editor could not read the directory of the people it publishes
about. Nothing failed — the role was simply two thirds absent, and the only reason it surfaced
is that this is the first roles file in the repo with a three-level chain.

Every fix above is asserted by a test, and every test was verified by reintroducing its bug and
watching the suite fail.

---

## 7. Why this one *is* on SharePoint

Phase 3's limits report argued for keeping Work Management off SharePoint. Phase 5's argues the
opposite, and it is the same report:

| Limit | Count | What it means here |
|---|---|---|
| list view threshold | 5 | `PolicyAttestation`, `ServiceRequest`, `ApprovalStep`, `Comment`, `DocumentReference`. Attestations grow by headcount × policies — 320 for a 64-person practice — so the threshold is years away, not months |
| no column security | 7 | six of them core PII plus the dietary requirements. All seven sit on lists a reader has no reason to open, and the two that matter can have their inheritance broken |
| lookup threshold | 0 | nothing here has more than 12 lookups in a view |

Compare with Phase 3, where one list exceeded the item threshold by a factor of three before
anybody had used the system. Same report, same generator, opposite conclusion — which is the
point of generating it rather than arguing about it.

---

## 8. Still to do

| | Needs |
|---|---|
| The hub itself, and the SPFx web parts | the roadmap's actual Phase 5 deliverable. This phase delivers the lists, the taxonomy, the permission design and the reports SharePoint cannot give you |
| Provision the site from the generated PnP template | a tenant |
| Break inheritance on the lists holding PII | the site, once provisioned — the limits report says which |
| Viva Connections dashboard and the mobile view | the tenant, and the "mobile-friendly" exit criterion is a judgement about the result rather than a property of the data |
| Audience reach as a measure | audience columns are multilookups, which the TDS endpoint does not expose, so "how many people could see this post" cannot be computed from the model as it stands. It is stated on the dashboard rather than worked around |
| `ApprovalPolicy` rows for news and policy versions | written, not generated |
| The report canvas | Power BI Desktop, from the generated `pages.md` |

That fifth row is the honest limit of the adoption page, and it is the same class of gap as the
two columns this phase added: a measure that cannot be written is usually telling you something
about the model.
