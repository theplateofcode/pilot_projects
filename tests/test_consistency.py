"""Cross-tool invariants: the generators must agree about the same environment.

Each generator is tested on its own elsewhere. These tests check the seams
between them, where a disagreement shows up as a Dataverse import that works
and a Power BI refresh that does not.

Run with: python -m unittest discover -s tests
"""
import json
import re
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import appgen  # noqa: E402
import dvgen  # noqa: E402
import flowgen  # noqa: E402
import pbigen  # noqa: E402
import secgen  # noqa: E402
import sqlgen  # noqa: E402

PREFIX = "wga"
MODEL_FILE = ROOT / "build" / "hr-starter.model.json"

# Columns Dataverse provides itself, which dvgen deliberately does not create
# but the TDS endpoint still serves.
NATIVE_COLUMNS = {
    "createdon", "createdby", "modifiedon", "modifiedby", "ownerid",
    "statecode", "statuscode", "versionnumber", "transactioncurrencyid",
}


LABEL_SUFFIX = "name"


def native(column: str) -> bool:
    if column in NATIVE_COLUMNS:
        return True
    return (column.endswith(LABEL_SUFFIX)
            and column[:-len(LABEL_SUFFIX)] in NATIVE_COLUMNS)


class CrossToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not MODEL_FILE.exists():
            raise unittest.SkipTest("run: python tools/schematool.py export --profile hr-starter")
        cls.model = json.loads(MODEL_FILE.read_text(encoding="utf-8"))

        cls.dv = dvgen.Generator(cls.model, PREFIX, ["core", "hr"])
        cls.plan = cls.dv.build()

        cls.pbi = pbigen.Builder(
            yaml.safe_load((ROOT / "powerbi" / "hr-dashboard.yaml").read_text(encoding="utf-8")),
            cls.model, PREFIX)

        cls.sec = secgen.RoleSet(
            yaml.safe_load((ROOT / "security" / "phase1-roles.yaml").read_text(encoding="utf-8")),
            cls.model, PREFIX)

        cls.app = appgen.Generator(
            yaml.safe_load((ROOT / "apps" / "hr-admin.yaml").read_text(encoding="utf-8")),
            cls.model, PREFIX, cls.sec.roles)
        cls.app_plan = cls.app.build()

        cls.flows = flowgen.Generator(cls.model, PREFIX, "WoodgroveHR").build()
        cls.sql = sqlgen.Generator(cls.model).build()

        # Every physical column dvgen will have created, per entity.
        created: dict[str, set[str]] = {}
        for table in cls.plan["tables"]:
            created[table["_name"]] = {a["LogicalName"] for a in table["payload"]["Attributes"]}
        for table in cls.plan["columns"]:
            created.setdefault(table["_name"], set()).update(
                c["LogicalName"] for c in table["attributes"])
        for rel in cls.plan["relationships"]["oneToMany"]:
            created.setdefault(rel["_entity"], set()).add(rel["payload"]["Lookup"]["LogicalName"])
        cls.created = created

        cls.table_logical = {t["_name"]: t["_logicalName"] for t in cls.plan["tables"]}

    # -- dvgen <-> pbigen
    def test_every_column_the_bi_model_reads_is_created_or_native(self):
        """A column the model reads but Dataverse does not have breaks the refresh."""
        missing = []
        for table, columns in self.pbi.columns.items():
            if table in (self.pbi.date_table, "Metrics"):
                continue
            logical_table = self.table_logical.get(table)
            for name, spec in columns.items():
                source = spec["sourceColumn"]
                if native(source):
                    continue
                if logical_table and source == f"{logical_table}id":
                    continue
                available = self.created.get(table, set())
                if source in available:
                    continue
                # The TDS endpoint adds a `<col>name` label column beside each
                # lookup and choice, which dvgen does not create itself. Check
                # the real column only after the full name has missed, or a
                # column genuinely called firstName looks like a label.
                if source.endswith(LABEL_SUFFIX) and source[:-len(LABEL_SUFFIX)] in available:
                    continue
                missing.append(f"{table}.{name} reads {source}")
        self.assertEqual(missing, [], "\n".join(missing))

    def test_both_generators_agree_on_the_currency_column(self):
        """A money table's currency is transactioncurrencyid in both tools."""
        for table in self.pbi.included:
            attributes = self.model["entities"][table]["attributes"]
            currency = dvgen.native_currency_column(attributes)
            if currency is None or attributes[currency].get("target") != "Currency":
                continue
            self.assertEqual(
                self.pbi.columns[table][currency]["sourceColumn"],
                "transactioncurrencyid", table)
            self.assertFalse(
                [r for r in self.plan["relationships"]["oneToMany"]
                 if r["_entity"] == table and r["_column"] == currency],
                f"{table}: dvgen should not create a second currency lookup")

    def test_a_currency_lookup_without_money_stays_a_real_column(self):
        """Country.defaultCurrency and LegalEntity.baseCurrency are ordinary lookups."""
        for table, column in (("Country", "defaultCurrency"), ("LegalEntity", "baseCurrency")):
            if column not in self.model["entities"].get(table, {}).get("attributes", {}):
                continue
            self.assertTrue(
                [r for r in self.plan["relationships"]["oneToMany"]
                 if r["_entity"] == table and r["_column"] == column], f"{table}.{column}")

    def test_every_bi_table_is_a_table_dataverse_will_have(self):
        for table in self.pbi.included:
            mode = (self.model["entities"][table].get("platforms", {}) or {}).get("dataverse")
            self.assertNotIn(mode, ("none",), table)

    # -- dvgen <-> secgen
    def test_every_privilege_names_a_table_that_exists(self):
        for payload in self.sec.payloads():
            for privilege in payload["addPrivileges"]["Privileges"]:
                table = privilege["_table"]
                mode = (self.model["entities"][table].get("platforms", {}) or {}).get("dataverse")
                if isinstance(mode, str) and (mode.startswith("standard:") or mode == "native"):
                    continue
                self.assertIn(table, self.table_logical,
                              f"{payload['_code']} grants on {table}, which dvgen does not create")

    def test_privilege_names_match_the_generated_logical_names(self):
        for payload in self.sec.payloads():
            for privilege in payload["addPrivileges"]["Privileges"]:
                table = privilege["_table"]
                if table not in self.table_logical:
                    continue
                self.assertTrue(
                    privilege["PrivilegeName"].endswith(self.table_logical[table]),
                    f"{privilege['PrivilegeName']} does not end with "
                    f"{self.table_logical[table]}")

    def test_secured_columns_are_exactly_the_ones_a_profile_covers(self):
        """dvgen marks IsSecured; the roles file must declare a profile for each."""
        secured = set()
        for table in self.plan["columns"]:
            for column in table["attributes"]:
                if column.get("IsSecured"):
                    secured.add(f"{table['_name']}.{column['LogicalName']}")
        for rel in self.plan["relationships"]["oneToMany"]:
            if rel["payload"]["Lookup"].get("IsSecured"):
                secured.add(f"{rel['_entity']}.{rel['payload']['Lookup']['LogicalName']}")

        declared = set()
        for profile in self.sec.spec.get("columnProfiles", []):
            for reference in profile.get("columns", []):
                table, _, attr = reference.partition(".")
                declared.add(f"{table}.{dvgen.logical(PREFIX, attr)}")
        self.assertEqual(secured, declared)

    # -- the compensation boundary, across all three
    def test_compensation_is_closed_in_dataverse_and_absent_from_power_bi(self):
        for role in ("employee", "manager"):
            grants = self.sec.roles[role]["effectiveGrants"].get("Compensation", {})
            self.assertEqual(grants, {"read": "user"}, role)
        self.assertNotIn("Compensation", self.pbi.columns)
        self.assertNotIn("Compensation", self.pbi.included)
        for measure in self.pbi.measures:
            self.assertNotIn("Compensation", measure["expression"], measure["name"])

    def test_pay_bearing_tables_are_closed_to_everyone_but_hr(self):
        for table in ("Offer", "Grade"):
            for role in ("employee", "manager"):
                self.assertNotIn(table, self.sec.roles[role]["effectiveGrants"],
                                 f"{role} can reach {table}")
            self.assertIn(table, self.sec.roles["hrAdmin"]["effectiveGrants"], table)

    # -- dvgen <-> appgen
    def test_every_form_control_binds_to_a_column_that_will_exist(self):
        unknown = []
        for table, xml in self.app_plan["forms"].items():
            import xml.etree.ElementTree as ElementTree
            for control in ElementTree.fromstring(xml).iter("control"):
                name = control.get("datafieldname")
                if native(name) or name in self.created.get(table, set()):
                    continue
                unknown.append(f"{table}: {name}")
        self.assertEqual(sorted(set(unknown)), [])

    def test_every_table_on_the_site_map_is_readable_by_some_role(self):
        """A model-driven app is shared, so each sub-area needs one role, not all."""
        wanted = self.app.meta.get("roles") or (
            [self.app.meta["role"]] if self.app.meta.get("role") else [])
        self.assertTrue(wanted, "the app declares no roles")
        grants = {r: self.sec.roles[r]["effectiveGrants"] for r in wanted}
        for table in self.app.tables:
            self.assertTrue(any(table in grants[r] for r in wanted),
                            f"none of {', '.join(wanted)} can read {table}")

    def test_the_app_places_no_table_the_build_will_not_create(self):
        for table in self.app.tables:
            mode = (self.model["entities"][table].get("platforms", {}) or {}) \
                .get("dataverse", "custom")
            if isinstance(mode, str) and mode.startswith("standard:"):
                continue
            self.assertIn(table, self.table_logical, table)

    # -- the currency rule, across every generator
    def test_all_four_generators_agree_on_the_currency_column(self):
        """It diverged once between dvgen and pbigen; now it is one function."""
        import xml.etree.ElementTree as ElementTree
        for table in sorted(self.table_logical):
            attributes = self.model["entities"][table]["attributes"]
            currency = dvgen.native_currency_column(attributes)
            if currency is None or attributes[currency].get("target") != "Currency":
                continue
            logical_currency = dvgen.logical(PREFIX, currency)

            # dvgen: no second lookup to transactioncurrency.
            self.assertFalse(
                [r for r in self.plan["relationships"]["oneToMany"]
                 if r["_entity"] == table and r["_column"] == currency],
                f"dvgen creates {table}.{currency}")

            # pbigen: reads the native column.
            if table in self.pbi.columns:
                self.assertEqual(
                    self.pbi.columns[table][currency]["sourceColumn"],
                    "transactioncurrencyid", f"pbigen {table}")

            # appgen: binds to the native column, never the custom one.
            if table in self.app_plan["forms"]:
                placed = {c.get("datafieldname") for c in
                          ElementTree.fromstring(self.app_plan["forms"][table])
                          .iter("control")}
                self.assertNotIn(logical_currency, placed, f"appgen {table}")

            # sqlgen keeps its own currencyId column with a real foreign key,
            # because SQL has no implicit currency behaviour to collide with.
            self.assertIn(f"[{currency}Id]", self.sql["files"]["003_tables.sql"],
                          f"sqlgen {table}")

    # -- dvgen <-> flowgen
    def test_every_flow_column_will_exist(self):
        import re as regex
        known = set(NATIVE_COLUMNS)
        for columns in self.created.values():
            known |= columns
        known |= set(self.table_logical.values())
        known |= {flowgen.plural(PREFIX, t) for t in self.table_logical}
        unknown = []
        pattern = regex.compile(rf"\b{PREFIX}_[a-z0-9]+\b")
        for flow in self.flows["flows"]:
            for token in set(pattern.findall(json.dumps(flow))):
                if token in known or token.endswith("sharedcommondataserviceforapps"):
                    continue
                if token.endswith("id") and token[:-2] in self.table_logical.values():
                    continue
                unknown.append(f"{flow['_name']}: {token}")
        self.assertEqual(sorted(set(unknown)), [])

    # -- the deployment plan
    def test_the_plan_creates_option_sets_before_the_columns_that_bind_to_them(self):
        available = {o["_logicalName"] for o in self.plan["optionSets"]}
        for table in self.plan["columns"]:
            for column in table["attributes"]:
                bind = column.get("GlobalOptionSet@odata.bind")
                if not bind:
                    continue
                name = re.search(r"Name='([^']+)'", bind).group(1)
                self.assertIn(name, available, f"{table['_name']}.{column['LogicalName']}")

    def test_every_relationship_target_is_a_table_that_exists_or_is_standard(self):
        standard = set()
        for entity, spec in self.model["entities"].items():
            mode = (spec.get("platforms", {}) or {}).get("dataverse", "")
            if isinstance(mode, str) and mode.startswith("standard:"):
                standard.add(mode.split(":", 1)[1].strip().lower())
        creating = set(self.table_logical.values())
        for rel in self.plan["relationships"]["oneToMany"]:
            target = rel["payload"]["ReferencedEntity"]
            self.assertTrue(target in creating or target in standard,
                            f"{rel['payload']['SchemaName']} points at {target}")

    def test_alternate_keys_only_cover_columns_the_plan_creates(self):
        for table in self.plan["alternateKeys"]:
            for key in table["keys"]:
                for column in key["KeyAttributes"]:
                    self.assertIn(column, self.created.get(table["_name"], set()),
                                  f"{key['SchemaName']} covers {column}")

    def test_the_plan_has_no_manual_steps_and_no_warnings(self):
        self.assertEqual(self.plan["manualSteps"], [])
        self.assertEqual(self.plan["warnings"], [])

    def test_every_generator_runs_clean_on_the_phase_1_profile(self):
        """One place to notice that something started warning."""
        reports = {
            # dvgen has no separate errors list: anything wrong is a warning,
            # which is why --strict matters for it.
            "dvgen": ([], self.plan["warnings"]),
            "pbigen": (self.pbi.errors, self.pbi.warnings),
            "appgen": (self.app_plan["errors"], self.app_plan["warnings"]),
            "flowgen": (self.flows["errors"], self.flows["warnings"]),
            "sqlgen": (self.sql["errors"], self.sql["warnings"]),
            "secgen": (self.sec.errors, self.sec.warnings),
        }
        for name, (errors, warnings) in reports.items():
            self.assertEqual(errors, [], name)
            self.assertEqual(warnings, [], name)


if __name__ == "__main__":
    unittest.main()
