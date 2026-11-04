#!/usr/bin/env python3
"""Generate Power Automate flow definitions for the shared approval engine.

The rules live in `tools/approvals.py`, which is tested; this module turns them
into the cloud-flow definitions a Dataverse solution carries, so the flow and
the pro-code API cannot drift apart.

Three flows, because each has a different trigger and they fail independently:

    approval-start      a record is submitted  ->  match a policy, create the
                        ApprovalRequest and its first ApprovalStep rows
    approval-decision   an ApprovalStep is decided  ->  evaluate the completion
                        rule, open the next step or finish the run, write back
    approval-reminders  a schedule  ->  remind and escalate overdue steps

Why a schedule rather than a delay inside the run: a flow suspended on a
three-day delay is a flow that breaks when the environment is updated, and its
state is invisible until it wakes. A nightly sweep over ApprovalStep is
restartable and queryable.

Usage
    python tools/flowgen.py --profile hr-starter --prefix wga
    python tools/flowgen.py --profile hr-starter --prefix wga --solution WoodgroveHR
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import approvals  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"

NAMESPACE = uuid.UUID("3a1f2c84-6b7d-4e55-9a0c-2f8b1d6e4a73")
DEFINITION_SCHEMA = ("https://schema.management.azure.com/providers/Microsoft.Logic/"
                     "schemas/2016-06-01/workflowdefinition.json#")
DATAVERSE_CONNECTOR = "shared_commondataserviceforapps"

# The entities the approval engine serves. Each needs a submit trigger.
APPROVABLE = {
    "LeaveRequest": {"status_column": "status", "submitted": "submitted",
                     "approved": "approved", "rejected": "rejected",
                     "amount_column": None},
    "Compensation": {"status_column": None, "submitted": None,
                     "approved": None, "rejected": None,
                     "amount_column": "amount"},
    "JobRequisition": {"status_column": "status", "submitted": "pendingApproval",
                       "approved": "open", "rejected": "draft",
                       "amount_column": None},
}


def flow_id(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, name))


def logical(prefix: str, name: str) -> str:
    return f"{prefix}_{re.sub(r'[^0-9a-zA-Z]', '', name).lower()}"


def plural(prefix: str, name: str) -> str:
    """The Dataverse entity set name, which is the plural of the logical name.

    Dataverse applies this rule itself when a table is created, and every Web
    API path and flow action uses the result, so getting it wrong produces a
    flow that imports cleanly and then cannot find the table.
    """
    base = logical(prefix, name)
    if base.endswith("y") and not base.endswith(("ay", "ey", "iy", "oy", "uy")):
        return base[:-1] + "ies"
    if base.endswith(("s", "x", "z", "ch", "sh")):
        return base + "es"
    return base + "s"


class FlowError(Exception):
    pass


class Generator:
    def __init__(self, model: dict[str, Any], prefix: str, solution: str | None = None):
        self.model = model
        self.prefix = prefix.lower()
        self.solution = solution
        self.entities: dict[str, Any] = model.get("entities", {})
        self.enums: dict[str, Any] = model.get("enums", {})
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.approvable = {name: spec for name, spec in APPROVABLE.items()
                           if name in self.entities}
        self._check()

    def _check(self) -> None:
        for name in APPROVABLE:
            if name not in self.entities:
                self.notes.append(f"{name} is not in this profile, so it gets no trigger")
        for name, spec in self.approvable.items():
            attributes = self.entities[name]["attributes"]
            if "approvalRequest" not in attributes:
                self.errors.append(
                    f"{name} has no approvalRequest column, so the flow cannot "
                    "write the outcome back")
            for column in filter(None, (spec["status_column"], spec["amount_column"])):
                if column not in attributes:
                    self.errors.append(f"{name} has no '{column}' column")
            if spec["status_column"]:
                enum = attributes[spec["status_column"]].get("enum")
                keys = {v["key"] for v in (self.enums.get(enum) or {}).get("values", [])}
                for key in filter(None, (spec["submitted"], spec["approved"],
                                         spec["rejected"])):
                    if key not in keys:
                        self.errors.append(
                            f"{name}.{spec['status_column']}: '{key}' is not a value "
                            f"of {enum}")

        for table in ("ApprovalPolicy", "ApprovalPolicyStep", "ApprovalRequest",
                      "ApprovalStep"):
            if table not in self.entities:
                self.errors.append(f"{table} is missing: the engine cannot run without it")

    # -- expressions ---------------------------------------------------------
    def _status_code(self, entity: str, column: str, key: str) -> int | None:
        enum = self.entities[entity]["attributes"][column].get("enum")
        for value in (self.enums.get(enum) or {}).get("values", []):
            if value.get("key") == key:
                return value.get("code")
        return None

    def _step_status_code(self, key: str) -> int | None:
        for value in (self.enums.get("ApprovalStepStatus") or {}).get("values", []):
            if value.get("key") == key:
                return value.get("code")
        return None

    def _request_status_code(self, key: str) -> int | None:
        for value in (self.enums.get("ApprovalStatus") or {}).get("values", []):
            if value.get("key") == key:
                return value.get("code")
        return None

    def _approver_type_code(self, key: str) -> int | None:
        for value in (self.enums.get("ApproverType") or {}).get("values", []):
            if value.get("key") == key:
                return value.get("code")
        return None

    # -- the flows -----------------------------------------------------------
    def flow_start(self) -> dict[str, Any]:
        """A record is submitted: match a policy and open the first step."""
        entity = next(iter(self.approvable), None)
        if entity is None:
            raise FlowError("no approvable entity in this profile")
        spec = self.approvable[entity]
        table = plural(self.prefix, entity)
        status = logical(self.prefix, spec["status_column"]) if spec["status_column"] else None
        submitted_code = (self._status_code(entity, spec["status_column"], spec["submitted"])
                          if spec["status_column"] else None)

        actions: dict[str, Any] = {}
        order: list[str] = []

        def add(name: str, body: dict[str, Any], after: str | None = None) -> str:
            if after:
                body["runAfter"] = {after: ["Succeeded"]}
            elif order:
                body["runAfter"] = {order[-1]: ["Succeeded"]}
            else:
                body["runAfter"] = {}
            actions[name] = body
            order.append(name)
            return name

        add("Get_the_submitter", self._dataverse_get(
            plural(self.prefix, "Employee"),
            f"@{{triggerOutputs()?['body/_ownerid_value']}}",
            select=["employeenumber", "orgunit", "statecode"],
            comment="The requester's directory record, for the approver rules."))

        add("List_matching_policies", self._dataverse_list(
            plural(self.prefix, "ApprovalPolicy"),
            filter_expression=(
                f"{logical(self.prefix, 'appliestoentity')} eq "
                f"'{logical(self.prefix, entity)}'"),
            order_by=f"{logical(self.prefix, 'evaluationorder')} asc,"
                     f"{logical(self.prefix, 'code')} asc",
            comment="Lowest evaluationOrder wins, code breaks a tie, so the choice "
                    "is the same every run. See tools/approvals.py match_policy."))

        add("Pick_the_policy", {
            "type": "Compose",
            "description": "The first policy whose legal entity, org unit and amount "
                           "band fit the record. The filter is applied here rather "
                           "than in the query because an amount band needs the "
                           "record's own amount.",
            "inputs": "@first(body('List_matching_policies')?['value'])",
        })

        add("Fail_when_no_policy_matches", {
            "type": "If",
            "description": "A record with no policy would sit in draft forever, so "
                           "stop loudly. Keep a catch-all policy with no filters and "
                           "the highest evaluationOrder to make this unreachable.",
            "expression": {"equals": [
                "@empty(outputs('Pick_the_policy'))", True]},
            "actions": {
                "Terminate_with_no_policy": {
                    "type": "Terminate",
                    "inputs": {"runStatus": "Failed", "runError": {
                        "code": "NoApprovalPolicy",
                        "message": f"No ApprovalPolicy matches "
                                   f"{logical(self.prefix, entity)}. Add a catch-all "
                                   "policy with no filters.",
                    }},
                }
            },
        })

        add("Create_the_approval_request", self._dataverse_create(
            plural(self.prefix, "ApprovalRequest"),
            {
                logical(self.prefix, "subject"): "@{triggerOutputs()?['body/"
                                                 f"{logical(self.prefix, 'name')}']}}",
                logical(self.prefix, "regardingtable"): logical(self.prefix, entity),
                logical(self.prefix, "regardingid"):
                    "@{triggerOutputs()?['body/" + logical(self.prefix, entity) + "id']}",
                logical(self.prefix, "status"): self._request_status_code("pending"),
                logical(self.prefix, "submittedon"): "@{utcNow()}",
                logical(self.prefix, "currentstep"): 1,
                f"{logical(self.prefix, 'policy')}@odata.bind":
                    f"/{plural(self.prefix, 'ApprovalPolicy')}("
                    "@{outputs('Pick_the_policy')?["
                    f"'{logical(self.prefix, 'approvalpolicyid')}']}})",
            },
            comment="One run per record. The record keeps a lookup to its latest "
                    "request, set at the end."))

        add("List_the_policy_steps", self._dataverse_list(
            plural(self.prefix, "ApprovalPolicyStep"),
            filter_expression="_" + logical(self.prefix, "policy") + "_value eq "
                              "@{outputs('Pick_the_policy')?['"
                              + logical(self.prefix, "approvalpolicyid") + "']}",
            order_by=f"{logical(self.prefix, 'sequence')} asc",
            comment="In sequence. A step whose minAmount is above the record's "
                    "amount is skipped by the condition below."))

        add("For_each_policy_step", {
            "type": "Foreach",
            "description": "Create an ApprovalStep per policy step. The first is "
                           "pending; the rest wait. Approver resolution is the "
                           "child flow, which carries the rules from "
                           "tools/approvals.py so the logic is in one place.",
            "foreach": "@body('List_the_policy_steps')?['value']",
            "actions": {
                "Resolve_the_approver": {
                    "type": "Workflow",
                    "description": "Child flow: approval-resolve-approver. Takes the "
                                   "policy step and the requester, returns the "
                                   "approvers and the reason.",
                    "inputs": {
                        "host": {"workflowReferenceName": "approval-resolve-approver"},
                        "body": {
                            "approverType": "@items('For_each_policy_step')?['"
                                            + logical(self.prefix, "approvertype") + "']",
                            "requester": "@body('Get_the_submitter')?['"
                                         + logical(self.prefix, "employeenumber") + "']",
                            "policyStepId": "@items('For_each_policy_step')?['"
                                            + logical(self.prefix, "approvalpolicystepid")
                                            + "']",
                        },
                    },
                    "runAfter": {},
                },
                "Create_the_step": self._dataverse_create(
                    plural(self.prefix, "ApprovalStep"),
                    {
                        logical(self.prefix, "name"):
                            "@{items('For_each_policy_step')?['"
                            + logical(self.prefix, "name") + "']}",
                        logical(self.prefix, "sequence"):
                            "@items('For_each_policy_step')?['"
                            + logical(self.prefix, "sequence") + "']",
                        logical(self.prefix, "status"):
                            "@if(equals(items('For_each_policy_step')?['"
                            + logical(self.prefix, "sequence") + "'], 1), "
                            f"{self._step_status_code('pending')}, "
                            f"{self._step_status_code('waiting')})",
                        logical(self.prefix, "assignedon"): "@{utcNow()}",
                        logical(self.prefix, "comments"):
                            "@{body('Resolve_the_approver')?['reason']}",
                        f"{logical(self.prefix, 'request')}@odata.bind":
                            f"/{plural(self.prefix, 'ApprovalRequest')}("
                            "@{outputs('Create_the_approval_request')?['body/"
                            + logical(self.prefix, "approvalrequestid") + "']})",
                    },
                    comment="The step's comments carry the resolution reason, so an "
                            "auditor can see why this person was asked.",
                    after="Resolve_the_approver"),
            },
        })

        add("Link_the_record_to_its_request", self._dataverse_update(
            table,
            "@{triggerOutputs()?['body/" + logical(self.prefix, entity) + "id']}",
            {
                f"{logical(self.prefix, 'approvalrequest')}@odata.bind":
                    f"/{plural(self.prefix, 'ApprovalRequest')}("
                    "@{outputs('Create_the_approval_request')?['body/"
                    + logical(self.prefix, "approvalrequestid") + "']})",
            },
            comment="So the record shows its latest run without a query."))

        trigger_filter = (f"{status} eq {submitted_code}" if status else None)
        return self._wrap(
            name=f"approval-start-{logical(self.prefix, entity)}",
            description=f"When a {entity} is submitted, match an ApprovalPolicy and "
                        "open its first step.",
            trigger=self._dataverse_trigger(table, "Update", trigger_filter),
            actions=actions,
        )

    def flow_decision(self) -> dict[str, Any]:
        """An ApprovalStep is decided: evaluate, advance or finish."""
        actions: dict[str, Any] = {}

        actions["Get_the_run"] = self._dataverse_get(
            plural(self.prefix, "ApprovalRequest"),
            "@{triggerOutputs()?['body/_" + logical(self.prefix, 'request') + "_value']}",
            comment="The run this step belongs to.")
        actions["Get_the_run"]["runAfter"] = {}

        actions["List_the_steps_of_this_run"] = self._dataverse_list(
            plural(self.prefix, "ApprovalStep"),
            filter_expression="_" + logical(self.prefix, "request") + "_value eq "
                              "@{triggerOutputs()?['body/_"
                              + logical(self.prefix, 'request') + "_value']}",
            order_by=f"{logical(self.prefix, 'sequence')} asc",
            comment="Every step, so the completion rule and the next sequence can "
                    "both be worked out in one pass.")
        actions["List_the_steps_of_this_run"]["runAfter"] = {"Get_the_run": ["Succeeded"]}

        rejected = self._step_status_code("rejected")
        approved = self._step_status_code("approved")
        waiting = self._step_status_code("waiting")
        pending = self._step_status_code("pending")

        actions["Branch_on_the_decision"] = {
            "type": "Switch",
            "description": "A rejection ends the run: an approval chain is a series "
                           "of gates, not a vote. An approval either completes the "
                           "step or waits for the other assignees, depending on the "
                           "completion rule.",
            "expression": "@triggerOutputs()?['body/" + logical(self.prefix, "status") + "']",
            "cases": {
                "Rejected": {
                    "case": rejected,
                    "actions": {
                        "Close_the_run_as_rejected": self._dataverse_update(
                            plural(self.prefix, "ApprovalRequest"),
                            "@{triggerOutputs()?['body/_"
                            + logical(self.prefix, 'request') + "_value']}",
                            {
                                logical(self.prefix, "status"):
                                    self._request_status_code("rejected"),
                                logical(self.prefix, "completedon"): "@{utcNow()}",
                                logical(self.prefix, "outcomecomments"):
                                    "@{triggerOutputs()?['body/"
                                    + logical(self.prefix, "comments") + "']}",
                            },
                            comment="No further steps are opened."),
                    },
                },
                "Approved": {
                    "case": approved,
                    "actions": {
                        "Is_the_step_complete": {
                            "type": "If",
                            "description": "anyApprover needs one approval; "
                                           "allApprovers needs every assignee. The "
                                           "rule is on the policy step, and the same "
                                           "rule is tested in tools/approvals.py "
                                           "is_complete.",
                            "expression": {"equals": [
                                "@length(filter(body('List_the_steps_of_this_run')?['value'], "
                                "item()?['" + logical(self.prefix, "sequence") + "'] "
                                "== triggerOutputs()?['body/"
                                + logical(self.prefix, "sequence") + "'] && "
                                "item()?['" + logical(self.prefix, "status") + "'] != "
                                f"{approved}))", 0]},
                            "actions": {
                                "Open_the_next_step": self._next_step_scope(
                                    waiting, pending),
                            },
                            "runAfter": {},
                        },
                    },
                },
            },
            "default": {"actions": {}},
            "runAfter": {"List_the_steps_of_this_run": ["Succeeded"]},
        }

        return self._wrap(
            name="approval-decision",
            description="When an ApprovalStep is decided, evaluate the completion "
                        "rule and either open the next step or close the run.",
            trigger=self._dataverse_trigger(
                plural(self.prefix, "ApprovalStep"), "Update",
                f"{logical(self.prefix, 'status')} ne {pending}"),
            actions=actions,
        )

    def _next_step_scope(self, waiting: int, pending: int) -> dict[str, Any]:
        return {
            "type": "Scope",
            "description": "The lowest waiting sequence becomes pending. If there is "
                           "none, every gate has passed and the run is approved.",
            "actions": {
                "Find_the_next_waiting_step": {
                    "type": "Compose",
                    "inputs": "@first(filter(body('List_the_steps_of_this_run')?['value'], "
                              "item()?['" + logical(self.prefix, "status") + "'] == "
                              f"{waiting}))",
                    "runAfter": {},
                },
                "Open_it_or_finish": {
                    "type": "If",
                    "expression": {"equals": [
                        "@empty(outputs('Find_the_next_waiting_step'))", True]},
                    "actions": {
                        "Close_the_run_as_approved": self._dataverse_update(
                            plural(self.prefix, "ApprovalRequest"),
                            "@{triggerOutputs()?['body/_"
                            + logical(self.prefix, 'request') + "_value']}",
                            {
                                logical(self.prefix, "status"):
                                    self._request_status_code("approved"),
                                logical(self.prefix, "completedon"): "@{utcNow()}",
                            },
                            comment="Then the write-back flow updates the record."),
                    },
                    "else": {
                        "actions": {
                            "Make_the_next_step_pending": self._dataverse_update(
                                plural(self.prefix, "ApprovalStep"),
                                "@{outputs('Find_the_next_waiting_step')?['"
                                + logical(self.prefix, "approvalstepid") + "']}",
                                {
                                    logical(self.prefix, "status"): pending,
                                    logical(self.prefix, "assignedon"): "@{utcNow()}",
                                },
                                comment="assignedOn is set here, which is what the "
                                        "Avg Leave Approval Hours measure reads."),
                        },
                    },
                    "runAfter": {"Find_the_next_waiting_step": ["Succeeded"]},
                },
            },
            "runAfter": {},
        }

    def flow_reminders(self) -> dict[str, Any]:
        """A nightly sweep: remind, then escalate."""
        pending = self._step_status_code("pending")
        expired = self._step_status_code("expired")
        return self._wrap(
            name="approval-reminders",
            description="Remind approvers and escalate overdue steps. A schedule "
                        "rather than a delay inside each run, because a flow "
                        "suspended for three days breaks when the environment is "
                        "updated and its state is invisible until it wakes.",
            trigger={
                "type": "Recurrence",
                "recurrence": {"frequency": "Day", "interval": 1,
                               "schedule": {"hours": ["7"], "minutes": [0]},
                               "timeZone": "GMT Standard Time"},
            },
            actions={
                "List_pending_steps": {
                    **self._dataverse_list(
                        plural(self.prefix, "ApprovalStep"),
                        filter_expression=f"{logical(self.prefix, 'status')} eq {pending}",
                        order_by=f"{logical(self.prefix, 'dueon')} asc",
                        comment="Pending only. The sweep is restartable, so a failed "
                                "night is simply picked up the next one."),
                    "runAfter": {},
                },
                "For_each_pending_step": {
                    "type": "Foreach",
                    "foreach": "@body('List_pending_steps')?['value']",
                    "description": "Remind once the reminder time has passed, and "
                                   "escalate once the due date has.",
                    "actions": {
                        "Overdue_or_due_a_reminder": {
                            "type": "Switch",
                            "expression": "@if(less(item()?['"
                                          + logical(self.prefix, "dueon")
                                          + "'], utcNow()), 'overdue', 'reminder')",
                            "cases": {
                                "Overdue": {
                                    "case": "overdue",
                                    "actions": {
                                        "Escalate_to_the_approvers_manager": {
                                            "type": "Workflow",
                                            "description": "Child flow: reassign to "
                                                           "the approver's manager and "
                                                           "note why on the step.",
                                            "inputs": {
                                                "host": {"workflowReferenceName":
                                                         "approval-escalate"},
                                                "body": {"stepId": "@item()?['"
                                                         + logical(self.prefix,
                                                                   "approvalstepid")
                                                         + "']"},
                                            },
                                            "runAfter": {},
                                        },
                                    },
                                },
                                "Reminder": {
                                    "case": "reminder",
                                    "actions": {
                                        "Send_a_reminder": {
                                            "type": "Workflow",
                                            "description": "Child flow: notify the "
                                                           "assignee in Teams.",
                                            "inputs": {
                                                "host": {"workflowReferenceName":
                                                         "approval-notify"},
                                                "body": {"stepId": "@item()?['"
                                                         + logical(self.prefix,
                                                                   "approvalstepid")
                                                         + "']"},
                                            },
                                            "runAfter": {},
                                        },
                                    },
                                },
                            },
                            "default": {"actions": {}},
                            "runAfter": {},
                        },
                    },
                    "runAfter": {"List_pending_steps": ["Succeeded"]},
                },
                "Expire_steps_past_their_timeout": {
                    "type": "Scope",
                    "description": f"A step left far past its due date becomes "
                                   f"{expired} so the dashboard stops counting it as "
                                   "pending work somebody will do.",
                    "actions": {},
                    "runAfter": {"For_each_pending_step": ["Succeeded"]},
                },
            },
        )

    def flow_onboarding(self) -> dict[str, Any]:
        """An Employment row with a future start date: build the checklist."""
        if "EmployeeChecklist" not in self.entities:
            raise FlowError("EmployeeChecklist is not in this profile")
        return self._wrap(
            name="onboarding-checklist",
            description="When an Employment row is created, instantiate the onboarding "
                        "checklist. Triggered on creation rather than on the start "
                        "date, because laptops take time.",
            trigger=self._dataverse_trigger(plural(self.prefix, "Employment"), "Create"),
            actions={
                "Find_the_onboarding_template": {
                    **self._dataverse_list(
                        plural(self.prefix, "ChecklistTemplate"),
                        filter_expression=f"{logical(self.prefix, 'checklisttype')} eq "
                                          f"{self._checklist_type_code('onboarding')}",
                        comment="One template per checklist type and legal entity."),
                    "runAfter": {},
                },
                "Create_the_employee_checklist": {
                    **self._dataverse_create(
                        plural(self.prefix, "EmployeeChecklist"),
                        {
                            logical(self.prefix, "name"):
                                "@{triggerOutputs()?['body/"
                                + logical(self.prefix, "name") + "']}",
                            logical(self.prefix, "anchordate"):
                                "@{triggerOutputs()?['body/"
                                + logical(self.prefix, "startdate") + "']}",
                            logical(self.prefix, "checklisttype"):
                                self._checklist_type_code("onboarding"),
                        },
                        comment="anchorDate is the start date; every task's due date "
                                "is an offset from it."),
                    "runAfter": {"Find_the_onboarding_template": ["Succeeded"]},
                },
                "For_each_template_task": {
                    "type": "Foreach",
                    "description": "One EmployeeChecklistTask per template task, with "
                                   "its due date offset from the anchor and its "
                                   "assignee resolved from assigneeRole — the same "
                                   "resolution the approval engine uses.",
                    "foreach": "@body('List_the_template_tasks')?['value']",
                    "actions": {},
                    "runAfter": {"List_the_template_tasks": ["Succeeded"]},
                },
                "List_the_template_tasks": {
                    **self._dataverse_list(
                        plural(self.prefix, "ChecklistTemplateTask"),
                        order_by=f"{logical(self.prefix, 'sequence')} asc",
                        comment="In sequence, so the checklist reads in order."),
                    "runAfter": {"Create_the_employee_checklist": ["Succeeded"]},
                },
            },
        )

    def _checklist_type_code(self, key: str) -> int | None:
        for value in (self.enums.get("ChecklistType") or {}).get("values", []):
            if value.get("key") == key:
                return value.get("code")
        return None

    # -- Dataverse action shapes --------------------------------------------
    def _dataverse_trigger(self, table: str, message: str,
                           filter_expression: str | None = None) -> dict[str, Any]:
        trigger: dict[str, Any] = {
            "type": "OpenApiConnectionWebhook",
            "inputs": {
                "host": {
                    "connectionName": "shared_commondataserviceforapps",
                    "operationId": "SubscribeWebhookTrigger",
                    "apiId": f"/providers/Microsoft.PowerApps/apis/{DATAVERSE_CONNECTOR}",
                },
                "parameters": {
                    "subscriptionRequest/message": {"Create": 1, "Update": 2,
                                                    "Delete": 3}[message],
                    "subscriptionRequest/entityname": table,
                    "subscriptionRequest/scope": 4,
                },
                "authentication": "@parameters('$authentication')",
            },
        }
        if filter_expression:
            trigger["inputs"]["parameters"]["subscriptionRequest/filterexpression"] = \
                filter_expression
        return trigger

    def _dataverse_get(self, table: str, row_id: str, select: list[str] | None = None,
                       comment: str = "") -> dict[str, Any]:
        parameters = {"entityName": table, "recordId": row_id}
        if select:
            parameters["$select"] = ",".join(select)
        return {
            "type": "OpenApiConnection",
            "description": comment,
            "inputs": {
                "host": {"connectionName": "shared_commondataserviceforapps",
                         "operationId": "GetItem",
                         "apiId": f"/providers/Microsoft.PowerApps/apis/"
                                  f"{DATAVERSE_CONNECTOR}"},
                "parameters": parameters,
                "authentication": "@parameters('$authentication')",
            },
        }

    def _dataverse_list(self, table: str, filter_expression: str | None = None,
                        order_by: str | None = None, top: int | None = None,
                        comment: str = "") -> dict[str, Any]:
        parameters: dict[str, Any] = {"entityName": table}
        if filter_expression:
            parameters["$filter"] = filter_expression
        if order_by:
            parameters["$orderby"] = order_by
        if top:
            parameters["$top"] = top
        return {
            "type": "OpenApiConnection",
            "description": comment,
            "inputs": {
                "host": {"connectionName": "shared_commondataserviceforapps",
                         "operationId": "ListRecords",
                         "apiId": f"/providers/Microsoft.PowerApps/apis/"
                                  f"{DATAVERSE_CONNECTOR}"},
                "parameters": parameters,
                "authentication": "@parameters('$authentication')",
            },
        }

    def _dataverse_create(self, table: str, item: dict[str, Any],
                          comment: str = "", after: str | None = None) -> dict[str, Any]:
        action = {
            "type": "OpenApiConnection",
            "description": comment,
            "inputs": {
                "host": {"connectionName": "shared_commondataserviceforapps",
                         "operationId": "CreateRecord",
                         "apiId": f"/providers/Microsoft.PowerApps/apis/"
                                  f"{DATAVERSE_CONNECTOR}"},
                "parameters": {"entityName": table, "item": item},
                "authentication": "@parameters('$authentication')",
            },
        }
        if after:
            action["runAfter"] = {after: ["Succeeded"]}
        return action

    def _dataverse_update(self, table: str, row_id: str, item: dict[str, Any],
                          comment: str = "") -> dict[str, Any]:
        return {
            "type": "OpenApiConnection",
            "description": comment,
            "inputs": {
                "host": {"connectionName": "shared_commondataserviceforapps",
                         "operationId": "UpdateRecord",
                         "apiId": f"/providers/Microsoft.PowerApps/apis/"
                                  f"{DATAVERSE_CONNECTOR}"},
                "parameters": {"entityName": table, "recordId": row_id, "item": item},
                "authentication": "@parameters('$authentication')",
            },
            "runAfter": {},
        }

    def _wrap(self, name: str, description: str, trigger: dict[str, Any],
              actions: dict[str, Any]) -> dict[str, Any]:
        return {
            "_name": name,
            "_id": flow_id(name),
            "properties": {
                "displayName": name,
                "description": " ".join(description.split()),
                "connectionReferences": {
                    "shared_commondataserviceforapps": {
                        "runtimeSource": "embedded",
                        "connection": {
                            "connectionReferenceLogicalName":
                                logical(self.prefix, "sharedcommondataserviceforapps"),
                        },
                        "api": {"name": DATAVERSE_CONNECTOR},
                    },
                },
                "definition": {
                    "$schema": DEFINITION_SCHEMA,
                    "contentVersion": "1.0.0.0",
                    "parameters": {
                        "$connections": {"defaultValue": {}, "type": "Object"},
                        "$authentication": {"defaultValue": {}, "type": "SecureObject"},
                    },
                    "triggers": {"When_it_starts": trigger},
                    "actions": actions,
                },
            },
            "schemaVersion": "1.0.0.0",
        }

    def build(self) -> dict[str, Any]:
        flows: list[dict[str, Any]] = []
        if not self.errors:
            for builder in (self.flow_start, self.flow_decision, self.flow_reminders,
                            self.flow_onboarding):
                try:
                    flows.append(builder())
                except FlowError as exc:
                    self.warnings.append(str(exc))
        return {
            "profile": self.model.get("profile"),
            "prefix": self.prefix,
            "solution": self.solution,
            "counts": {
                "flows": len(flows),
                "approvableEntities": len(self.approvable),
                "approverTypes": len(approvals.APPROVER_TYPES),
            },
            "flows": flows,
            "childFlows": [
                {"name": "approval-resolve-approver",
                 "why": "the seven approverType rules, in one place; "
                        "tools/approvals.py resolve_approvers is the tested spec"},
                {"name": "approval-notify",
                 "why": "an adaptive card in Teams for one step"},
                {"name": "approval-escalate",
                 "why": "reassign an overdue step to the approver's manager"},
                {"name": "approval-writeback",
                 "why": "set the record's own status and adjust LeaveBalance"},
            ],
            "notes": self.notes,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate Power Automate flows for the approval engine.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--solution", help="solution unique name the flows belong to")
    parser.add_argument("--model", help="resolved model JSON (default: build/<profile>.model.json)")
    parser.add_argument("--out", help="output directory (default: build/flows/<profile>)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args(argv)

    if not re.fullmatch(r"[a-z][a-z0-9]{1,7}", args.prefix.lower()):
        print(f"error: prefix '{args.prefix}' must be 2-8 characters, lowercase letters "
              "and digits, starting with a letter", file=sys.stderr)
        return 2

    model_file = Path(args.model) if args.model else BUILD_DIR / f"{args.profile}.model.json"
    if not model_file.exists():
        print(f"error: {model_file} not found — run: python tools/schematool.py export "
              f"--profile {args.profile}", file=sys.stderr)
        return 2

    model = json.loads(model_file.read_text(encoding="utf-8"))
    generator = Generator(model, args.prefix, args.solution)
    plan = generator.build()

    out_dir = Path(args.out) if args.out else BUILD_DIR / "flows" / args.profile
    if not plan["errors"]:
        (out_dir / "Workflows").mkdir(parents=True, exist_ok=True)
        for flow in plan["flows"]:
            name = f"{flow['_name']}-{flow['_id'][:8].upper()}"
            (out_dir / "Workflows" / f"{name}.json").write_text(
                json.dumps({k: v for k, v in flow.items() if not k.startswith("_")},
                           indent=2), encoding="utf-8")
        (out_dir / "manifest.json").write_text(json.dumps(
            {k: v for k, v in plan.items() if k != "flows"}
            | {"flows": [f["_name"] for f in plan["flows"]]}, indent=2), encoding="utf-8")
        counts = plan["counts"]
        print(f"Wrote {out_dir}")
        print(f"  {counts['flows']} flow(s) for {counts['approvableEntities']} "
              "approvable entit(ies)")
        print(f"  {len(plan['childFlows'])} child flow(s) still to build — "
              "see manifest.json")
    for note in plan["notes"]:
        print(f"  note: {note}")
    for warning in plan["warnings"]:
        print(f"  warning: {warning}")
    for error in plan["errors"]:
        print(f"  ERROR: {error}")

    if plan["errors"]:
        return 1
    if args.strict and plan["warnings"]:
        print("\nfailing because --strict was given and there are warnings")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        os._exit(0)
