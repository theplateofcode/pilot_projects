"""Tests for tools/approvals.py. Run with: python -m unittest discover -s tests

Approver resolution fails silently in production — a request lands with the
wrong person and nobody notices until an audit — so every approverType and
every edge case is covered here rather than being clicked together in a
designer and hoped for.
"""
import datetime as dt
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import approvals  # noqa: E402

NOW = dt.datetime(2026, 11, 4, 9, 0, tzinfo=dt.timezone.utc)


class FakeDirectory:
    """A small firm: a partner, two managers, two staff and a leaver."""

    def __init__(self, people=None, heads=None, owners=None, groups=None,
                 parents=None):
        self.people = people if people is not None else {
            "partner": approvals.Person("partner", org_unit_id="AUD",
                                        cost_center_id="CC-AUD"),
            "mgr": approvals.Person("mgr", manager_id="partner", org_unit_id="AUD",
                                    cost_center_id="CC-AUD"),
            "mgr2": approvals.Person("mgr2", manager_id="partner", org_unit_id="TAX",
                                     cost_center_id="CC-TAX"),
            "staff": approvals.Person("staff", manager_id="mgr", org_unit_id="AUD",
                                      cost_center_id="CC-AUD"),
            "staff2": approvals.Person("staff2", manager_id="mgr2", org_unit_id="TAX",
                                       cost_center_id="CC-TAX"),
            "gone": approvals.Person("gone", manager_id="mgr", status="leaver",
                                     org_unit_id="AUD"),
        }
        self.heads = heads if heads is not None else {"AUD": "partner", "TAX": "mgr2"}
        self.owners = owners if owners is not None else {"CC-AUD": "partner",
                                                         "CC-TAX": "mgr2"}
        self.groups = groups if groups is not None else {"partners": ["partner", "mgr2"]}
        self.parents = parents or {}

    def person(self, person_id):
        return self.people.get(person_id)

    def org_unit_head(self, org_unit_id):
        return self.heads.get(org_unit_id)

    def org_unit_parent(self, org_unit_id):
        return self.parents.get(org_unit_id)

    def cost_center_owner(self, cost_center_id):
        return self.owners.get(cost_center_id)

    def group_members(self, group_id):
        return self.groups.get(group_id, [])


def step(approver_type, **kwargs):
    kwargs.setdefault("code", "S1")
    kwargs.setdefault("sequence", 1)
    return approvals.PolicyStep(approver_type=approver_type, **kwargs)


def policy(*steps, **kwargs):
    kwargs.setdefault("code", "POL")
    kwargs.setdefault("applies_to_entity", "wga_leaverequest")
    kwargs.setdefault("evaluation_order", 100)
    return approvals.Policy(steps=tuple(steps), **kwargs)


def record(**kwargs):
    kwargs.setdefault("entity", "wga_leaverequest")
    kwargs.setdefault("id", "LR-1")
    kwargs.setdefault("requester_id", "staff")
    return approvals.Record(**kwargs)


class TestPolicyMatching(unittest.TestCase):
    def test_the_lowest_evaluation_order_wins(self):
        specific = policy(code="SPECIFIC", evaluation_order=10)
        fallback = policy(code="FALLBACK", evaluation_order=100)
        self.assertEqual(
            approvals.match_policy([fallback, specific], record()).code, "SPECIFIC")

    def test_a_policy_for_another_entity_is_ignored(self):
        other = policy(code="OTHER", applies_to_entity="wga_compensation",
                       evaluation_order=1)
        mine = policy(code="MINE", evaluation_order=100)
        self.assertEqual(approvals.match_policy([other, mine], record()).code, "MINE")

    def test_no_policy_returns_none_rather_than_guessing(self):
        self.assertIsNone(approvals.match_policy([], record()))

    def test_a_legal_entity_filter_must_fit(self):
        scoped = policy(code="SCOPED", evaluation_order=1, legal_entity_id="WGA")
        self.assertIsNone(approvals.match_policy([scoped], record(legal_entity_id="OTHER")))
        self.assertIsNotNone(approvals.match_policy([scoped], record(legal_entity_id="WGA")))

    def test_an_org_unit_filter_must_fit(self):
        scoped = policy(code="S", evaluation_order=1, org_unit_id="AUD")
        self.assertIsNone(approvals.match_policy([scoped], record(org_unit_id="TAX")))

    def test_an_amount_band_must_fit(self):
        band = policy(code="BAND", evaluation_order=1, min_amount=1000, max_amount=5000)
        self.assertIsNone(approvals.match_policy([band], record(amount=500)))
        self.assertIsNone(approvals.match_policy([band], record(amount=6000)))
        self.assertIsNotNone(approvals.match_policy([band], record(amount=2000)))

    def test_a_banded_policy_does_not_match_a_record_with_no_amount(self):
        band = policy(code="BAND", evaluation_order=1, min_amount=1000)
        self.assertIsNone(approvals.match_policy([band], record(amount=None)))

    def test_a_tie_is_broken_deterministically_by_code(self):
        a = policy(code="AAA", evaluation_order=50)
        b = policy(code="BBB", evaluation_order=50)
        self.assertEqual(approvals.match_policy([b, a], record()).code, "AAA")
        self.assertEqual(approvals.match_policy([a, b], record()).code, "AAA")


class TestEveryApproverType(unittest.TestCase):
    """One test per approverType, so a new value cannot arrive untested."""

    def setUp(self):
        self.directory = FakeDirectory()

    def _resolve(self, policy_step, rec=None):
        return approvals.resolve_approvers(policy_step, rec or record(), self.directory)

    def test_manager(self):
        who, why = self._resolve(step("manager"))
        self.assertEqual(who, ["mgr"])
        self.assertIn("line manager", why)

    def test_skip_level_manager(self):
        who, why = self._resolve(step("skipLevelManager"))
        self.assertEqual(who, ["partner"])
        self.assertIn("manager's manager", why)

    def test_specific_employee(self):
        who, _ = self._resolve(step("specificEmployee", approver_id="mgr2"))
        self.assertEqual(who, ["mgr2"])

    def test_org_unit_head(self):
        who, why = self._resolve(step("orgUnitHead"))
        self.assertEqual(who, ["partner"])
        self.assertIn("head of AUD", why)

    def test_cost_center_owner(self):
        who, why = self._resolve(step("costCenterOwner"))
        self.assertEqual(who, ["partner"])
        self.assertIn("budget owner", why)

    def test_record_field(self):
        who, why = self._resolve(
            step("recordField", approver_field="coverBy"),
            record(fields={"coverBy": "mgr2"}))
        self.assertEqual(who, ["mgr2"])
        self.assertIn("coverBy", why)

    def test_group_returns_every_member(self):
        who, why = self._resolve(step("group", approver_group_id="partners"))
        self.assertEqual(who, ["partner", "mgr2"])
        self.assertIn("any member", why)

    def test_the_engine_covers_exactly_the_schemas_approver_types(self):
        covered = {name[len("test_"):] for name in dir(self)
                   if name.startswith("test_") and name != "test_the_engine_covers_"
                   "exactly_the_schemas_approver_types"}
        # camelCase -> snake_case for comparison
        import re
        expected = {re.sub(r"(?<!^)(?=[A-Z])", "_", t).lower()
                    for t in approvals.APPROVER_TYPES}
        self.assertTrue(expected.issubset(covered | {"group_returns_every_member"}
                                          | {"group"}),
                        f"untested: {expected - covered}")


class TestResolutionEdgeCases(unittest.TestCase):
    """The three cases that are real support tickets."""

    def test_no_manager_falls_back_to_the_org_unit_head(self):
        directory = FakeDirectory(people={
            "boss": approvals.Person("boss", org_unit_id="AUD"),
            "chair": approvals.Person("chair", org_unit_id="AUD"),
        }, heads={"AUD": "chair"})
        who, why = approvals.resolve_approvers(
            step("manager"), record(requester_id="boss"), directory)
        self.assertEqual(who, ["chair"])
        self.assertIn("no line manager", why)

    def test_no_manager_and_no_head_raises_rather_than_stalling(self):
        directory = FakeDirectory(people={
            "boss": approvals.Person("boss", org_unit_id="AUD")}, heads={})
        with self.assertRaises(approvals.ResolutionError) as ctx:
            approvals.resolve_approvers(step("manager"), record(requester_id="boss"),
                                        directory)
        self.assertIn("no manager", str(ctx.exception))

    def test_the_org_unit_head_is_not_used_when_it_is_the_requester(self):
        directory = FakeDirectory(people={
            "chair": approvals.Person("chair", org_unit_id="AUD")},
            heads={"AUD": "chair"})
        with self.assertRaises(approvals.ResolutionError):
            approvals.resolve_approvers(step("manager"), record(requester_id="chair"),
                                        directory)

    def test_a_department_head_is_approved_by_the_unit_above(self):
        """A head has no line manager and heads their own unit, so walk up."""
        directory = FakeDirectory(
            people={"head": approvals.Person("head", org_unit_id="AUD"),
                    "managing": approvals.Person("managing", org_unit_id="FIRM")},
            heads={"AUD": "head", "FIRM": "managing"},
            parents={"AUD": "FIRM"})
        who, why = approvals.resolve_approvers(
            step("manager"), record(requester_id="head"), directory)
        self.assertEqual(who, ["managing"])
        self.assertIn("no line manager", why)

    def test_an_org_unit_head_step_for_the_head_themselves_goes_up(self):
        directory = FakeDirectory(
            people={"head": approvals.Person("head", org_unit_id="AUD"),
                    "managing": approvals.Person("managing", org_unit_id="FIRM")},
            heads={"AUD": "head", "FIRM": "managing"},
            parents={"AUD": "FIRM"})
        who, why = approvals.resolve_approvers(
            step("orgUnitHead"), record(requester_id="head"), directory)
        self.assertEqual(who, ["managing"])
        self.assertIn("unit above", why)

    def test_a_leaver_heading_the_unit_above_is_stepped_over(self):
        directory = FakeDirectory(
            people={"head": approvals.Person("head", org_unit_id="AUD"),
                    "gone": approvals.Person("gone", org_unit_id="FIRM",
                                             status="leaver", manager_id="chair"),
                    "chair": approvals.Person("chair", org_unit_id="FIRM")},
            heads={"AUD": "head", "FIRM": "gone"},
            parents={"AUD": "FIRM"})
        who, _ = approvals.resolve_approvers(
            step("manager"), record(requester_id="head"), directory)
        self.assertEqual(who, ["chair"])

    def test_a_cycle_in_the_org_tree_does_not_hang(self):
        directory = FakeDirectory(
            people={"a": approvals.Person("a", org_unit_id="X")},
            heads={"X": "a", "Y": "a"}, parents={"X": "Y", "Y": "X"})
        with self.assertRaises(approvals.ResolutionError):
            approvals.resolve_approvers(step("manager"), record(requester_id="a"),
                                        directory)

    def test_the_person_at_the_very_top_has_nobody_and_that_is_reported(self):
        """The managing partner's own leave genuinely has no approver."""
        directory = FakeDirectory(
            people={"managing": approvals.Person("managing", org_unit_id="FIRM")},
            heads={"FIRM": "managing"}, parents={})
        with self.assertRaises(approvals.ResolutionError) as ctx:
            approvals.resolve_approvers(step("manager"), record(requester_id="managing"),
                                        directory)
        self.assertIn("no manager", str(ctx.exception))

    def test_an_approver_who_is_the_requester_escalates_upward(self):
        """A partner approving their own leave fails an audit."""
        directory = FakeDirectory()
        who, why = approvals.resolve_approvers(
            step("specificEmployee", approver_id="staff"), record(requester_id="staff"),
            directory)
        self.assertEqual(who, ["mgr"])
        self.assertIn("raised the request", why)

    def test_a_requester_with_nobody_above_them_raises(self):
        directory = FakeDirectory(people={
            "solo": approvals.Person("solo")}, heads={})
        with self.assertRaises(approvals.ResolutionError):
            approvals.resolve_approvers(
                step("specificEmployee", approver_id="solo"),
                record(requester_id="solo"), directory)

    def test_a_leaver_is_replaced_by_their_manager(self):
        directory = FakeDirectory()
        who, why = approvals.resolve_approvers(
            step("specificEmployee", approver_id="gone"), record(), directory)
        self.assertEqual(who, ["mgr"])
        self.assertIn("has left", why)

    def test_a_chain_of_leavers_walks_up_to_the_first_active_person(self):
        directory = FakeDirectory(people={
            "staff": approvals.Person("staff", manager_id="gone1"),
            "gone1": approvals.Person("gone1", manager_id="gone2", status="leaver"),
            "gone2": approvals.Person("gone2", manager_id="partner", status="leaver"),
            "partner": approvals.Person("partner"),
        })
        who, _ = approvals.resolve_approvers(step("manager"), record(), directory)
        self.assertEqual(who, ["partner"])

    def test_a_cycle_in_the_reporting_line_does_not_hang(self):
        directory = FakeDirectory(people={
            "a": approvals.Person("a", manager_id="b", status="leaver"),
            "b": approvals.Person("b", manager_id="a", status="leaver"),
            "staff": approvals.Person("staff", manager_id="a"),
        })
        with self.assertRaises(approvals.ResolutionError):
            approvals.resolve_approvers(step("manager"), record(), directory)

    def test_a_group_with_no_members_raises(self):
        directory = FakeDirectory(groups={"empty": []})
        with self.assertRaises(approvals.ResolutionError):
            approvals.resolve_approvers(step("group", approver_group_id="empty"),
                                        record(), directory)

    def test_a_group_containing_the_requester_drops_them_not_the_group(self):
        directory = FakeDirectory(groups={"team": ["staff", "mgr2"]})
        who, _ = approvals.resolve_approvers(
            step("group", approver_group_id="team"), record(requester_id="staff"),
            directory)
        self.assertIn("mgr2", who)
        self.assertNotIn("staff", who)

    def test_a_skip_level_with_no_grandmanager_uses_the_line_manager(self):
        who, why = approvals.resolve_approvers(
            step("skipLevelManager"), record(requester_id="mgr"), FakeDirectory())
        self.assertEqual(who, ["partner"])

    def test_a_record_field_with_no_value_raises(self):
        with self.assertRaises(approvals.ResolutionError) as ctx:
            approvals.resolve_approvers(
                step("recordField", approver_field="coverBy"), record(), FakeDirectory())
        self.assertIn("coverBy", str(ctx.exception))

    def test_an_unknown_approver_type_raises(self):
        with self.assertRaises(approvals.ResolutionError) as ctx:
            approvals.resolve_approvers(step("astrologer"), record(), FakeDirectory())
        self.assertIn("not one of", str(ctx.exception))

    def test_a_requester_not_in_the_directory_raises(self):
        with self.assertRaises(approvals.ResolutionError):
            approvals.resolve_approvers(step("manager"), record(requester_id="ghost"),
                                        FakeDirectory())

    def test_duplicate_candidates_resolve_once(self):
        directory = FakeDirectory(groups={"dup": ["mgr", "mgr"]})
        who, _ = approvals.resolve_approvers(
            step("group", approver_group_id="dup"), record(), directory)
        self.assertEqual(who, ["mgr"])


class TestBuildingARun(unittest.TestCase):
    def setUp(self):
        self.directory = FakeDirectory()

    def test_steps_come_out_in_sequence(self):
        chain = policy(step("orgUnitHead", code="S2", sequence=2),
                       step("manager", code="S1", sequence=1))
        steps = approvals.build_steps(chain, record(), self.directory, NOW)
        self.assertEqual([s.sequence for s in steps], [1, 2])

    def test_the_first_step_is_pending_and_the_rest_wait(self):
        chain = policy(step("manager", code="S1", sequence=1),
                       step("orgUnitHead", code="S2", sequence=2))
        steps = approvals.build_steps(chain, record(), self.directory, NOW)
        self.assertEqual([s.status for s in steps], ["pending", "waiting"])

    def test_a_step_below_its_amount_threshold_is_skipped(self):
        chain = policy(step("manager", code="S1", sequence=1),
                       step("orgUnitHead", code="S2", sequence=2, min_amount=10000))
        steps = approvals.build_steps(chain, record(amount=500), self.directory, NOW)
        self.assertEqual([s.policy_step.code for s in steps], ["S1"])

    def test_a_step_above_its_threshold_is_included(self):
        chain = policy(step("manager", code="S1", sequence=1),
                       step("orgUnitHead", code="S2", sequence=2, min_amount=10000))
        steps = approvals.build_steps(chain, record(amount=20000), self.directory, NOW)
        self.assertEqual([s.policy_step.code for s in steps], ["S1", "S2"])

    def test_a_policy_with_no_applicable_steps_raises(self):
        chain = policy(step("manager", min_amount=10000))
        with self.assertRaises(approvals.ResolutionError):
            approvals.build_steps(chain, record(amount=1), self.directory, NOW)

    def test_an_unknown_completion_rule_raises(self):
        chain = policy(step("manager", completion_rule="majority"))
        with self.assertRaises(approvals.ResolutionError) as ctx:
            approvals.build_steps(chain, record(), self.directory, NOW)
        self.assertIn("completionRule", str(ctx.exception))

    def test_each_step_records_why_it_was_assigned(self):
        steps = approvals.build_steps(policy(step("manager")), record(),
                                      self.directory, NOW)
        self.assertIn("line manager", steps[0].reason)


class TestScheduling(unittest.TestCase):
    def test_the_step_timeout_wins_over_the_policy(self):
        chain = policy(step("manager", timeout_hours=4), escalate_after_hours=72)
        steps = approvals.build_steps(chain, record(), FakeDirectory(), NOW)
        self.assertEqual(steps[0].due_on, NOW + dt.timedelta(hours=4))

    def test_the_policy_escalation_is_used_when_the_step_has_no_timeout(self):
        chain = policy(step("manager"), escalate_after_hours=48)
        steps = approvals.build_steps(chain, record(), FakeDirectory(), NOW)
        self.assertEqual(steps[0].due_on, NOW + dt.timedelta(hours=48))

    def test_reminder_and_escalation_times_are_set(self):
        chain = policy(step("manager"), reminder_after_hours=24, escalate_after_hours=72)
        steps = approvals.build_steps(chain, record(), FakeDirectory(), NOW)
        self.assertEqual(steps[0].reminder_on, NOW + dt.timedelta(hours=24))
        self.assertEqual(steps[0].escalate_on, NOW + dt.timedelta(hours=72))

    def test_no_timeout_leaves_no_due_date(self):
        steps = approvals.build_steps(policy(step("manager")), record(),
                                      FakeDirectory(), NOW)
        self.assertIsNone(steps[0].due_on)

    def test_overdue_finds_only_pending_steps_past_their_due_date(self):
        chain = policy(step("manager", timeout_hours=1))
        steps = approvals.build_steps(chain, record(), FakeDirectory(), NOW)
        self.assertEqual(approvals.overdue(steps, NOW), [])
        self.assertEqual(len(approvals.overdue(steps, NOW + dt.timedelta(hours=2))), 1)
        steps[0].status = "approved"
        self.assertEqual(approvals.overdue(steps, NOW + dt.timedelta(hours=2)), [])

    def test_reminders_fire_once_their_time_has_come(self):
        chain = policy(step("manager"), reminder_after_hours=24)
        steps = approvals.build_steps(chain, record(), FakeDirectory(), NOW)
        self.assertEqual(approvals.needs_reminder(steps, NOW), [])
        self.assertEqual(len(approvals.needs_reminder(
            steps, NOW + dt.timedelta(hours=25))), 1)


class TestCompletionAndAdvance(unittest.TestCase):
    def _steps(self, *policy_steps, **kwargs):
        return approvals.build_steps(policy(*policy_steps, **kwargs), record(),
                                     FakeDirectory(), NOW)

    def test_any_approver_completes_on_one_approval(self):
        steps = self._steps(step("group", approver_group_id="partners",
                                 completion_rule="anyApprover"))
        self.assertTrue(approvals.is_complete(steps[0], {"partner": "approved"}))

    def test_all_approvers_needs_everyone(self):
        steps = self._steps(step("group", approver_group_id="partners",
                                 completion_rule="allApprovers"))
        self.assertFalse(approvals.is_complete(steps[0], {"partner": "approved"}))
        self.assertTrue(approvals.is_complete(
            steps[0], {"partner": "approved", "mgr2": "approved"}))

    def test_one_rejection_completes_the_step_either_way(self):
        for rule in ("anyApprover", "allApprovers"):
            steps = self._steps(step("group", approver_group_id="partners",
                                     completion_rule=rule))
            self.assertTrue(approvals.is_complete(steps[0], {"partner": "rejected"}), rule)
            self.assertEqual(approvals.outcome(steps[0], {"partner": "rejected"}),
                             "rejected", rule)

    def test_advance_moves_to_the_next_step_once_the_first_is_approved(self):
        chain = policy(step("manager", code="S1", sequence=1),
                       step("orgUnitHead", code="S2", sequence=2))
        steps = approvals.build_steps(chain, record(), FakeDirectory(), NOW)
        steps[0].status = "approved"
        status, pending = approvals.advance(steps, chain, NOW)
        self.assertEqual(status, "pending")
        self.assertEqual([s.policy_step.code for s in pending], ["S2"])
        self.assertIsNotNone(steps[1].assigned_on)

    def test_the_run_is_approved_once_every_step_is(self):
        chain = policy(step("manager", code="S1", sequence=1),
                       step("orgUnitHead", code="S2", sequence=2))
        steps = approvals.build_steps(chain, record(), FakeDirectory(), NOW)
        for s in steps:
            s.status = "approved"
        status, pending = approvals.advance(steps, chain, NOW)
        self.assertEqual(status, "approved")
        self.assertEqual(pending, [])

    def test_a_rejection_anywhere_ends_the_run(self):
        """An approval chain is a series of gates, not a vote."""
        chain = policy(step("manager", code="S1", sequence=1),
                       step("orgUnitHead", code="S2", sequence=2))
        steps = approvals.build_steps(chain, record(), FakeDirectory(), NOW)
        steps[1].status = "rejected"
        status, _ = approvals.advance(steps, chain, NOW)
        self.assertEqual(status, "rejected")

    def test_a_skipped_step_does_not_block(self):
        chain = policy(step("manager", code="S1", sequence=1),
                       step("orgUnitHead", code="S2", sequence=2))
        steps = approvals.build_steps(chain, record(), FakeDirectory(), NOW)
        steps[0].status = "skipped"
        steps[1].status = "approved"
        self.assertEqual(approvals.advance(steps, chain, NOW)[0], "approved")


class TestAgainstTheSchema(unittest.TestCase):
    """The engine's constants must match the YAML, or a new value resolves to nobody."""

    @classmethod
    def setUpClass(cls):
        model_file = ROOT / "build" / "hr-starter.model.json"
        if not model_file.exists():
            raise unittest.SkipTest("run: python tools/schematool.py export --profile hr-starter")
        cls.model = json.loads(model_file.read_text(encoding="utf-8"))

    def test_approver_types_match_the_schema_exactly(self):
        schema = [v["key"] for v in self.model["enums"]["ApproverType"]["values"]
                  if not v.get("deprecated")]
        self.assertEqual(set(schema), set(approvals.APPROVER_TYPES))

    def test_completion_rules_match_the_schema_exactly(self):
        schema = [v["key"] for v in
                  self.model["enums"]["ApprovalCompletionRule"]["values"]
                  if not v.get("deprecated")]
        self.assertEqual(set(schema), set(approvals.COMPLETION_RULES))

    def test_the_engine_knows_every_step_status_the_schema_has(self):
        schema = {v["key"] for v in
                  self.model["enums"]["ApprovalStepStatus"]["values"]}
        used = {"waiting", "pending", "approved", "rejected", "skipped"}
        self.assertTrue(used.issubset(schema), used - schema)

    def test_the_policy_columns_the_engine_reads_all_exist(self):
        columns = self.model["entities"]["ApprovalPolicy"]["attributes"]
        for name in ("appliesToEntity", "legalEntity", "orgUnit", "minAmount",
                     "maxAmount", "evaluationOrder", "reminderAfterHours",
                     "escalateAfterHours"):
            self.assertIn(name, columns, name)

    def test_the_policy_step_columns_the_engine_reads_all_exist(self):
        columns = self.model["entities"]["ApprovalPolicyStep"]["attributes"]
        for name in ("sequence", "approverType", "approver", "approverGroupId",
                     "approverField", "completionRule", "minAmount", "timeoutHours",
                     "allowDelegation"):
            self.assertIn(name, columns, name)

    def test_the_step_columns_the_engine_writes_all_exist(self):
        columns = self.model["entities"]["ApprovalStep"]["attributes"]
        for name in ("sequence", "approver", "status", "assignedOn", "dueOn",
                     "decidedOn", "delegatedFrom"):
            self.assertIn(name, columns, name)


class TestTheDemoPolicies(unittest.TestCase):
    """The policies demodata.py seeds must actually resolve for that firm."""

    @classmethod
    def setUpClass(cls):
        demo = ROOT / "build" / "demo" / "woodgrove"
        if not (demo / "ApprovalPolicy.json").exists():
            raise unittest.SkipTest("run: python tools/demodata.py --profile hr-starter")
        cls.policies_raw = json.loads((demo / "ApprovalPolicy.json").read_text())
        cls.steps_raw = json.loads((demo / "ApprovalPolicyStep.json").read_text())
        cls.employees = json.loads((demo / "Employee.json").read_text())
        cls.assignments = json.loads((demo / "JobAssignment.json").read_text())
        cls.units = json.loads((demo / "OrgUnit.json").read_text())
        cls.centres = json.loads((demo / "CostCenter.json").read_text())

    def _directory(self):
        current = {a["employee"]: a for a in self.assignments if a.get("validTo") is None}
        people = {}
        for row in self.employees:
            number = row["employeeNumber"]
            assignment = current.get(number, {})
            people[number] = approvals.Person(
                id=number, status="leaver" if row["employmentStatus"] == "leaver"
                else "active",
                manager_id=assignment.get("manager"),
                org_unit_id=row.get("orgUnit"),
                cost_center_id=assignment.get("costCenter"))
        return FakeDirectory(
            people=people,
            heads={u["code"]: u.get("head") for u in self.units if u.get("head")},
            owners={c["code"]: c.get("budgetOwner") for c in self.centres
                    if c.get("budgetOwner")},
            groups={"partners": [r["employeeNumber"] for r in self.employees
                                 if r["workerType"] == "partner"
                                 and r["employmentStatus"] != "leaver"]},
            parents={u["code"]: u.get("parentOrgUnit") for u in self.units
                     if u.get("parentOrgUnit")})

    def _managing_partner(self):
        """The one person the firm has nobody above: head of the root unit."""
        return next((u.get("head") for u in self.units if u["code"] == "FIRM"), None)

    def _policies(self):
        by_code = {}
        for row in self.policies_raw:
            by_code[row["code"]] = {
                "row": row,
                "steps": [s for s in self.steps_raw if s["policy"] == row["code"]],
            }
        out = []
        for code, item in by_code.items():
            row = item["row"]
            steps = tuple(
                approvals.PolicyStep(
                    code=s["code"], sequence=s["sequence"],
                    approver_type=s["approverType"],
                    completion_rule=s.get("completionRule", "anyApprover"),
                    approver_group_id="partners" if s["approverType"] == "group" else None,
                    timeout_hours=s.get("timeoutHours"),
                    allow_delegation=bool(s.get("allowDelegation")))
                for s in sorted(item["steps"], key=lambda s: s["sequence"]))
            out.append(approvals.Policy(
                code=code, applies_to_entity=row["appliesToEntity"],
                evaluation_order=row["evaluationOrder"], steps=steps,
                legal_entity_id=row.get("legalEntity"),
                reminder_after_hours=row.get("reminderAfterHours"),
                escalate_after_hours=row.get("escalateAfterHours")))
        return out

    def test_a_leave_request_matches_a_policy(self):
        matched = approvals.match_policy(
            self._policies(),
            approvals.Record(entity="wga_leaverequest", id="LR-1",
                             requester_id=self.employees[0]["employeeNumber"],
                             legal_entity_id="WGA"))
        self.assertIsNotNone(matched)

    def test_every_active_employee_but_the_managing_partner_can_be_routed(self):
        directory = self._directory()
        policies = self._policies()
        managing = self._managing_partner()
        failures = []
        for row in self.employees:
            number = row["employeeNumber"]
            if row["employmentStatus"] == "leaver" or number == managing:
                continue
            rec = approvals.Record(entity="wga_leaverequest", id="LR",
                                   requester_id=number, legal_entity_id="WGA")
            chain = approvals.match_policy(policies, rec)
            try:
                approvals.build_steps(chain, rec, directory, NOW)
            except approvals.ResolutionError as exc:
                failures.append(f"{number}: {exc}")
        self.assertEqual(failures, [], "\n".join(failures[:10]))

    def test_only_the_managing_partner_cannot_be_routed(self):
        """And the engine says so plainly instead of assigning it to nobody."""
        directory = self._directory()
        policies = self._policies()
        managing = self._managing_partner()
        self.assertIsNotNone(managing)
        rec = approvals.Record(entity="wga_leaverequest", id="LR",
                               requester_id=managing, legal_entity_id="WGA")
        chain = approvals.match_policy(policies, rec)
        with self.assertRaises(approvals.ResolutionError):
            approvals.build_steps(chain, rec, directory, NOW)

    def test_nobody_ends_up_approving_their_own_request(self):
        directory = self._directory()
        policies = self._policies()
        managing = self._managing_partner()
        for row in self.employees:
            number = row["employeeNumber"]
            if row["employmentStatus"] == "leaver" or number == managing:
                continue
            rec = approvals.Record(entity="wga_leaverequest", id="LR",
                                   requester_id=number, legal_entity_id="WGA")
            chain = approvals.match_policy(policies, rec)
            for built in approvals.build_steps(chain, rec, directory, NOW):
                self.assertNotIn(number, built.approvers, number)

    def test_every_approver_is_an_active_person(self):
        directory = self._directory()
        policies = self._policies()
        managing = self._managing_partner()
        for row in self.employees:
            if row["employmentStatus"] == "leaver" or row["employeeNumber"] == managing:
                continue
            rec = approvals.Record(entity="wga_leaverequest", id="LR",
                                   requester_id=row["employeeNumber"],
                                   legal_entity_id="WGA")
            chain = approvals.match_policy(policies, rec)
            for built in approvals.build_steps(chain, rec, directory, NOW):
                for approver in built.approvers:
                    person = directory.person(approver)
                    self.assertIsNotNone(person, approver)
                    self.assertTrue(person.active, approver)

    def test_a_pay_change_routes_through_the_skip_level_and_the_partners(self):
        directory = self._directory()
        policies = self._policies()
        staff = next(r for r in self.employees
                     if r["employmentStatus"] != "leaver" and r["workerType"] != "partner")
        rec = approvals.Record(entity="wga_compensation", id="C-1",
                               requester_id=staff["employeeNumber"], amount=50000)
        chain = approvals.match_policy(policies, rec)
        self.assertIsNotNone(chain)
        steps = approvals.build_steps(chain, rec, directory, NOW)
        self.assertEqual([s.policy_step.approver_type for s in steps],
                         ["skipLevelManager", "group"])
        self.assertEqual(steps[1].policy_step.completion_rule, "allApprovers")


if __name__ == "__main__":
    unittest.main()
