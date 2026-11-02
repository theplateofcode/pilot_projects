"""Tests for the CRM pilot's Dataverse deployer.

These run entirely offline: the model is resolved from the repo schemas and every payload and
XML document the deployer would send is built and checked. Run with:

    python -m unittest discover -s tests
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from xml.etree import ElementTree

import yaml

REPO = Path(__file__).resolve().parent.parent
PILOT = REPO / "pilots" / "crm-dataverse"
sys.path.insert(0, str(PILOT))

from deploy.client import Dataverse, DataverseError, normalise_org_url  # noqa: E402
from deploy.data_steps import DataDeployer, relative_dates  # noqa: E402
from deploy.model import CrmModel, ScopeError, load_model  # noqa: E402
from deploy.security_steps import SecurityDeployer  # noqa: E402
from deploy.state import State  # noqa: E402
from deploy.ux_steps import UxDeployer  # noqa: E402

PROFILE = "law-crm-starter"
MODEL_PATH = REPO / "build" / f"{PROFILE}.model.json"


def ensure_model() -> dict:
    if not MODEL_PATH.exists():
        subprocess.run(
            [sys.executable, str(REPO / "tools" / "schematool.py"), "export", "--profile", PROFILE],
            cwd=REPO, check=True, capture_output=True,
        )
    return load_model(str(MODEL_PATH))


class Recorder(Dataverse):
    """A Dataverse client that records instead of calling."""

    def __init__(self):
        super().__init__("https://test.crm.dynamics.com", dry_run=True, log=lambda *_: None)


def build(config=None):
    model_json = ensure_model()
    config = config or yaml.safe_load((PILOT / "app" / "crm-app.yaml").read_text())
    model = CrmModel(
        model_json,
        config["solution"]["publisher"]["prefix"],
        config["tables"]["include"],
        config["tables"].get("exclude", []),
    )
    state = State(Path("/tmp/does-not-exist.json"), read_only=True)
    dv = Recorder()
    return dv, model, config, state


class ModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dv, cls.model, cls.config, cls.state = build()

    def test_standard_tables_are_extended_not_duplicated(self):
        logicals = {t.logical for t in self.model.tables.values()}
        self.assertIn("account", logicals)
        self.assertIn("contact", logicals)
        self.assertIn("transactioncurrency", logicals)
        self.assertNotIn("pfx_account", logicals)
        self.assertNotIn("pfx_contact", logicals)

    def test_native_columns_are_not_recreated(self):
        account = self.model.tables["Account"]
        created = {c.logical for c in account.columns}
        self.assertNotIn("name", created)
        self.assertNotIn("telephone1", created)
        self.assertEqual(account.logical_of("name"), "name")
        self.assertEqual(account.logical_of("mainPhone"), "telephone1")
        self.assertEqual(account.logical_of("accountTier"), "pfx_accounttier")

    def test_every_column_has_a_payload_or_relationship(self):
        for table in self.model.tables.values():
            for column in table.columns:
                with self.subTest(column=f"{table.logical}.{column.logical}"):
                    if column.kind == "attribute":
                        self.assertIsNotNone(column.payload)
                        self.assertIn("@odata.type", column.payload)
                    else:
                        self.assertIsNotNone(column.relationship)

    def test_names_are_unique_and_within_limits(self):
        for table in self.model.tables.values():
            logicals = [c.logical for c in table.columns if c.kind in ("attribute", "lookup")]
            self.assertEqual(len(logicals), len(set(logicals)), f"duplicate column in {table.logical}")
            for logical in logicals:
                self.assertLessEqual(len(logical), 50, logical)
        relationships = [
            c.relationship["SchemaName"]
            for t in self.model.tables.values()
            for c in t.columns
            if c.relationship
        ]
        self.assertEqual(len(relationships), len(set(relationships)), "duplicate relationship name")
        for name in relationships:
            self.assertLessEqual(len(name), 100, name)

    def test_required_lookups_point_at_deployed_tables(self):
        for table in self.model.tables.values():
            for column in table.columns:
                if column.kind != "lookup":
                    continue
                target = column.relationship["ReferencedEntity"]
                with self.subTest(column=f"{table.logical}.{column.logical}"):
                    self.assertIn(target, {t.logical for t in self.model.tables.values()})

    def test_choice_columns_bind_to_a_global_choice_set(self):
        used = set()
        for table in self.model.tables.values():
            for column in table.columns:
                if column.spec.get("type") in ("choice", "multichoice"):
                    self.assertIn("_globalOptionSet", column.payload)
                    used.add(column.spec["enum"])
        self.assertEqual(used, self.model.used_enums)
        for enum_name in used:
            payload = self.model.option_set_payload(enum_name)
            self.assertLessEqual(len(payload["Name"]), 50)
            self.assertTrue(payload["Options"])

    def test_choice_defaults_are_real_option_values(self):
        for table in self.model.tables.values():
            for column in table.columns:
                if column.spec.get("type") != "choice":
                    continue
                default = column.spec.get("default")
                if default is None:
                    continue
                value = self.model.enum_value(column.spec["enum"], default)
                self.assertIsNotNone(value, f"{table.logical}.{column.logical} default '{default}'")
                self.assertEqual(column.payload.get("DefaultFormValue"), value)

    def test_generated_primary_label_is_optional(self):
        payload = self.model.table_payload(self.model.tables["ConsentRecord"])
        primary = payload["Attributes"][0]
        self.assertTrue(primary["IsPrimaryName"])
        self.assertEqual(primary["RequiredLevel"]["Value"], "None")

    def test_required_primary_label_stays_required(self):
        payload = self.model.table_payload(self.model.tables["Opportunity"])
        self.assertEqual(payload["Attributes"][0]["RequiredLevel"]["Value"], "ApplicationRequired")

    def test_autonumber_carries_its_format(self):
        column = self.model.tables["Opportunity"].column("opportunityNumber")
        self.assertEqual(column.payload["AutoNumberFormat"], "OPP-{SEQNUM:6}")

    def test_parent_lookups_cascade(self):
        column = self.model.tables["ProposalLine"].column("proposal")
        self.assertEqual(column.relationship["CascadeConfiguration"]["Delete"], "Cascade")

    def test_address_expands_into_five_columns_and_a_country_lookup(self):
        office = self.model.tables["Office"]
        self.assertEqual(office.logical_of("addressCity"), "pfx_addresscity")
        self.assertEqual(office.column("addressCountry").relationship["ReferencedEntity"], "pfx_country")

    def test_polymorphic_regarding_becomes_table_plus_id(self):
        comment = self.model.tables["Comment"]
        self.assertIsNotNone(comment.logical_of("regardingTable"))
        self.assertIsNotNone(comment.logical_of("regardingId"))

    def test_multilookup_becomes_many_to_many(self):
        column = self.model.tables["Employee"].column("languages")
        self.assertEqual(column.kind, "manytomany")
        self.assertEqual(column.relationship["Entity2LogicalName"], "pfx_language")

    def test_alternate_keys_only_use_deployed_columns(self):
        for table in self.model.custom_tables:
            for payload in self.model.key_payloads(table):
                for logical in payload["KeyAttributes"]:
                    self.assertTrue(
                        any(c.logical == logical for c in table.columns + table.natives),
                        f"{table.logical}: key column {logical}",
                    )

    def test_terminology_from_the_overlay_reaches_the_labels(self):
        self.assertEqual(self.model.tables["Opportunity"].display, "Pitch")
        self.assertEqual(self.model.tables["Proposal"].plural, "Fee Proposals")

    def test_excluding_a_required_target_is_an_error(self):
        with self.assertRaises(ScopeError):
            CrmModel(ensure_model(), "pfx", ["Opportunity"], ["Account"])

    def test_scope_pulls_in_required_parents(self):
        small = CrmModel(ensure_model(), "pfx", ["Opportunity"])
        names = set(small.tables)
        self.assertLessEqual({"Opportunity", "Account", "SalesPipeline", "PipelineStage", "Currency"}, names)


class UxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dv, model, config, state = build()
        cls.model = model
        cls.config = config
        cls.ux = UxDeployer(dv, model, config, state, lambda *_: None, True)
        cls.ux.refresh_table_metadata()

    def test_every_spec_column_exists(self):
        for table_name, spec in self.config["ux"].items():
            table = self.model.tables[table_name]
            for view in spec.get("views", []):
                for column in view["columns"]:
                    self.assertIsNotNone(self.ux.resolve(table, column), f"{table_name}.{column}")
            for tab in spec.get("form", []):
                for section in tab.get("sections", []) or []:
                    for column in section["columns"]:
                        self.assertIsNotNone(self.ux.resolve(table, column), f"{table_name}.{column}")

    def test_view_fetch_and_layout_are_well_formed(self):
        for table_name, spec in self.config["ux"].items():
            table = self.model.tables[table_name]
            for view in spec.get("views", []):
                columns = [self.ux.resolve(table, c) for c in view["columns"]]
                columns = [c for c in columns if c]
                fetch = self.ux.fetch_xml(table, columns, view.get("order", []), view.get("filter", []))
                layout = self.ux.layout_xml(table, columns)
                ElementTree.fromstring(fetch)
                ElementTree.fromstring(layout)
                self.assertIn(table.logical, fetch)

    def test_choice_filters_are_translated_to_option_values(self):
        table = self.model.tables["Opportunity"]
        xml = self.ux.condition_xml(table, {"column": "status", "eq": "won"})
        self.assertIn('value="2"', xml)
        self.assertIn('attribute="pfx_status"', xml)

    def test_unknown_choice_in_a_filter_is_dropped_not_guessed(self):
        table = self.model.tables["Opportunity"]
        self.assertEqual(self.ux.condition_xml(table, {"column": "status", "eq": "nonsense"}), "")

    def test_owner_filter_uses_the_current_user(self):
        table = self.model.tables["Opportunity"]
        self.assertIn("eq-userid", self.ux.condition_xml(table, {"owner": "me"}))

    def test_forms_are_well_formed_and_place_every_column_once(self):
        for table in self.model.tables.values():
            spec = (self.config.get("ux", {}) or {}).get(table.name, {})
            tabs = spec.get("form") or self.ux.generated_form(table)
            xml = self.ux.form_xml(table, tabs)
            tree = ElementTree.fromstring(xml)
            fields = [
                control.get("datafieldname")
                for control in tree.iter("control")
                if control.get("datafieldname")
            ]
            self.assertEqual(len(fields), len(set(fields)), f"{table.logical}: a column is on the form twice")
            self.assertTrue(fields, f"{table.logical}: form has no columns")

    def test_forms_cover_every_created_column(self):
        for table in self.model.tables.values():
            spec = (self.config.get("ux", {}) or {}).get(table.name, {})
            tabs = spec.get("form") or self.ux.generated_form(table)
            xml = self.ux.form_xml(table, tabs)
            placed = {c.get("datafieldname") for c in ElementTree.fromstring(xml).iter("control")}
            expected = {
                c.logical
                for c in table.columns
                if c.kind in ("attribute", "lookup") and c.type not in ("image", "file")
                and c.name not in {"externalId", "sourceSystem", "lastSyncedOn", "retentionLabel"}
            }
            self.assertEqual(expected - placed, set(), f"{table.logical}: columns missing from the form")

    def test_subgrids_reference_a_real_relationship(self):
        table = self.model.tables["Opportunity"]
        xml = self.ux.subgrid_xml(table, {"table": "Proposal", "column": "opportunity", "label": "Fee proposals"})
        self.assertIn("<TargetEntityType>pfx_proposal</TargetEntityType>", xml)
        relationship = self.model.tables["Proposal"].column("opportunity").relationship["SchemaName"]
        self.assertIn(relationship, xml)

    def test_site_map_is_well_formed_and_covers_the_config(self):
        xml = self.ux.site_map_xml()
        tree = ElementTree.fromstring(xml)
        entities = {sub.get("Entity") for sub in tree.iter("SubArea")}
        self.assertIn("pfx_opportunity", entities)
        self.assertIn("account", entities)
        self.assertIn("activitypointer", entities)
        self.assertEqual(len(list(tree.iter("Area"))), len(self.config["app"]["areas"]))

    def test_charts_build_well_formed_xml(self):
        for spec in self.config["charts"]:
            table = self.model.tables[spec["table"]]
            self.assertIsNotNone(self.ux.resolve(table, spec["group_by"]))
        self.ux.ensure_charts()
        posts = [r for r in self.ux.dv.recorded if "savedqueryvisualizations" in r["url"]]
        self.assertEqual(len(posts), len(self.config["charts"]))
        for post in posts:
            ElementTree.fromstring(post["body"]["datadescription"])
            ElementTree.fromstring(post["body"]["presentationdescription"])


class SecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dv, model, config, state = build()
        cls.security = SecurityDeployer(dv, model, config, state, lambda *_: None, True)
        cls.config = config

    def test_every_table_in_scope_gets_privileges(self):
        for role in self.config["security"]["roles"]:
            wanted = self.security.wanted_privileges(role)
            for table in self.security.model.tables.values():
                self.assertIn(table.logical, wanted, f"{role['name']} misses {table.logical}")

    def test_platform_privileges_are_always_included(self):
        for role in self.config["security"]["roles"]:
            wanted = self.security.wanted_privileges(role)
            self.assertIn("appmodule", wanted)
            self.assertEqual(wanted["appmodule"]["read"], "global")

    def test_read_only_role_grants_no_writes(self):
        role = next(r for r in self.config["security"]["roles"] if "Read only" in r["name"])
        wanted = self.security.wanted_privileges(role)
        for logical, depths in wanted.items():
            if logical in ("userquery", "userqueryvisualization", "annotation"):
                continue   # personal views and notes stay editable on purpose
            self.assertNotIn("write", depths, logical)
            self.assertNotIn("delete", depths, logical)

    def test_readonly_tables_are_read_only_for_business_developers(self):
        role = next(r for r in self.config["security"]["roles"] if "Business Developer" in r["name"])
        wanted = self.security.wanted_privileges(role)
        self.assertEqual(wanted["pfx_pipelinestage"], {"read": "global"})
        self.assertIn("write", wanted["pfx_opportunity"])

    def test_depth_is_clamped_to_what_the_privilege_allows(self):
        privilege = {"CanBeBasic": True, "CanBeLocal": True, "CanBeDeep": False, "CanBeGlobal": False}
        self.assertEqual(SecurityDeployer.clamp(privilege, "global"), "Local")
        self.assertEqual(SecurityDeployer.clamp(privilege, "basic"), "Basic")
        self.assertIsNone(SecurityDeployer.clamp(privilege, "none"))


class DataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dv, model, config, state = build()
        cls.model = model
        cls.data = DataDeployer(dv, model, config, state, lambda *_: None, True)
        cls.data.refresh_table_metadata()
        cls.demo = yaml.safe_load((PILOT / "data" / "demo-fabrikam.yaml").read_text())

    def test_relative_dates(self):
        self.assertNotEqual(relative_dates("today+30"), "today+30")
        self.assertEqual(relative_dates("2024-01-01"), "2024-01-01")
        self.assertEqual(relative_dates(42), 42)

    def test_every_demo_column_exists(self):
        for block in self.demo["data"]:
            if block["table"].startswith("native:"):
                continue
            table = self.model.tables[block["table"]]
            for record in block["records"]:
                for key in record:
                    if key.startswith("_"):
                        continue
                    known = table.column(key) is not None
                    native_style = key.islower() and key.replace("_", "").isalnum()
                    self.assertTrue(known or native_style, f"{block['table']}.{key}")

    def test_demo_choices_are_valid_keys(self):
        for block in self.demo["data"]:
            if block["table"].startswith("native:"):
                continue
            table = self.model.tables[block["table"]]
            for record in block["records"]:
                for key, value in record.items():
                    column = table.column(key)
                    if not column or column.spec.get("type") not in ("choice", "multichoice"):
                        continue
                    for item in (value if isinstance(value, list) else [value]):
                        self.assertIsNotNone(
                            self.model.enum_value(column.spec["enum"], item),
                            f"{block['table']}.{key} = {item}",
                        )

    def test_demo_required_columns_are_present(self):
        for block in self.demo["data"]:
            if block["table"].startswith("native:"):
                continue
            table = self.model.tables[block["table"]]
            required = [
                c for c in table.columns
                if c.required and c.kind in ("attribute", "lookup") and not c.read_only
                and c.logical != table.primary_logical
            ]
            for record in block["records"]:
                for column in required:
                    if column.name.startswith(table.spec.get("primary") or "~"):
                        continue
                    self.assertIn(column.name, record, f"{block['table']} row misses {column.name}")

    def test_lookups_in_demo_data_bind_by_reference(self):
        table = self.model.tables["Opportunity"]
        body = self.data.build_body(table, {"name": "X", "account": "Brightwater Capital plc", "status": "open"})
        self.assertIn("pfx_account@odata.bind", body)
        self.assertEqual(body["pfx_status"], 1)

    def test_customer_lookups_get_a_table_qualified_bind(self):
        body = self.data.build_body(self.model.tables["Contact"], {"account": "Brightwater Capital plc"})
        self.assertIn("parentcustomerid_account@odata.bind", body)

    def test_fingerprint_is_stable_and_ignores_private_keys(self):
        first = DataDeployer.fingerprint("X", {"a": 1, "_alias": "z"})
        second = DataDeployer.fingerprint("X", {"_alias": "other", "a": 1})
        self.assertEqual(first, second)
        self.assertNotEqual(first, DataDeployer.fingerprint("X", {"a": 2}))

    def test_term_sets_are_seeded_from_the_model(self):
        self.data.load_term_sets()
        posts = [r for r in self.data.dv.recorded if r["method"] == "POST"]
        self.assertTrue(any("termset" in r["url"] for r in posts))
        self.assertTrue(len([r for r in posts if r["url"].endswith("s") and "term" in r["url"]]) > 5)


class ClientTests(unittest.TestCase):
    def test_org_url_is_normalised(self):
        self.assertEqual(normalise_org_url("contoso.crm.dynamics.com/"), "https://contoso.crm.dynamics.com")
        self.assertEqual(
            normalise_org_url("https://contoso.crm.dynamics.com/main.aspx"), "https://contoso.crm.dynamics.com"
        )

    def test_duplicate_errors_are_recognised(self):
        error = DataverseError("A record with these values already exists.")
        self.assertTrue(error.is_duplicate)
        self.assertFalse(DataverseError("insufficient privileges").is_duplicate)

    def test_dry_run_records_instead_of_calling(self):
        dv = Recorder()
        dv.post("accounts", {"name": "X"})
        self.assertEqual(len(dv.recorded), 1)
        self.assertEqual(dv.calls, 0)


class PlanTests(unittest.TestCase):
    def test_plan_runs_and_writes_payloads(self):
        out = REPO / "build" / "test-payloads"
        result = subprocess.run(
            [sys.executable, "-m", "deploy.cli", "plan", "--payload-dir", str(out)],
            cwd=PILOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("scope.json", "optionsets.json", "tables.json", "columns.json",
                     "relationships.json", "sitemap.xml"):
            self.assertTrue((out / name).exists(), name)
        tables = json.loads((out / "tables.json").read_text())
        self.assertTrue(all(t["Attributes"][0]["IsPrimaryName"] for t in tables))
        ElementTree.fromstring((out / "sitemap.xml").read_text())


if __name__ == "__main__":
    unittest.main()
