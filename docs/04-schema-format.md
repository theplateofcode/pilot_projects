# Schema format reference

How to read and write the YAML under [`schemas/`](../schemas). Run the tool after every change:

```bash
pip install -r tools/requirements.txt
python tools/schematool.py validate            # check every layer
python tools/schematool.py docs                # regenerate docs/data-dictionary
python tools/schematool.py export --profile law-firm   # resolved model → build/law-firm.model.json
python -m unittest discover -s tests           # tool tests
```

## Files

| Path | Contents |
|---|---|
| `schemas/core.yaml` | The foundation layer (also defines the traits) |
| `schemas/modules/*.yaml` | One module per pilot |
| `schemas/bridges.yaml` | Bridge layers, one YAML document per bridge (separated by `---`) |
| `schemas/industries/*.yaml` | Industry overlays, one document per overlay layer |
| `schemas/profiles.yaml` | Deployment profiles |

A file may hold several layers as separate YAML documents. Each document is one layer.

## A layer

```yaml
module:
  id: legal-work                 # kebab-case, unique
  name: Legal – Matter management overlay
  kind: industry                 # foundation | module | bridge | industry | client
  version: 1.0.0                 # semantic version of this layer
  dependsOn: [work, proserv-compliance]
  description: What this layer adds and why.

terminology: {}   # display-name overrides (industry/client layers)
traits: {}        # foundation layer only
enums: {}         # fixed choice sets
termSets: {}      # taxonomies
entities: {}      # new tables
extend: {}        # columns added to tables from dependencies
seed: {}          # reference data rows
```

## Entities

```yaml
entities:
  TripRequest:
    description: Request to travel, approved before booking.   # required
    label: Trip Request            # optional display name (default: split PascalCase)
    plural: Trip Requests          # optional (default: simple English plural)
    ownership: user                # user (row-level security) | organization  — default user
    volume: medium                 # low | medium | high  — default low
    sensitivity: internal          # public | internal | confidential | restricted — default internal
    traits: [integration, classified]    # opt-in traits
    primary: title                 # primary/display column (default: name; "auto" = generated label)
    keys: [[tripNumber]]           # alternate keys (lists of columns)
    platforms:                     # defaults: dataverse custom, sharepoint list, sql table
      dataverse: custom            # custom | standard:<table> | native | none
      sharepoint: list             # list | library | pages | termstore | native | none
      sql: table                   # table | none
    attributes:
      title: string(200)!
      traveller: lookup(Employee)!
      status: { type: choice(TripStatus)!, default: draft }
    notes:
      - Free-text notes shown in the data dictionary.
```

`primary: auto` adds a read-only `name` column that a flow/plug-in fills with a readable label
(e.g. "Jane Doe – Annual leave – 3 Nov"). Dataverse and SharePoint both require a primary text column.

## Column types

Write a column as a **type string**, or as a **mapping** whose `type` is a type string. A trailing `!`
means required.

| Type | Example | Meaning |
|---|---|---|
| `string(n)` | `string(200)!` | Single-line text, max *n* (default 100) |
| `text` | `text` | Multi-line plain text |
| `richtext` | `richtext` | Multi-line formatted text |
| `email`, `phone`, `url` | `email!` | Formatted text |
| `integer` | `integer` | Whole number |
| `decimal(p,s)` | `"decimal(12,2)"` | Decimal number — **quote it inside `{ }`** because of the comma |
| `percent` | `percent` | 0–100 with two decimals |
| `money` | `money!` | Amount; needs a `currency` lookup(Currency) on the entity, or `currencyField:` |
| `boolean` | `boolean` | Yes/No |
| `date` | `date!` | Date only (business dates — no time-zone shift) |
| `datetime` | `datetime` | Moment in time, stored in UTC |
| `duration` | `duration` | Whole minutes |
| `choice(Enum)` / `multichoice(Enum)` | `choice(TripStatus)!` | One / several values of an enum |
| `lookup(Entity)` | `lookup(Employee)!` | Reference to one row |
| `parent(Entity)` | `parent(ExpenseReport)` | Required lookup that owns this row (cascade delete, parental relationship) |
| `multilookup(Entity)` | `multilookup(OrgUnit)` | Many-to-many reference |
| `polylookup(A\|B)` / `polylookup(*)` | `polylookup(Account\|Contact)!` | Reference to a row in one of several tables, or any table |
| `term(Set)` / `multiterm(Set)` | `term(ServiceLine)` | One / several terms of a term set |
| `address` | `address` | Composite: line 1, line 2, city, state/province, postal code, country lookup |
| `file`, `image` | `image` | Binary stored by the platform (prefer `url` + `DocumentReference` for documents) |
| `autonumber` | `{ type: autonumber, format: "PRJ-{SEQNUM:6}" }` | Generated business number |

### Column properties (mapping form)

| Property | Example | Meaning |
|---|---|---|
| `description` | `Client-facing text on invoices.` | Shown in the dictionary and copied to platform descriptions |
| `default` | `draft` | Default value (enum key for choices) |
| `unique`, `indexed` | `true` | Uniqueness / index hint |
| `pii` | `true` | Personal data |
| `sensitivity` | `confidential` | Overrides the entity's sensitivity for this column (column security) |
| `readOnly`, `calculated` | `calculated: "hours × billRate"` | Computed by formula, rollup, flow or the server |
| `onDelete` | `cascade` | For `lookup`: `restrict` (default when required), `setNull` (default when optional), `cascade` |
| `currencyField` | `originalCurrency` | For `money` whose currency is not the `currency` column |
| `min`, `max` | `1`, `5` | Numeric range |
| `label` | `Matter` | Display name |
| `deprecated` | `true` | Still present, do not use; remove in the next major version |
| `dataverse`, `sharepoint`, `sql` | `ownerid (native)` | Override the physical mapping shown in the dictionary (used for native/standard columns) |

**YAML gotchas** — inside `{ … }` flow mappings, quote any value that contains a comma, a colon followed by a
space, or starts with a special character. The validator reports the stray keys this causes.

## Traits

Defined once in `core.yaml`; they add standard columns.

| Trait | Applies to | Columns |
|---|---|---|
| `base` | every entity | `id`, `createdOn`, `createdBy`, `modifiedOn`, `modifiedBy`, `stateCode`, `statusReason`, `rowVersion` |
| `owned` | user-owned entities | `owner` |
| `integration` | opt-in | `externalId`, `sourceSystem`, `lastSyncedOn` |
| `classified` | opt-in | `sensitivity`, `retentionLabel` |
| `effectiveDated` | opt-in | `validFrom`, `validTo` |

## Enums

```yaml
enums:
  TripStatus:
    description: Lifecycle of a trip request.
    values:
      - { code: 1, key: draft }
      - { code: 5, key: booking, label: Booking in progress }   # label defaults to "Booking"
      - { code: 9, key: cancelled, deprecated: true }
```

Codes are permanent. Add new values with new codes; never renumber or reuse.

## Term sets

```yaml
termSets:
  ServiceLine:
    description: The firm's lines of service.
    hierarchical: true
    open: false              # true = users may add terms
    terms:
      - Consulting
      - { name: Tax, children: [Corporate tax, Personal tax] }

# in an overlay — add terms to an existing set
termSets:
  ServiceLine:
    extends: true
    terms: [Litigation & dispute resolution, Corporate & M&A]
```

## Extending another layer's table

```yaml
extend:
  Project:
    attributes:
      limitationDate: { type: date, description: Critical date; also docketed as a KeyDate. }
```

Only tables from dependencies can be extended, and only with new column names.

## Seed (reference data)

```yaml
seed:
  ProjectType:
    - { code: LEG_LITIGATION, name: Litigation, defaultBillingModel: timeAndMaterials, requiresClient: true }
  ProjectPhaseDefinition:
    - { code: LEG_LIT_PLEAD, name: Pleadings, projectType: LEG_LITIGATION, sequence: 2 }
```

- Choice columns take the enum **key**; lookups take the target row's **natural key** (its `code`, or its
  unique column such as `isoCode`); term columns take the term name.
- Required columns must be present unless they have a default or are read-only/autonumber.
- Natural keys must be unique across all layers — prefix overlay codes (`LEG_`, `ACC_`, `ARC_`, `TEC_`).

## Terminology

```yaml
terminology:
  Project: { singular: Matter, plural: Matters }       # entity
  ServiceLine: { singular: Practice Area, plural: Practice Areas }   # term set
  Project.projectManager: { label: Responsible Lawyer }              # column
```

Later layers in a profile override earlier ones.

## Profiles

```yaml
profiles:
  law-firm:
    description: Law firms.
    layers: [core, crm, work, ..., legal-work]
```

A profile must list every dependency of every layer in it. `export --profile` merges the layers into one
JSON model (entities with all their columns, enums, merged term sets, seeds and terminology) — the input
for generators and demo-data tools.

## What the validator checks

- YAML is well-formed; no unknown keys anywhere (catches typos and comma mistakes)
- Layer ids, versions, kinds; dependencies exist; no cycles; everything reaches `core`
- Naming conventions and length limits; reserved platform column names
- Unique names for entities, enums and term sets across all layers
- Every reference (lookup targets, enums, term sets) is defined in the layer or its dependencies
- Primary column exists and is text; key columns exist
- Money columns have a currency lookup
- Extensions target visible tables and never collide
- Enum codes and keys are unique; defaults are valid keys
- Seed rows: known columns, required values present, valid enum keys and term names, lookups resolve to
  visible seed rows, natural keys unique across layers
- Profiles include all dependencies
