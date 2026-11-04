"""Tests for tools/appgen.py. Run with: python -m unittest discover -s tests"""
import json
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import appgen  # noqa: E402

MODEL_FILE = ROOT / "build" / "hr-starter.model.json"
PREFIX = "wga"

# Columns Dataverse provides itself, which dvgen does not create but a form or
# view may legitimately bind to. Defined once so the form and view checks
# cannot disagree about what counts as native.
NATIVE_COLUMNS = {
    "createdon", "createdby", "modifiedon", "modifiedby", "ownerid",
    "statecode", "statuscode", "versionnumber", "transactioncurrencyid",
}


def _columns_dvgen_creates(model_json):
    """Every column the Dataverse build will actually have."""
    import dvgen
    plan = dvgen.Generator(model_json, PREFIX, ["core", "hr"]).build()
    known = set(NATIVE_COLUMNS)
    for table in plan["tables"]:
        known.update(a["LogicalName"] for a in table["payload"]["Attributes"])
    for table in plan["columns"]:
        known.update(c["LogicalName"] for c in table["attributes"])
    for rel in plan["relationships"]["oneToMany"]:
        known.add(rel["payload"]["Lookup"]["LogicalName"])
    return known


def attr(kind, **extra):
    out = {"type": kind, "required": False, "physical": {"dataverse": "Text (100)"}}
    out.update(extra)
    return out


def native(kind, physical, trait="base"):
    return {"type": kind, "required": True, "trait": trait,
            "physical": {"dataverse": physical}}


BASE = {
    "id": native("guid", "<table>id (native primary key)"),
    "createdOn": native("datetime", "createdon (native)"),
    "createdBy": native("lookup", "createdby (native → systemuser)"),
    "modifiedOn": native("datetime", "modifiedon (native)"),
    "modifiedBy": native("lookup", "modifiedby (native → systemuser)"),
    "stateCode": native("choice", "statecode (native)"),
    "rowVersion": native("integer", "versionnumber (native)"),
    "owner": {"type": "lookup", "target": "Employee", "trait": "owned",
              "physical": {"dataverse": "ownerid (native, user or team)"}},
}


def entity(attributes=None, sensitivity="internal", dataverse="custom",
           primary="name", description="A table.", plural=None):
    spec = {
        "layer": "hr", "description": description, "ownership": "user",
        "volume": "low", "sensitivity": sensitivity,
        "platforms": {"dataverse": dataverse}, "primary": primary, "keys": [],
        "attributes": {**BASE, **(attributes or {"name": attr("string", required=True)})},
    }
    if plural:
        spec["plural"] = plural
    return spec


def model(entities, enums=None, terminology=None):
    return {"profile": "t", "layers": {"hr": "1.0.0"}, "entities": entities,
            "enums": enums or {}, "termSets": {}, "terminology": terminology or {},
            "seed": {}}


def spec(areas=None, forms=None, views=None, **meta):
    base = {"name": "Test App", "prefix": PREFIX, "description": "d"}
    base.update(meta)
    return {"meta": base,
            "areas": areas if areas is not None else [
                {"id": "a1", "name": "Area one",
                 "groups": [{"id": "g1", "name": "Group one", "tables": ["T"]}]}],
            "forms": forms or {}, "views": views or {}}


class TestSiteMap(unittest.TestCase):
    def test_areas_groups_and_sub_areas_are_emitted(self):
        g = appgen.Generator(spec(), model({"T": entity()}), PREFIX)
        root = g.sitemap()
        self.assertEqual(root.find("Area").get("Id"), "a1")
        self.assertEqual(root.find("Area/Group").get("Id"), "g1")
        sub = root.find("Area/Group/SubArea")
        self.assertEqual(sub.get("Entity"), "wga_t")

    def test_a_sub_area_is_titled_with_the_plural(self):
        g = appgen.Generator(spec(), model({"T": entity(plural="Thingies")}), PREFIX)
        title = g.sitemap().find("Area/Group/SubArea/Titles/Title")
        self.assertEqual(title.get("Title"), "Thingies")

    def test_terminology_renames_a_sub_area(self):
        g = appgen.Generator(
            spec(areas=[{"id": "a", "name": "A", "groups": [
                {"id": "g", "name": "G", "tables": ["Project"]}]}]),
            model({"Project": entity()},
                  terminology={"Project": {"singular": "Matter", "plural": "Matters"}}),
            PREFIX)
        self.assertEqual(g.sitemap().find("Area/Group/SubArea/Titles/Title").get("Title"),
                         "Matters")

    def test_a_standard_table_uses_its_own_logical_name(self):
        g = appgen.Generator(
            spec(areas=[{"id": "a", "name": "A", "groups": [
                {"id": "g", "name": "G", "tables": ["Account"]}]}]),
            model({"Account": entity(dataverse="standard:account")}), PREFIX)
        self.assertEqual(g.sitemap().find("Area/Group/SubArea").get("Entity"), "account")

    def test_a_table_not_in_the_profile_is_an_error(self):
        g = appgen.Generator(
            spec(areas=[{"id": "a", "name": "A", "groups": [
                {"id": "g", "name": "G", "tables": ["Ghost"]}]}]),
            model({"T": entity()}), PREFIX)
        self.assertTrue(any("not in this profile" in e for e in g.errors))

    def test_a_table_with_no_dataverse_table_is_an_error(self):
        g = appgen.Generator(
            spec(areas=[{"id": "a", "name": "A", "groups": [
                {"id": "g", "name": "G", "tables": ["Gone"]}]}]),
            model({"Gone": entity(dataverse="none")}), PREFIX)
        self.assertTrue(any("no Dataverse table" in e for e in g.errors))

    def test_a_table_placed_twice_is_a_warning(self):
        g = appgen.Generator(
            spec(areas=[{"id": "a", "name": "A", "groups": [
                {"id": "g1", "name": "G1", "tables": ["T"]},
                {"id": "g2", "name": "G2", "tables": ["T"]}]}]),
            model({"T": entity()}), PREFIX)
        self.assertTrue(any("more than once" in w for w in g.warnings))
        self.assertEqual(g.tables, ["T"])

    def test_a_table_left_off_the_site_map_is_noted(self):
        g = appgen.Generator(spec(), model({"T": entity(), "Hidden": entity()}), PREFIX)
        self.assertTrue(any("Hidden is not on the site map" in n for n in g.notes))

    def test_a_table_the_role_cannot_read_is_a_warning(self):
        roles = {"hrAdmin": {"effectiveGrants": {"Allowed": {"read": "organization"}}}}
        g = appgen.Generator(
            spec(areas=[{"id": "a", "name": "A", "groups": [
                {"id": "g", "name": "G", "tables": ["Allowed", "Closed"]}]}],
                 role="hrAdmin"),
            model({"Allowed": entity(), "Closed": entity()}), PREFIX, roles)
        self.assertTrue(any("'Closed' is on the site map" in w for w in g.warnings))
        self.assertFalse(any("'Allowed' is on the site map" in w for w in g.warnings))


class TestFormLayout(unittest.TestCase):
    def _sections(self, generator, table="T"):
        root = generator.form(table)
        return {s.find("labels/label").get("description"):
                [c.find("control").get("datafieldname") for c in s.iter("cell")]
                for s in root.iter("section")}

    def test_required_columns_go_in_key_information(self):
        g = appgen.Generator(spec(), model({"T": entity({
            "name": attr("string", required=True),
            "note": attr("string")})}), PREFIX)
        sections = self._sections(g)
        self.assertIn("wga_name", sections["Key information"])
        self.assertIn("wga_note", sections["Details"])

    def test_platform_managed_columns_are_never_on_a_form(self):
        """statecode and statuscode live in the header; versionnumber nowhere."""
        g = appgen.Generator(spec(), model({"T": entity()}), PREFIX)
        placed = {c for cells in self._sections(g).values() for c in cells}
        for column in appgen.NOT_ON_A_FORM:
            self.assertNotIn(column, placed, column)

    def test_the_owner_is_in_details_not_key(self):
        g = appgen.Generator(spec(), model({"T": entity()}), PREFIX)
        sections = self._sections(g)
        self.assertIn("ownerid", sections["Details"])
        self.assertNotIn("ownerid", sections.get("Key information", []))

    def test_audit_columns_are_in_their_own_section_and_read_only(self):
        g = appgen.Generator(spec(), model({"T": entity()}), PREFIX)
        sections = self._sections(g)
        self.assertEqual(set(sections["Audit"]),
                         {"createdon", "createdby", "modifiedon", "modifiedby"})
        root = g.form("T")
        audit = next(s for s in root.iter("section")
                     if s.find("labels/label").get("description") == "Audit")
        for control in audit.iter("control"):
            self.assertEqual(control.get("disabled"), "true")

    def test_effective_dating_gets_a_validity_section(self):
        g = appgen.Generator(spec(), model({"T": entity({
            "name": attr("string", required=True),
            "validFrom": {"type": "date", "required": True, "trait": "effectiveDated",
                          "physical": {"dataverse": "Date only"}},
            "validTo": {"type": "date", "trait": "effectiveDated",
                        "physical": {"dataverse": "Date only"}}})}), PREFIX)
        self.assertEqual(set(self._sections(g)["Validity"]),
                         {"wga_validfrom", "wga_validto"})

    def test_integration_columns_are_grouped_together(self):
        g = appgen.Generator(spec(), model({"T": entity({
            "name": attr("string", required=True),
            "externalId": {"type": "string", "trait": "integration",
                           "physical": {"dataverse": "Text (100)"}}})}), PREFIX)
        self.assertIn("wga_externalid", self._sections(g)["Integration"])

    def test_a_column_stricter_than_its_table_gets_a_sensitive_section(self):
        """So a column security profile maps to one section rather than scattered."""
        g = appgen.Generator(spec(), model({"T": entity({
            "name": attr("string", required=True),
            "mobile": attr("phone", pii=True)})}), PREFIX)
        self.assertIn("wga_mobile", self._sections(g)["Sensitive"])

    def test_a_wholly_restricted_table_needs_no_sensitive_section(self):
        g = appgen.Generator(spec(), model({"T": entity({
            "name": attr("string", required=True),
            "amount": attr("money", pii=True)}, sensitivity="restricted")}), PREFIX)
        self.assertNotIn("Sensitive", self._sections(g))

    def test_a_declared_order_is_honoured(self):
        g = appgen.Generator(
            spec(forms={"T": {"order": ["b", "a"]}}),
            model({"T": entity({"name": attr("string", required=True),
                                "a": attr("string"), "b": attr("string")})}), PREFIX)
        key = self._sections(g)["Key information"]
        self.assertEqual(key[:2], ["wga_b", "wga_a"])

    def test_a_declared_order_cannot_drag_plumbing_into_key(self):
        g = appgen.Generator(
            spec(forms={"T": {"order": ["owner", "name"]}}),
            model({"T": entity({"name": attr("string", required=True)})}), PREFIX)
        self.assertNotIn("ownerid", self._sections(g)["Key information"])

    def test_a_declared_order_naming_an_unknown_column_is_an_error(self):
        g = appgen.Generator(
            spec(forms={"T": {"order": ["ghost"]}}),
            model({"T": entity()}), PREFIX)
        g.form_columns("T")
        self.assertTrue(any("'ghost' is not a column" in e for e in g.errors))

    def test_long_text_gets_more_than_one_row(self):
        g = appgen.Generator(spec(), model({"T": entity({
            "name": attr("string", required=True), "note": attr("text")})}), PREFIX)
        root = g.form("T")
        cell = next(c for c in root.iter("cell")
                    if c.find("control").get("datafieldname") == "wga_note")
        self.assertEqual(cell.get("rowspan"), "4")

    def test_calculated_columns_are_read_only(self):
        g = appgen.Generator(spec(), model({"T": entity({
            "name": attr("string", required=True),
            "ratio": attr("decimal", calculated="a / b")})}), PREFIX)
        root = g.form("T")
        control = next(c for c in root.iter("control")
                       if c.get("datafieldname") == "wga_ratio")
        self.assertEqual(control.get("disabled"), "true")

    def test_types_with_no_control_are_skipped(self):
        g = appgen.Generator(spec(), model({"T": entity({
            "name": attr("string", required=True),
            "home": attr("address"), "regarding": attr("polylookup", targets=["*"]),
            "units": attr("multilookup", target="T")})}), PREFIX)
        placed = {c for cells in self._sections(g).values() for c in cells}
        for column in ("wga_home", "wga_regarding", "wga_units"):
            self.assertNotIn(column, placed, column)


class TestViews(unittest.TestCase):
    def _view(self, generator, table="T"):
        return generator.view(table)

    def test_a_default_view_starts_with_the_primary_column(self):
        g = appgen.Generator(spec(), model({"T": entity({
            "name": attr("string", required=True), "code": attr("string")})}), PREFIX)
        layout = ET.fromstring(self._view(g).find("layoutxml").text)
        first = layout.find("row/cell").get("name")
        self.assertEqual(first, "wga_name")

    def test_an_auto_primary_jumps_to_the_generated_name_column(self):
        """`primary: auto` has no attribute of that name; dvgen makes one."""
        g = appgen.Generator(spec(), model({"T": entity(primary="auto")}), PREFIX)
        layout = ET.fromstring(self._view(g).find("layoutxml").text)
        self.assertEqual(layout.get("jump"), "wga_name")

    def test_a_named_primary_still_jumps_to_itself(self):
        g = appgen.Generator(spec(), model({"T": entity(
            {**BASE, "name": attr("string", required=True)})}), PREFIX)
        layout = ET.fromstring(self._view(g).find("layoutxml").text)
        self.assertEqual(layout.get("jump"), "wga_name")

    def test_a_view_is_filtered_to_active_rows(self):
        g = appgen.Generator(spec(), model({"T": entity()}), PREFIX)
        fetch = ET.fromstring(self._view(g).find("fetchxml").text)
        condition = fetch.find("entity/filter/condition")
        self.assertEqual(condition.get("attribute"), "statecode")
        self.assertEqual(condition.get("value"), "0")

    def test_declared_columns_and_sort_are_honoured(self):
        g = appgen.Generator(
            spec(views={"T": {"columns": ["code", "name"], "sort": [["code", "desc"]]}}),
            model({"T": entity({"name": attr("string", required=True),
                                "code": attr("string")})}), PREFIX)
        view = self._view(g)
        fetch = ET.fromstring(view.find("fetchxml").text)
        names = [a.get("name") for a in fetch.findall("entity/attribute")]
        self.assertEqual(names, ["wga_code", "wga_name"])
        order = fetch.find("entity/order")
        self.assertEqual(order.get("attribute"), "wga_code")
        self.assertEqual(order.get("descending"), "true")

    def test_a_view_override_naming_an_unknown_column_is_an_error(self):
        g = appgen.Generator(spec(views={"T": {"columns": ["ghost"]}}),
                             model({"T": entity()}), PREFIX)
        g.view("T")
        self.assertTrue(any("'ghost' is not a column" in e for e in g.errors))

    def test_a_sort_on_an_unknown_column_is_reported_not_crashed(self):
        g = appgen.Generator(spec(views={"T": {"sort": [["ghost", "asc"]]}}),
                             model({"T": entity()}), PREFIX)
        g.view("T")                         # must not raise
        self.assertTrue(any("cannot sort on 'ghost'" in e for e in g.errors))

    def test_a_money_tables_currency_binds_to_the_native_column(self):
        """dvgen maps it there, so binding to wga_currency binds to nothing."""
        g = appgen.Generator(spec(), model({
            "Currency": entity(dataverse="standard:transactioncurrency"),
            "T": entity({"name": attr("string", required=True),
                         "amount": attr("money", required=True),
                         "currency": attr("lookup", target="Currency",
                                          required=True)})}), PREFIX)
        placed = {c.get("datafieldname") for c in g.form("T").iter("control")}
        self.assertIn("transactioncurrencyid", placed)
        self.assertNotIn("wga_currency", placed)

    def test_a_wide_table_is_trimmed_to_stay_readable(self):
        attributes = {"name": attr("string", required=True)}
        for i in range(20):
            attributes[f"c{i}"] = attr("string", required=True)
        g = appgen.Generator(spec(), model({"T": entity(attributes)}), PREFIX)
        fetch = ET.fromstring(self._view(g).find("fetchxml").text)
        self.assertLessEqual(len(fetch.findall("entity/attribute")),
                             appgen.MAX_VIEW_COLUMNS)
        self.assertTrue(any("view trimmed" in n for n in g.notes))

    def test_sensitive_columns_stay_off_the_default_view(self):
        """A grid is the easiest place to leak a column nobody meant to show."""
        g = appgen.Generator(spec(), model({"T": entity({
            "name": attr("string", required=True),
            "mobile": attr("phone", pii=True)})}), PREFIX)
        fetch = ET.fromstring(self._view(g).find("fetchxml").text)
        names = [a.get("name") for a in fetch.findall("entity/attribute")]
        self.assertNotIn("wga_mobile", names)

    def test_long_text_stays_off_the_default_view(self):
        g = appgen.Generator(spec(), model({"T": entity({
            "name": attr("string", required=True), "note": attr("text")})}), PREFIX)
        fetch = ET.fromstring(self._view(g).find("fetchxml").text)
        self.assertNotIn("wga_note", [a.get("name")
                                      for a in fetch.findall("entity/attribute")])


class TestAppModule(unittest.TestCase):
    def test_the_app_lists_every_table_as_a_component(self):
        g = appgen.Generator(spec(), model({"T": entity()}), PREFIX)
        root = g.app_module()
        entities = [c.get("schemaName") for c in root.iter("AppModuleComponent")
                    if c.get("type") == "1"]
        self.assertEqual(entities, ["wga_t"])

    def test_the_app_has_a_stable_id_and_a_unique_name(self):
        a = appgen.Generator(spec(), model({"T": entity()}), PREFIX).app_module()
        b = appgen.Generator(spec(), model({"T": entity()}), PREFIX).app_module()
        self.assertEqual(a.find("AppModuleId").text, b.find("AppModuleId").text)
        self.assertEqual(a.find("UniqueName").text, "wga_TestApp")

    def test_forms_and_views_are_components_too(self):
        g = appgen.Generator(spec(), model({"T": entity()}), PREFIX)
        types = {c.get("type") for c in g.app_module().iter("AppModuleComponent")}
        self.assertEqual(types, {"1", "60", "26"})


class TestCli(unittest.TestCase):
    def test_a_bad_prefix_is_rejected(self):
        for bad in ("W", "1abc", "toolongprefix"):
            self.assertEqual(appgen.main(["--profile", "x", "--prefix", bad]), 2, bad)


class TestTheRealApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not MODEL_FILE.exists():
            raise unittest.SkipTest("run: python tools/schematool.py export --profile hr-starter")
        cls.spec = yaml.safe_load(
            (ROOT / "apps" / "hr-admin.yaml").read_text(encoding="utf-8"))
        cls.model = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
        roles, why_not = appgen.load_roles("hr-starter", PREFIX)
        cls.why_not = why_not
        cls.generator = appgen.Generator(cls.spec, cls.model, PREFIX, roles)
        cls.plan = cls.generator.build()

    def test_no_errors_and_no_warnings(self):
        self.assertEqual(self.plan["errors"], [])
        self.assertEqual(self.plan["warnings"], [])

    def test_the_roles_were_actually_loaded(self):
        """A silent skip looks exactly like a pass, so assert the check ran."""
        self.assertIsNone(self.why_not)
        self.assertTrue(self.generator.roles)

    def test_every_table_gets_a_form_and_a_view(self):
        self.assertEqual(set(self.plan["forms"]), set(self.generator.tables))
        self.assertEqual(set(self.plan["views"]), set(self.generator.tables))

    def test_every_form_and_view_is_well_formed_xml(self):
        for table, xml in {**self.plan["forms"], **self.plan["views"]}.items():
            ET.fromstring(xml)              # raises if malformed
        ET.fromstring(self.plan["sitemap"])
        ET.fromstring(self.plan["appModule"])

    def test_compensation_is_in_the_reward_area_only(self):
        """The exit criterion, expressed in the navigation as well as the roles."""
        areas = {a["id"]: {t for g in a["groups"] for t in g["tables"]}
                 for a in self.spec["areas"]}
        self.assertIn("Compensation", areas["reward"])
        for area_id, tables in areas.items():
            if area_id != "reward":
                self.assertNotIn("Compensation", tables, area_id)

    def test_no_form_carries_a_platform_managed_column(self):
        for table, xml in self.plan["forms"].items():
            root = ET.fromstring(xml)
            placed = {c.get("datafieldname") for c in root.iter("control")}
            for column in appgen.NOT_ON_A_FORM:
                self.assertNotIn(column, placed, f"{table}.{column}")

    def test_every_form_control_names_a_column_the_build_creates(self):
        known = _columns_dvgen_creates(self.model)
        unknown = []
        for table, xml in self.plan["forms"].items():
            for control in ET.fromstring(xml).iter("control"):
                if control.get("datafieldname") not in known:
                    unknown.append(f"{table}: {control.get('datafieldname')}")
        self.assertEqual(sorted(set(unknown)), [])

    def test_every_view_column_names_a_column_the_build_creates(self):
        known = _columns_dvgen_creates(self.model)
        unknown = []
        for table, xml in self.plan["views"].items():
            fetch = ET.fromstring(ET.fromstring(xml).find("fetchxml").text)
            for attribute in fetch.findall("entity/attribute"):
                if attribute.get("name") not in known:
                    unknown.append(f"{table}: {attribute.get('name')}")
            for order in fetch.findall("entity/order"):
                if order.get("attribute") not in known:
                    unknown.append(f"{table}: sort on {order.get('attribute')}")
        self.assertEqual(sorted(set(unknown)), [])

    def test_the_credential_view_puts_the_soonest_expiry_first(self):
        fetch = ET.fromstring(
            ET.fromstring(self.plan["views"]["EmployeeCredential"]).find("fetchxml").text)
        order = fetch.find("entity/order")
        self.assertEqual(order.get("attribute"), "wga_expireson")
        self.assertEqual(order.get("descending"), "false")

    def test_the_site_map_covers_every_area_in_the_spec(self):
        root = ET.fromstring(self.plan["sitemap"])
        self.assertEqual(len(root.findall("Area")), len(self.spec["areas"]))


if __name__ == "__main__":
    unittest.main()
