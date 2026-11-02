# CRM pilot — Dataverse model-driven app

A complete, deployable CRM for a professional-services firm: **clients, contacts, leads,
pitches on configurable pipelines, fee proposals with priced lines, campaigns, consent and
client relationships**, with a model-driven app, three security roles, reference data and a
demo firm's data.

Everything is generated from the canonical model in [`schemas/`](../../schemas) — the same YAML
that produces the [data dictionary](../../docs/data-dictionary/crm.md). Nothing here re-states
the schema: the deployer reads the resolved model and follows the Dataverse mapping the schema
already prescribes (standard `account`/`contact`/`transactioncurrency` extended rather than
duplicated, native activity tables used for interactions, addresses expanded into their parts).

```
app/crm-app.yaml        what the app shows: navigation, views, forms, charts, roles
data/demo-fabrikam.yaml demo data for Fabrikam Legal LLP
deploy/                 the deployer (Dataverse Web API, no pac CLI needed)
```

## Quick start

```bash
pip install -r pilots/crm-dataverse/requirements.txt
```

```bash
cd pilots/crm-dataverse && python -m deploy.cli plan
```

```bash
cd pilots/crm-dataverse && python -m deploy.cli deploy --org https://yourorg.crm.dynamics.com --assign-me
```

`plan` is offline: it resolves the model, prints the scope and writes every payload and XML
document it would send to `build/payloads/`. Read that before the first deploy.

`deploy` signs you in with a device code (no app registration needed — the link and code appear
in the terminal), then creates everything. It takes roughly 15–30 minutes on a trial
environment, mostly metadata creation. **Every step is idempotent**: anything already there is
skipped, so if a step fails, fix the cause and run exactly the same command again.

## What gets created

| | |
|---|--:|
| Publisher `pilotprojects` (prefix `pfx`) and unmanaged solution `pfx_crm` | 1 |
| Global choice sets | 28 |
| New tables (Lead, Pitch, Pipeline, Stage, Fee Proposal, Proposal Line, Campaign, Consent, Employee, Office, Term…) | 27 |
| Standard tables extended (`account`, `contact`; `transactioncurrency` reused as-is) | 2 |
| Columns, lookups and many-to-many relationships | ~360 |
| Alternate keys (pitch number, proposal number + revision, codes on reference tables) | 10 |
| Views | 40 |
| Main forms (one per table, with sub-grids for teams, proposals and lines) | 30 |
| Charts (pipeline by stage, pitches by practice area, won value by month, leads by source) | 4 |
| Model-driven app **CRM** with a three-area site map (Sales / Marketing / Setup) | 1 |
| Security roles: *CRM – Partner*, *CRM – Business Developer*, *CRM – Read only* | 3 |
| Reference data from the schema seeds: currencies, countries, languages, lead sources, pipelines and stages, term sets and terms | ~90 rows |
| Demo data: Fabrikam Legal LLP — 8 clients, 14 contacts, 7 leads, 10 pitches, 4 fee proposals, campaigns, consent, activities | 109 rows |

The app opens at `https://<yourorg>.crm.dynamics.com/main.aspx?appname=pfx_crm_app`.

## Steps

`solution` → `choices` → `tables` → `columns` → `relationships` → `keys` → `publish` → `views` →
`forms` → `charts` → `sitemap` → `app` → `security` → `reference` → `demo`

Run a subset with `--steps`, or leave parts out with `--skip`:

```bash
python -m deploy.cli deploy --org https://yourorg.crm.dynamics.com --steps views,forms,sitemap,app
```

```bash
python -m deploy.cli status --org https://yourorg.crm.dynamics.com
```

Progress is cached in `.deploy-state/<env>.<solution>.json`. Delete that file to force the
deployer to re-check everything against the environment (it still skips what exists).

## Options

| Option | Default | Notes |
|---|---|---|
| `--org` | `$DATAVERSE_URL` | Environment URL. Required for `deploy` and `status`. |
| `--profile` | `law-crm-starter` | Schema profile. `crm-starter` for generic wording; `law-crm-starter` adds the law-firm overlay (Client / Pitch / Fee Proposal, practice areas, panel pipeline). |
| `--prefix` | `pfx` (from `crm-app.yaml`) | Publisher prefix. **Decide before the first deploy — changing it later renames every table and column.** |
| `--auth` | `device` | `secret` uses a service principal: set `--client-id` and `DATAVERSE_CLIENT_SECRET`, and give the application user a System Customizer role. |
| `--tenant` | `common` | Set it when device-code sign-in picks the wrong tenant. |
| `--assign-me` | off | Assigns you the *CRM – Partner* role so the app works straight away. |
| `--stop-on-error` | off | Stop at the first failure instead of collecting problems and carrying on. |
| `--demo-data` | `data/demo-fabrikam.yaml` | Point it at your own file to load different data. |

## After deploying

1. **Give people roles.** Each user needs *Basic User* (or *Common Data Service User*) plus one
   of the three CRM roles. `--assign-me` does this for you.
2. **Check the app** at Power Apps → Apps → CRM. Sales → Pitches should show the Fabrikam
   pipeline; open *Brightwater – Fourth Coffee bolt-on acquisition* to see the team and proposal
   sub-grids.
3. **Demo path** (about four minutes):
   lead *Employment retainer enquiry* → qualify → pitch *Relecloud – employment retainer* →
   fee proposal PRP-000101 with priced lines → move the pitch to *Negotiate* → mark it won →
   open the *Pipeline by stage* chart.

## What is deliberately not here

- **Business process flow, Power Automate approvals, Power Pages portal, Power BI report.**
  The pilot's definition of done in the [roadmap](../../docs/01-plan-and-roadmap.md) includes
  them; they are built on top of this schema, not generated from it.
- **Calculated and rollup columns.** `weightedValue`, `subtotal`, `totalAmount`, `lineTotal` and
  `lastActivityOn` are created as ordinary columns, read-only on the form, for a flow or a
  calculated-field definition to fill. The schema records the formula for each.
- **Dashboards.** The four charts are created and can be pinned to a dashboard in a minute in
  the maker portal.
- **Multi-table (polymorphic) lookups.** `Comment.regarding`, `DocumentReference.regarding` and
  `ApprovalRequest.regarding` are stored as table name + id, exactly as the schema's Dataverse
  mapping prescribes. `Activity` is not deployed at all: interactions use the native `task`,
  `phonecall`, `appointment` and `email` tables, which the app's Activities view shows.
- **Column-level security** for the columns the schema marks confidential, and **auditing** is
  switched on only for confidential and restricted tables.

## Adapting it

- **Another industry**: deploy a different profile (`--profile crm-starter`), or add an overlay
  layer in `schemas/industries/` and a profile in `schemas/profiles.yaml`. Terminology from the
  overlay becomes the Dataverse display names automatically.
- **A client's own wording and columns**: add a client overlay layer, re-export, re-run `deploy`.
  New columns and tables are added; nothing existing is touched.
- **Different navigation, views or forms**: edit `app/crm-app.yaml` and re-run
  `--steps views,forms,sitemap,app`. Forms are rewritten in place, so a changed layout is
  applied; views you have edited by hand in the maker portal are left alone (they are matched by
  name and skipped).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `publisher prefix mismatch` | The publisher already exists with a different prefix. Change `solution.publisher` in `crm-app.yaml`, or pass `--prefix` to match. |
| `insufficient privileges` on metadata | You need System Administrator or System Customizer in that environment. |
| Column creation fails with "name in use" | A table of the same logical name exists from an earlier attempt with a different prefix. Use a clean environment or a new prefix. |
| The app opens but a table is missing from navigation | The site map references a table that was not deployed — check the `Notes` section of `plan` output, then re-run `--steps sitemap,app`. |
| Views or forms look wrong after a config change | Re-run `--steps views,forms` and hard-refresh the app (the browser caches form definitions). |
| Throttling (429) | The client backs off and retries automatically; a large deployment may simply take longer. |

## A note on testing

The generator, every payload and every XML document it produces are covered by the tests in
[`tests/test_crm_dataverse.py`](../../tests/test_crm_dataverse.py), which run offline in CI —
they check the mapping, name limits, uniqueness, choice values, form and view XML, the site map,
the security matrix and the demo data (every column exists, every choice value is real, every
required column is supplied). What they cannot check is the Dataverse service itself: the first
run against a real environment is where API-level surprises (a rejected form layout, a chart
definition, an app-component call) will show up. Run `plan` first, then `deploy` into a
**developer or trial environment**, and expect to re-run the command once or twice — failures
are reported per object at the end of the run and nothing successful is repeated.
