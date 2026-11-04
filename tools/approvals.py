#!/usr/bin/env python3
"""The shared approval engine, as a reference implementation.

The roadmap says to build the approval engine once, because travel, expenses,
timesheets, leave and pay changes all reuse it. This module is that engine's
rules in executable form:

  · which policy applies to a record (entity, legal entity, org unit, amount)
  · who the approvers of a step are, for every approverType the schema defines
  · what the steps of a run look like, in order
  · whether a run is approved, rejected or still waiting
  · when a step is due, reminded and escalated

It exists as code rather than only as a flow because three places need the same
rules and must not diverge: the Power Automate flow that Phase 1 builds
(tools/flowgen.py generates it from here), the pro-code API in Phase 3, and the
tests. Getting the resolution rules wrong is silent — an approval lands with the
wrong person and nobody notices until an audit — so they are tested here instead
of being retyped into a designer.

The engine is pure: it reads a Directory and returns decisions. It performs no
I/O, so it can be tested exhaustively and called from any platform.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Iterable, Protocol

# The schema's ApproverType values. Keeping them here as a constant means a new
# value added to the YAML fails the test that compares the two lists, rather
# than silently resolving to nobody.
APPROVER_TYPES = (
    "manager",
    "skipLevelManager",
    "specificEmployee",
    "orgUnitHead",
    "costCenterOwner",
    "recordField",
    "group",
)
COMPLETION_RULES = ("anyApprover", "allApprovers")

# EmploymentStatus values that mean the person cannot act.
INACTIVE_STATUSES = ("leaver",)

# How far up the chain to look before giving up, so a cycle in the reporting
# line cannot hang the engine.
MAX_CHAIN = 20


class ResolutionError(Exception):
    """The step cannot be assigned to anybody, and the caller must decide."""


@dataclass(frozen=True)
class Person:
    id: str
    status: str = "active"
    manager_id: str | None = None
    org_unit_id: str | None = None
    cost_center_id: str | None = None
    display_name: str = ""

    @property
    def active(self) -> bool:
        return self.status not in INACTIVE_STATUSES


@dataclass(frozen=True)
class PolicyStep:
    code: str
    sequence: int
    approver_type: str
    completion_rule: str = "anyApprover"
    approver_id: str | None = None          # specificEmployee
    approver_group_id: str | None = None    # group
    approver_field: str | None = None       # recordField
    min_amount: float | None = None
    timeout_hours: int | None = None
    allow_delegation: bool = False
    name: str = ""


@dataclass(frozen=True)
class Policy:
    code: str
    applies_to_entity: str
    evaluation_order: int
    steps: tuple[PolicyStep, ...] = ()
    legal_entity_id: str | None = None
    org_unit_id: str | None = None
    min_amount: float | None = None
    max_amount: float | None = None
    reminder_after_hours: int | None = None
    escalate_after_hours: int | None = None
    name: str = ""


@dataclass(frozen=True)
class Record:
    entity: str
    id: str
    requester_id: str
    amount: float | None = None
    legal_entity_id: str | None = None
    org_unit_id: str | None = None
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class Step:
    """A resolved step of a run: who it is assigned to and why."""
    policy_step: PolicyStep
    sequence: int
    approvers: list[str]
    reason: str
    status: str = "waiting"
    assigned_on: dt.datetime | None = None
    due_on: dt.datetime | None = None
    reminder_on: dt.datetime | None = None
    escalate_on: dt.datetime | None = None


class Directory(Protocol):
    """What the engine needs to know about the organisation."""

    def person(self, person_id: str) -> Person | None: ...
    def org_unit_head(self, org_unit_id: str) -> str | None: ...
    def org_unit_parent(self, org_unit_id: str) -> str | None: ...
    def cost_center_owner(self, cost_center_id: str) -> str | None: ...
    def group_members(self, group_id: str) -> list[str]: ...


# --- policy matching --------------------------------------------------------
def match_policy(policies: Iterable[Policy], record: Record) -> Policy | None:
    """The first policy that fits, in evaluation order.

    Lowest evaluation_order wins, so a specific policy is written with a low
    number and the firm-wide fallback with a high one. A policy whose
    legalEntity, orgUnit or amount band does not fit the record is skipped.
    Ties are broken by code, so the choice is deterministic rather than
    depending on the order rows came back in.
    """
    candidates = [p for p in policies
                  if p.applies_to_entity == record.entity and _fits(p, record)]
    if not candidates:
        return None
    return min(candidates, key=lambda p: (p.evaluation_order, p.code))


def _fits(policy: Policy, record: Record) -> bool:
    if policy.legal_entity_id and policy.legal_entity_id != record.legal_entity_id:
        return False
    if policy.org_unit_id and policy.org_unit_id != record.org_unit_id:
        return False
    amount = record.amount
    if policy.min_amount is not None:
        if amount is None or amount < policy.min_amount:
            return False
    if policy.max_amount is not None:
        if amount is None or amount > policy.max_amount:
            return False
    return True


# --- approver resolution ----------------------------------------------------
def resolve_approvers(step: PolicyStep, record: Record,
                      directory: Directory) -> tuple[list[str], str]:
    """Who this step is assigned to, and the sentence explaining it.

    Three cases are handled explicitly because each is a real support ticket:

      · **No manager.** The managing partner has none. Fall back to the head of
        their org unit; if that is also missing, raise rather than leaving the
        step assigned to nobody, which would stall the run forever.
      · **The approver is the requester.** A partner approving their own leave
        fails an audit. Escalate to the next person up instead of skipping
        silently, so the record still carries a decision by someone else.
      · **The approver has left.** Resolve to their manager, walking up until
        somebody active is found.
    """
    if step.approver_type not in APPROVER_TYPES:
        raise ResolutionError(
            f"approverType '{step.approver_type}' is not one of {', '.join(APPROVER_TYPES)}")

    requester = directory.person(record.requester_id)
    if requester is None:
        raise ResolutionError(f"requester {record.requester_id} is not in the directory")

    candidates, reason = _candidates(step, record, requester, directory)
    resolved: list[str] = []
    notes: list[str] = []

    for candidate in candidates:
        person = directory.person(candidate)
        if person is None:
            notes.append(f"{candidate} is not in the directory")
            continue
        if not person.active:
            replacement = _first_active_above(person, directory)
            if replacement is None:
                notes.append(f"{candidate} has left and has no active manager")
                continue
            notes.append(f"{candidate} has left, so their manager {replacement} acts")
            candidate = replacement
        if candidate == record.requester_id:
            replacement = _escalate_past_self(candidate, directory)
            if replacement is None:
                notes.append(
                    f"{candidate} is the requester and has no one above them")
                continue
            notes.append(
                f"{candidate} raised the request, so it goes up to {replacement}")
            candidate = replacement
        if candidate not in resolved:
            resolved.append(candidate)

    if not resolved:
        raise ResolutionError(
            f"step {step.code or step.sequence} ({step.approver_type}) resolved to "
            f"nobody: {'; '.join(notes) if notes else reason}")

    if notes:
        reason = f"{reason}; " + "; ".join(notes)
    return resolved, reason


def _candidates(step: PolicyStep, record: Record, requester: Person,
                directory: Directory) -> tuple[list[str], str]:
    kind = step.approver_type

    if kind == "manager":
        if requester.manager_id:
            return [requester.manager_id], "the requester's line manager"
        head = _org_unit_head_other_than(requester, directory)
        if head:
            return [head], ("the requester has no line manager, so the head of their "
                            "org unit acts")
        raise ResolutionError(
            f"{record.requester_id} has no manager and their org unit has no head")

    if kind == "skipLevelManager":
        if not requester.manager_id:
            raise ResolutionError(f"{record.requester_id} has no manager, so no skip level")
        manager = directory.person(requester.manager_id)
        if manager is None:
            raise ResolutionError(f"manager {requester.manager_id} is not in the directory")
        if manager.manager_id:
            return [manager.manager_id], "the requester's manager's manager"
        return [requester.manager_id], ("the requester's manager has no manager, so the "
                                        "line manager acts at the skip level")

    if kind == "specificEmployee":
        if not step.approver_id:
            raise ResolutionError(f"step {step.code} is specificEmployee with no approver")
        return [step.approver_id], f"the named approver {step.approver_id}"

    if kind == "orgUnitHead":
        if not requester.org_unit_id:
            raise ResolutionError(f"{record.requester_id} is in no org unit")
        head = directory.org_unit_head(requester.org_unit_id)
        if head and head != record.requester_id:
            return [head], f"the head of {requester.org_unit_id}"
        above = _org_unit_head_other_than(requester, directory)
        if above:
            return [above], (f"the requester heads {requester.org_unit_id}, so the unit "
                             "above signs it off")
        raise ResolutionError(
            f"org unit {requester.org_unit_id} has no head other than the requester")

    if kind == "costCenterOwner":
        if not requester.cost_center_id:
            raise ResolutionError(f"{record.requester_id} has no cost centre")
        owner = directory.cost_center_owner(requester.cost_center_id)
        if not owner:
            raise ResolutionError(f"cost centre {requester.cost_center_id} has no owner")
        return [owner], f"the budget owner of {requester.cost_center_id}"

    if kind == "recordField":
        if not step.approver_field:
            raise ResolutionError(f"step {step.code} is recordField with no approverField")
        value = record.fields.get(step.approver_field)
        if not value:
            raise ResolutionError(
                f"{record.entity} {record.id} has no value in {step.approver_field}")
        return [value], f"the person named in {step.approver_field}"

    # group
    if not step.approver_group_id:
        raise ResolutionError(f"step {step.code} is group with no approverGroupId")
    members = directory.group_members(step.approver_group_id)
    if not members:
        raise ResolutionError(f"group {step.approver_group_id} has no members")
    return list(members), f"any member of {step.approver_group_id}"


def _org_unit_head_other_than(person: Person, directory: Directory) -> str | None:
    """The head of the person's unit, or of the first unit above it that is not them.

    A department head has no line manager and is the head of their own unit, so
    their own leave would have nobody to approve it. Walking up the org tree
    gives the realistic answer: the division above them signs it off. Only the
    person at the very top of the tree is left without an approver, which is the
    honest state of affairs and is reported rather than guessed at.
    """
    unit = person.org_unit_id
    if not unit:
        return None
    seen: set[str] = set()
    for _ in range(MAX_CHAIN):
        if not unit or unit in seen:
            return None
        seen.add(unit)
        head = directory.org_unit_head(unit)
        if head and head != person.id:
            candidate = directory.person(head)
            if candidate is not None and candidate.active:
                return head
            if candidate is not None:
                replacement = _first_active_above(candidate, directory)
                if replacement and replacement != person.id:
                    return replacement
        unit = _parent_unit(directory, unit)
    return None


def _parent_unit(directory: Directory, org_unit_id: str) -> str | None:
    """The unit above this one, where the directory can say."""
    getter = getattr(directory, "org_unit_parent", None)
    return getter(org_unit_id) if callable(getter) else None


def _first_active_above(person: Person, directory: Directory) -> str | None:
    """Walk up the reporting line to the first person who can act."""
    seen = {person.id}
    current = person
    for _ in range(MAX_CHAIN):
        if not current.manager_id or current.manager_id in seen:
            return None
        seen.add(current.manager_id)
        candidate = directory.person(current.manager_id)
        if candidate is None:
            return None
        if candidate.active:
            return candidate.id
        current = candidate
    return None


def _escalate_past_self(person_id: str, directory: Directory) -> str | None:
    """The first active person above someone, used when they are the requester."""
    person = directory.person(person_id)
    if person is None:
        return None
    return _first_active_above(person, directory)


# --- building a run ---------------------------------------------------------
def build_steps(policy: Policy, record: Record, directory: Directory,
                submitted_on: dt.datetime | None = None) -> list[Step]:
    """The steps of one run, in sequence, with their dates.

    A step whose min_amount is above the record's amount is skipped: that is how
    a policy says "a second approval only over £10,000" without needing two
    policies. A step that resolves to nobody raises, because a run with an
    unassigned step waits forever.
    """
    submitted_on = submitted_on or dt.datetime.now(dt.timezone.utc)
    steps: list[Step] = []
    for policy_step in sorted(policy.steps, key=lambda s: s.sequence):
        if policy_step.completion_rule not in COMPLETION_RULES:
            raise ResolutionError(
                f"step {policy_step.code}: completionRule "
                f"'{policy_step.completion_rule}' is not one of "
                f"{', '.join(COMPLETION_RULES)}")
        if policy_step.min_amount is not None:
            if record.amount is None or record.amount < policy_step.min_amount:
                continue
        approvers, reason = resolve_approvers(policy_step, record, directory)
        steps.append(Step(policy_step=policy_step, sequence=policy_step.sequence,
                          approvers=approvers, reason=reason))

    if not steps:
        raise ResolutionError(
            f"policy {policy.code} produced no steps for {record.entity} {record.id}")

    for index, step in enumerate(steps):
        if index == 0:
            schedule(step, policy, submitted_on)
            step.status = "pending"
        else:
            step.status = "waiting"
    return steps


def schedule(step: Step, policy: Policy, assigned_on: dt.datetime) -> Step:
    """Set the assignment, due, reminder and escalation times for a step.

    The step's own timeout wins over the policy's, so one slow step in a fast
    chain can be given longer without changing the rest.
    """
    step.assigned_on = assigned_on
    hours = step.policy_step.timeout_hours or policy.escalate_after_hours
    if hours:
        step.due_on = assigned_on + dt.timedelta(hours=hours)
    if policy.reminder_after_hours:
        step.reminder_on = assigned_on + dt.timedelta(hours=policy.reminder_after_hours)
    if policy.escalate_after_hours:
        step.escalate_on = assigned_on + dt.timedelta(hours=policy.escalate_after_hours)
    return step


def advance(steps: list[Step], policy: Policy,
            now: dt.datetime | None = None) -> tuple[str, list[Step]]:
    """The run's status, and the steps that are now pending.

    Returns one of pending, approved or rejected. A rejection anywhere ends the
    run: an approval chain is a series of gates, not a vote.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if any(step.status == "rejected" for step in steps):
        return "rejected", []

    for step in sorted(steps, key=lambda s: s.sequence):
        if step.status in ("approved", "skipped"):
            continue
        if step.status == "rejected":
            return "rejected", []
        if step.status == "waiting":
            step.status = "pending"
            schedule(step, policy, now)
        return "pending", [s for s in steps if s.sequence == step.sequence
                           and s.status == "pending"]
    return "approved", []


def is_complete(step: Step, decisions: dict[str, str]) -> bool:
    """Whether a step's completion rule is satisfied by the decisions so far.

    `anyApprover` needs one approval; `allApprovers` needs every assignee. A
    rejection by anybody completes the step either way, because the run ends.
    """
    if any(outcome == "rejected" for outcome in decisions.values()):
        return True
    approvals = [a for a in step.approvers if decisions.get(a) == "approved"]
    if step.policy_step.completion_rule == "allApprovers":
        return len(approvals) == len(step.approvers)
    return bool(approvals)


def outcome(step: Step, decisions: dict[str, str]) -> str:
    """The step's outcome once complete: approved or rejected."""
    if any(outcome == "rejected" for outcome in decisions.values()):
        return "rejected"
    return "approved" if is_complete(step, decisions) else "pending"


def overdue(steps: Iterable[Step], now: dt.datetime | None = None) -> list[Step]:
    """Pending steps past their due date — what the dashboard counts."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return [s for s in steps
            if s.status == "pending" and s.due_on is not None and s.due_on < now]


def needs_reminder(steps: Iterable[Step], now: dt.datetime | None = None) -> list[Step]:
    now = now or dt.datetime.now(dt.timezone.utc)
    return [s for s in steps
            if s.status == "pending" and s.reminder_on is not None
            and s.reminder_on <= now]
