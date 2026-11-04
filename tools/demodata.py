#!/usr/bin/env python3
"""Generate demo data for a fictional firm, validated against the resolved model.

The firm itself is data: `data/firms/*.yaml` carries its offices, departments,
career ladder, professional credentials, courses, leave rules and onboarding
checklist, so one generator serves all five fictional brands rather than
hardcoding one. Each file is dressed for its industry, which is the point — the
compliance page looks different for a law firm (practising certificates) than
for a software company (almost none), and that is the model proving it is not
shaped only for regulated practices.

Writes one JSON file per table, with lookups given as the target row's natural
key — the same convention `schemas/*.yaml` uses for `seed:` — so the output can
be loaded by the same code path as the reference data.

Deterministic: the same --seed always produces the same firm, so a demo can be
rebuilt and screenshots still match.

Every row is checked against the model before it is written:

  · required columns are present
  · choice values are real enum keys
  · lookups resolve to a row that exists in this data or in the schema's seeds
  · dates are coherent (no assignment ending before it starts, no leave outside
    its balance period, exactly one current assignment per person)

Usage
    python tools/demodata.py --profile hr-starter
    python tools/demodata.py --profile hr-starter --people 80 --seed 7
    python tools/demodata.py --profile law-firm --firm fabrikam
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"

TODAY = dt.date(2026, 11, 4)

FIRMS_DIR = ROOT / "data" / "firms"

# Which firm each profile is dressed as, from the roadmap's demo companies.
PROFILE_FIRMS = {
    "accounting-firm": "woodgrove",
    "law-firm": "fabrikam",
    "architecture-firm": "adatum",
    "tech-company": "contoso",
    "general-firm": "northwind",
    "hr-starter": "woodgrove",
    "travel-starter": "northwind",
    "work-starter": "contoso",
    "crm-starter": "fabrikam",
    "intranet-starter": "adatum",
}


@dataclass
class FirmConfig:
    """One fictional firm, loaded from data/firms/<slug>.yaml."""
    slug: str
    profile: str
    story: str
    firm: dict[str, Any]
    offices: list[dict[str, Any]]
    departments: list[dict[str, Any]]
    ladder: list[dict[str, Any]]
    credentials: list[dict[str, Any]]
    courses: list[dict[str, Any]]
    leave: dict[str, Any]
    holiday_calendar: dict[str, Any]
    onboarding: list[dict[str, Any]]

    @property
    def name(self) -> str:
        return str(self.firm["name"])

    @property
    def top_grade(self) -> str:
        return str(self.ladder[-1]["grade"])

    @property
    def top_worker_type(self) -> str:
        """A partnership has partners at the top; a company has employees."""
        return str(self.firm.get("topWorkerType", "employee"))

    def salary_band(self, rung: dict[str, Any]) -> tuple[int, int]:
        low, high = rung["salary"]
        return int(low), int(high)


REQUIRED_FIRM_KEYS = ("name", "code", "country", "currency", "domain")
REQUIRED_SECTIONS = ("firm", "offices", "departments", "ladder", "leave",
                     "holidayCalendar", "onboarding")


def load_firm(slug: str) -> FirmConfig:
    """Read a firm file and check it before anything is generated from it."""
    path = FIRMS_DIR / f"{slug}.yaml"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in FIRMS_DIR.glob("*.yaml")))
        raise DataError(f"no firm file {path} — available: {available or 'none'}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    missing = [key for key in REQUIRED_SECTIONS if not raw.get(key)]
    if missing:
        raise DataError(f"{path.name} is missing: {', '.join(missing)}")
    absent = [key for key in REQUIRED_FIRM_KEYS if not raw["firm"].get(key)]
    if absent:
        raise DataError(f"{path.name}: firm is missing {', '.join(absent)}")

    for section, fields in (("offices", ("code", "name", "share")),
                            ("departments", ("code", "name", "unitType", "share",
                                             "family")),
                            ("ladder", ("grade", "title", "share", "salary"))):
        for index, row in enumerate(raw[section]):
            for field_name in fields:
                if row.get(field_name) is None:
                    raise DataError(
                        f"{path.name}: {section}[{index}] has no '{field_name}'")

    for section in ("offices", "departments", "ladder"):
        total = sum(float(row["share"]) for row in raw[section])
        if not 0.95 <= total <= 1.05:
            raise DataError(
                f"{path.name}: {section} shares add up to {total:.2f}, not ~1.0")

    codes = [row["code"] for row in raw["departments"]]
    if len(codes) != len(set(codes)):
        raise DataError(f"{path.name}: duplicate department codes")

    return FirmConfig(
        slug=slug,
        profile=str(raw.get("profile", "")),
        story=" ".join(str(raw.get("story", "")).split()),
        firm=raw["firm"],
        offices=raw["offices"],
        departments=raw["departments"],
        ladder=raw["ladder"],
        credentials=raw.get("credentials") or [],
        courses=raw.get("courses") or [],
        leave=raw["leave"],
        holiday_calendar=raw["holidayCalendar"],
        onboarding=raw["onboarding"],
    )




# Shared across every firm: a deliberately international mix, so a screenshot
# does not read as one country's staff list.
FIRST_NAMES = [
    "Aisha", "Alice", "Amara", "Amelia", "Anil", "Anna", "Arjun", "Beatrice", "Ben",
    "Bilal", "Caitlin", "Callum", "Carlos", "Catherine", "Chen", "Chloe", "Daniel",
    "Darren", "Deepa", "Dmitri", "Edward", "Eleanor", "Elena", "Emeka", "Emma",
    "Farah", "Fatima", "Fiona", "Gabriel", "George", "Grace", "Hannah", "Harriet",
    "Hassan", "Heather", "Hugo", "Ibrahim", "Imogen", "Isabel", "Jacob", "James",
    "Jasmine", "Joanna", "Joseph", "Karim", "Kate", "Kwame", "Laila", "Laura",
    "Leo", "Lucas", "Lucy", "Mahesh", "Maria", "Mark", "Martha", "Mei", "Michael",
    "Mohammed", "Naomi", "Natalia", "Nathan", "Niamh", "Nina", "Olivia", "Omar",
    "Oscar", "Patrick", "Paula", "Peter", "Priya", "Rachel", "Rafael", "Raj",
    "Rebecca", "Robert", "Rosa", "Ruth", "Sam", "Samira", "Sarah", "Sean", "Sofia",
    "Stefan", "Stephanie", "Tariq", "Thomas", "Tobias", "Tomas", "Victoria",
    "Wei", "William", "Yasmin", "Yuki", "Zainab", "Zoe",
]
LAST_NAMES = [
    "Abbott", "Adeyemi", "Ahmed", "Akhtar", "Almeida", "Andersson", "Bailey", "Barnes",
    "Bennett", "Bhatt", "Blackwood", "Brennan", "Carter", "Chakraborty", "Chen",
    "Clarke", "Collins", "Costa", "Dalton", "Davies", "Delacroix", "Dimitriou",
    "Donnelly", "Duarte", "Edwards", "Ellis", "Fairweather", "Ferreira", "Fitzgerald",
    "Fletcher", "Forsyth", "Gallagher", "Gardner", "Ghosh", "Gilmore", "Grant",
    "Hargreaves", "Harper", "Hayashi", "Hendricks", "Hughes", "Iqbal", "Jameson",
    "Jenkins", "Kaur", "Keller", "Khan", "Kowalski", "Lambert", "Lawson", "Lindqvist",
    "Lowry", "Mackenzie", "Mahmood", "Mansfield", "Marchetti", "Mbeki", "McAllister",
    "Mehta", "Mitchell", "Moreau", "Morrison", "Nakamura", "Novak", "Nwosu",
    "O'Brien", "Okafor", "Oyelaran", "Pattison", "Pereira", "Pettigrew", "Quinn",
    "Ramsay", "Reyes", "Richardson", "Rossi", "Sandhu", "Schneider", "Sharma",
    "Sinclair", "Sorensen", "Stanton", "Sullivan", "Tanaka", "Thornton", "Tremblay",
    "Vasquez", "Wainwright", "Walsh", "Whitfield", "Wilkinson", "Winterbourne",
    "Yildiz", "Zhang",
]


# The approverGroupId a group step carries in the demo data. A real tenant uses
# an Entra group object id; the demo resolves it to the firm's top rung.
TOP_RUNG_GROUP = "top-rung"


# Fictional client names, built from neutral parts so none reads as a real
# company. Used for the CRM pilot's accounts, leads and opposing parties.
CLIENT_PREFIXES = [
    "Northpoint", "Harrow", "Linden", "Carrick", "Brightwater", "Ashcombe",
    "Pentland", "Marlowe", "Kestrel", "Oakhaven", "Thornbury", "Varden",
    "Saltmarsh", "Draycott", "Westerly", "Fenwick", "Ravensholme", "Calderbank",
    "Quillon", "Embersea", "Highclere", "Larkmead", "Stonebridge", "Vellacourt",
]
CLIENT_SUFFIXES = [
    "Holdings", "Group", "Industries", "Partners", "Capital", "Developments",
    "Logistics", "Energy", "Foods", "Technologies", "Properties", "Shipping",
    "Pharma", "Media", "Rail", "Mining", "Insurance", "Retail",
]
CLIENT_FORMS = ["plc", "Limited", "LLP", "GmbH", "Inc."]

# What a law firm's pipeline is made of.
SERVICE_OFFERINGS = [
    {"code": "LIT_COMM", "name": "Commercial litigation", "model": "timeAndMaterials"},
    {"code": "LIT_EMP", "name": "Employment tribunal", "model": "cappedFee"},
    {"code": "COR_MA", "name": "Mergers and acquisitions", "model": "fixedFee"},
    {"code": "COR_FIN", "name": "Banking and finance", "model": "timeAndMaterials"},
    {"code": "RE_COMM", "name": "Commercial property", "model": "fixedFee"},
    {"code": "EMP_ADV", "name": "Employment advisory retainer", "model": "retainer"},
]
LEAD_SOURCES = [
    {"code": "REF_CLIENT", "name": "Client referral", "share": 0.30},
    {"code": "REF_PROF", "name": "Professional referral", "share": 0.22},
    {"code": "DIRECTORY", "name": "Legal directory", "share": 0.14},
    {"code": "EVENT", "name": "Seminar or event", "share": 0.12},
    {"code": "WEBSITE", "name": "Website enquiry", "share": 0.12},
    {"code": "PANEL", "name": "Panel or tender", "share": 0.10},
]
PIPELINE_STAGES = [
    {"code": "QUALIFY", "name": "Qualify", "sequence": 1, "probability": 10},
    {"code": "SCOPE", "name": "Scope and conflicts", "sequence": 2, "probability": 25},
    {"code": "PROPOSE", "name": "Fee proposal", "sequence": 3, "probability": 50},
    {"code": "NEGOTIATE", "name": "Negotiation", "sequence": 4, "probability": 75},
    {"code": "WON", "name": "Won", "sequence": 5, "probability": 100,
     "closed": True, "won": True},
    {"code": "LOST", "name": "Lost", "sequence": 6, "probability": 0,
     "closed": True, "won": False},
]


class DataError(Exception):
    pass


class Firm:
    """Builds one coherent firm. Every method appends to self.rows."""

    def __init__(self, model: dict[str, Any], config: FirmConfig, people: int,
                 seed: int):
        self.model = model
        self.config = config
        self.entities = model.get("entities", {})
        self.enums = model.get("enums", {})
        self.seeds = model.get("seed", {})
        self.people = people
        self.random = random.Random(seed)
        self.rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.errors: list[str] = []
        self.warnings: list[str] = []
        # Notes record a deliberate choice the generator made. Warnings are
        # things that may be wrong, so --strict fails on those and not these.
        self.notes: list[str] = []
        self.employees: list[dict[str, Any]] = []
        self._used_emails: set[str] = set()

    # -- helpers --------------------------------------------------------------
    def add(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        self.rows[table].append(row)
        return row

    def pick_weighted(self, options: list[dict[str, Any]], key: str = "share") -> dict[str, Any]:
        total = sum(o[key] for o in options)
        roll = self.random.random() * total
        for option in options:
            roll -= option[key]
            if roll <= 0:
                return option
        return options[-1]

    def date_between(self, start: dt.date, end: dt.date) -> dt.date:
        span = (end - start).days
        return start + dt.timedelta(days=self.random.randint(0, max(span, 0)))

    @staticmethod
    def iso(value: dt.date | None) -> str | None:
        return value.isoformat() if value else None

    def _email(self, first: str, last: str) -> str:
        def strip(text: str) -> str:
            plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
            return "".join(c for c in plain.lower() if c.isalpha())

        base = f"{strip(first)}.{strip(last)}"
        candidate = f"{base}@{self.config.firm['domain']}"
        suffix = 2
        while candidate in self._used_emails:
            candidate = f"{base}{suffix}@{self.config.firm['domain']}"
            suffix += 1
        self._used_emails.add(candidate)
        return candidate

    # -- the firm -------------------------------------------------------------
    def has(self, *tables: str) -> bool:
        """Whether every table a section needs is in this profile."""
        return all(table in self.entities for table in tables)

    def optional(self, table: str, **columns: Any) -> dict[str, Any]:
        """Only the columns this profile's model actually has.

        An industry overlay adds columns to a table the base layer already
        provides — legal-work puts billingPartner and clientMatterReference on
        Project — so a generator that writes them unconditionally produces rows
        that fail on a profile without that overlay.
        """
        attributes = self.entities.get(table, {}).get("attributes", {})
        return {name: value for name, value in columns.items() if name in attributes}

    def build(self) -> None:
        """Each section runs only if its tables are in the profile.

        The firm is HR-shaped, but a profile need not be: law-firm-crm has no
        leave, no performance reviews and no onboarding checklists, so those
        sections are skipped with a note rather than generating rows that point
        at tables the profile does not have.
        """
        sections = (
            ("organisation", ("LegalEntity", "OrgUnit", "Office"), self.build_org),
            ("job architecture", ("JobProfile",), self.build_job_architecture),
            ("people", ("Employee",), self.build_people),
            ("employment history", ("Employment", "JobAssignment"),
             self.build_assignments),
            ("pay", ("Compensation",), self.build_compensation),
            ("leave", ("LeaveRequest", "LeaveBalance", "LeavePolicy"),
             self.build_leave),
            ("approvals", ("ApprovalPolicy", "ApprovalPolicyStep", "ApprovalRequest",
                           "ApprovalStep"), self.build_approvals),
            ("credentials", ("CredentialType", "EmployeeCredential"),
             self.build_credentials),
            ("training", ("TrainingCourse", "TrainingRecord"), self.build_training),
            ("performance", ("ReviewCycle", "PerformanceReview"),
             self.build_performance),
            ("recruitment", ("JobRequisition", "Candidate", "JobApplication"),
             self.build_recruitment),
            ("onboarding checklists", ("ChecklistTemplate", "ChecklistTemplateTask",
                                       "EmployeeChecklist", "EmployeeChecklistTask"),
             self.build_checklists),
            ("clients and pipeline",
             ("Account", "Contact", "LeadSource", "Lead", "SalesPipeline",
              "PipelineStage", "ServiceOffering", "Opportunity", "Proposal",
              "ProposalLine", "Activity"), self.build_pipeline),
            ("intake compliance", ("ConflictCheck", "DueDiligenceCheck",
                                   "ConsentRecord"), self.build_intake_compliance),
            ("matters", ("ProjectType", "Project"), self.build_matters),
        )
        for label, tables, builder in sections:
            missing = [t for t in tables if t not in self.entities]
            if missing:
                self.notes.append(
                    f"no {label}: this profile has no {', '.join(missing)}")
                continue
            builder()

    def build_org(self) -> None:
        self.add("LegalEntity", {
            "code": self.config.firm["code"], "name": self.config.firm["name"],
            "country": self.config.firm["country"], "baseCurrency": self.config.firm["currency"],
            "registrationNumber": self.config.firm.get("registrationNumber"),
            "fiscalYearStartMonth": int(
                self.config.firm.get("fiscalYearStartMonth", 1)),
            "timeZone": self.config.firm.get("timeZone", "UTC"),
        })
        for office in self.config.offices:
            self.add("Office", {
                "code": office["code"], "name": office["name"],
                "legalEntity": self.config.firm["code"],
                "timeZone": office.get("timeZone",
                                       self.config.firm.get("timeZone", "UTC")),
                "addressLine1": office.get("line1"),
                "addressCity": office.get("city"),
                "addressPostalCode": office.get("postalCode"),
                "addressCountry": self.config.firm["country"],
            })
        self.add("OrgUnit", {
            "code": "FIRM", "name": self.config.firm["name"], "unitType": "division",
            "legalEntity": self.config.firm["code"],
        })
        for dept in self.config.departments:
            self.add("OrgUnit", {
                "code": dept["code"], "name": dept["name"], "unitType": dept["unitType"],
                "parentOrgUnit": "FIRM", "legalEntity": self.config.firm["code"],
                "defaultCostCenter": f"CC-{dept['code']}",
            })
            self.add("CostCenter", {
                "code": f"CC-{dept['code']}", "name": f"{dept['name']} cost centre",
                "legalEntity": self.config.firm["code"], "orgUnit": dept["code"],
            })
        calendar = self.config.holiday_calendar
        self.add("HolidayCalendar", {
            "code": calendar["code"], "name": calendar["name"],
            "country": self.config.firm["country"],
        })
        for holiday in calendar.get("holidays") or []:
            self.add("Holiday", {
                "name": holiday["name"], "calendar": calendar["code"],
                "holidayDate": str(holiday["date"]),
            })

    def build_job_architecture(self) -> None:
        for dept in self.config.departments:
            for rung in self.config.ladder:
                code = f"{dept['code']}-{rung['grade']}"
                self.add("JobProfile", {
                    "code": code,
                    "name": f"{rung['title']}, {dept['name']}",
                    "jobFamily": "LEADERSHIP" if rung["grade"] == "L7" else dept["family"],
                    "grade": rung["grade"],
                    "isPeopleManager": self.config.ladder.index(rung) >= 3,
                    "overtimeEligible": self.config.ladder.index(rung) <= 1,
                })

    def build_people(self) -> None:
        names: set[tuple[str, str]] = set()
        while len(names) < self.people:
            names.add((self.random.choice(FIRST_NAMES), self.random.choice(LAST_NAMES)))

        for index, (first, last) in enumerate(sorted(names), start=1):
            dept = self.pick_weighted(self.config.departments)
            rung = self.pick_weighted(self.config.ladder)
            office = self.pick_weighted(self.config.offices)
            number = f"WGA{index:04d}"
            # Seniority bounds how long ago they could have joined.
            max_years = {"L1": 3, "L2": 6, "L3": 9, "L4": 13,
                         "L5": 18, "L6": 24, "L7": 30}[rung["grade"]]
            joined = self.date_between(
                TODAY - dt.timedelta(days=int(max_years * 365.25)),
                TODAY - dt.timedelta(days=90),
            )
            left = None
            status = "active"
            # About one in nine has left, concentrated in the last two years.
            if self.random.random() < 0.11 and joined < TODAY - dt.timedelta(days=400):
                left = self.date_between(max(joined + dt.timedelta(days=200),
                                             TODAY - dt.timedelta(days=730)),
                                         TODAY - dt.timedelta(days=5))
                status = "leaver"
            elif self.random.random() < 0.04:
                status = "onLeave"

            person = {
                "employeeNumber": number,
                "firstName": first, "lastName": last,
                "displayName": f"{first} {last}",
                "workEmail": self._email(first, last),
                "workerType": (self.config.top_worker_type
                               if rung["grade"] == self.config.top_grade
                               else "employee"),
                "employmentStatus": status,
                "orgUnit": dept["code"],
                "office": office["code"],
                "preferredName": first,
                "_dept": dept, "_rung": rung, "_office": office,
                "_joined": joined, "_left": left,
            }
            self.employees.append(person)

        self._ensure_partners()

        # The most senior active partner runs the firm. Every department head
        # reports to them, so only one person in the firm has no manager —
        # which is the state the approval engine is entitled to assume.
        partners = [p for p in self.employees
                    if p["_rung"]["grade"] == "L7" and p["_left"] is None]
        if not partners:
            partners = sorted((p for p in self.employees if p["_left"] is None),
                              key=lambda p: self.config.ladder.index(p["_rung"]), reverse=True)[:1]
        self.managing_partner = partners[0]["employeeNumber"] if partners else None
        for unit in self.rows["OrgUnit"]:
            if unit["code"] == "FIRM":
                unit["head"] = self.managing_partner

        # Each department's most senior active person becomes its head.
        for dept in self.config.departments:
            members = [p for p in self.employees
                       if p["_dept"]["code"] == dept["code"] and p["_left"] is None]
            if not members:
                continue
            head = max(members, key=lambda p: self.config.ladder.index(p["_rung"]))
            for unit in self.rows["OrgUnit"]:
                if unit["code"] == dept["code"]:
                    unit["head"] = head["employeeNumber"]
            for centre in self.rows["CostCenter"]:
                if centre["orgUnit"] == dept["code"]:
                    centre["budgetOwner"] = head["employeeNumber"]
            dept["_head"] = head["employeeNumber"]

        for person in self.employees:
            # workerType follows the rung, which _ensure_partners may have raised.
            person["workerType"] = (self.config.top_worker_type
                                    if person["_rung"]["grade"] == self.config.top_grade
                                    else "employee")
            self.add("Employee", {
                k: v for k, v in person.items() if not k.startswith("_")
            })

    def _ensure_partners(self) -> None:
        """A firm this size has people at the top, whatever the random draw says.

        The top rung is a few per cent of staff, so a 64-person firm expects two
        and a single draw can easily give none. A partnership with no partners is
        wrong on its face, and either way it leaves the pay-change policy's group
        step with no members, so the floor is enforced rather than hoped for.
        """
        wanted = max(2, self.people // 25)
        top = self.config.ladder[-1]
        label = self.config.ladder[-1]["title"].lower()
        partners = [p for p in self.employees
                    if p["_rung"]["grade"] == top["grade"] and p["_left"] is None]
        if len(partners) >= wanted:
            return

        # Promote the most senior people who are still here.
        candidates = sorted(
            (p for p in self.employees
             if p["_left"] is None and p["_rung"]["grade"] != top["grade"]),
            key=lambda p: (self.config.ladder.index(p["_rung"]), p["_joined"]),
            reverse=True,
        )
        promoted = candidates[:wanted - len(partners)]
        for person in promoted:
            person["_rung"] = top
            partners.append(person)
        self.notes.append(
            f"the random draw gave {len(partners) - len(promoted)} "
            f"{label}(s); promoted {len(promoted)} to reach the floor of {wanted} "
            f"for a firm of {self.people}")

    def _manager_for(self, person: dict[str, Any]) -> str | None:
        """Someone more senior in the same department, else up the chain.

        Order: a more senior colleague in the department, then the department
        head, then the managing partner. Only the managing partner is left with
        nobody, so every other person's leave has somewhere to go.
        """
        number = person["employeeNumber"]
        candidates = [
            p for p in self.employees
            if p["_dept"]["code"] == person["_dept"]["code"]
            and p["_left"] is None
            and self.config.ladder.index(p["_rung"]) > self.config.ladder.index(person["_rung"])
        ]
        if candidates:
            return self.random.choice(candidates)["employeeNumber"]
        head = person["_dept"].get("_head")
        if head and head != number:
            return head
        managing = getattr(self, "managing_partner", None)
        return managing if managing and managing != number else None

    def build_assignments(self) -> None:
        for person in self.employees:
            number = person["employeeNumber"]
            dept, rung, office = person["_dept"], person["_rung"], person["_office"]
            joined, left = person["_joined"], person["_left"]
            employment_code = f"EMP-{number}"

            self.add("Employment", {
                "code": employment_code,
                "name": f"{person['displayName']} — {self.config.firm['name']}",
                "employee": number, "legalEntity": self.config.firm["code"],
                "employmentType": "partTime" if self.random.random() < 0.12 else "fullTime",
                "contractType": "permanent" if self.random.random() > 0.08 else "fixedTerm",
                "startDate": self.iso(joined),
                "continuousServiceDate": self.iso(joined),
                "probationEndDate": self.iso(joined + dt.timedelta(days=180)),
                "endDate": self.iso(left),
                "noticePeriodDays": 30 if self.config.ladder.index(rung) < 3 else 90,
                "terminationType": self.random.choice(
                    ["resignation", "resignation", "resignation", "redundancy", "retirement"]
                ) if left else None,
                "lastWorkingDay": self.iso(left),
                "eligibleForRehire": True if left else None,
            })

            # Assignment history: a promotion roughly every four years of service.
            service_years = ((left or TODAY) - joined).days / 365.25
            steps = min(int(service_years // 4), self.config.ladder.index(rung))
            start_rung = self.config.ladder.index(rung) - steps
            current_from = joined
            manager = self._manager_for(person)
            fte = 1.0 if self.random.random() > 0.12 else self.random.choice([0.6, 0.8, 0.8])

            for step in range(steps + 1):
                this_rung = self.config.ladder[start_rung + step]
                is_last = step == steps
                valid_to = None if is_last else current_from + dt.timedelta(
                    days=self.random.randint(1100, 1600))
                if valid_to and valid_to > (left or TODAY):
                    valid_to = None
                    is_last = True
                self.add("JobAssignment", {
                    "code": f"JA-{number}-{step + 1}",
                    "name": f"{person['displayName']} — {this_rung['title']}",
                    "employee": number, "employment": employment_code,
                    "jobProfile": f"{dept['code']}-{this_rung['grade']}",
                    "jobTitle": f"{this_rung['title']}, {dept['name']}",
                    "grade": this_rung["grade"],
                    "orgUnit": dept["code"], "office": office["code"],
                    "costCenter": f"CC-{dept['code']}",
                    "manager": manager,
                    "fte": fte, "weeklyHours": round(37.5 * fte, 1),
                    "workArrangement": self.random.choice(
                        ["hybrid", "hybrid", "hybrid", "onsite", "remote"]),
                    "changeReason": "hire" if step == 0 else "promotion",
                    "isPrimary": True,
                    "validFrom": self.iso(current_from),
                    "validTo": self.iso(valid_to) if not is_last else (
                        self.iso(left) if left else None),
                })
                if is_last:
                    break
                current_from = valid_to

    def build_compensation(self) -> None:
        for person in self.employees:
            number = person["employeeNumber"]
            low, high = person["_rung"]["salary"]
            amount = self.random.randrange(low, high, 500)
            assignments = [r for r in self.rows["JobAssignment"] if r["employee"] == number]
            for index, assignment in enumerate(assignments):
                # Earlier rows pay the earlier band; the last pays the current one.
                factor = 1.0 if index == len(assignments) - 1 else 0.78 ** (
                    len(assignments) - 1 - index)
                self.add("Compensation", {
                    "code": f"COMP-{number}-{index + 1}",
                    "name": f"{person['displayName']} — base pay",
                    "employee": number, "basis": "annual",
                    "amount": round(amount * factor, -2),
                    "currency": self.config.firm["currency"],
                    "grade": assignment["grade"],
                    "changeReason": "newHire" if index == 0 else "promotion",
                    "validFrom": assignment["validFrom"],
                    "validTo": assignment["validTo"],
                })

    def build_leave(self) -> None:
        entitlements = self.config.leave.get("entitlement") or {}
        month = int(self.config.leave.get("yearStartMonth", 1))
        year = TODAY.year if TODAY.month >= month else TODAY.year - 1
        year_start = dt.date(year, month, 1)
        year_end = dt.date(year + 1, month, 1) - dt.timedelta(days=1)
        for item in self.config.leave["mix"]:
            leave_type = item["code"]
            entitlement = entitlements.get(leave_type, 0)
            self.add("LeavePolicy", {
                "code": f"POL-{leave_type}",
                "name": f"{leave_type.title()} — {self.config.firm['name']}",
                "leaveType": leave_type, "legalEntity": self.config.firm["code"],
                "country": self.config.firm["country"],
                "entitlementPerYear": entitlement,
                "accrualFrequency": "upfrontAnnually" if leave_type == "ANNUAL" else "none",
                "maxCarryOver": 5 if leave_type == "ANNUAL" else 0,
                "carryOverExpiryMonths": 3 if leave_type == "ANNUAL" else None,
                "minNoticeDays": 14 if leave_type == "ANNUAL" else 0,
                "proRataForPartTime": True,
                "yearStartMonth": month,
                "validFrom": self.iso(year_start),
            })

        request_index = 0
        for person in self.employees:
            if person["_left"] is not None:
                continue
            number = person["employeeNumber"]
            assignment = next(
                (r for r in self.rows["JobAssignment"]
                 if r["employee"] == number and r["validTo"] is None), None)
            fte = assignment["fte"] if assignment else 1.0

            annual = float(entitlements.get("ANNUAL", 25))
            entitlement = round(annual * fte, 1)
            carried = round(self.random.choice([0, 0, 0, 1, 2, 3]) * fte, 1)
            taken = 0.0
            pending = 0.0

            # Requests across the leave year; senior people take less. Annual
            # leave is capped at the balance, because LeaveType.ANNUAL does not
            # allow a negative one — a demo with staff 40 days overdrawn looks
            # broken rather than realistic.
            annual_allowance = entitlement + carried
            annual_committed = 0.0
            count = self.random.randint(2, 9 - self.config.ladder.index(person["_rung"]) // 2)
            for _ in range(count):
                leave_type = self.pick_weighted(self.config.leave["mix"])["code"]
                start = self.date_between(year_start, min(year_end, TODAY + dt.timedelta(days=120)))
                length = self.random.choice([1, 1, 2, 3, 5, 5, 10])
                end = start + dt.timedelta(days=length - 1)
                if end > year_end:
                    continue
                duration = float(min(length, 10))

                if start > TODAY:
                    status = self.random.choice(["submitted", "submitted", "approved"])
                elif end < TODAY:
                    status = self.random.choice(
                        ["taken", "taken", "taken", "taken", "rejected", "cancelled"])
                else:
                    status = "approved"

                counts_against = status in ("taken", "approved", "submitted")
                if leave_type == "ANNUAL" and counts_against:
                    if annual_committed + duration > annual_allowance:
                        # Shorten it to what is left, or drop it entirely.
                        remaining = annual_allowance - annual_committed
                        if remaining < 1:
                            continue
                        duration = float(int(remaining))
                        end = start + dt.timedelta(days=int(duration) - 1)
                    annual_committed += duration
                    if status == "taken":
                        taken += duration
                    else:
                        pending += duration
                request_index += 1

                self.add("LeaveRequest", {
                    "code": f"LR-{request_index:05d}",
                    "name": f"{person['displayName']} — {leave_type.title()} — "
                            f"{start.strftime('%-d %b %Y')}",
                    "employee": number, "leaveType": leave_type,
                    "startDate": self.iso(start), "endDate": self.iso(end),
                    "duration": duration, "status": status,
                    "reason": "Annual leave" if leave_type == "ANNUAL" else None,
                    "submittedOn": f"{self.iso(start - dt.timedelta(days=self.random.randint(14, 60)))}T09:00:00Z"
                    if status != "draft" else None,
                })

            self.add("LeaveBalance", {
                "code": f"LB-{number}-ANNUAL-{year}",
                "name": f"{person['displayName']} — Annual leave — "
                        f"{year}/{str(year + 1)[-2:]}",
                "employee": number, "leaveType": "ANNUAL",
                "periodStart": self.iso(year_start), "periodEnd": self.iso(year_end),
                "entitlement": entitlement, "carriedOver": carried,
                "accrued": entitlement, "adjustment": 0.0,
                "taken": round(taken, 1), "pending": round(pending, 1),
                "available": round(entitlement + carried - taken - pending, 1),
                "lastCalculatedOn": f"{self.iso(TODAY)}T02:00:00Z",
            })

    def build_approvals(self) -> None:
        if not self.has("LeaveRequest"):
            # Without leave there is nothing to raise a run against here; the
            # pay-change policy below still applies.
            self.notes.append(
                "no leave policies or runs: this profile has no LeaveRequest")
            self._build_pay_approval_policy()
            return

        self.add("ApprovalPolicy", {
            "code": "POL-LEAVE-ALL", "name": "Leave — all requests",
            "appliesToEntity": "wga_leaverequest", "evaluationOrder": 100,
            "reminderAfterHours": 24, "escalateAfterHours": 72,
            "description": "Catch-all: a leave request is approved by the line manager.",
        })
        self.add("ApprovalPolicyStep", {
            "code": "POL-LEAVE-ALL-1", "policy": "POL-LEAVE-ALL",
            "name": "Line manager", "sequence": 1, "approverType": "manager",
            "completionRule": "anyApprover", "allowDelegation": True, "timeoutHours": 72,
        })
        self.add("ApprovalPolicy", {
            "code": "POL-LEAVE-LONG", "name": "Leave — over 10 days",
            "appliesToEntity": "wga_leaverequest", "legalEntity": self.config.firm["code"],
            "evaluationOrder": 10, "reminderAfterHours": 24, "escalateAfterHours": 48,
            "description": "Longer absences also need the service line head.",
        })
        for sequence, (name, kind) in enumerate(
                [("Line manager", "manager"), ("Service line head", "orgUnitHead")], start=1):
            self.add("ApprovalPolicyStep", {
                "code": f"POL-LEAVE-LONG-{sequence}", "policy": "POL-LEAVE-LONG",
                "name": name, "sequence": sequence, "approverType": kind,
                "completionRule": "anyApprover", "allowDelegation": True, "timeoutHours": 48,
            })
        self._build_pay_approval_policy()
        # One approval run per decided leave request, so the cycle-time measure
        # has something to read.
        decided = [r for r in self.rows["LeaveRequest"]
                   if r["status"] in ("approved", "rejected", "taken")]
        for index, request in enumerate(decided, start=1):
            person = next(p for p in self.employees if p["employeeNumber"] == request["employee"])
            manager = self._manager_for(person)
            if not manager:
                continue
            submitted = dt.date.fromisoformat(request["submittedOn"][:10])
            hours = self.random.choice([2, 4, 6, 8, 14, 20, 26, 40, 52, 70])
            decided_at = dt.datetime.combine(submitted, dt.time(9, 0)) + dt.timedelta(hours=hours)
            approved = request["status"] != "rejected"
            code = f"AR-{index:05d}"
            self.add("ApprovalRequest", {
                "code": code,
                "subject": request["name"],
                "regarding": {"table": "LeaveRequest", "key": request["code"]},
                "policy": "POL-LEAVE-LONG" if request["duration"] > 10 else "POL-LEAVE-ALL",
                "requester": request["employee"],
                "status": "approved" if approved else "rejected",
                "submittedOn": request["submittedOn"],
                "completedOn": decided_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "currentStep": 1,
                "summary": f"{request['duration']} day(s) of leave.",
            })
            request["approvalRequest"] = code
            self.add("ApprovalStep", {
                "code": f"{code}-1",
                "name": f"{request['name']} — line manager",
                "request": code, "sequence": 1, "approver": manager,
                "status": "approved" if approved else "rejected",
                "assignedOn": request["submittedOn"],
                "dueOn": (dt.datetime.combine(submitted, dt.time(9, 0))
                          + dt.timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "decidedOn": decided_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "comments": "Approved." if approved else "Clashes with a filing deadline.",
            })

        # A handful still waiting, some already overdue.
        waiting = [r for r in self.rows["LeaveRequest"] if r["status"] == "submitted"][:12]
        for index, request in enumerate(waiting, start=len(decided) + 1):
            person = next(p for p in self.employees if p["employeeNumber"] == request["employee"])
            manager = self._manager_for(person)
            if not manager:
                continue
            code = f"AR-{index:05d}"
            assigned = TODAY - dt.timedelta(days=self.random.randint(1, 9))
            self.add("ApprovalRequest", {
                "code": code, "subject": request["name"],
                "regarding": {"table": "LeaveRequest", "key": request["code"]},
                "policy": "POL-LEAVE-ALL", "requester": request["employee"],
                "status": "pending", "submittedOn": f"{self.iso(assigned)}T09:00:00Z",
                "currentStep": 1, "summary": f"{request['duration']} day(s) of leave.",
            })
            request["approvalRequest"] = code
            self.add("ApprovalStep", {
                "code": f"{code}-1",
                "name": f"{request['name']} — line manager",
                "request": code, "sequence": 1, "approver": manager, "status": "pending",
                "assignedOn": f"{self.iso(assigned)}T09:00:00Z",
                "dueOn": f"{self.iso(assigned + dt.timedelta(days=3))}T09:00:00Z",
            })

    def _build_pay_approval_policy(self) -> None:
        """The pay-change chain, which every profile with Compensation needs."""
        if not self.has("Compensation"):
            self.notes.append(
                "no pay-change policy: this profile has no Compensation")
            return
        self.add("ApprovalPolicy", {
            "code": "POL-COMP", "name": "Pay change",
            "appliesToEntity": "wga_compensation", "evaluationOrder": 20,
            "reminderAfterHours": 48, "escalateAfterHours": 120,
            "description": "A pay change needs the skip-level manager and a partner.",
        })
        for sequence, (name, kind) in enumerate(
                [("Skip-level manager", "skipLevelManager"), ("Partner", "group")], start=1):
            self.add("ApprovalPolicyStep", {
                "code": f"POL-COMP-{sequence}", "policy": "POL-COMP",
                "name": name, "sequence": sequence, "approverType": kind,
                # A group step needs a group, or the engine has nobody to ask.
                # In a real tenant this is an Entra group object id; here it is
                # the firm's top rung, which the directory resolves by grade.
                "approverGroupId": (TOP_RUNG_GROUP if kind == "group" else None),
                "completionRule": "allApprovers" if kind == "group" else "anyApprover",
                "timeoutHours": 120,
            })


    def build_credentials(self) -> None:
        for credential in self.config.credentials:
            self.add("CredentialType", {
                "code": credential["code"], "name": credential["name"],
                "category": "professionalLicence" if credential["code"] == "RI"
                else "professionalMembership",
                "issuingBody": credential["body"], "requiresRenewal": True,
                "renewalPeriodMonths": 12, "requiresCpd": True,
                "annualCpdHours": credential["cpe"],
            })

        index = 0
        for person in self.employees:
            if person["_left"] is not None:
                continue
            for credential in self.config.credentials:
                if person["_rung"]["grade"] not in credential["grades"]:
                    continue
                if person["_dept"]["code"] not in credential["departments"]:
                    continue
                if self.random.random() > 0.88:
                    continue
                index += 1
                # Most expire next year; a few inside 90 days, a couple already gone.
                roll = self.random.random()
                if roll < 0.08:
                    expires = TODAY + dt.timedelta(days=self.random.randint(5, 89))
                    status = "active"
                elif roll < 0.11:
                    expires = TODAY - dt.timedelta(days=self.random.randint(5, 120))
                    status = "expired"
                else:
                    expires = TODAY + dt.timedelta(days=self.random.randint(100, 400))
                    status = "active"

                required = float(credential["cpe"])
                # About a fifth are behind on CPD.
                completed = (round(required * self.random.uniform(0.35, 0.95), 1)
                             if self.random.random() < 0.22
                             else round(required * self.random.uniform(1.0, 1.6), 1))
                self.add("EmployeeCredential", {
                    "code": f"EC-{index:05d}",
                    "name": f"{person['displayName']} — {credential['code']}",
                    "employee": person["employeeNumber"],
                    "credentialType": credential["code"],
                    "credentialNumber": f"{credential['code']}-{self.random.randint(100000, 999999)}",
                    "jurisdiction": "United Kingdom", "country": self.config.firm["country"],
                    "issuedOn": self.iso(person["_joined"] + dt.timedelta(days=200)),
                    "expiresOn": self.iso(expires), "status": status,
                    "verifiedOn": self.iso(TODAY - dt.timedelta(days=self.random.randint(30, 300))),
                    "cpdHoursRequired": required, "cpdHoursCompleted": completed,
                })

        self._ensure_expiry_cohort()

    def _ensure_expiry_cohort(self) -> None:
        """Make sure the credential watchlist is never empty.

        The expiry buckets are random, so a small firm can come out with none
        expiring and none expired — and then two of the dashboard's compliance
        tiles read zero and the demo shows nothing. Force a few.
        """
        rows = self.rows.get("EmployeeCredential", [])
        if not rows:
            if self.config.credentials:
                self.warnings.append(
                    "no credentials generated although the firm defines some — "
                    "check the grade and department filters")
            else:
                self.notes.append(
                    f"{self.config.name} has no professional credentials, so the "
                    "compliance page shows mandatory training only")
            return
        wanted_expiring, wanted_expired = 4, 2
        expiring = [r for r in rows if r["status"] == "active"
                    and TODAY <= dt.date.fromisoformat(r["expiresOn"])
                    <= TODAY + dt.timedelta(days=90)]
        expired = [r for r in rows if dt.date.fromisoformat(r["expiresOn"]) < TODAY]
        spare = [r for r in rows if r not in expiring and r not in expired]
        self.random.shuffle(spare)

        for row in spare[:max(0, wanted_expiring - len(expiring))]:
            row["expiresOn"] = self.iso(TODAY + dt.timedelta(days=self.random.randint(7, 88)))
            row["status"] = "active"
        spare = spare[max(0, wanted_expiring - len(expiring)):]
        for row in spare[:max(0, wanted_expired - len(expired))]:
            row["expiresOn"] = self.iso(TODAY - dt.timedelta(days=self.random.randint(10, 150)))
            row["status"] = "expired"

    def build_training(self) -> None:
        for course in self.config.courses:
            self.add("TrainingCourse", {
                "code": course["code"], "name": course["name"],
                "deliveryMethod": course["method"], "durationHours": course["hours"],
                "cpdHours": course["cpd"], "isMandatory": course["mandatory"],
                "validityMonths": 12 if course["mandatory"] else None,
                "currency": self.config.firm["currency"],
            })

        index = 0
        for person in self.employees:
            if person["_left"] is not None:
                continue
            for course in self.config.courses:
                if not course["mandatory"] and self.random.random() > 0.35:
                    continue
                index += 1
                if course["mandatory"]:
                    # An annual refresher is due on the anniversary of joining,
                    # so at any moment some of the firm is overdue — which is
                    # the point of the tile.
                    anniversary = dt.date(TODAY.year, person["_joined"].month,
                                          min(person["_joined"].day, 28))
                    due = anniversary
                else:
                    due = self.date_between(TODAY - dt.timedelta(days=200),
                                            TODAY + dt.timedelta(days=120))
                roll = self.random.random()
                if roll < 0.82:
                    earliest = max(due - dt.timedelta(days=120), person["_joined"])
                    latest = min(due, TODAY)
                    status, completed = "completed", self.date_between(
                        earliest, max(earliest, latest))
                elif roll < 0.86:
                    status, completed = "inProgress", None
                elif roll < 0.95:
                    status, completed = "assigned", None
                else:
                    status, completed = "waived", None
                self.add("TrainingRecord", {
                    "code": f"TR-{index:05d}",
                    "name": f"{person['displayName']} — {course['code']}",
                    "employee": person["employeeNumber"], "course": course["code"],
                    "status": status,
                    "assignedOn": self.iso(due - dt.timedelta(days=180)),
                    "dueDate": self.iso(due),
                    "completedOn": self.iso(completed),
                    "cpdHoursEarned": course["cpd"] if status == "completed" else None,
                    "score": self.random.randint(72, 100) if status == "completed" else None,
                })

    def build_performance(self) -> None:
        self.add("ReviewCycle", {
            "code": "FY26", "name": "FY2026 annual review", "cycleType": "annual",
            "periodStart": self.iso(dt.date(2025, 4, 1)),
            "periodEnd": self.iso(dt.date(2026, 3, 31)),
            "selfReviewDue": self.iso(dt.date(2026, 4, 17)),
            "managerReviewDue": self.iso(dt.date(2026, 5, 8)),
            "status": "closed",
        })
        self.add("ReviewCycle", {
            "code": "FY27", "name": "FY2027 annual review", "cycleType": "annual",
            "periodStart": self.iso(dt.date(2026, 4, 1)),
            "periodEnd": self.iso(dt.date(2027, 3, 31)),
            "selfReviewDue": self.iso(dt.date(2027, 4, 16)),
            "managerReviewDue": self.iso(dt.date(2027, 5, 7)),
            "status": "open",
        })

        index = 0
        for person in self.employees:
            if person["_left"] is not None or person["_joined"] > dt.date(2025, 10, 1):
                continue
            manager = self._manager_for(person)
            if not manager:
                continue
            index += 1
            rating = self.pick_weighted([
                {"code": "R5", "share": 0.08}, {"code": "R4", "share": 0.27},
                {"code": "R3", "share": 0.48}, {"code": "R2", "share": 0.13},
                {"code": "R1", "share": 0.04},
            ])["code"]
            self.add("PerformanceReview", {
                "code": f"PR-FY26-{index:04d}",
                "name": f"{person['displayName']} — FY2026",
                "cycle": "FY26", "employee": person["employeeNumber"], "reviewer": manager,
                "status": "completed", "selfRating": rating, "managerRating": rating,
                "finalRating": rating,
                "promotionRecommended": rating in ("R4", "R5") and self.random.random() < 0.3,
                "completedOn": f"{self.iso(dt.date(2026, 5, 6))}T16:00:00Z",
                "employeeAcknowledgedOn": f"{self.iso(dt.date(2026, 5, 12))}T10:00:00Z",
            })
            # The open cycle, mostly unfinished.
            self.add("PerformanceReview", {
                "code": f"PR-FY27-{index:04d}",
                "name": f"{person['displayName']} — FY2027",
                "cycle": "FY27", "employee": person["employeeNumber"], "reviewer": manager,
                "status": self.pick_weighted([
                    {"code": "notStarted", "share": 0.52},
                    {"code": "selfAssessment", "share": 0.28},
                    {"code": "managerAssessment", "share": 0.12},
                    {"code": "completed", "share": 0.08},
                ])["code"],
            })

    def build_recruitment(self) -> None:
        open_roles = max(4, self.people // 12)
        for index in range(1, open_roles + 1):
            dept = self.pick_weighted(self.config.departments)
            rung = self.pick_weighted(self.config.ladder[:5])
            office = self.pick_weighted(self.config.offices)
            head = dept.get("_head")
            if not head:
                continue
            posted = self.date_between(TODAY - dt.timedelta(days=150),
                                       TODAY - dt.timedelta(days=10))
            filled = self.random.random() < 0.35
            closed = (posted + dt.timedelta(days=self.random.randint(28, 95))
                      if filled else None)
            if closed and closed > TODAY:
                closed, filled = None, False
            code = f"REQ-{index:04d}"
            low, high = rung["salary"]
            self.add("JobRequisition", {
                "code": code,
                "title": f"{rung['title']}, {dept['name']}",
                "jobProfile": f"{dept['code']}-{rung['grade']}",
                "orgUnit": dept["code"], "office": office["code"],
                "hiringManager": head,
                "openings": self.random.choice([1, 1, 1, 2]),
                "reason": self.random.choice(["replacement", "newHeadcount", "replacement"]),
                "employmentType": "fullTime", "contractType": "permanent",
                "workArrangement": "hybrid",
                "salaryMin": low, "salaryMax": high, "currency": self.config.firm["currency"],
                "targetStartDate": self.iso(posted + dt.timedelta(days=120)),
                "status": "filled" if filled else "open",
                "isInternalOnly": False,
                "postedOn": self.iso(posted), "closedOn": self.iso(closed),
            })

            applicants = self.random.randint(5, 22)
            for app_index in range(1, applicants + 1):
                first = self.random.choice(FIRST_NAMES)
                last = self.random.choice(LAST_NAMES)
                candidate_code = f"CAND-{index:04d}-{app_index:03d}"
                applied_on = self.date_between(posted, min(closed or TODAY, TODAY))
                self.add("Candidate", {
                    "code": candidate_code,
                    "firstName": first, "lastName": last,
                    "fullName": f"{first} {last}",
                    "email": f"{first.lower()}.{last.lower().replace(chr(39), '')}"
                             f"{app_index}@example.com",
                    "consentToRetain": True,
                    # UK GDPR: an unsuccessful applicant's data is kept for six
                    # months from application, then deleted or re-consented.
                    "retainUntil": self.iso(applied_on + dt.timedelta(days=183)),
                    "source": self.pick_weighted([
                        {"code": "jobBoard", "share": 0.3},
                        {"code": "careersSite", "share": 0.22},
                        {"code": "linkedIn", "share": 0.2},
                        {"code": "employeeReferral", "share": 0.14},
                        {"code": "agency", "share": 0.09},
                        {"code": "university", "share": 0.05},
                    ])["code"],
                })
                applied = applied_on
                stage = self.pick_weighted([
                    {"code": "rejected", "share": 0.48},
                    {"code": "screening", "share": 0.14},
                    {"code": "assessment", "share": 0.10},
                    {"code": "interview", "share": 0.12},
                    {"code": "applied", "share": 0.08},
                    {"code": "offer", "share": 0.04},
                    {"code": "hired", "share": 0.03},
                    {"code": "withdrawn", "share": 0.01},
                ])["code"]
                if filled and app_index == 1:
                    stage = "hired"
                self.add("JobApplication", {
                    "code": f"APP-{index:04d}-{app_index:03d}",
                    "name": f"{first} {last} — {code}",
                    "requisition": code, "candidate": candidate_code,
                    "stage": stage, "appliedOn": self.iso(applied),
                    "stageChangedOn": f"{self.iso(applied + dt.timedelta(days=self.random.randint(2, 30)))}T12:00:00Z",
                    "rejectionReason": "notQualified" if stage == "rejected" else None,
                    "overallRating": round(self.random.uniform(2.0, 4.8), 1)
                    if stage not in ("applied", "rejected") else None,
                })

    def build_checklists(self) -> None:
        self.add("ChecklistTemplate", {
            "code": "ONB-STD", "name": "Standard onboarding", "checklistType": "onboarding",
            "legalEntity": self.config.firm["code"],
            "description": f"Everything a new joiner at {self.config.name} needs in "
                           "their first fortnight.",
        })
        tasks = [(t["name"], t["role"], int(t["offsetDays"]))
                 for t in self.config.onboarding]
        for sequence, (name, role, offset) in enumerate(tasks, start=1):
            self.add("ChecklistTemplateTask", {
                "code": f"ONB-STD-{sequence:02d}", "template": "ONB-STD", "title": name,
                "sequence": sequence, "assigneeRole": role, "dueOffsetDays": offset,
                "isMandatory": True,
            })

        # Live checklists for the most recent joiners.
        recent = sorted([p for p in self.employees if p["_left"] is None],
                        key=lambda p: p["_joined"], reverse=True)[:6]
        for index, person in enumerate(recent, start=1):
            code = f"ONB-{index:04d}"
            done = person["_joined"] < TODAY - dt.timedelta(days=45)
            # The anchor is the start date: every task's due date is an offset from it.
            task_states = [
                done or (person["_joined"] + dt.timedelta(days=offset))
                < TODAY - dt.timedelta(days=2)
                for _, _, offset in tasks
            ]
            self.add("EmployeeChecklist", {
                "code": code,
                "name": f"{person['displayName']} — onboarding",
                "employee": person["employeeNumber"], "template": "ONB-STD",
                "checklistType": "onboarding",
                "anchorDate": self.iso(person["_joined"]),
                "status": "completed" if done else "inProgress",
                "percentComplete": round(100.0 * sum(task_states) / len(task_states), 2),
            })
            for sequence, (name, role, offset) in enumerate(tasks, start=1):
                due = person["_joined"] + dt.timedelta(days=offset)
                complete = task_states[sequence - 1]
                self.add("EmployeeChecklistTask", {
                    "code": f"{code}-{sequence:02d}",
                    "title": name, "checklist": code,
                    "templateTask": f"ONB-STD-{sequence:02d}",
                    "assigneeRole": role, "dueDate": self.iso(due),
                    "status": "completed" if complete else "notStarted",
                    "completedOn": self.iso(min(due, TODAY)) if complete else None,
                })


    # -- CRM -----------------------------------------------------------------
    def _client_name(self, used: set[str]) -> str:
        for _ in range(200):
            name = (f"{self.random.choice(CLIENT_PREFIXES)} "
                    f"{self.random.choice(CLIENT_SUFFIXES)} "
                    f"{self.random.choice(CLIENT_FORMS)}")
            if name not in used:
                used.add(name)
                return name
        raise DataError("ran out of distinct client names")

    def build_pipeline(self) -> None:
        """Clients, enquiries, pitches and fee proposals.

        Shaped so the pipeline reads like a real one: most enquiries do not
        become pitches, most pitches are still open, and the ones that closed
        split roughly two to one in the firm's favour.
        """
        billable = [d for d in self.config.departments
                    if d["family"] == "DELIVERY"] or self.config.departments
        partners = [p for p in self.employees if p["_left"] is None]
        if not partners:
            self.warnings.append("no active staff, so no pipeline was generated")
            return

        for source in LEAD_SOURCES:
            self.add("LeadSource", {"code": source["code"], "name": source["name"]})
        for offering in SERVICE_OFFERINGS:
            self.add("ServiceOffering", {
                "code": offering["code"], "name": offering["name"],
                "defaultPrice": float(self.random.randrange(250, 750, 25)),
                "currency": self.config.firm["currency"],
            })
        self.add("SalesPipeline", {
            "code": "NEWBIZ", "name": "New business",
            "description": "The firm's standard pursuit process.",
        })
        for stage in PIPELINE_STAGES:
            self.add("PipelineStage", {
                "code": stage["code"], "pipeline": "NEWBIZ", "name": stage["name"],
                "sequence": stage["sequence"],
                "defaultProbability": float(stage["probability"]),
                "isClosed": bool(stage.get("closed")),
                "isWon": bool(stage.get("won")),
            })

        # Clients and prospects. Roughly two in three are existing clients.
        used: set[str] = set()
        clients = max(12, self.people // 3)
        self.accounts: list[dict[str, Any]] = []
        for index in range(1, clients + 1):
            name = self._client_name(used)
            is_client = self.random.random() < 0.65
            code = f"ACC-{index:04d}"
            owner = self.random.choice(partners)
            self.add("Account", {
                "code": code, "name": name,
                "accountNumber": code,
                "roles": ["client"] if is_client else ["prospect"],
                "website": f"https://www.{re.sub(r'[^a-z]', '', name.lower())[:18]}.example",
                "relationshipManager": owner["employeeNumber"],
                "currency": self.config.firm["currency"],
                **self.optional(
                    "Account",
                    clientSince=self.iso(self.date_between(
                        TODAY - dt.timedelta(days=4000), TODAY - dt.timedelta(days=60)))
                    if is_client else None),
                "addressCountry": self.config.firm["country"],
            })
            self.accounts.append({"code": code, "name": name, "isClient": is_client,
                                  "owner": owner})

            # One or two named contacts each.
            for contact_index in range(1, self.random.randint(1, 2) + 1):
                first = self.random.choice(FIRST_NAMES)
                last = self.random.choice(LAST_NAMES)
                self.add("Contact", {
                    "code": f"{code}-C{contact_index}",
                    "firstName": first, "lastName": last,
                    "fullName": f"{first} {last}",
                    "account": code,
                    "jobTitle": self.random.choice(
                        ["General Counsel", "Finance Director", "Chief Executive",
                         "Company Secretary", "Head of Legal", "Operations Director"]),
                })

        # Enquiries. Most do not become pitches, which is what a real funnel does.
        leads = max(20, self.people // 2)
        for index in range(1, leads + 1):
            first = self.random.choice(FIRST_NAMES)
            last = self.random.choice(LAST_NAMES)
            offering = self.random.choice(SERVICE_OFFERINGS)
            source = self.pick_weighted(LEAD_SOURCES)
            received = self.date_between(TODAY - dt.timedelta(days=540), TODAY)
            status = self.pick_weighted([
                {"code": "disqualified", "share": 0.34},
                {"code": "qualified", "share": 0.24},
                {"code": "nurturing", "share": 0.16},
                {"code": "contacted", "share": 0.16},
                {"code": "new", "share": 0.10},
            ])["code"]
            self.add("Lead", {
                "code": f"LEAD-{index:04d}",
                "topic": f"{offering['name']} enquiry",
                "firstName": first, "lastName": last,
                "companyName": self._client_name(used),
                "jobTitle": "General Counsel",
                "email": f"{first.lower()}.{last.lower().replace(chr(39), '')}"
                         f"{index}@example.com",
                "source": source["code"],
                "status": status,
                "estimatedValue": float(self.random.randrange(20000, 400000, 5000)),
                "currency": self.config.firm["currency"],
                **self.optional("Lead",
                                disqualifiedReason="noBudget"
                                if status == "disqualified" else None),
            })

        # Pitches. Each belongs to a client or prospect and a practice area.
        # Roughly one per two people: a 64-lawyer firm with a dozen live pitches
        # would be a quiet one, and the matter hand-off needs enough won pitches
        # to be worth showing.
        pitches = max(20, self.people // 2)
        self.opportunities: list[dict[str, Any]] = []
        for index in range(1, pitches + 1):
            account = self.random.choice(self.accounts)
            offering = self.random.choice(SERVICE_OFFERINGS)
            department = self.random.choice(billable)
            created = self.date_between(TODAY - dt.timedelta(days=500),
                                        TODAY - dt.timedelta(days=5))
            value = self.random.randrange(25000, 900000, 5000)

            # Of the pitches that close, about four in ten are won. A firm
            # winning most of its competitive pitches is not a firm anyone will
            # believe, and the win-rate tile is the first thing a partner reads.
            roll = self.random.random()
            if roll < 0.46:
                stage, status = self.random.choice(
                    ["QUALIFY", "SCOPE", "PROPOSE", "NEGOTIATE"]), "open"
                closed_on, actual = None, None
            elif roll < 0.68:
                stage, status = "WON", "won"
                closed_on = self.date_between(created + dt.timedelta(days=20),
                                              min(TODAY, created + dt.timedelta(days=260)))
                actual = round(value * self.random.uniform(0.82, 1.08), -3)
            else:
                stage, status = "LOST", self.random.choice(["lost", "lost", "abandoned"])
                closed_on = self.date_between(created + dt.timedelta(days=15),
                                              min(TODAY, created + dt.timedelta(days=200)))
                actual = None

            probability = next(s["probability"] for s in PIPELINE_STAGES
                               if s["code"] == stage)
            code = f"OPP-{index:04d}"
            self.add("Opportunity", {
                "code": code,
                "name": f"{account['name']} — {offering['name']}",
                "account": account["code"],
                "pipeline": "NEWBIZ", "stage": stage, "status": status,
                "probability": float(probability),
                "estimatedValue": float(value),
                "actualValue": float(actual) if actual else None,
                "currency": self.config.firm["currency"],
                "estimatedCloseDate": self.iso(
                    closed_on or self.date_between(TODAY - dt.timedelta(days=40),
                                                   TODAY + dt.timedelta(days=150))),
                "actualCloseDate": self.iso(closed_on),
                "isTender": self.random.random() < 0.18,
                **self.optional("Opportunity",
                                lossReason="price" if status == "lost" else None),
            })
            self.opportunities.append({
                "code": code, "account": account, "status": status,
                "department": department, "offering": offering, "created": created,
                "closedOn": closed_on, "value": value, "actual": actual,
            })

            # The pursuit team.
            team = self.random.sample(partners, min(len(partners),
                                                    self.random.randint(1, 3)))
            for member_index, member in enumerate(team, start=1):
                self.add("OpportunityTeamMember", {
                    "code": f"{code}-T{member_index}",
                    "name": f"{member['displayName']} on {code}",
                    "opportunity": code, "employee": member["employeeNumber"],
                    "role": "lead" if member_index == 1 else "supporting",
                })

        # A fee proposal for anything that reached the proposal stage or beyond.
        proposal_index = 0
        for pitch in self.opportunities:
            if pitch["status"] == "open" and self.random.random() < 0.5:
                continue
            proposal_index += 1
            issued = self.date_between(
                pitch["created"] + dt.timedelta(days=10),
                min(TODAY, pitch["closedOn"] or TODAY))
            if pitch["status"] == "won":
                status, accepted = "accepted", pitch["closedOn"]
            elif pitch["status"] in ("lost", "abandoned"):
                status, accepted = self.random.choice(["rejected", "expired"]), None
            else:
                status, accepted = "sent", None
            subtotal = float(pitch["value"])
            discount = round(subtotal * self.random.choice([0, 0, 0.05, 0.1]), -2)
            tax = round((subtotal - discount) * 0.20, -2)
            code = f"PROP-{proposal_index:04d}"
            self.add("Proposal", {
                "code": code,
                "name": f"Fee proposal — {pitch['account']['name']}",
                "proposalNumber": code, "revision": 1,
                "opportunity": pitch["code"], "account": pitch["account"]["code"],
                "status": status,
                "billingModel": pitch["offering"]["model"],
                "issuedOn": self.iso(issued),
                "validUntil": self.iso(issued + dt.timedelta(days=30)),
                "acceptedOn": self.iso(accepted),
                "subtotal": subtotal, "discountAmount": discount,
                "taxAmount": tax, "totalAmount": subtotal - discount + tax,
                "currency": self.config.firm["currency"],
            })
            for line_index in range(1, self.random.randint(1, 3) + 1):
                quantity = float(self.random.randint(10, 240))
                self.add("ProposalLine", {
                    "code": f"{code}-L{line_index}",
                    "proposal": code, "lineNumber": line_index,
                    "description": f"{pitch['offering']['name']} — phase {line_index}",
                    "quantity": quantity,
                    "unitPrice": round(subtotal / (quantity * line_index), 2),
                    "currency": self.config.firm["currency"],
                    "service": pitch["offering"]["code"],
                })

        # Logged contact, so the activity timeline is not empty.
        activity_index = 0
        for pitch in self.opportunities:
            for _ in range(self.random.randint(1, 4)):
                activity_index += 1
                kind = self.pick_weighted([
                    {"code": "email", "share": 0.42}, {"code": "call", "share": 0.28},
                    {"code": "meeting", "share": 0.22}, {"code": "note", "share": 0.08},
                ])["code"]
                when = self.date_between(pitch["created"],
                                         min(TODAY, pitch["closedOn"] or TODAY))
                self.add("Activity", {
                    "code": f"ACT-{activity_index:05d}",
                    "subject": f"{kind.title()} about {pitch['account']['name']}",
                    "activityType": kind,
                    "regarding": {"table": "Opportunity", "key": pitch["code"]},
                    "status": "completed",
                    "direction": self.random.choice(["inbound", "outbound"]),
                    "startOn": f"{self.iso(when)}T10:00:00Z",
                    "completedOn": f"{self.iso(when)}T10:30:00Z",
                })

    def build_intake_compliance(self) -> None:
        """Conflict checks, client due diligence and marketing consent.

        The numbers are deliberately uncomfortable: about one check in five
        finds something, a few are still waiting past their turnaround, and some
        due diligence is overdue for review — because a compliance dashboard
        where everything is green demonstrates nothing.
        """
        accounts = getattr(self, "accounts", [])
        opportunities = getattr(self, "opportunities", [])
        partners = [p for p in self.employees if p["_left"] is None]
        if not (accounts and partners):
            self.warnings.append("no clients, so no intake compliance was generated")
            return

        for index, pitch in enumerate(opportunities, start=1):
            requested = dt.datetime.combine(pitch["created"], dt.time(9, 0))
            # About one decided check in five finds something. Higher than that
            # and the conflicts team looks broken; lower and the tile is
            # decoration.
            roll = self.random.random()
            if roll < 0.10:
                status, decided = self.random.choice(["pending", "searching"]), None
            elif roll < 0.82:
                status = "clear"
                decided = requested + dt.timedelta(
                    hours=self.random.choice([2, 4, 6, 9, 14, 22, 30, 48]))
            elif roll < 0.94:
                status = "conflictWaived"
                decided = requested + dt.timedelta(hours=self.random.randint(24, 96))
            else:
                status = self.random.choice(["potentialConflict", "declined"])
                decided = requested + dt.timedelta(hours=self.random.randint(12, 72))

            self.add("ConflictCheck", {
                "code": f"CC-{index:05d}",
                "title": f"Conflict check — {pitch['account']['name']}",
                "requestedBy": self.random.choice(partners)["employeeNumber"],
                "requestedOn": requested.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "prospectiveClient": pitch["account"]["code"],
                "prospectiveClientName": pitch["account"]["name"],
                "opportunity": pitch["code"],
                "matterDescription": f"{pitch['offering']['name']} for "
                                     f"{pitch['account']['name']}.",
                "status": status,
                "reviewedBy": self.random.choice(partners)["employeeNumber"]
                if decided else None,
                "decidedOn": decided.strftime("%Y-%m-%dT%H:%M:%SZ") if decided else None,
            })

        self._ensure_conflicts_found()

        for index, account in enumerate(accounts, start=1):
            if not account["isClient"]:
                continue
            level = self.pick_weighted([
                {"code": "standard", "share": 0.62},
                {"code": "simplified", "share": 0.22},
                {"code": "enhanced", "share": 0.16},
            ])["code"]
            risk = ("high" if level == "enhanced" else
                    self.pick_weighted([{"code": "low", "share": 0.62},
                                        {"code": "medium", "share": 0.38}])["code"])
            completed = self.date_between(TODAY - dt.timedelta(days=900),
                                          TODAY - dt.timedelta(days=20))
            months = {"simplified": 36, "standard": 24, "enhanced": 12}[level]
            review = completed + dt.timedelta(days=int(months * 30.4))
            self.add("DueDiligenceCheck", {
                "code": f"DD-{index:05d}",
                "name": f"Client due diligence — {account['name']}",
                "account": account["code"], "level": level, "riskRating": risk,
                "status": self.pick_weighted([
                    {"code": "completed", "share": 0.80},
                    {"code": "inProgress", "share": 0.11},
                    {"code": "awaitingDocuments", "share": 0.09},
                ])["code"],
                "performedBy": self.random.choice(partners)["employeeNumber"],
                "completedOn": self.iso(completed),
                "nextReviewDate": self.iso(review),
                "hasPoliticallyExposedPerson": level == "enhanced"
                and self.random.random() < 0.45,
                "sanctionsScreenedOn": f"{self.iso(completed)}T11:00:00Z",
                "sanctionsResult": self.pick_weighted([
                    {"code": "noMatch", "share": 0.88},
                    {"code": "falsePositive", "share": 0.10},
                    {"code": "possibleMatch", "share": 0.02},
                ])["code"],
            })

        contacts = [r for r in self.rows.get("Contact", [])]
        for index, contact in enumerate(contacts, start=1):
            if self.random.random() > 0.72:
                continue                    # not everyone has been asked
            captured = self.date_between(TODAY - dt.timedelta(days=700), TODAY)
            self.add("ConsentRecord", {
                "code": f"CONS-{index:05d}",
                "name": f"Marketing consent — {contact['fullName']}",
                "contact": contact["code"],
                "purpose": "marketingEmail",
                "granted": self.random.random() < 0.78,
                "capturedOn": f"{self.iso(captured)}T09:00:00Z",
                "method": self.random.choice(["webForm", "email", "paperForm"]),
                "source": "Fabrikam website",
            })

    def _ensure_conflicts_found(self) -> None:
        """At least a couple of checks must find something.

        The conflict rate is random, and on a firm this size a draw can easily
        find nothing at all — which leaves the conflict-rate tile reading zero
        and demonstrates the opposite of what it is for.
        """
        rows = self.rows.get("ConflictCheck", [])
        found = [r for r in rows
                 if r["status"] in ("potentialConflict", "conflictWaived", "declined")]
        wanted = 2
        if len(found) >= wanted:
            return
        spare = [r for r in rows if r["status"] == "clear"]
        self.random.shuffle(spare)
        for row in spare[:wanted - len(found)]:
            row["status"] = self.random.choice(["potentialConflict", "conflictWaived"])
        self.notes.append(
            f"the draw found {len(found)} conflict(s); raised {len(spare[:wanted - len(found)])} "
            f"to reach the floor of {wanted}, so the conflict-rate tile is not zero")

    def build_matters(self) -> None:
        """A matter for each won pitch — the hand-off the pilot demonstrates."""
        opportunities = [o for o in getattr(self, "opportunities", [])
                         if o["status"] == "won"]
        partners = [p for p in self.employees if p["_left"] is None]
        if not (opportunities and partners):
            self.notes.append("no won pitches, so no matters were created")
            return

        for offering in SERVICE_OFFERINGS:
            self.add("ProjectType", {
                "code": offering["code"], "name": offering["name"],
                "defaultBillingModel": offering["model"],
                "requiresClient": True,
            })

        for index, pitch in enumerate(opportunities, start=1):
            opened = pitch["closedOn"] or TODAY
            self.add("Project", {
                "code": f"MAT-{index:05d}",
                "name": f"{pitch['account']['name']} — {pitch['offering']['name']}",
                "projectType": pitch["offering"]["code"],
                "client": pitch["account"]["code"],
                "projectManager": self.random.choice(partners)["employeeNumber"],
                "orgUnit": pitch["department"]["code"],
                "billingModel": pitch["offering"]["model"],
                "currency": self.config.firm["currency"],
                "status": self.pick_weighted([
                    {"code": "active", "share": 0.62},
                    {"code": "completed", "share": 0.28},
                    {"code": "onHold", "share": 0.10},
                ])["code"],
                "startDate": self.iso(opened),
                "contractValue": float(pitch["actual"] or pitch["value"]),
                "originatingOpportunity": pitch["code"],
                # legal-work adds these; a profile without that overlay has not.
                **self.optional(
                    "Project",
                    billingPartner=self.random.choice(partners)["employeeNumber"],
                    clientMatterReference=f"{pitch['account']['code']}/{index:03d}"),
            })

# --- validation --------------------------------------------------------------
class Validator:
    """Checks generated rows against the resolved model."""

    def __init__(self, model: dict[str, Any], rows: dict[str, list[dict[str, Any]]]):
        self.model = model
        self.entities = model.get("entities", {})
        self.enums = model.get("enums", {})
        self.seeds = model.get("seed", {})
        self.rows = rows
        self.errors: list[str] = []
        self.warnings: list[str] = []
        # Notes record a deliberate choice the generator made. Warnings are
        # things that may be wrong, so --strict fails on those and not these.
        self.notes: list[str] = []
        self.keys = self._natural_keys()

    def _natural_keys(self) -> dict[str, set[str]]:
        """Every natural key available to a lookup: generated rows plus schema seeds."""
        keys: dict[str, set[str]] = defaultdict(set)
        for table, rows in self.rows.items():
            for row in rows:
                for field in ("code", "employeeNumber", "isoCode", "isoCode2"):
                    if row.get(field):
                        keys[table].add(str(row[field]))
        for table, rows in self.seeds.items():
            for row in rows:
                for field in ("code", "isoCode", "isoCode2", "isoCode3"):
                    if row.get(field):
                        keys[table].add(str(row[field]))
        return keys

    def run(self) -> None:
        for table, rows in sorted(self.rows.items()):
            if table not in self.entities:
                self.errors.append(f"{table}: not a table in this profile")
                continue
            attributes = self.entities[table]["attributes"]
            for index, row in enumerate(rows):
                self._check_row(table, index, row, attributes)
        self._check_assignment_history()
        self._check_leave_within_balance()
        self._check_balances_not_overdrawn()

    def _check_row(self, table: str, index: int, row: dict[str, Any],
                   attributes: dict[str, Any]) -> None:
        where = f"{table}[{index}]"
        for field, value in row.items():
            if field.startswith("_") or field == "code":
                continue
            spec = attributes.get(field)
            if spec is None and not self._is_address_part(field, attributes):
                self.errors.append(f"{where}: '{field}' is not a column of {table}")
                continue
            if spec is None or value is None:
                continue
            kind = spec["type"]
            if kind in ("choice", "multichoice"):
                self._check_choice(where, field, value, spec)
            elif kind in ("lookup", "parent"):
                self._check_lookup(where, field, value, spec)
            elif kind == "polylookup":
                self._check_polylookup(where, field, value, spec)

        for field, spec in attributes.items():
            if spec.get("trait") in ("base", "owned") or spec["type"] == "guid":
                continue
            if not spec.get("required") or spec.get("readOnly") or spec.get("calculated"):
                continue
            if spec.get("default") is not None or spec["type"] == "autonumber":
                continue
            if spec["type"] == "address":
                # A composite has no physical base column; its parts carry the
                # value, so it counts as present when any part is given.
                if not any(k.startswith(field) and k != field and v is not None
                           for k, v in row.items()):
                    self.errors.append(
                        f"{where}: required address '{field}' has no parts "
                        f"(expected {field}Line1, {field}City, …)")
                continue
            if row.get(field) is None:
                self.errors.append(f"{where}: required column '{field}' is missing")

    @staticmethod
    def _is_address_part(field: str, attributes: dict[str, Any]) -> bool:
        for base, spec in attributes.items():
            if spec["type"] == "address" and field.startswith(base) and field != base:
                return True
        return False

    def _check_choice(self, where: str, field: str, value: Any, spec: dict[str, Any]) -> None:
        enum_name = spec.get("enum") or spec.get("target")
        enum = self.enums.get(enum_name) or {}
        valid = {v["key"] for v in enum.get("values", []) if not v.get("deprecated")}
        for item in (value if isinstance(value, list) else [value]):
            if item not in valid:
                self.errors.append(
                    f"{where}: {field} = '{item}' is not a value of {enum_name} "
                    f"({', '.join(sorted(valid))})")

    def _check_lookup(self, where: str, field: str, value: Any, spec: dict[str, Any]) -> None:
        target = spec.get("target")
        if not target:
            return
        available = self.keys.get(target, set())
        if not available:
            self.warnings.append(
                f"{where}: {field} points at {target}, which has no rows — "
                "load its reference data first")
            return
        if str(value) not in available:
            self.errors.append(f"{where}: {field} = '{value}' is not a {target} that exists")

    def _check_polylookup(self, where: str, field: str, value: Any,
                          spec: dict[str, Any]) -> None:
        """A multi-table lookup carries its own target: {"table": ..., "key": ...}."""
        if not isinstance(value, dict) or "table" not in value or "key" not in value:
            self.errors.append(
                f"{where}: {field} is a multi-table lookup and must be written "
                '{"table": "<Entity>", "key": "<natural key>"}')
            return
        allowed = spec.get("targets") or spec.get("target") or "*"
        if isinstance(allowed, str):
            allowed = ["*"] if allowed == "*" else [t.strip() for t in allowed.split("|")]
        if "*" not in allowed and value["table"] not in allowed:
            self.errors.append(
                f"{where}: {field} points at {value['table']}, not one of "
                f"{', '.join(allowed)}")
            return
        if value["table"] not in self.entities:
            self.errors.append(
                f"{where}: {field} points at '{value['table']}', which is not a table")
            return
        if str(value["key"]) not in self.keys.get(value["table"], set()):
            self.errors.append(
                f"{where}: {field} = {value['table']}/{value['key']} does not exist")

    def _check_assignment_history(self) -> None:
        """Exactly one current assignment per person, and no row ending before it starts."""
        if "JobAssignment" not in self.rows:
            return          # a dataset that carries no assignments has none to check
        current: dict[str, int] = defaultdict(int)
        for index, row in enumerate(self.rows.get("JobAssignment", [])):
            start, end = row.get("validFrom"), row.get("validTo")
            if end and start and end < start:
                self.errors.append(
                    f"JobAssignment[{index}]: validTo {end} is before validFrom {start}")
            if end is None:
                current[row["employee"]] += 1

        leavers = {
            row["employee"] for row in self.rows.get("Employment", []) if row.get("endDate")}
        for employee, count in current.items():
            if count > 1:
                self.errors.append(
                    f"JobAssignment: {employee} has {count} assignments with no validTo; "
                    "exactly one row must be current")
        for row in self.rows.get("Employee", []):
            number = row["employeeNumber"]
            if number in leavers:
                continue
            if current.get(number, 0) == 0:
                self.errors.append(
                    f"JobAssignment: active employee {number} has no current assignment")

    def _check_balances_not_overdrawn(self) -> None:
        """A leave type that forbids a negative balance must not have one."""
        allows_negative = {
            row["code"]: bool(row.get("allowNegativeBalance"))
            for row in self.seeds.get("LeaveType", [])
        }
        for index, row in enumerate(self.rows.get("LeaveBalance", [])):
            if allows_negative.get(row["leaveType"], False):
                continue
            if (row.get("available") or 0) < 0:
                self.errors.append(
                    f"LeaveBalance[{index}]: {row['employee']} is "
                    f"{abs(row['available'])} day(s) overdrawn on {row['leaveType']}, "
                    "which does not allow a negative balance")

    def _check_leave_within_balance(self) -> None:
        periods = {
            (row["employee"], row["leaveType"]): (row["periodStart"], row["periodEnd"])
            for row in self.rows.get("LeaveBalance", [])
        }
        for index, row in enumerate(self.rows.get("LeaveRequest", [])):
            if row["endDate"] < row["startDate"]:
                self.errors.append(
                    f"LeaveRequest[{index}]: endDate is before startDate")
            period = periods.get((row["employee"], row["leaveType"]))
            if period and not (period[0] <= row["startDate"] <= period[1]):
                self.errors.append(
                    f"LeaveRequest[{index}]: starts {row['startDate']}, outside the "
                    f"balance period {period[0]}..{period[1]}")


# --- output ------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate validated demo data.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--firm", help="firm slug in data/firms (default: the firm the "
                                       "roadmap dresses this profile as)")
    parser.add_argument("--people", type=int, default=64, help="how many staff (default 64)")
    parser.add_argument("--seed", type=int, default=20261104, help="random seed")
    parser.add_argument("--model", help="resolved model JSON (default: build/<profile>.model.json)")
    parser.add_argument("--out", help="output directory (default: build/demo/<profile>/<firm>)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args(argv)

    model_file = Path(args.model) if args.model else BUILD_DIR / f"{args.profile}.model.json"
    if not model_file.exists():
        print(f"error: {model_file} not found — run: python tools/schematool.py export "
              f"--profile {args.profile}", file=sys.stderr)
        return 2
    if args.people < 8:
        print("error: --people must be at least 8 for a sensible org chart", file=sys.stderr)
        return 2

    slug = args.firm or PROFILE_FIRMS.get(args.profile)
    if not slug:
        available = ", ".join(sorted(p.stem for p in FIRMS_DIR.glob("*.yaml")))
        print(f"error: no firm is mapped to profile '{args.profile}' — pass --firm "
              f"(available: {available})", file=sys.stderr)
        return 2
    try:
        config = load_firm(slug)
    except DataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if config.profile and config.profile != args.profile:
        print(f"note: {slug} is written for the '{config.profile}' profile, and you "
              f"asked for '{args.profile}' — generating anyway")

    model = json.loads(model_file.read_text(encoding="utf-8"))
    firm = Firm(model, config, args.people, args.seed)
    firm.build()

    validator = Validator(model, firm.rows)
    validator.run()
    errors = firm.errors + validator.errors
    warnings = firm.warnings + validator.warnings

    # Per profile as well as per firm. One firm can be used by more than one
    # profile — Fabrikam is both law-firm and law-firm-crm — and a shared folder
    # meant the second run silently overwrote the first, which is how a stale
    # row count ended up in the documentation.
    out_dir = Path(args.out) if args.out else BUILD_DIR / "demo" / args.profile / slug
    if not errors:
        out_dir.mkdir(parents=True, exist_ok=True)
        for table, rows in sorted(firm.rows.items()):
            clean = [{k: v for k, v in row.items()
                      if not k.startswith("_") and v is not None} for row in rows]
            (out_dir / f"{table}.json").write_text(
                json.dumps(clean, indent=2), encoding="utf-8")
        (out_dir / "manifest.json").write_text(json.dumps({
            "firm": config.name, "firmSlug": slug, "story": config.story,
            "profile": args.profile,
            "people": args.people, "seed": args.seed, "generatedFor": TODAY.isoformat(),
            "rowCounts": {t: len(r) for t, r in sorted(firm.rows.items())},
            "totalRows": sum(len(r) for r in firm.rows.values()),
            "loadOrder": load_order(model, firm.rows),
            "notes": firm.notes,
            "warnings": warnings,
        }, indent=2), encoding="utf-8")

        print(f"Wrote {out_dir}")
        print(f"  {config.name}: {args.people} staff, "
              f"{sum(len(r) for r in firm.rows.values())} rows across {len(firm.rows)} tables")
        for table, rows in sorted(firm.rows.items(), key=lambda kv: -len(kv[1])):
            print(f"    {len(rows):5d}  {table}")
        for note in firm.notes:
            print(f"  note: {note}")
    for warning in warnings:
        print(f"  warning: {warning}")
    for error in errors[:40]:
        print(f"  ERROR: {error}")
    if len(errors) > 40:
        print(f"  … and {len(errors) - 40} more")

    if errors:
        return 1
    if args.strict and warnings:
        print("\nfailing because --strict was given and there are warnings")
        return 1
    return 0


def load_order(model: dict[str, Any], rows: dict[str, list[Any]]) -> list[str]:
    """Tables ordered so a lookup target is always loaded before its referrers."""
    entities = model.get("entities", {})
    remaining = set(rows)
    ordered: list[str] = []
    while remaining:
        ready = []
        for table in sorted(remaining):
            targets = {
                spec.get("target")
                for spec in entities.get(table, {}).get("attributes", {}).values()
                if spec["type"] in ("lookup", "parent") and not spec.get("trait")
            }
            if not (targets & remaining - {table}):
                ready.append(table)
        if not ready:                      # a cycle: break it and say so
            ready = [sorted(remaining)[0]]
        ordered.extend(ready)
        remaining -= set(ready)
    return ordered


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Output was piped into something that closed early, e.g. `| head`.
        os._exit(0)
