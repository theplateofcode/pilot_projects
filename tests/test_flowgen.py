"""Tests for tools/flowgen.py. Run with: python -m unittest discover -s tests

A flow definition cannot be executed here, so these tests check the two things
that actually go wrong: a reference to a column or option-set value that does
not exist, and a runAfter that names an action which is not there. Both import
cleanly into Power Automate and then fail at run time.
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import approvals  # noqa: E402
import dvgen  # noqa: E402
import flowgen  # noqa: E402

MODEL_FILE = ROOT / "build" / "hr-starter.model.json"
PREFIX = "wga"


def _entity_set(logical_name: str) -> str:
    """Dataverse's own pluralisation, written out here as the expected answer."""
    if logical_name.endswith("y") and logical_name[-2] not in "aeiou":
        return logical_name[:-1] + "ies"
    if logical_name.endswith(("s", "x", "z", "ch", "sh")):
        return logical_name + "es"
    return logical_name + "s"


class TestNaming(unittest.TestCase):
    def test_logical_names_are_prefixed_and_lowercased(self):
        self.assertEqual(flowgen.logical("wga", "LeaveRequest"), "wga_leaverequest")

    def test_entity_set_names_are_pluralised(self):
        expected = {
            "LeaveRequest": "wga_leaverequests",
            "ApprovalPolicy": "wga_approvalpolicies",
            "ApprovalStep": "wga_approvalsteps",
            "Employee": "wga_employees",
            "Employment": "wga_employments",
            "Address": "wga_addresses",
            "Box": "wga_boxes",
            "Branch": "wga_branches",
        }
        for name, want in expected.items():
            self.assertEqual(flowgen.plural("wga", name), want, name)

    def test_a_vowel_before_the_y_keeps_the_s(self):
        self.assertEqual(flowgen.plural("wga", "Journey"), "wga_journeys")

    def test_flow_ids_are_stable(self):
        self.assertEqual(flowgen.flow_id("approval-decision"),
                         flowgen.flow_id("approval-decision"))
        self.assertNotEqual(flowgen.flow_id("a"), flowgen.flow_id("b"))


class TestValidationAgainstTheModel(unittest.TestCase):
    def _model(self, entities, enums=None):
        return {"profile": "t", "layers": {"hr": "1.0.0"}, "entities": entities,
                "enums": enums or {}, "termSets": {}, "terminology": {}, "seed": {}}

    def _entity(self, attributes):
        return {"layer": "hr", "description": "A table.", "ownership": "user",
                "volume": "low", "sensitivity": "internal",
                "platforms": {"dataverse": "custom"}, "primary": "name", "keys": [],
                "attributes": attributes}

    def test_a_missing_engine_table_is_an_error(self):
        g = flowgen.Generator(self._model({}), PREFIX)
        for table in ("ApprovalPolicy", "ApprovalPolicyStep", "ApprovalRequest",
                      "ApprovalStep"):
            self.assertTrue(any(table in e for e in g.errors), table)

    def test_an_approvable_entity_without_a_writeback_column_is_an_error(self):
        entities = {name: self._entity({"name": {"type": "string"}})
                    for name in ("ApprovalPolicy", "ApprovalPolicyStep",
                                 "ApprovalRequest", "ApprovalStep")}
        entities["LeaveRequest"] = self._entity({
            "name": {"type": "string"},
            "status": {"type": "choice", "enum": "S"}})
        g = flowgen.Generator(self._model(entities, enums={"S": {"values": [
            {"code": 1, "key": "submitted"}, {"code": 2, "key": "approved"},
            {"code": 3, "key": "rejected"}]}}), PREFIX)
        self.assertTrue(any("approvalRequest column" in e for e in g.errors))

    def test_a_status_value_the_enum_does_not_have_is_an_error(self):
        entities = {name: self._entity({"name": {"type": "string"}})
                    for name in ("ApprovalPolicy", "ApprovalPolicyStep",
                                 "ApprovalRequest", "ApprovalStep")}
        entities["LeaveRequest"] = self._entity({
            "name": {"type": "string"},
            "approvalRequest": {"type": "lookup", "target": "ApprovalRequest"},
            "status": {"type": "choice", "enum": "S"}})
        g = flowgen.Generator(self._model(entities, enums={"S": {"values": [
            {"code": 1, "key": "draft"}]}}), PREFIX)
        self.assertTrue(any("'submitted' is not a value" in e for e in g.errors))

    def test_an_entity_not_in_the_profile_is_noted_not_failed(self):
        entities = {name: self._entity({"name": {"type": "string"}})
                    for name in ("ApprovalPolicy", "ApprovalPolicyStep",
                                 "ApprovalRequest", "ApprovalStep")}
        g = flowgen.Generator(self._model(entities), PREFIX)
        self.assertTrue(any("Compensation is not in this profile" in n for n in g.notes))


class TestCli(unittest.TestCase):
    def test_a_bad_prefix_is_rejected(self):
        for bad in ("W", "1abc", "toolongprefix", "ab-c"):
            self.assertEqual(flowgen.main(["--profile", "x", "--prefix", bad]), 2, bad)


class TestTheRealFlows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not MODEL_FILE.exists():
            raise unittest.SkipTest("run: python tools/schematool.py export --profile hr-starter")
        cls.model = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
        cls.plan = flowgen.Generator(cls.model, PREFIX, "WoodgroveHR").build()
        # Everything the Dataverse plan will actually create.
        dv = dvgen.Generator(cls.model, PREFIX, ["core", "hr"])
        dv_plan = dv.build()
        cls.columns: set[str] = set()
        for table in dv_plan["tables"]:
            cls.columns.update(a["LogicalName"] for a in table["payload"]["Attributes"])
            cls.columns.add(table["_logicalName"] + "id")
        for table in dv_plan["columns"]:
            cls.columns.update(c["LogicalName"] for c in table["attributes"])
        for rel in dv_plan["relationships"]["oneToMany"]:
            cls.columns.add(rel["payload"]["Lookup"]["LogicalName"])
        cls.tables = {t["_logicalName"] for t in dv_plan["tables"]}
        # Derived here, independently of flowgen.plural: using the tool's own
        # function on both sides of a comparison proves nothing, which is how a
        # typo in it survived until the unit test above caught it.
        cls.entity_sets = {_entity_set(t["_logicalName"]) for t in dv_plan["tables"]}

    def test_no_errors_and_no_warnings(self):
        self.assertEqual(self.plan["errors"], [])
        self.assertEqual(self.plan["warnings"], [])

    def test_four_flows_are_generated(self):
        names = [f["_name"] for f in self.plan["flows"]]
        self.assertEqual(len(names), 4)
        self.assertIn("approval-decision", names)
        self.assertIn("approval-reminders", names)
        self.assertIn("onboarding-checklist", names)

    def test_each_flow_has_one_trigger_and_at_least_one_action(self):
        for flow in self.plan["flows"]:
            definition = flow["properties"]["definition"]
            self.assertEqual(len(definition["triggers"]), 1, flow["_name"])
            self.assertTrue(definition["actions"], flow["_name"])

    def test_each_flow_declares_the_dataverse_connection(self):
        for flow in self.plan["flows"]:
            self.assertIn("shared_commondataserviceforapps",
                          flow["properties"]["connectionReferences"], flow["_name"])

    def test_each_flow_and_action_carries_a_description(self):
        """A flow nobody can read is a flow nobody will maintain."""
        for flow in self.plan["flows"]:
            self.assertTrue(flow["properties"]["description"], flow["_name"])
            for name, action in flow["properties"]["definition"]["actions"].items():
                if action["type"] in ("Compose", "Scope", "Terminate"):
                    continue
                self.assertTrue(action.get("description"),
                                f"{flow['_name']}/{name}")

    def _walk(self, actions, path=""):
        """Yield (path, name, action) for every action at every depth."""
        for name, action in actions.items():
            yield path, name, action
            for key in ("actions",):
                if isinstance(action.get(key), dict):
                    yield from self._walk(action[key], f"{path}/{name}")
            if isinstance(action.get("else"), dict):
                yield from self._walk(action["else"].get("actions", {}), f"{path}/{name}")
            for case in (action.get("cases") or {}).values():
                yield from self._walk(case.get("actions", {}), f"{path}/{name}")

    def test_every_run_after_names_an_action_at_the_same_level(self):
        """A runAfter pointing at nothing imports fine and never runs."""
        broken = []
        for flow in self.plan["flows"]:
            levels: dict[str, set[str]] = {}
            for path, name, _ in self._walk(flow["properties"]["definition"]["actions"]):
                levels.setdefault(path, set()).add(name)
            for path, name, action in self._walk(
                    flow["properties"]["definition"]["actions"]):
                for dependency in (action.get("runAfter") or {}):
                    if dependency not in levels.get(path, set()):
                        broken.append(f"{flow['_name']}{path}/{name} -> {dependency}")
        self.assertEqual(broken, [])

    def test_every_entity_name_is_a_table_the_build_creates(self):
        unknown = []
        for flow in self.plan["flows"]:
            text = json.dumps(flow)
            for match in re.finditer(r'"entityName": "([^"]+)"', text):
                if match.group(1) not in self.entity_sets:
                    unknown.append(f"{flow['_name']}: {match.group(1)}")
            for match in re.finditer(
                    r'"subscriptionRequest/entityname": "([^"]+)"', text):
                if match.group(1) not in self.entity_sets:
                    unknown.append(f"{flow['_name']}: trigger on {match.group(1)}")
        self.assertEqual(unknown, [])

    def test_every_column_the_flows_reference_exists(self):
        """The check that matters: a flow naming a column Dataverse will not have."""
        unknown = []
        pattern = re.compile(rf"\b{PREFIX}_[a-z0-9]+\b")
        for flow in self.plan["flows"]:
            for token in set(pattern.findall(json.dumps(flow))):
                if token in self.columns or token in self.tables:
                    continue
                if token in self.entity_sets:
                    continue
                # A lookup's raw value is exposed as _<name>_value, and the
                # connection reference name is not a column.
                if token.endswith("sharedcommondataserviceforapps"):
                    continue
                unknown.append(f"{flow['_name']}: {token}")
        self.assertEqual(sorted(set(unknown)), [])

    def test_every_option_set_code_matches_the_schema(self):
        """A hard-coded code that has drifted routes a run into the wrong branch."""
        enums = self.model["enums"]
        codes = {
            "ApprovalStepStatus": {v["key"]: v["code"]
                                   for v in enums["ApprovalStepStatus"]["values"]},
            "ApprovalStatus": {v["key"]: v["code"]
                               for v in enums["ApprovalStatus"]["values"]},
            "ChecklistType": {v["key"]: v["code"]
                              for v in enums["ChecklistType"]["values"]},
        }
        generator = flowgen.Generator(self.model, PREFIX)
        for key, expected in codes["ApprovalStepStatus"].items():
            self.assertEqual(generator._step_status_code(key), expected, key)
        for key, expected in codes["ApprovalStatus"].items():
            self.assertEqual(generator._request_status_code(key), expected, key)
        for key, expected in codes["ChecklistType"].items():
            self.assertEqual(generator._checklist_type_code(key), expected, key)

    def test_the_start_flow_triggers_on_the_submitted_status(self):
        start = next(f for f in self.plan["flows"] if f["_name"].startswith("approval-start"))
        trigger = start["properties"]["definition"]["triggers"]["When_it_starts"]
        expression = trigger["inputs"]["parameters"]["subscriptionRequest/filterexpression"]
        submitted = next(v["code"] for v in self.model["enums"]
                         ["LeaveRequestStatus"]["values"] if v["key"] == "submitted")
        self.assertIn(str(submitted), expression)

    def test_the_start_flow_stops_when_no_policy_matches(self):
        """Otherwise the record sits in draft forever and nobody is told."""
        start = next(f for f in self.plan["flows"] if f["_name"].startswith("approval-start"))
        found = [a for _, _, a in self._walk(start["properties"]["definition"]["actions"])
                 if a["type"] == "Terminate"]
        self.assertTrue(found)
        self.assertEqual(found[0]["inputs"]["runStatus"], "Failed")

    def test_the_decision_flow_ends_the_run_on_a_rejection(self):
        decision = next(f for f in self.plan["flows"] if f["_name"] == "approval-decision")
        switch = decision["properties"]["definition"]["actions"]["Branch_on_the_decision"]
        rejected = next(v["code"] for v in self.model["enums"]
                        ["ApprovalStepStatus"]["values"] if v["key"] == "rejected")
        self.assertEqual(switch["cases"]["Rejected"]["case"], rejected)

    def test_the_reminder_flow_is_scheduled_not_delayed(self):
        """A flow suspended on a three-day delay breaks on an environment update."""
        reminders = next(f for f in self.plan["flows"] if f["_name"] == "approval-reminders")
        trigger = reminders["properties"]["definition"]["triggers"]["When_it_starts"]
        self.assertEqual(trigger["type"], "Recurrence")
        for _, _, action in self._walk(
                reminders["properties"]["definition"]["actions"]):
            self.assertNotEqual(action["type"], "Wait")

    def test_the_child_flows_are_listed_rather_than_implied(self):
        names = {c["name"] for c in self.plan["childFlows"]}
        self.assertIn("approval-resolve-approver", names)
        for child in self.plan["childFlows"]:
            self.assertTrue(child["why"], child["name"])

    def test_the_resolver_child_flow_covers_every_approver_type(self):
        """The rules live in approvals.py, and the flow defers to them."""
        resolver = next(c for c in self.plan["childFlows"]
                        if c["name"] == "approval-resolve-approver")
        self.assertIn("approvals.py", resolver["why"])
        self.assertEqual(len(approvals.APPROVER_TYPES),
                         self.plan["counts"]["approverTypes"])


if __name__ == "__main__":
    unittest.main()
