# Security review of the tooling

The generators read credentials, build SQL and XML from schema-supplied strings, write
deployment artefacts and shell out to each other. This is a review of those surfaces, done by
feeding the tools hostile input rather than by reading them and concluding they look fine.

It covers the **tooling**, not the data model's own security design — that is
[Phase 1 §3](06-phase1-core-hr.md#3-security) and [Phase 2 §2](08-phase2-crm.md#2-security-selling-and-clearing-are-different-jobs).

Last run against 635 tests, all green.

---

## 1. Findings

| # | Finding | Severity | State |
|---|---|---|---|
| 1 | Four fictional firms used invented `.co.uk` domains, and the demo data held 1,080 email addresses at them | low, but real | **fixed** |
| 2 | XML attribute values have their CRLF normalised to LF on round-trip | not a defect | explained below |

### Finding 1 — demo email domains could belong to someone

`woodgroveadvisory.co.uk`, `fabrikamlegal.co.uk`, `adatumarchitects.co.uk` and
`northwindgroup.co.uk` were invented. An invented domain in a real top-level domain may be
registered by an actual company, and the demo data holds **1,080 addresses**. If anyone ever
loads it into a tenant and a flow sends mail — a leave approval, a proposal notification, an
onboarding reminder — it reaches a stranger's inbox. `contoso.com` is Microsoft's own sample
domain and was safe, but mixing the two conventions invites the next firm file to get it
wrong.

RFC 2606 reserves `example.com` and the `.example`, `.test` and `.invalid` top-level domains
for exactly this. Every firm now uses `.example`, so the addresses **cannot resolve**:

| Firm | Domain |
|---|---|
| Woodgrove Advisory | `woodgroveadvisory.example` |
| Fabrikam Legal | `fabrikamlegal.example` |
| Adatum Architects | `adatumarchitects.example` |
| Contoso Technologies | `contoso.example` |
| Northwind Group | `northwindgroup.example` |
| candidates and leads | `example.com` |

Two tests enforce it: every firm file's own domain, and every address in every generated row
across all five firms. Reintroducing a `.co.uk` domain fails the suite.

`*.crm11.dynamics.com` environment hostnames are left as they are — that is Microsoft's own
namespace and the strings are illustrative, never contacted.

### Finding 2 — XML newline normalisation

A CRLF inside an XML attribute value comes back as LF. That is XML 1.0's attribute-value
normalisation, not an escaping failure, and it cannot be used to break out of an attribute.
It would matter if a generated label were expected to hold a line break; none is, because
every description passes through `" ".join(text.split())` before it reaches the XML. Checked:
**0** generated attributes contain a newline.

---

## 2. What was tested, and what it showed

### Credentials

| Check | Result |
|---|---|
| Is a secret ever a command-line flag? | No. `ppcheck` has 2 flags and `dvdeploy` 7; none takes a secret. Credentials come only from `PP_TENANT_ID`, `PP_CLIENT_ID`, `PP_CLIENT_SECRET`, so they stay out of shell history, `ps` output and CI logs. |
| Does any tool print the secret? | No. Run with a sentinel secret, neither `ppcheck --json` nor `dvdeploy --dry-run` emits it. The client id is not printed either; the tenant id is, deliberately, because it is not a secret and an operator needs to see which tenant they are pointed at. |
| Does a generated artefact contain one? | No. Zero occurrences across every file in `build/`. |
| Is the token stored anywhere it could be serialised? | No. `Authorization` is added inside `_request`; it is not in the headers a plan carries, and no plan or manifest holds a token. |
| Does an error path echo a token? | A server error body is passed through verbatim, which is right — it is the server's message and the fix usually depends on reading it. Entra and Dataverse do not echo credentials back, and our own token is never put into a message. |
| Does anything weaken TLS or route around the proxy? | No. No `verify=False`, no `_create_unverified_context`, no unsetting of `HTTPS_PROXY`, no CA overrides. |

`ppcheck` decodes its own freshly issued JWT to show the tenant, app and object ids. It is
unverified and says so in the code: it is for display, and the token came from the call that
just succeeded.

### SQL generation

Seed values and demo rows become SQL literals, so the escaping was tested against eleven
hostile strings — `'; DROP TABLE Employee; --`, `a' OR '1'='1`,
`x'); EXEC xp_cmdshell 'whoami'; --`, fifty consecutive apostrophes, a NUL byte, mixed
Unicode, embedded CRLF, and XML-breaking markup.

- **Literals:** 0 of 11 escaped unsafely. Every one is a single `N'…'` token with each quote
  doubled. T-SQL has no backslash escape, so doubling is sufficient and complete.
- **Identifiers:** `quote()` bracket-quotes and **raises** on a closing bracket, which is the
  one character that could escape `[...]`. It does not try to sanitise, because silently
  rewriting an identifier is how a generator ends up pointing at the wrong column.

The generated DDL is also transpiled and executed against SQLite in the test suite, so a
malformed statement fails there rather than in a migration.

### XML generation

`spgen`, `appgen` and `solgen` build XML with `ElementTree`, which escapes attribute values
and text itself. Hostile strings round-trip unchanged except for the CRLF normalisation above;
`<script>` and `]]><!--injected-->` survive only as escaped text. A NUL byte is rejected,
which is correct — XML 1.0 forbids it.

### Process execution

`build.py` is the only tool that runs another. It passes `argv` as a list with no shell: no
`shell=True`, no `os.system`, no `eval`. The generated `pack.sh` sets `-euo pipefail`, quotes
its one argument, guards on usage before acting, and contains no `eval` or backticks.

### Demo data

1,080 addresses, 568 people across five firms, and all of it synthetic: names drawn from a
fixed international pool, companies assembled from neutral word parts, and no real
registration numbers. The firms are Microsoft's documentation brands, as the roadmap intends,
so a screenshot never shows a real practice.

`EmployeePersonalDetails` holds `dateOfBirth` and `nationalIdReference` in the model. The
generator **does not populate them**: the table exists so a real deployment has somewhere
properly secured to put them, and the demo has no need.

---

## 3. What this review does not cover

Being explicit, because a review that implies more than it did is worse than none:

| Not covered | Why |
|---|---|
| The generated artefacts' behaviour in a tenant | Needs a tenant. The security *design* is reviewed in the phase guides; whether Dataverse enforces it as expected can only be tested there. |
| Power Pages table permissions | The portal does not exist yet. The role is the floor; the table permissions are the boundary, and they are Phase 2's remaining work. |
| The flows at run time | Four child flows are unbuilt, and a flow's effective permissions depend on its connection owner — which is a tenant question. |
| Dependency vulnerabilities | Two dependencies: PyYAML, and sqlglot for tests only. Neither is pinned to a vulnerable version at the time of writing, but nothing here watches for new advisories. |
| Whether the roles are *correct* | Reviewed as a design in the phase guides and asserted by tests, but a role matrix is a business judgement, not a property a test can settle. |

## 4. Reproducing it

The credential and injection checks are inline in this document's history rather than a
committed script, because they are a one-off audit rather than something to run on every
push. What *is* enforced on every push:

```bash
python -m unittest discover -s tests
```

- every demo email domain is reserved for documentation (`test_demodata.py`)
- every firm's own domain is reserved (`test_demodata.py`)
- the generated T-SQL parses and executes (`test_sqlgen.py`)
- the generated XML parses and every reference resolves (`test_spgen.py`, `test_appgen.py`)
- `ppcheck` exits 2 with no credentials and never prints them (`test_ppcheck.py`)
