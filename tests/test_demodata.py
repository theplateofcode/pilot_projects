"""Tests for tools/demodata.py. Run with: python -m unittest discover -s tests"""
import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import demodata  # noqa: E402

MODEL_FILE = ROOT / "build" / "hr-starter.model.json"


def tiny_model():
    return {
        "profile": "t", "layers": {"hr": "1.0.0"},
        "entities": {
            "Employee": {
                "layer": "core", "description": "A person.", "ownership": "organization",
                "volume": "low", "sensitivity": "internal",
                "platforms": {"dataverse": "custom"}, "primary": "displayName", "keys": [],
                "attributes": {
                    "id": {"type": "guid", "required": True, "trait": "base",
                           "physical": {"dataverse": "<table>id (native primary key)"}},
                    "displayName": {"type": "string", "required": True},
                    "employeeNumber": {"type": "string", "unique": True},
                    "status": {"type": "choice", "enum": "EmploymentStatus", "required": True},
                    "orgUnit": {"type": "lookup", "target": "OrgUnit", "required": True},
                    "home": {"type": "address", "required": True},
                },
            },
            "OrgUnit": {
                "layer": "core", "description": "A unit.", "ownership": "organization",
                "volume": "low", "sensitivity": "internal",
                "platforms": {"dataverse": "custom"}, "primary": "name", "keys": [],
                "attributes": {
                    "id": {"type": "guid", "required": True, "trait": "base",
                           "physical": {"dataverse": "<table>id (native primary key)"}},
                    "name": {"type": "string", "required": True},
                },
            },
            "LeaveBalance": {
                "layer": "hr", "description": "A balance.", "ownership": "user",
                "volume": "low", "sensitivity": "internal",
                "platforms": {"dataverse": "custom"}, "primary": "name", "keys": [],
                "attributes": {
                    "id": {"type": "guid", "required": True, "trait": "base",
                           "physical": {"dataverse": "<table>id (native primary key)"}},
                    "employee": {"type": "lookup", "target": "Employee", "required": True},
                    "leaveType": {"type": "string", "required": True},
                    "periodStart": {"type": "date", "required": True},
                    "periodEnd": {"type": "date", "required": True},
                    "available": {"type": "decimal"},
                },
            },
            "Note": {
                "layer": "hr", "description": "A note.", "ownership": "user",
                "volume": "low", "sensitivity": "internal",
                "platforms": {"dataverse": "custom"}, "primary": "name", "keys": [],
                "attributes": {
                    "id": {"type": "guid", "required": True, "trait": "base",
                           "physical": {"dataverse": "<table>id (native primary key)"}},
                    "name": {"type": "string", "required": True},
                    "regarding": {"type": "polylookup", "required": True, "targets": ["*"]},
                },
            },
        },
        "enums": {"EmploymentStatus": {"values": [
            {"code": 1, "key": "active", "label": "Active"},
            {"code": 2, "key": "leaver", "label": "Leaver"},
            {"code": 9, "key": "old", "label": "Old", "deprecated": True}]}},
        "termSets": {},
        "seed": {"LeaveType": [{"code": "ANNUAL", "allowNegativeBalance": False},
                               {"code": "TOIL", "allowNegativeBalance": True}]},
    }


def validate(rows, model=None):
    v = demodata.Validator(model or tiny_model(), rows)
    v.run()
    return v


class TestValidatorColumns(unittest.TestCase):
    def test_a_good_row_passes(self):
        v = validate({
            "OrgUnit": [{"code": "AUD", "name": "Audit"}],
            "Employee": [{"code": "E1", "employeeNumber": "E1", "displayName": "A B",
                          "status": "active", "orgUnit": "AUD", "homeCity": "London"}],
        })
        self.assertEqual(v.errors, [])

    def test_an_unknown_column_is_an_error(self):
        v = validate({"OrgUnit": [{"code": "AUD", "name": "Audit", "colour": "blue"}]})
        self.assertTrue(any("'colour' is not a column" in e for e in v.errors))

    def test_a_missing_required_column_is_an_error(self):
        v = validate({"OrgUnit": [{"code": "AUD"}]})
        self.assertTrue(any("required column 'name'" in e for e in v.errors))

    def test_a_table_outside_the_profile_is_an_error(self):
        v = validate({"Unicorn": [{"code": "U"}]})
        self.assertTrue(any("not a table in this profile" in e for e in v.errors))

    def test_an_address_is_satisfied_by_its_parts(self):
        v = validate({
            "OrgUnit": [{"code": "AUD", "name": "Audit"}],
            "Employee": [{"code": "E1", "employeeNumber": "E1", "displayName": "A B",
                          "status": "active", "orgUnit": "AUD", "homeLine1": "1 Road"}],
        })
        self.assertEqual(v.errors, [])

    def test_an_address_with_no_parts_at_all_is_an_error(self):
        v = validate({
            "OrgUnit": [{"code": "AUD", "name": "Audit"}],
            "Employee": [{"code": "E1", "employeeNumber": "E1", "displayName": "A B",
                          "status": "active", "orgUnit": "AUD"}],
        })
        self.assertTrue(any("required address 'home'" in e for e in v.errors))


class TestValidatorRealCodeColumn(unittest.TestCase):
    """`code` used to be skipped outright, whatever the entity.

    That was a blind spot rather than a live bug: every real `code` column in
    the schema today is a plain string, and the validator has nothing to say
    about one. The skip exists because demodata adds a synthetic `code` natural
    key to tables that have no such column. It is now conditional on the entity
    actually lacking the column, so the day a `code` becomes a choice or a
    lookup it is checked like anything else — which is what these tests pin.
    """

    @staticmethod
    def _model(code_spec):
        m = tiny_model()
        m["entities"]["LeaveType"] = {
            "layer": "hr", "description": "A leave type.", "ownership": "organization",
            "volume": "low", "sensitivity": "internal",
            "platforms": {"dataverse": "custom"}, "primary": "name", "keys": [],
            "attributes": {
                "id": {"type": "guid", "required": True, "trait": "base",
                       "physical": {"dataverse": "<table>id (native primary key)"}},
                "name": {"type": "string", "required": True},
                "code": code_spec,
            },
        }
        return m

    def test_a_bad_choice_value_in_a_code_column_is_caught(self):
        """With the old unconditional skip this passed validation silently."""
        model = self._model({"type": "choice", "enum": "EmploymentStatus",
                             "required": True})
        v = validate({"LeaveType": [{"code": "nonsense", "name": "Annual"}]}, model)
        self.assertTrue(any("nonsense" in e and "EmploymentStatus" in e
                            for e in v.errors), v.errors)

    def test_a_good_choice_value_in_a_code_column_passes(self):
        model = self._model({"type": "choice", "enum": "EmploymentStatus",
                             "required": True})
        v = validate({"LeaveType": [{"code": "active", "name": "Annual"}]}, model)
        self.assertEqual(v.errors, [])

    def test_an_unresolvable_lookup_in_a_code_column_is_caught(self):
        model = self._model({"type": "lookup", "target": "Currency"})
        model["seed"] = {"Currency": [{"code": "GBP"}]}
        v = validate({"LeaveType": [{"code": "ZZZ", "name": "Annual"}]}, model)
        self.assertTrue(any("ZZZ" in e for e in v.errors), v.errors)

    def test_a_synthetic_code_on_a_table_without_one_is_still_ignored(self):
        """The reason the skip exists: demodata's own natural key."""
        v = validate({"OrgUnit": [{"code": "AUD", "name": "Audit"}]})
        self.assertFalse(any("'code' is not a column" in e for e in v.errors))

    def test_every_real_code_column_in_the_schema_is_a_plain_string(self):
        """Which is why the old skip never suppressed a real error.

        If this ever fails, a code column has gained a type the validator can
        check, and the conditional skip above is what makes that work.
        """
        import glob
        for path in glob.glob(str(ROOT / "build" / "*.model.json")):
            with open(path, encoding="utf-8") as handle:
                entities = json.load(handle)["entities"]
            for name, spec in entities.items():
                column = spec["attributes"].get("code")
                if column:
                    self.assertEqual(column["type"], "string",
                                     f"{Path(path).stem}/{name}")


class TestValidatorChoices(unittest.TestCase):
    def _employee(self, status):
        return {
            "OrgUnit": [{"code": "AUD", "name": "Audit"}],
            "Employee": [{"code": "E1", "employeeNumber": "E1", "displayName": "A B",
                          "status": status, "orgUnit": "AUD", "homeCity": "London"}],
        }

    def test_a_valid_enum_key_passes(self):
        self.assertEqual(validate(self._employee("active")).errors, [])

    def test_an_invalid_enum_key_is_an_error(self):
        v = validate(self._employee("employed"))
        self.assertTrue(any("'employed' is not a value of EmploymentStatus" in e
                            for e in v.errors))

    def test_a_deprecated_enum_key_is_rejected(self):
        v = validate(self._employee("old"))
        self.assertTrue(any("is not a value of" in e for e in v.errors))


class TestValidatorLookups(unittest.TestCase):
    def test_a_lookup_to_a_generated_row_resolves(self):
        v = validate({
            "OrgUnit": [{"code": "AUD", "name": "Audit"}],
            "Employee": [{"code": "E1", "employeeNumber": "E1", "displayName": "A B",
                          "status": "active", "orgUnit": "AUD", "homeCity": "X"}],
        })
        self.assertEqual(v.errors, [])

    def test_a_lookup_to_a_row_that_does_not_exist_is_an_error(self):
        v = validate({
            "OrgUnit": [{"code": "AUD", "name": "Audit"}],
            "Employee": [{"code": "E1", "employeeNumber": "E1", "displayName": "A B",
                          "status": "active", "orgUnit": "GHOST", "homeCity": "X"}],
        })
        self.assertTrue(any("'GHOST' is not a OrgUnit that exists" in e for e in v.errors))

    def test_a_lookup_to_an_empty_table_warns_rather_than_fails(self):
        v = validate({
            "Employee": [{"code": "E1", "employeeNumber": "E1", "displayName": "A B",
                          "status": "active", "orgUnit": "AUD", "homeCity": "X"}],
        })
        self.assertTrue(any("load its reference data first" in w for w in v.warnings))

    def test_schema_seed_rows_count_as_existing(self):
        """A lookup may point at reference data the schema seeds, not just generated rows."""
        v = validate({"LeaveBalance": [
            {"code": "LB1", "employee": "E1", "leaveType": "ANNUAL",
             "periodStart": "2026-04-01", "periodEnd": "2027-03-31"}],
            "Employee": [{"code": "E1", "employeeNumber": "E1", "displayName": "A",
                          "status": "active", "orgUnit": "AUD", "homeCity": "X"}],
            "OrgUnit": [{"code": "AUD", "name": "Audit"}]})
        self.assertEqual(v.errors, [])


class TestValidatorPolylookups(unittest.TestCase):
    def test_a_qualified_reference_resolves(self):
        v = validate({
            "OrgUnit": [{"code": "AUD", "name": "Audit"}],
            "Note": [{"code": "N1", "name": "A note",
                      "regarding": {"table": "OrgUnit", "key": "AUD"}}],
        })
        self.assertEqual(v.errors, [])

    def test_a_bare_string_is_an_error(self):
        v = validate({"Note": [{"code": "N1", "name": "A note", "regarding": "AUD"}]})
        self.assertTrue(any("multi-table lookup and must be written" in e for e in v.errors))

    def test_a_reference_to_a_row_that_does_not_exist_is_an_error(self):
        v = validate({
            "OrgUnit": [{"code": "AUD", "name": "Audit"}],
            "Note": [{"code": "N1", "name": "A note",
                      "regarding": {"table": "OrgUnit", "key": "GHOST"}}],
        })
        self.assertTrue(any("OrgUnit/GHOST does not exist" in e for e in v.errors))

    def test_a_reference_to_a_table_that_does_not_exist_is_an_error(self):
        v = validate({"Note": [{"code": "N1", "name": "A note",
                                "regarding": {"table": "Unicorn", "key": "U"}}]})
        self.assertTrue(any("not a table" in e for e in v.errors))


class TestValidatorInvariants(unittest.TestCase):
    def test_an_overdrawn_balance_is_an_error_when_the_type_forbids_it(self):
        v = validate({"LeaveBalance": [
            {"code": "LB1", "employee": "E1", "leaveType": "ANNUAL",
             "periodStart": "2026-04-01", "periodEnd": "2027-03-31", "available": -4.0}]})
        self.assertTrue(any("overdrawn" in e for e in v.errors))

    def test_an_overdrawn_balance_is_allowed_when_the_type_permits_it(self):
        v = validate({"LeaveBalance": [
            {"code": "LB1", "employee": "E1", "leaveType": "TOIL",
             "periodStart": "2026-04-01", "periodEnd": "2027-03-31", "available": -4.0}]})
        self.assertFalse(any("overdrawn" in e for e in v.errors))


class TestFirmFiles(unittest.TestCase):
    """The firm is data now, so the files themselves need checking."""

    SLUGS = ("woodgrove", "fabrikam", "adatum", "contoso", "northwind")

    def test_every_firm_file_loads(self):
        for slug in self.SLUGS:
            config = demodata.load_firm(slug)
            self.assertTrue(config.name, slug)
            self.assertTrue(config.profile, slug)
            self.assertTrue(config.story, slug)

    def test_every_firm_is_mapped_from_a_profile(self):
        mapped = set(demodata.PROFILE_FIRMS.values())
        for slug in self.SLUGS:
            self.assertIn(slug, mapped, slug)

    def test_each_firms_profile_is_the_one_it_is_mapped_from(self):
        for profile, slug in demodata.PROFILE_FIRMS.items():
            config = demodata.load_firm(slug)
            if not config.profile or profile.endswith("-starter"):
                continue
            self.assertEqual(config.profile, profile, slug)

    def test_an_unknown_firm_is_reported_with_the_alternatives(self):
        with self.assertRaises(demodata.DataError) as ctx:
            demodata.load_firm("acme")
        self.assertIn("available:", str(ctx.exception))

    def test_shares_add_up_to_one(self):
        for slug in self.SLUGS:
            config = demodata.load_firm(slug)
            for name, rows in (("offices", config.offices),
                               ("departments", config.departments),
                               ("ladder", config.ladder)):
                total = sum(float(r["share"]) for r in rows)
                self.assertAlmostEqual(total, 1.0, delta=0.05, msg=f"{slug}/{name}")

    def test_credential_grades_and_departments_exist_in_that_firm(self):
        for slug in self.SLUGS:
            config = demodata.load_firm(slug)
            grades = {r["grade"] for r in config.ladder}
            departments = {r["code"] for r in config.departments}
            for credential in config.credentials:
                for grade in credential["grades"]:
                    self.assertIn(grade, grades, f"{slug}/{credential['code']}")
                for department in credential["departments"]:
                    self.assertIn(department, departments,
                                  f"{slug}/{credential['code']}")

    def test_every_firm_has_a_mandatory_course(self):
        """Otherwise the Mandatory Training Overdue tile can only read zero."""
        for slug in self.SLUGS:
            config = demodata.load_firm(slug)
            self.assertTrue([c for c in config.courses if c.get("mandatory")], slug)

    def test_leave_entitlement_covers_every_type_in_the_mix(self):
        for slug in self.SLUGS:
            config = demodata.load_firm(slug)
            entitlement = config.leave.get("entitlement") or {}
            for item in config.leave["mix"]:
                self.assertIn(item["code"], entitlement, f"{slug}/{item['code']}")

    def test_a_firm_file_missing_a_section_is_rejected(self):
        import tempfile
        import yaml as yaml_module
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.yaml"
            path.write_text(yaml_module.safe_dump({"firm": {"name": "X"}}))
            with mock.patch.object(demodata, "FIRMS_DIR", Path(tmp)):
                with self.assertRaises(demodata.DataError) as ctx:
                    demodata.load_firm("broken")
        self.assertIn("missing", str(ctx.exception))

    def test_shares_that_do_not_add_up_are_rejected(self):
        import tempfile
        import yaml as yaml_module
        good = yaml_module.safe_load(
            (ROOT / "data" / "firms" / "woodgrove.yaml").read_text(encoding="utf-8"))
        good["offices"][0]["share"] = 0.1
        good["offices"][1]["share"] = 0.1
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skewed.yaml"
            path.write_text(yaml_module.safe_dump(good))
            with mock.patch.object(demodata, "FIRMS_DIR", Path(tmp)):
                with self.assertRaises(demodata.DataError) as ctx:
                    demodata.load_firm("skewed")
        self.assertIn("add up to", str(ctx.exception))


class TestEveryFirmGenerates(unittest.TestCase):
    """Each fictional firm must produce a valid, differentiated dataset."""

    CASES = (("woodgrove", "accounting-firm"), ("fabrikam", "law-firm"),
             ("adatum", "architecture-firm"), ("contoso", "tech-company"),
             ("northwind", "general-firm"))

    def _build(self, slug, profile, people=40):
        model_file = ROOT / "build" / f"{profile}.model.json"
        if not model_file.exists():
            self.skipTest(f"run: python tools/schematool.py export --profile {profile}")
        model = json.loads(model_file.read_text(encoding="utf-8"))
        firm = demodata.Firm(model, demodata.load_firm(slug), people, 7)
        firm.build()
        validator = demodata.Validator(model, firm.rows)
        validator.run()
        return firm, validator

    def test_every_firm_validates(self):
        for slug, profile in self.CASES:
            firm, validator = self._build(slug, profile)
            self.assertEqual(firm.errors + validator.errors, [], slug)

    def test_each_firm_uses_its_own_name_and_departments(self):
        for slug, profile in self.CASES:
            firm, _ = self._build(slug, profile)
            config = demodata.load_firm(slug)
            self.assertEqual(firm.rows["LegalEntity"][0]["name"], config.name)
            codes = {u["code"] for u in firm.rows["OrgUnit"]} - {"FIRM"}
            self.assertEqual(codes, {d["code"] for d in config.departments}, slug)

    def test_the_leave_year_follows_the_firm(self):
        for slug, profile in self.CASES:
            firm, _ = self._build(slug, profile)
            month = int(demodata.load_firm(slug).leave["yearStartMonth"])
            for balance in firm.rows["LeaveBalance"]:
                self.assertEqual(int(balance["periodStart"][5:7]), month, slug)

    def test_the_annual_entitlement_follows_the_firm(self):
        for slug, profile in self.CASES:
            firm, _ = self._build(slug, profile)
            expected = float(demodata.load_firm(slug).leave["entitlement"]["ANNUAL"])
            self.assertEqual(max(b["entitlement"] for b in firm.rows["LeaveBalance"]),
                             expected, slug)

    def test_only_a_partnership_has_partners(self):
        for slug, profile in self.CASES:
            firm, _ = self._build(slug, profile)
            config = demodata.load_firm(slug)
            types = {r["workerType"] for r in firm.rows["Employee"]}
            if config.top_worker_type == "partner":
                self.assertIn("partner", types, slug)
            else:
                self.assertNotIn("partner", types, slug)

    def test_each_firm_seeds_its_own_credential_types(self):
        for slug, profile in self.CASES:
            firm, _ = self._build(slug, profile)
            config = demodata.load_firm(slug)
            seeded = {r["code"] for r in firm.rows.get("CredentialType", [])}
            self.assertEqual(seeded, {c["code"] for c in config.credentials}, slug)

    def test_every_email_domain_is_reserved_for_documentation(self):
        """A thousand demo addresses must not point at a domain someone owns.

        RFC 2606 reserves example.com and the .example, .test and .invalid
        top-level domains for exactly this. An invented .co.uk may belong to a
        real company, and if anyone ever sends from this data it reaches them.
        """
        reserved_tlds = (".example", ".test", ".invalid")
        for slug, profile in self.CASES:
            firm, _ = self._build(slug, profile)
            domains = set()
            for rows in firm.rows.values():
                for row in rows:
                    for value in row.values():
                        if isinstance(value, str) and "@" in value and "." in value:
                            domains.add(value.rsplit("@", 1)[1].lower())
            for domain in domains:
                self.assertTrue(
                    domain == "example.com" or domain.endswith(reserved_tlds),
                    f"{slug}: {domain} is not reserved for documentation")

    def test_every_firms_own_domain_is_reserved(self):
        reserved_tlds = (".example", ".test", ".invalid")
        for slug in self.SLUGS if hasattr(self, "SLUGS") else [c[0] for c in self.CASES]:
            domain = demodata.load_firm(slug).firm["domain"].lower()
            self.assertTrue(domain == "example.com" or domain.endswith(reserved_tlds),
                            f"{slug}: {domain}")

    def test_the_approval_group_step_carries_a_group(self):
        """Without one the engine has nobody to ask and the run stalls."""
        for slug, profile in self.CASES:
            firm, _ = self._build(slug, profile)
            group_steps = [s for s in firm.rows["ApprovalPolicyStep"]
                           if s["approverType"] == "group"]
            self.assertTrue(group_steps, slug)
            for step in group_steps:
                self.assertTrue(step.get("approverGroupId"), f"{slug}/{step['code']}")


class TestLoadOrder(unittest.TestCase):
    def test_a_lookup_target_is_ordered_before_its_referrer(self):
        model = tiny_model()
        rows = {"Employee": [], "OrgUnit": [], "LeaveBalance": []}
        order = demodata.load_order(model, rows)
        self.assertLess(order.index("OrgUnit"), order.index("Employee"))
        self.assertLess(order.index("Employee"), order.index("LeaveBalance"))

    def test_a_cycle_is_broken_rather_than_hanging(self):
        model = tiny_model()
        model["entities"]["OrgUnit"]["attributes"]["head"] = {
            "type": "lookup", "target": "Employee"}
        order = demodata.load_order(model, {"Employee": [], "OrgUnit": []})
        self.assertEqual(sorted(order), ["Employee", "OrgUnit"])


class TestFirmShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not MODEL_FILE.exists():
            raise unittest.SkipTest("run: python tools/schematool.py export --profile hr-starter")
        cls.model = json.loads(MODEL_FILE.read_text(encoding="utf-8"))

    def _firm(self, people=24, seed=1, slug="woodgrove"):
        firm = demodata.Firm(self.model, demodata.load_firm(slug), people, seed)
        firm.build()
        return firm

    def test_the_whole_firm_validates(self):
        firm = self._firm()
        v = demodata.Validator(self.model, firm.rows)
        v.run()
        self.assertEqual(firm.errors + v.errors, [])

    def test_the_same_seed_gives_the_same_firm(self):
        a, b = self._firm(seed=42), self._firm(seed=42)
        self.assertEqual([r["displayName"] for r in a.rows["Employee"]],
                         [r["displayName"] for r in b.rows["Employee"]])

    def test_a_different_seed_gives_a_different_firm(self):
        a, b = self._firm(seed=1), self._firm(seed=2)
        self.assertNotEqual([r["displayName"] for r in a.rows["Employee"]],
                            [r["displayName"] for r in b.rows["Employee"]])

    def test_work_emails_are_unique(self):
        emails = [r["workEmail"] for r in self._firm(people=64).rows["Employee"]]
        self.assertEqual(len(emails), len(set(emails)))

    def test_exactly_one_current_assignment_per_active_person(self):
        firm = self._firm()
        leavers = {r["employee"] for r in firm.rows["Employment"] if r.get("endDate")}
        current = [r for r in firm.rows["JobAssignment"] if r["validTo"] is None]
        counts = {}
        for row in current:
            counts[row["employee"]] = counts.get(row["employee"], 0) + 1
        for row in firm.rows["Employee"]:
            number = row["employeeNumber"]
            self.assertEqual(counts.get(number, 0), 0 if number in leavers else 1, number)

    def test_assignment_history_is_contiguous(self):
        """Each closed assignment ends where the next begins."""
        firm = self._firm()
        by_person = {}
        for row in firm.rows["JobAssignment"]:
            by_person.setdefault(row["employee"], []).append(row)
        for number, rows in by_person.items():
            rows.sort(key=lambda r: r["validFrom"])
            for earlier, later in zip(rows, rows[1:]):
                self.assertEqual(earlier["validTo"], later["validFrom"], number)

    def test_nobody_is_overdrawn_on_annual_leave(self):
        for row in self._firm(people=64).rows["LeaveBalance"]:
            self.assertGreaterEqual(row["available"], 0, row["code"])

    def test_the_two_step_leave_policy_is_actually_used(self):
        """It was seeded and unreachable: leave capped at 10 days, policy needed 11."""
        firm = self._firm(people=64)
        policies = {r.get("policy") for r in firm.rows["ApprovalRequest"]}
        self.assertIn("POL-LEAVE-LONG", policies)
        self.assertIn("POL-LEAVE-ALL", policies)

    def test_some_leave_is_longer_than_the_two_step_threshold(self):
        durations = [r["duration"] for r in self._firm(people=64).rows["LeaveRequest"]]
        self.assertGreater(max(durations), demodata.LONG_LEAVE_DAYS)

    def test_an_approval_step_is_never_assigned_to_the_requester(self):
        firm = self._firm(people=64)
        requesters = {r["code"]: r["requester"] for r in firm.rows["ApprovalRequest"]}
        for step in firm.rows["ApprovalStep"]:
            self.assertNotEqual(step["approver"], requesters[step["request"]], step["code"])

    def test_decided_steps_are_decided_after_they_are_assigned(self):
        for step in self._firm(people=64).rows["ApprovalStep"]:
            if step.get("decidedOn"):
                self.assertGreater(step["decidedOn"], step["assignedOn"], step["code"])

    def test_the_credential_watchlist_is_never_empty(self):
        """Two compliance tiles read zero if it is, whatever the seed."""
        for seed in (1, 2, 3, 99):
            firm = self._firm(people=48, seed=seed)
            rows = firm.rows["EmployeeCredential"]
            expiring = [r for r in rows if r["status"] == "active"
                        and demodata.TODAY <= dt.date.fromisoformat(r["expiresOn"])
                        <= demodata.TODAY + dt.timedelta(days=90)]
            expired = [r for r in rows if
                       dt.date.fromisoformat(r["expiresOn"]) < demodata.TODAY]
            self.assertGreaterEqual(len(expiring), 4, f"seed {seed}")
            self.assertGreaterEqual(len(expired), 2, f"seed {seed}")

    def test_some_mandatory_training_is_overdue(self):
        firm = self._firm(people=64)
        mandatory = {r["code"] for r in firm.rows["TrainingCourse"] if r.get("isMandatory")}
        overdue = [
            r for r in firm.rows["TrainingRecord"]
            if r["course"] in mandatory and r["status"] != "completed"
            and r.get("dueDate") and dt.date.fromisoformat(r["dueDate"]) < demodata.TODAY
        ]
        self.assertGreater(len(overdue), 0)

    def test_every_department_has_a_head(self):
        firm = self._firm(people=64)
        for unit in firm.rows["OrgUnit"]:
            if unit["code"] == "FIRM":
                continue
            self.assertIn("head", unit, unit["code"])

    def test_leavers_have_an_employment_end_date_and_a_reason(self):
        for row in self._firm(people=64).rows["Employment"]:
            if row.get("endDate"):
                self.assertIsNotNone(row.get("terminationType"), row["code"])

    def test_compensation_covers_every_assignment(self):
        firm = self._firm()
        self.assertEqual(len(firm.rows["Compensation"]), len(firm.rows["JobAssignment"]))

    def test_a_tiny_firm_is_rejected(self):
        self.assertEqual(demodata.main(["--profile", "hr-starter", "--people", "3"]), 2)


if __name__ == "__main__":
    unittest.main()
