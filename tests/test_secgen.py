"""Tests for tools/secgen.py. Run with: python -m unittest discover -s tests"""
import json
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import secgen  # noqa: E402


def entity(sensitivity="internal", ownership="user", dataverse="custom", attributes=None):
    return {
        "layer": "hr",
        "description": "A table.",
        "ownership": ownership,
        "volume": "low",
        "sensitivity": sensitivity,
        "platforms": {"dataverse": dataverse},
        "primary": "name",
        "keys": [],
        "attributes": attributes or {"name": {"type": "string", "required": True}},
    }


def model(entities):
    return {"profile": "t", "layers": {"hr": "1.0.0"}, "entities": entities,
            "enums": {}, "termSets": {}, "terminology": {}, "seed": {}}


class TestGrants(unittest.TestCase):
    def test_privileges_map_to_dataverse_names_and_depths(self):
        spec = {"roles": [{"code": "e", "name": "E", "rules": [
            {"tables": ["LeaveRequest"], "privileges": ["create", "read"], "depth": "user"}]}]}
        rs = secgen.RoleSet(spec, model({"LeaveRequest": entity()}), "wga")
        names = {p["PrivilegeName"]: p["Depth"] for p in rs.payloads()[0]["addPrivileges"]["Privileges"]}
        self.assertEqual(names, {"prvCreatewga_leaverequest": "Basic",
                                 "prvReadwga_leaverequest": "Basic"})

    def test_all_four_depths_translate(self):
        pairs = [("user", "Basic"), ("businessUnit", "Local"),
                 ("parentChild", "Deep"), ("organization", "Global")]
        for ours, theirs in pairs:
            spec = {"roles": [{"code": "e", "name": "E", "rules": [
                {"tables": ["T"], "privileges": ["read"], "depth": ours}]}]}
            rs = secgen.RoleSet(spec, model({"T": entity()}), "wga")
            self.assertEqual(rs.payloads()[0]["addPrivileges"]["Privileges"][0]["Depth"], theirs)

    def test_a_later_rule_widens_but_never_narrows(self):
        spec = {"roles": [{"code": "e", "name": "E", "rules": [
            {"tables": ["T"], "privileges": ["read"], "depth": "businessUnit"},
            {"tables": ["T"], "privileges": ["read"], "depth": "user"}]}]}
        rs = secgen.RoleSet(spec, model({"T": entity()}), "wga")
        self.assertEqual(rs.roles["e"]["grants"]["T"]["read"], "businessUnit")

    def test_organization_owned_table_is_raised_to_global(self):
        spec = {"roles": [{"code": "e", "name": "E", "rules": [
            {"tables": ["Employee"], "privileges": ["read"], "depth": "user"}]}]}
        rs = secgen.RoleSet(spec, model({"Employee": entity(ownership="organization")}), "wga")
        self.assertEqual(rs.roles["e"]["grants"]["Employee"]["read"], "organization")
        self.assertTrue(any("organization-owned" in n for n in rs.notes))

    def test_standard_table_uses_its_own_privilege_name(self):
        spec = {"roles": [{"code": "e", "name": "E", "rules": [
            {"tables": ["Account"], "privileges": ["read"], "depth": "organization"}]}]}
        rs = secgen.RoleSet(spec, model({"Account": entity(dataverse="standard:account")}), "wga")
        privileges = rs.payloads()[0]["addPrivileges"]["Privileges"]
        self.assertEqual(privileges[0]["PrivilegeName"], "prvReadaccount")
        self.assertEqual(rs.warnings, [])

    def test_native_table_is_reported_not_emitted(self):
        spec = {"roles": [{"code": "e", "name": "E", "rules": [
            {"tables": ["AuditEvent"], "privileges": ["read"], "depth": "organization"}]}]}
        rs = secgen.RoleSet(spec, model({"AuditEvent": entity(dataverse="native")}), "wga")
        self.assertNotIn("AuditEvent", rs.roles["e"]["grants"])
        self.assertTrue(any("by hand" in w for w in rs.warnings))


class TestValidation(unittest.TestCase):
    def test_unknown_table_is_an_error(self):
        spec = {"roles": [{"code": "e", "name": "E", "rules": [
            {"tables": ["Nope"], "privileges": ["read"], "depth": "user"}]}]}
        rs = secgen.RoleSet(spec, model({"T": entity()}), "wga")
        self.assertTrue(any("not in this profile" in e for e in rs.errors))

    def test_unknown_depth_is_an_error(self):
        spec = {"roles": [{"code": "e", "name": "E", "rules": [
            {"tables": ["T"], "privileges": ["read"], "depth": "cosmic"}]}]}
        rs = secgen.RoleSet(spec, model({"T": entity()}), "wga")
        self.assertTrue(any("depth 'cosmic'" in e for e in rs.errors))

    def test_unknown_privilege_is_an_error(self):
        spec = {"roles": [{"code": "e", "name": "E", "rules": [
            {"tables": ["T"], "privileges": ["obliterate"], "depth": "user"}]}]}
        rs = secgen.RoleSet(spec, model({"T": entity()}), "wga")
        self.assertTrue(any("obliterate" in e for e in rs.errors))

    def test_granting_and_denying_the_same_table_is_an_error(self):
        spec = {"roles": [{"code": "e", "name": "E",
                           "rules": [{"tables": ["T"], "privileges": ["read"], "depth": "user"}],
                           "denied": {"tables": ["T"], "why": "x"}}]}
        rs = secgen.RoleSet(spec, model({"T": entity()}), "wga")
        self.assertTrue(any("both granted and denied" in e for e in rs.errors))

    def test_unreachable_table_is_a_warning(self):
        spec = {"roles": [{"code": "e", "name": "E", "rules": [
            {"tables": ["Seen"], "privileges": ["read"], "depth": "user"}]}]}
        rs = secgen.RoleSet(spec, model({"Seen": entity(), "Forgotten": entity()}), "wga")
        self.assertTrue(any("no role can reach 'Forgotten'" in w for w in rs.warnings))

    def test_out_of_scope_table_is_a_note_not_a_warning(self):
        spec = {"outOfScope": [{"table": "Later", "why": "Phase 2 owns it."}],
                "roles": [{"code": "e", "name": "E", "rules": [
                    {"tables": ["Seen"], "privileges": ["read"], "depth": "user"}]}]}
        rs = secgen.RoleSet(spec, model({"Seen": entity(), "Later": entity()}), "wga")
        self.assertFalse(any("Later" in w for w in rs.warnings))
        self.assertTrue(any("Later is out of scope" in n for n in rs.notes))

    def test_a_table_both_out_of_scope_and_granted_is_an_error(self):
        """The roles file contradicting itself. Travel declared Currency out of
        scope and then granted it to two of three roles, so the matrix said the
        travel desk could not read a currency every traveller could."""
        spec = {"outOfScope": [{"table": "Cur", "why": "Standard table."}],
                "roles": [{"code": "e", "name": "E", "rules": [
                    {"tables": ["Cur"], "privileges": ["read"], "depth": "user"}]}]}
        rs = secgen.RoleSet(spec, model({"Cur": entity()}), "wga")
        self.assertTrue(any("declared outOfScope but granted by e" in e
                            for e in rs.errors), rs.errors)

    def test_an_out_of_scope_table_granted_by_nobody_stays_clean(self):
        spec = {"outOfScope": [{"table": "Cur", "why": "Standard table."}],
                "roles": [{"code": "e", "name": "E", "rules": [
                    {"tables": ["T"], "privileges": ["read"], "depth": "user"}]}]}
        rs = secgen.RoleSet(spec, model({"T": entity(), "Cur": entity()}), "wga")
        self.assertEqual(rs.errors, [])

    def test_out_of_scope_table_not_in_profile_is_an_error(self):
        spec = {"outOfScope": [{"table": "Ghost", "why": "x"}], "roles": []}
        rs = secgen.RoleSet(spec, model({"T": entity()}), "wga")
        self.assertTrue(any("Ghost" in e for e in rs.errors))

    def test_inheriting_an_unknown_role_is_an_error(self):
        spec = {"roles": [{"code": "m", "name": "M", "inherits": "ghost", "rules": []}]}
        rs = secgen.RoleSet(spec, model({"T": entity()}), "wga")
        self.assertTrue(any("unknown role 'ghost'" in e for e in rs.errors))


class TestInheritance(unittest.TestCase):
    SPEC = {
        "roles": [
            {"code": "employee", "name": "Employee", "rules": [
                {"tables": ["LeaveRequest"], "privileges": ["create", "read"], "depth": "user"},
                {"tables": ["Compensation"], "privileges": ["read"], "depth": "user"}]},
            {"code": "manager", "name": "Manager", "inherits": "employee", "rules": [
                {"tables": ["LeaveRequest"], "privileges": ["read", "write"],
                 "depth": "businessUnit"}],
             "denied": {"tables": ["Compensation"], "why": "no team pay"}},
        ]
    }
    MODEL = model({"LeaveRequest": entity(), "Compensation": entity(sensitivity="restricted")})

    def test_child_keeps_what_the_parent_granted(self):
        rs = secgen.RoleSet(self.SPEC, self.MODEL, "wga")
        self.assertIn("create", rs.roles["manager"]["effectiveGrants"]["LeaveRequest"])

    def test_child_widens_the_parents_depth(self):
        rs = secgen.RoleSet(self.SPEC, self.MODEL, "wga")
        grants = rs.roles["manager"]["effectiveGrants"]["LeaveRequest"]
        self.assertEqual(grants["read"], "businessUnit")
        self.assertEqual(grants["write"], "businessUnit")
        self.assertEqual(grants["create"], "user")

    def test_denial_in_the_child_keeps_only_the_parents_own_row_access(self):
        """A manager must still see their own pay, but never their team's."""
        rs = secgen.RoleSet(self.SPEC, self.MODEL, "wga")
        grants = rs.roles["manager"]["effectiveGrants"]["Compensation"]
        self.assertEqual(grants, {"read": "user"})
        self.assertTrue(any("denies Compensation" in n for n in rs.notes))

    def test_denial_of_a_table_the_parent_never_had_removes_it(self):
        spec = {"roles": [
            {"code": "employee", "name": "E", "rules": []},
            {"code": "manager", "name": "M", "inherits": "employee", "rules": [],
             "denied": {"tables": ["Compensation"], "why": "x"}},
        ]}
        rs = secgen.RoleSet(spec, self.MODEL, "wga")
        self.assertNotIn("Compensation", rs.roles["manager"]["effectiveGrants"])


class TestColumnProfiles(unittest.TestCase):
    MODEL = model({
        "Employee": entity(attributes={
            "name": {"type": "string", "required": True},
            "mobilePhone": {"type": "phone", "pii": True}}),
        "Compensation": entity(sensitivity="restricted", attributes={
            "name": {"type": "string", "required": True},
            "amount": {"type": "money", "pii": True}}),
    })

    def test_uncovered_strict_column_is_an_error(self):
        spec = {"columnProfiles": [], "roles": [{"code": "hr", "name": "HR", "rules": [
            {"tables": ["Employee", "Compensation"], "privileges": ["read"],
             "depth": "organization"}]}]}
        rs = secgen.RoleSet(spec, self.MODEL, "wga")
        self.assertTrue(any("Employee.mobilePhone" in e for e in rs.errors))

    def test_a_restricted_table_needs_no_column_profile(self):
        """Table-level roles protect it; column security would be redundant."""
        spec = {"columnProfiles": [{"name": "P", "columns": ["Employee.mobilePhone"],
                                    "readFor": ["hr"]}],
                "roles": [{"code": "hr", "name": "HR", "rules": [
                    {"tables": ["Employee", "Compensation"], "privileges": ["read"],
                     "depth": "organization"}]}]}
        rs = secgen.RoleSet(spec, self.MODEL, "wga")
        self.assertEqual(rs.errors, [])
        self.assertFalse(any("Compensation.amount" in w for w in rs.warnings))

    def test_profile_covering_an_unclassified_column_is_a_warning(self):
        spec = {"columnProfiles": [{"name": "P",
                                    "columns": ["Employee.mobilePhone", "Employee.name"],
                                    "readFor": ["hr"]}],
                "roles": [{"code": "hr", "name": "HR", "rules": [
                    {"tables": ["Employee", "Compensation"], "privileges": ["read"],
                     "depth": "organization"}]}]}
        rs = secgen.RoleSet(spec, self.MODEL, "wga")
        self.assertTrue(any("Employee.name" in w for w in rs.warnings))

    def test_unknown_column_in_a_profile_is_an_error(self):
        spec = {"columnProfiles": [{"name": "P", "columns": ["Employee.ghost"],
                                    "readFor": ["hr"]}],
                "roles": [{"code": "hr", "name": "HR", "rules": []}]}
        rs = secgen.RoleSet(spec, self.MODEL, "wga")
        self.assertTrue(any("Employee.ghost" in e for e in rs.errors))

    def test_unknown_role_in_a_profile_is_an_error(self):
        spec = {"columnProfiles": [{"name": "P", "columns": ["Employee.mobilePhone"],
                                    "readFor": ["nobody"]}],
                "roles": [{"code": "hr", "name": "HR", "rules": []}]}
        rs = secgen.RoleSet(spec, self.MODEL, "wga")
        self.assertTrue(any("unknown role 'nobody'" in e for e in rs.errors))


class TestMatrix(unittest.TestCase):
    def test_matrix_shows_scope_and_letters(self):
        spec = {"roles": [{"code": "e", "name": "Employee", "rules": [
            {"tables": ["LeaveRequest"], "privileges": ["create", "read", "write", "delete"],
             "depth": "user"}]}]}
        rs = secgen.RoleSet(spec, model({"LeaveRequest": entity()}), "wga")
        text = rs.matrix()
        self.assertIn("| LeaveRequest |", text)
        self.assertIn("CRUD (own)", text)

    def test_no_access_shows_a_dash(self):
        spec = {"roles": [{"code": "e", "name": "Employee", "rules": []}]}
        rs = secgen.RoleSet(spec, model({"Offer": entity(sensitivity="restricted")}), "wga")
        self.assertRegex(rs.matrix(), r"\| Offer \| restricted \| — \|")

    def test_a_cell_with_two_depths_shows_both(self):
        """A role that reads its unit but creates only its own rows rendered as
        "CRUD (unit)" — a claim the role does not make. The matrix is what a
        reviewer approves, so overstating a privilege in it is as bad as
        granting one."""
        spec = {"roles": [{"code": "m", "name": "M", "rules": [
            {"tables": ["T"], "privileges": ["create", "delete"], "depth": "user"},
            {"tables": ["T"], "privileges": ["read", "write"],
             "depth": "businessUnit"}]}]}
        rs = secgen.RoleSet(spec, model({"T": entity()}), "wga")
        self.assertIn("| RU (unit) CD (own) |", rs.matrix())
        self.assertNotIn("CRUD (unit)", rs.matrix())

    def test_a_cell_at_one_depth_is_unchanged(self):
        spec = {"roles": [{"code": "m", "name": "M", "rules": [
            {"tables": ["T"], "privileges": ["create", "read", "write", "delete"],
             "depth": "businessUnit"}]}]}
        rs = secgen.RoleSet(spec, model({"T": entity()}), "wga")
        self.assertIn("| CRUD (unit) |", rs.matrix())

    def test_the_widest_group_is_listed_first(self):
        spec = {"roles": [{"code": "m", "name": "M", "rules": [
            {"tables": ["T"], "privileges": ["create"], "depth": "user"},
            {"tables": ["T"], "privileges": ["read"], "depth": "organization"},
            {"tables": ["T"], "privileges": ["write"], "depth": "businessUnit"}]}]}
        rs = secgen.RoleSet(spec, model({"T": entity()}), "wga")
        self.assertIn("| R (all) U (unit) C (own) |", rs.matrix())

    def test_out_of_scope_tables_are_left_out(self):
        spec = {"outOfScope": [{"table": "Account", "why": "Phase 2."}],
                "roles": [{"code": "e", "name": "E", "rules": []}]}
        rs = secgen.RoleSet(spec, model({"Account": entity(dataverse="standard:account")}), "wga")
        self.assertNotIn("| Account |", rs.matrix())


class TestRealPhase1Roles(unittest.TestCase):
    """The committed roles file must be valid against the real HR model."""

    @classmethod
    def setUpClass(cls):
        model_file = ROOT / "build" / "hr-starter.model.json"
        if not model_file.exists():
            raise unittest.SkipTest("run: python tools/schematool.py export --profile hr-starter")
        cls.rs = secgen.RoleSet(
            yaml.safe_load((ROOT / "security" / "phase1-roles.yaml").read_text(encoding="utf-8")),
            json.loads(model_file.read_text(encoding="utf-8")),
            "wga",
        )

    def test_no_errors_and_no_warnings(self):
        self.assertEqual(self.rs.errors, [])
        self.assertEqual(self.rs.warnings, [])

    def test_three_personas(self):
        self.assertEqual(set(self.rs.roles), {"employee", "manager", "hrAdmin"})

    def test_manager_cannot_see_the_teams_compensation(self):
        grants = self.rs.roles["manager"]["effectiveGrants"]["Compensation"]
        self.assertEqual(grants, {"read": "user"})

    def test_manager_cannot_see_offers_or_pay_grades_at_all(self):
        for table in ("Offer", "Grade"):
            self.assertNotIn(table, self.rs.roles["manager"]["effectiveGrants"], table)

    def test_employee_cannot_see_the_recruitment_pipeline(self):
        for table in ("Candidate", "JobApplication", "Offer", "InterviewFeedback"):
            self.assertNotIn(table, self.rs.roles["employee"]["effectiveGrants"], table)

    def test_employee_can_run_their_own_leave_request_end_to_end(self):
        grants = self.rs.roles["employee"]["effectiveGrants"]["LeaveRequest"]
        for privilege in ("create", "read", "write", "delete"):
            self.assertEqual(grants.get(privilege), "user", privilege)

    def test_hr_admin_can_write_compensation(self):
        grants = self.rs.roles["hrAdmin"]["effectiveGrants"]["Compensation"]
        self.assertEqual(grants.get("write"), "parentChild")

    def test_hr_admin_cannot_change_the_org_structure(self):
        grants = self.rs.roles["hrAdmin"]["effectiveGrants"]["OrgUnit"]
        self.assertEqual(set(grants), {"read"})

    def test_every_role_has_a_rationale_for_what_it_grants(self):
        for code, role in self.rs.roles.items():
            self.assertTrue(role["rationale"], code)
            self.assertTrue(role["description"], code)


class TestRealTravelRoles(unittest.TestCase):
    """The travel roles file, which is also the SharePoint permission spec."""

    @classmethod
    def setUpClass(cls):
        model_file = ROOT / "build" / "travel-starter.model.json"
        if not model_file.exists():
            raise unittest.SkipTest(
                "run: python tools/schematool.py export --profile travel-starter")
        cls.rs = secgen.RoleSet(
            yaml.safe_load((ROOT / "security" / "travel-roles.yaml")
                           .read_text(encoding="utf-8")),
            json.loads(model_file.read_text(encoding="utf-8")),
            "nwd",
        )

    def test_no_errors_and_no_warnings(self):
        self.assertEqual(self.rs.errors, [])
        self.assertEqual(self.rs.warnings, [])

    def test_three_personas(self):
        self.assertEqual(set(self.rs.roles),
                         {"traveller", "approver", "travelAdmin"})

    def test_an_approver_sees_the_teams_trips_but_not_their_passports(self):
        """The Phase 4 boundary. An approver needs the team's trips and claims;
        a passport number is the travel desk's business and nobody else's."""
        approver = self.rs.roles["approver"]["effectiveGrants"]
        self.assertEqual(approver["TripRequest"]["read"], "businessUnit")
        for table in ("TravellerProfile", "TravelDocument", "LoyaltyMembership"):
            self.assertEqual(approver[table]["read"], "user", table)

    def test_the_desk_holds_the_restricted_tables(self):
        desk = self.rs.roles["travelAdmin"]["effectiveGrants"]
        for table in ("TravellerProfile", "TravelDocument"):
            self.assertEqual(desk[table]["read"], "parentChild", table)

    def test_a_traveller_runs_their_own_trip_and_claim_end_to_end(self):
        traveller = self.rs.roles["traveller"]["effectiveGrants"]
        for table in ("TripRequest", "ExpenseReport", "ExpenseLine"):
            for privilege in ("create", "read", "write", "delete"):
                self.assertEqual(traveller[table].get(privilege), "user",
                                 f"{table}.{privilege}")

    def test_a_traveller_cannot_edit_the_policy_or_the_rates(self):
        traveller = self.rs.roles["traveller"]["effectiveGrants"]
        for table in ("TravelPolicy", "PerDiemRate", "DestinationAdvisory"):
            self.assertEqual(set(traveller[table]), {"read"}, table)
        self.assertNotIn("CorporateRate", traveller)

    def test_every_role_has_a_rationale(self):
        for code, role in self.rs.roles.items():
            self.assertTrue(role["rationale"], code)
            self.assertTrue(role["description"], code)


class TestOutputDirectoryIsOwned(unittest.TestCase):
    def test_a_role_file_from_an_earlier_run_is_removed(self):
        """solgen packs whatever is in roles/, so a leftover file is a
        privilege grant nobody asked for — three HR roles once shipped in the
        travel solution this way."""
        import tempfile
        model_file = ROOT / "build" / "travel-starter.model.json"
        if not model_file.exists():
            self.skipTest("run: python tools/schematool.py export --profile travel-starter")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "security"
            (out / "roles").mkdir(parents=True)
            stale = out / "roles" / "hrAdmin.json"
            stale.write_text("{}", encoding="utf-8")
            code = secgen.main(["--profile", "travel-starter", "--prefix", "nwd",
                                "--roles", str(ROOT / "security" / "travel-roles.yaml"),
                                "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertFalse(stale.exists())
            self.assertEqual(
                sorted(p.stem for p in (out / "roles").glob("*.json")),
                ["approver", "travelAdmin", "traveller"])


if __name__ == "__main__":
    unittest.main()
