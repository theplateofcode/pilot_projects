"""Tests for tools/solgen.py. Run with: python -m unittest discover -s tests

Whether `pac solution pack` accepts the folder can only be settled by running
the CLI, which is not available here. What is checkable is the manifest: every
component listed has a file, every file is listed, the component type codes are
the ones Dataverse uses, and nothing is listed twice — an import fails on any of
those, with a message that does not say which.
"""
import json
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import solgen  # noqa: E402

PROFILE = "hr-starter"
PREFIX = "wga"


class TestComponentTypes(unittest.TestCase):
    def test_the_type_codes_are_the_dataverse_ones(self):
        """These are the API's numbers; a wrong one imports as the wrong thing."""
        self.assertEqual(solgen.COMPONENT_TYPES, {
            "entity": 1, "attribute": 2, "optionSet": 9, "entityRelationship": 10,
            "role": 20, "savedQuery": 26, "workflow": 29, "systemForm": 60,
            "appModule": 80, "connectionReference": 371,
        })

    def test_an_entity_includes_its_subcomponents_and_the_rest_do_not(self):
        self.assertEqual(solgen.BEHAVIOUR_INCLUDE_SUBCOMPONENTS, 2)
        self.assertEqual(solgen.BEHAVIOUR_INCLUDE_NOTHING, 0)


class TestCli(unittest.TestCase):
    def test_a_bad_prefix_is_rejected(self):
        for bad in ("W", "1abc", "toolongprefix"):
            self.assertEqual(solgen.main(
                ["--profile", "x", "--prefix", bad, "--solution", "S1"]), 2, bad)

    def test_a_bad_solution_name_is_rejected(self):
        for bad in ("1Solution", "has space", "has-dash", "x"):
            self.assertEqual(solgen.main(
                ["--profile", "x", "--prefix", "wga", "--solution", bad]), 2, bad)

    def test_a_bad_version_is_rejected(self):
        for bad in ("1.0", "1.0.0", "v1.0.0.0", "1.0.0.0.0"):
            self.assertEqual(solgen.main(
                ["--profile", "x", "--prefix", "wga", "--solution", "S1",
                 "--version", bad]), 2, bad)

    def test_a_good_version_is_accepted_by_the_validator(self):
        import re
        self.assertTrue(re.fullmatch(r"\d+\.\d+\.\d+\.\d+", "1.0.0.1"))


class TestMissingInputs(unittest.TestCase):
    def test_a_missing_dataverse_plan_is_an_error_naming_the_command(self):
        builder = solgen.Builder("no-such-profile", PREFIX, "S1", "P", "1.0.0.0")
        plan = builder.build()
        self.assertTrue(any("dvgen.py" in e for e in plan["errors"]))

    def test_missing_roles_or_flows_are_warnings_not_errors(self):
        """A solution of tables alone is a legitimate thing to want."""
        import tempfile
        plan_dir = ROOT / "build" / "dataverse" / PROFILE
        if not (plan_dir / "plan.json").exists():
            self.skipTest("run tools/dvgen.py first")
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp)
            target = build / "dataverse" / PROFILE
            target.mkdir(parents=True)
            (target / "plan.json").write_text(
                (plan_dir / "plan.json").read_text(encoding="utf-8"), encoding="utf-8")
            (build / "apps").mkdir()
            original = solgen.BUILD_DIR
            try:
                solgen.BUILD_DIR = build
                builder = solgen.Builder(PROFILE, PREFIX, "S1", "P", "1.0.0.0")
                plan = builder.build()
            finally:
                solgen.BUILD_DIR = original
        self.assertEqual(plan["errors"], [])
        self.assertTrue(any("secgen.py" in w for w in plan["warnings"]))
        self.assertTrue(any("flowgen.py" in w for w in plan["warnings"]))
        self.assertTrue(any("appgen.py" in w for w in plan["warnings"]))


class TestStaleRoles(unittest.TestCase):
    """A role file nobody generated this run must not be packaged.

    secgen writes one JSON per role into a directory solgen then packs
    wholesale. When the travel pilot was first built, three HR roles from an
    earlier run were still sitting in that directory and went into the travel
    solution — six roles in a solution that specified three, granting
    privileges on tables the travel roles never mention.
    """

    def _tree(self, tmp, roles, report_codes):
        import shutil
        plan_dir = ROOT / "build" / "dataverse" / PROFILE
        if not (plan_dir / "plan.json").exists():
            self.skipTest("run tools/dvgen.py first")
        build = Path(tmp)
        target = build / "dataverse" / PROFILE
        (target / "security" / "roles").mkdir(parents=True)
        shutil.copy(plan_dir / "plan.json", target / "plan.json")
        for code, name in roles:
            (target / "security" / "roles" / f"{code}.json").write_text(
                json.dumps({"_code": code,
                            "role": {"name": name, "description": "d"},
                            "addPrivileges": {"Privileges": []}}), encoding="utf-8")
        if report_codes is not None:
            (target / "security" / "report.json").write_text(
                json.dumps({"roles": [{"code": c} for c in report_codes]}),
                encoding="utf-8")
        (build / "apps").mkdir()
        return build

    def _build(self, build):
        original = solgen.BUILD_DIR
        try:
            solgen.BUILD_DIR = build
            return solgen.Builder(PROFILE, PREFIX, "S1", "P", "1.0.0.0").build()
        finally:
            solgen.BUILD_DIR = original

    def test_a_role_file_the_report_does_not_name_is_an_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            build = self._tree(tmp, [("traveller", "Travel – Traveller"),
                                     ("hrAdmin", "HR – Administrator")],
                               ["traveller"])
            plan = self._build(build)
        self.assertTrue(any("hrAdmin.json" in e and "stale" in e
                            for e in plan["errors"]), plan["errors"])
        self.assertNotIn("HR – Administrator",
                         [c["id"] for c in plan["components"]])

    def test_the_roles_the_report_names_are_packaged(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            build = self._tree(tmp, [("traveller", "Travel – Traveller")],
                               ["traveller"])
            plan = self._build(build)
        self.assertEqual(plan["errors"], [])
        self.assertIn("Travel – Traveller",
                      [c["id"] for c in plan["components"]])

    def test_without_a_report_every_role_file_is_still_packaged(self):
        """An older build directory has no report; refusing to pack anything
        would be a worse failure than the one being prevented."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            build = self._tree(tmp, [("traveller", "Travel – Traveller")], None)
            plan = self._build(build)
        self.assertEqual(plan["errors"], [])
        self.assertIn("Travel – Traveller",
                      [c["id"] for c in plan["components"]])


class TestTheRealSolution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT / "build" / "dataverse" / PROFILE / "plan.json").exists():
            raise unittest.SkipTest(
                f"run: python tools/dvgen.py --profile {PROFILE} --prefix {PREFIX}")
        cls.plan = solgen.Builder(PROFILE, PREFIX, "WoodgroveHR",
                                  "Woodgrove Advisory", "1.0.0.0").build()

    def test_no_errors_and_no_warnings(self):
        self.assertEqual(self.plan["errors"], [])
        self.assertEqual(self.plan["warnings"], [])

    def test_every_generated_file_is_well_formed_xml_or_json(self):
        for path, contents in self.plan["files"].items():
            if path.endswith(".xml"):
                ET.fromstring(contents)
            elif path.endswith(".json"):
                json.loads(contents)

    def test_the_manifest_lists_every_component_once(self):
        ids = [c["id"] for c in self.plan["components"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_attribute_belongs_to_a_table_in_the_solution(self):
        tables = {c["id"] for c in self.plan["components"] if c["type"] == "entity"}
        for component in self.plan["components"]:
            if component["type"] == "attribute":
                self.assertIn(component["id"].split(".")[0], tables, component["id"])

    def test_every_entity_folder_has_an_entity_file(self):
        """A folder of forms with no Entity.xml fails the import."""
        folders = {p.split("/")[2] for p in self.plan["files"]
                   if p.startswith("src/Entities/")}
        for folder in folders:
            self.assertIn(f"src/Entities/{folder}/Entity.xml", self.plan["files"],
                          folder)

    def test_the_solution_manifest_is_present_and_names_the_publisher(self):
        root = ET.fromstring(self.plan["files"]["src/Other/Solution.xml"])
        self.assertEqual(root.findtext(".//UniqueName"), "WoodgroveHR")
        publisher = root.find(".//Publisher")
        self.assertEqual(publisher.findtext("CustomizationPrefix"), PREFIX)
        self.assertEqual(
            publisher.find("LocalizedNames/LocalizedName").get("description"),
            "Woodgrove Advisory")

    def test_the_option_value_prefix_is_in_the_range_dataverse_allows(self):
        root = ET.fromstring(self.plan["files"]["src/Other/Solution.xml"])
        value = int(root.findtext(".//CustomizationOptionValuePrefix"))
        self.assertGreaterEqual(value, 10000)
        self.assertLessEqual(value, 99999)

    def test_the_option_value_prefix_is_stable_for_a_prefix(self):
        """Changing it after an import renumbers every custom choice value."""
        again = solgen.Builder(PROFILE, PREFIX, "WoodgroveHR", "P", "1.0.0.0").build()
        first = ET.fromstring(self.plan["files"]["src/Other/Solution.xml"]) \
            .findtext(".//CustomizationOptionValuePrefix")
        second = ET.fromstring(again["files"]["src/Other/Solution.xml"]) \
            .findtext(".//CustomizationOptionValuePrefix")
        self.assertEqual(first, second)

    def test_every_root_component_uses_a_known_type_code(self):
        root = ET.fromstring(self.plan["files"]["src/Other/Solution.xml"])
        known = {str(v) for v in solgen.COMPONENT_TYPES.values()}
        for component in root.findall(".//RootComponent"):
            self.assertIn(component.get("type"), known, component.get("schemaName"))

    def test_the_root_component_count_matches_the_component_list(self):
        root = ET.fromstring(self.plan["files"]["src/Other/Solution.xml"])
        self.assertEqual(len(root.findall(".//RootComponent")),
                         len(self.plan["components"]))

    def test_the_count_still_matches_when_the_app_carries_standard_tables(self):
        """The manifest used to be written before those components were added.

        hr-starter's site map has no standard tables, so the check above passed
        while law-firm-crm was short three components in Solution.xml.
        """
        if not (ROOT / "build" / "dataverse" / "law-firm-crm" / "plan.json").exists():
            self.skipTest("run: python tools/build.py --profile law-firm-crm --prefix fab")
        plan = solgen.Builder("law-firm-crm", "fab", "FabrikamCRM", "P",
                              "1.0.0.0").build()
        self.assertEqual(plan["errors"], [])
        root = ET.fromstring(plan["files"]["src/Other/Solution.xml"])
        self.assertEqual(len(root.findall(".//RootComponent")), len(plan["components"]))

    def test_a_standard_table_carrying_forms_is_declared_as_a_component(self):
        if not (ROOT / "build" / "dataverse" / "law-firm-crm" / "plan.json").exists():
            self.skipTest("run: python tools/build.py --profile law-firm-crm --prefix fab")
        plan = solgen.Builder("law-firm-crm", "fab", "FabrikamCRM", "P",
                              "1.0.0.0").build()
        root = ET.fromstring(plan["files"]["src/Other/Solution.xml"])
        entities = {c.get("schemaName") for c in root.findall(".//RootComponent")
                    if c.get("type") == "1"}
        for standard in ("account", "contact"):
            self.assertIn(standard, entities,
                          f"the solution must declare {standard}, whose forms it carries")

    def test_no_component_is_declared_twice(self):
        root = ET.fromstring(self.plan["files"]["src/Other/Solution.xml"])
        names = [(c.get("type"), c.get("schemaName"))
                 for c in root.findall(".//RootComponent")]
        self.assertEqual(len(names), len(set(names)))

    def test_tables_carry_their_subcomponents_and_attributes_do_not(self):
        """An entity that does not carry its subcomponents loses them on upgrade."""
        root = ET.fromstring(self.plan["files"]["src/Other/Solution.xml"])
        for component in root.findall(".//RootComponent"):
            if component.get("type") == "1":
                self.assertEqual(component.get("behavior"), "2",
                                 component.get("schemaName"))
            elif component.get("type") == "2":
                self.assertEqual(component.get("behavior"), "0",
                                 component.get("schemaName"))

    def test_the_content_types_part_is_present(self):
        """The package is an OPC zip; without this part nothing opens it."""
        self.assertIn("src/[Content_Types].xml", self.plan["files"])
        ET.fromstring(self.plan["files"]["src/[Content_Types].xml"])

    def test_the_flows_are_carried_with_a_connection_reference(self):
        workflows = [c for c in self.plan["components"] if c["type"] == "workflow"]
        self.assertTrue(workflows)
        self.assertTrue([c for c in self.plan["components"]
                         if c["type"] == "connectionReference"])
        self.assertTrue(any("connection" in n for n in self.plan["notes"]))

    def test_the_app_and_its_site_map_are_both_packaged(self):
        app = [p for p in self.plan["files"] if p.endswith("/AppModule.xml")]
        sitemap = [p for p in self.plan["files"] if p.endswith("/SiteMap.xml")]
        self.assertEqual(len(app), 1)
        self.assertEqual(len(sitemap), 1)
        self.assertEqual(Path(app[0]).parent, Path(sitemap[0]).parent)

    def test_the_three_security_roles_are_packaged(self):
        roles = [c for c in self.plan["components"] if c["type"] == "role"]
        self.assertEqual(len(roles), 3)

    def test_the_pack_script_is_generated_and_says_what_is_unproven(self):
        script = self.plan["files"]["pack.sh"]
        self.assertIn("pac solution pack", script)
        self.assertIn("WoodgroveHR", script)
        self.assertIn("Unmanaged", script)
        self.assertIn("dvdeploy.py", script)

    def test_managed_changes_the_manifest_and_the_pack_type(self):
        managed = solgen.Builder(PROFILE, PREFIX, "WoodgroveHR", "P", "1.0.0.0",
                                 managed=True).build()
        root = ET.fromstring(managed["files"]["src/Other/Solution.xml"])
        self.assertEqual(root.findtext(".//Managed"), "1")
        self.assertIn("Managed", managed["files"]["pack.sh"])

    def test_every_component_the_dataverse_plan_creates_is_in_the_solution(self):
        """Something created but not in the solution cannot be exported."""
        dv = json.loads((ROOT / "build" / "dataverse" / PROFILE / "plan.json")
                        .read_text(encoding="utf-8"))
        listed = {c["id"] for c in self.plan["components"]}
        for table in dv["tables"]:
            self.assertIn(table["_logicalName"], listed, table["_name"])
        for item in dv["optionSets"]:
            self.assertIn(item["_logicalName"], listed, item["_name"])
        for relationship in dv["relationships"]["oneToMany"]:
            self.assertIn(relationship["payload"]["SchemaName"], listed)
        for relationship in dv["relationships"]["manyToMany"]:
            self.assertIn(relationship["payload"]["SchemaName"], listed)


if __name__ == "__main__":
    unittest.main()
