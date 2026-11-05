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
    work: dict[str, Any]
    intranet: dict[str, Any]

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

    @property
    def runs_sprints(self) -> bool:
        """A software company runs iterations; a practice runs matter phases.

        Same tables either way — only the cadence differs, which is the whole
        argument for one work module serving both.
        """
        return str(self.work.get("cadence", "phases")) == "sprints"

    def is_billable_department(self, code: str) -> bool:
        """Whether this department's people charge their time to clients.

        Defaults to every department the firm files mark as a DELIVERY family,
        which is right for a practice. A software company overrides it, because
        its billable people sit in Engineering, whose family is IT.
        """
        declared = self.work.get("billableDepartments")
        if declared:
            return code in set(declared)
        return any(d["code"] == code and d.get("family") == "DELIVERY"
                   for d in self.departments)


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

    work = raw.get("work") or {}
    if work:
        if work.get("cadence") not in ("phases", "sprints"):
            raise DataError(
                f"{path.name}: work.cadence must be 'phases' or 'sprints', "
                f"not {work.get('cadence')!r}")
        for section, fields in (("projectTypes", ("code", "name", "model", "phases")),
                                ("roles", ("code", "name", "billRate", "costRate")),
                                ("timeCodes", ("code", "name", "billable", "share"))):
            rows = work.get(section) or []
            if not rows:
                raise DataError(f"{path.name}: work has no '{section}'")
            for index, row in enumerate(rows):
                for field_name in fields:
                    if row.get(field_name) is None:
                        raise DataError(
                            f"{path.name}: work.{section}[{index}] has no "
                            f"'{field_name}'")
            seen = [row["code"] for row in rows]
            if len(seen) != len(set(seen)):
                raise DataError(f"{path.name}: duplicate work.{section} codes")
        total = sum(float(row["share"]) for row in work["timeCodes"])
        if not 0.95 <= total <= 1.05:
            raise DataError(
                f"{path.name}: work.timeCodes shares add up to {total:.2f}, "
                "not ~1.0")
        # A department named here that does not exist means a firm whose
        # billable half is silently empty, and a utilisation report of zeros.
        unknown = [c for c in (work.get("billableDepartments") or [])
                   if c not in set(codes)]
        if unknown:
            raise DataError(
                f"{path.name}: work.billableDepartments names "
                f"{', '.join(unknown)}, which are not departments of this firm")
        # A cost rate above the bill rate is a loss-making role, which is
        # always a typo in demo data rather than a business decision.
        for index, role in enumerate(work["roles"]):
            if float(role["costRate"]) >= float(role["billRate"]):
                raise DataError(
                    f"{path.name}: work.roles[{index}] ({role['code']}) has a "
                    "cost rate at or above its bill rate")

    intranet = raw.get("intranet") or {}
    if intranet:
        for index, category in enumerate(intranet.get("serviceCategories") or []):
            for field_name in ("code", "name", "unit", "response", "resolve"):
                if category.get(field_name) is None:
                    raise DataError(
                        f"{path.name}: intranet.serviceCategories[{index}] has "
                        f"no '{field_name}'")
            if category["unit"] not in set(codes):
                raise DataError(
                    f"{path.name}: intranet.serviceCategories[{index}] is owned "
                    f"by {category['unit']}, which is not a department of this firm")
            # A target resolution inside the target response is an SLA that is
            # breached the moment it is set.
            if int(category["resolve"]) <= int(category["response"]):
                raise DataError(
                    f"{path.name}: intranet.serviceCategories[{index}] "
                    f"({category['code']}) resolves in {category['resolve']}h but "
                    f"responds in {category['response']}h")
        for index, policy in enumerate(intranet.get("policies") or []):
            for field_name in ("number", "title", "category"):
                if policy.get(field_name) is None:
                    raise DataError(
                        f"{path.name}: intranet.policies[{index}] has no "
                        f"'{field_name}'")
            if policy.get("attest") and not policy.get("days"):
                raise DataError(
                    f"{path.name}: intranet.policies[{index}] "
                    f"({policy['number']}) requires attestation but gives no "
                    "'days' to do it in")
        numbers = [p["number"] for p in intranet.get("policies") or []]
        if len(numbers) != len(set(numbers)):
            raise DataError(f"{path.name}: duplicate intranet.policies numbers")

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
        work=raw.get("work") or {},
        intranet=raw.get("intranet") or {},
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

# A request longer than this needs the service line head as well as the line
# manager. The number is in one place because the policy's name, its threshold
# and the selector all have to agree, and they did not: the selector required
# more than ten days while leave was capped at ten, so the two-step chain was
# seeded and never used.
LONG_LEAVE_DAYS = 10


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


# Destinations the demo travels to, with a plausible risk level and a one-way
# flight cost and emissions figure. Risk is what drives the duty-of-care page,
# so the mix deliberately includes places that need security approval.
# Every country here must be in the core Country seed, because a trip's
# destinationCountry is a lookup at it. test_demodata enforces that.
DESTINATIONS = [
    {"city": "Dublin", "country": "IE", "cost": 180, "co2": 95, "risk": "low",
     "hours": 1.5, "share": 0.11},
    {"city": "Amsterdam", "country": "NL", "cost": 210, "co2": 110, "risk": "low",
     "hours": 1.5, "share": 0.13},
    {"city": "Frankfurt", "country": "DE", "cost": 240, "co2": 125, "risk": "low",
     "hours": 2.0, "share": 0.12},
    {"city": "Paris", "country": "FR", "cost": 170, "co2": 90, "risk": "low",
     "hours": 1.5, "share": 0.12},
    {"city": "New York", "country": "US", "cost": 980, "co2": 1_050, "risk": "low",
     "hours": 8.0, "share": 0.14},
    {"city": "Toronto", "country": "CA", "cost": 860, "co2": 980, "risk": "low",
     "hours": 8.0, "share": 0.07},
    {"city": "Dubai", "country": "AE", "cost": 1_150, "co2": 1_250, "risk": "medium",
     "hours": 7.0, "share": 0.09},
    {"city": "Mumbai", "country": "IN", "cost": 1_050, "co2": 1_400, "risk": "high",
     "hours": 9.0, "share": 0.10},
    {"city": "Singapore", "country": "SG", "cost": 1_280, "co2": 1_900, "risk": "low",
     "hours": 13.0, "share": 0.05},
    {"city": "Tokyo", "country": "JP", "cost": 1_320, "co2": 1_850, "risk": "low",
     "hours": 12.0, "share": 0.04},
    {"city": "Sydney", "country": "AU", "cost": 1_450, "co2": 2_400, "risk": "low",
     "hours": 22.0, "share": 0.03},
]
RISK_RANK = {"low": 1, "medium": 2, "high": 3, "extreme": 4}

# What a trip's expenses are made of, as a share of the claim.
NEWS_SUBJECTS = [
    "a new studio in the north", "our carbon reporting for the year",
    "the summer intake arrives", "a planning consent on the riverside scheme",
    "what the new building regulations change", "our best-of-year drawings",
    "a conservation award shortlisting", "the mentoring scheme reopens",
    "an update on the office refurbishment", "how we are measuring wellbeing",
]
EVENT_SUBJECTS = [
    "quarterly all-hands", "Passivhaus detailing", "fire safety refresher",
    "the autumn social", "client breakfast on retrofit",
    "BIM coordination clinic", "mental health first aid",
    "a lunchtime talk on timber", "volunteering at the community garden",
]
KB_SUBJECTS = [
    "setting up a new project folder", "exporting a drawing set",
    "requesting CAD licences", "booking a meeting room",
    "claiming mileage", "the drawing numbering convention",
    "how to raise a change request", "accessing the model from site",
    "submitting a planning pack", "the template library",
]
SR_SUBJECTS = [
    "licence will not activate", "plotter jammed again",
    "cannot open the shared model", "new starter needs accounts",
    "laptop running very slowly", "need access to a project folder",
    "monitor flickering", "VPN drops on site", "password expired",
    "desk phone not ringing", "request a second screen",
]
RECOGNITION_REASONS = [
    "turning the planning pack round over a weekend",
    "covering a site visit at no notice",
    "patiently teaching the new starters the model standards",
    "spotting the clash before it reached the contractor",
    "keeping the client calm through a difficult week",
    "rewriting the template library nobody else would touch",
]

# A four-point scale multiplied out, so a RAID severity score is probability
# times impact rather than a number somebody invented.
IMPACT_SCORE = {"low": 1, "medium": 2, "high": 3, "veryHigh": 4}
WORK_ITEM_SUBJECTS = [
    "data model review", "client sign-off pack", "migration dry run",
    "access provisioning", "integration endpoint", "reporting layer",
    "performance pass", "accessibility audit", "cutover plan",
    "training material", "handover documentation", "defect triage",
    "environment refresh", "security review", "stakeholder workshop",
]
RAID_SUBJECTS = [
    "client data arriving late", "a dependency on a third-party release",
    "scope added without a change order", "key person on leave at cutover",
    "legacy system access not granted", "test environment unavailable",
    "regulatory sign-off outstanding", "a fixed-fee phase running over",
    "an assumption about data quality", "the integration contract changing",
]
# How many weeks of timesheet history a full run generates. 44 is a working
# year less holiday, which at the default headcount clears the roadmap's
# 10,000-time-entry exit criterion without inventing a decade of history.
TIMESHEET_WEEKS = 44
# Entries a person logs in a week. More than one a day, because a consultant
# splits a day across a client call, the work itself and an internal task —
# and a timesheet with one row a day is not a timesheet anyone recognises.
ENTRIES_PER_WEEK = (4, 9)
# The shape of a work item backlog, as a share of the items on a project.
WORK_ITEM_MIX = [
    {"code": "STORY", "name": "Story", "category": "story", "share": 0.34},
    {"code": "TASK", "name": "Task", "category": "task", "share": 0.28},
    {"code": "BUG", "name": "Bug", "category": "bug", "share": 0.14},
    {"code": "DELIV", "name": "Deliverable", "category": "deliverable", "share": 0.10},
    {"code": "FEAT", "name": "Feature", "category": "feature", "share": 0.07},
    {"code": "ISSUE", "name": "Issue", "category": "issue", "share": 0.04},
    {"code": "EPIC", "name": "Epic", "category": "epic", "share": 0.03},
]
# Each category carries the typical spend and the policy limit it is judged
# against, so an over-limit line is the natural consequence of the draw rather
# than a flag set at random. HOTEL is per night and its limit matches the
# generated TravelPolicy's maxHotelRatePerNight; the rest are per line.
EXPENSE_MIX = [
    {"code": "HOTEL", "share": 0.30, "nightly": True, "typical": 150, "cap": 220},
    {"code": "MEALS", "share": 0.18, "typical": 45, "cap": 70},
    {"code": "TAXI", "share": 0.14, "typical": 40, "cap": 60},
    {"code": "AIR", "share": 0.12, "typical": 600, "cap": 900},
    {"code": "RAIL", "share": 0.08, "typical": 165, "cap": 250},
    {"code": "PARKING", "share": 0.06, "typical": 55, "cap": 80},
    {"code": "COMMS", "share": 0.05, "typical": 33, "cap": 50},
    {"code": "CONF_FEES", "share": 0.04, "typical": 800, "cap": 1200},
    {"code": "VISA_FEES", "share": 0.03, "typical": 135, "cap": 200},
]
# Every limit sits at about 1.5x the typical value, and the multiplier below is
# drawn from [0.45, 1.65), so roughly one line in eight breaches its category
# limit. A claim set where nothing is ever over is not worth reviewing.
EXPENSE_SPREAD = (0.45, 1.65)
# Rank of a cabin class, so "above the policy's class" is a comparison rather
# than a hardcoded list of offending values.
CABIN_RANK = {"economy": 1, "premiumEconomy": 2, "business": 3, "first": 4}

MERCHANTS = [
    "Skyline Airways", "Harbourview Hotel", "City Rail", "Metro Cabs",
    "Northgate Parking", "Riverside Bistro", "Summit Conferences",
    "Waypoint Car Hire", "Transit Telecom", "Gateway Lounge",
]


class DataError(Exception):
    pass


class Firm:
    """Builds one coherent firm. Every method appends to self.rows."""

    def __init__(self, model: dict[str, Any], config: FirmConfig, people: int,
                 seed: int, weeks: int = TIMESHEET_WEEKS):
        self.model = model
        self.config = config
        self.entities = model.get("entities", {})
        self.enums = model.get("enums", {})
        self.seeds = model.get("seed", {})
        self.people = people
        # How much timesheet history to generate. The default clears the
        # roadmap's 10,000-time-entry exit criterion at the default headcount;
        # tests that are not about volume pass a smaller number so the suite
        # stays fast.
        self.weeks = max(1, weeks)
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
            # After matters, so on a profile with the CRM this adds delivery
            # data to the projects won pitches created rather than duplicating
            # them.
            ("delivery", ("ProjectRole", "TimeCode", "WorkItemType",
                          "ProjectPhaseDefinition", "ProjectPhase",
                          "ProjectMember", "WorkItem", "WorkItemDependency",
                          "Milestone", "Timesheet", "TimeEntry",
                          "ResourceRequest", "RaidItem", "StatusReport"),
             self.build_delivery),
            ("intranet", ("Site", "QuickLink", "NewsArticle", "Announcement",
                          "CompanyEvent", "EventRegistration", "PolicyDocument",
                          "PolicyAttestation", "KnowledgeArticle",
                          "ServiceCategory", "ServiceRequest", "Recognition"),
             self.build_intranet),
            ("travel", ("TravelPolicy", "DestinationAdvisory", "PerDiemRate",
                        "TripRequest", "TripSegment", "TravelBooking",
                        "ExpenseReport", "ExpenseLine", "CardTransaction"),
             self.build_travel),
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
            **self.optional("OrgUnit", isDeliveryUnit=False),
        })
        for dept in self.config.departments:
            self.add("OrgUnit", {
                "code": dept["code"], "name": dept["name"], "unitType": dept["unitType"],
                "parentOrgUnit": "FIRM", "legalEntity": self.config.firm["code"],
                "defaultCostCenter": f"CC-{dept['code']}",
                # Which units earn fees. A utilisation report divides by this;
                # without it the number is the firm's billable headcount ratio.
                **self.optional("OrgUnit", isDeliveryUnit=
                                self.config.is_billable_department(dept["code"])),
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
                # Up to four weeks: parental and study leave are measured in
                # weeks, and without a request over ten days the two-step
                # approval policy can never be chosen.
                length = self.random.choice([1, 1, 2, 3, 5, 5, 10, 15, 20])
                end = start + dt.timedelta(days=length - 1)
                if end > year_end:
                    continue
                duration = float(length)

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
            "code": "POL-LEAVE-LONG",
            "name": f"Leave — over {LONG_LEAVE_DAYS} days",
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
                "policy": ("POL-LEAVE-LONG" if request["duration"] > LONG_LEAVE_DAYS
                           else "POL-LEAVE-ALL"),
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

    # -- Intranet ------------------------------------------------------------
    def build_intranet(self) -> None:
        """Sites, news, events, policies, knowledge, the service desk.

        Two things here are the pilot rather than decoration. **Audience
        targeting** is what an intranet is judged on: every piece of content
        carries audienceOrgUnits and audienceOffices, empty meaning everyone,
        and a demo where everything is addressed to everyone demonstrates
        nothing. **Policy attestation** is the other: the report has to show
        people who have not signed, including some who are past their due date,
        or there is nothing to chase.
        """
        people = [p for p in self.employees if p["_left"] is None]
        if not people:
            self.warnings.append("no active staff, so no intranet content")
            return
        config = self.config.intranet
        if not config:
            self.warnings.append(
                f"{self.config.slug} has no 'intranet' section, so no intranet "
                "content")
            return

        self._build_sites(config, people)
        self._build_quick_links()
        self._build_news(people)
        self._build_announcements()
        self._build_events(people)
        self._build_policies(config, people)
        self._build_knowledge(people)
        self._build_service_desk(config, people)
        self._build_recognition(people)

    def _audience(self, everyone: float = 0.6) -> dict[str, Any]:
        """Who a piece of content is for. Empty means everyone.

        Most content is for everybody, which is why the default is 60% — but
        the interesting cases are the targeted ones, and a demo with none of
        them cannot show that audience targeting works at all.
        """
        if self.random.random() < everyone:
            return {}
        out: dict[str, Any] = {}
        if self.random.random() < 0.7:
            units = self.random.sample(
                [d["code"] for d in self.config.departments],
                self.random.randint(1, min(3, len(self.config.departments))))
            out["audienceOrgUnits"] = units
        if not out or self.random.random() < 0.4:
            out["audienceOffices"] = self.random.sample(
                [o["code"] for o in self.config.offices],
                self.random.randint(1, len(self.config.offices)))
        return out

    def _build_sites(self, config: dict[str, Any],
                     people: list[dict[str, Any]]) -> None:
        """The site inventory, which is what site governance reports on."""
        domain = config.get("domain", "contoso.sharepoint.example")
        hub_name = config.get("hubName", f"{self.config.name} Hub")
        owners = [p for p in people
                  if p["_dept"]["code"] in {d["code"] for d in self.config.departments}]
        hub = self.add("Site", {
            "code": "SITE-HUB",
            "name": hub_name,
            "url": f"https://{domain}/sites/hub",
            "siteType": "hub",
            "primaryOwner": self.random.choice(owners)["employeeNumber"],
            "secondaryOwner": self.random.choice(owners)["employeeNumber"],
            "teamsEnabled": False,
            "externalSharingEnabled": False,
            "lastReviewedOn": self.iso(TODAY - dt.timedelta(days=40)),
            "nextReviewDate": self.iso(TODAY + dt.timedelta(days=325)),
            "description": "The front door. Every other site rolls up to it.",
        })

        index = 0
        for department in self.config.departments:
            index += 1
            head = next((p for p in people
                         if p["_dept"]["code"] == department["code"]), None)
            if head is None:
                continue
            # A site nobody has reviewed for over a year is the finding a
            # governance report exists to produce, so some are overdue.
            reviewed = TODAY - dt.timedelta(days=self.random.choice(
                [20, 60, 120, 200, 400, 500]))
            self.add("Site", {
                "code": f"SITE-{department['code']}",
                "name": f"{department['name']}",
                "url": f"https://{domain}/sites/{department['code'].lower()}",
                "siteType": "team",
                "hubSite": "SITE-HUB",
                "primaryOwner": head["employeeNumber"],
                "secondaryOwner": self.random.choice(owners)["employeeNumber"],
                "orgUnit": department["code"],
                "teamsEnabled": True,
                "externalSharingEnabled": False,
                "lastReviewedOn": self.iso(reviewed),
                "nextReviewDate": self.iso(reviewed + dt.timedelta(days=365)),
            })

        for name in config.get("communities") or []:
            index += 1
            community_reviewed = TODAY - dt.timedelta(
                days=self.random.choice([45, 90, 200, 395, 480]))
            self.add("Site", {
                "code": f"SITE-COM{index:02d}",
                "name": name,
                "url": f"https://{domain}/sites/"
                       f"{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}",
                "siteType": "community",
                "hubSite": "SITE-HUB",
                "primaryOwner": self.random.choice(owners)["employeeNumber"],
                "teamsEnabled": True,
                "externalSharingEnabled": False,
                "lastReviewedOn": self.iso(community_reviewed),
                # A community site with no review date at all is the row a
                # governance report should flag hardest, so one is left
                # without one on purpose.
                "nextReviewDate": self.iso(
                    community_reviewed + dt.timedelta(days=365))
                if index % 3 else None,
            })

        # One client extranet with external sharing on, because that is the
        # row a governance review has to look at first.
        clients = [a["code"] for a in self.rows.get("Account", [])]
        self.add("Site", {
            "code": "SITE-EXT01",
            "name": "Client collaboration",
            "url": f"https://{domain}/sites/client-collab",
            "siteType": "clientExtranet",
            "hubSite": "SITE-HUB",
            "primaryOwner": self.random.choice(owners)["employeeNumber"],
            "client": self.random.choice(clients) if clients else None,
            "teamsEnabled": True,
            "externalSharingEnabled": True,
            "lastReviewedOn": self.iso(TODAY - dt.timedelta(days=15)),
            "nextReviewDate": self.iso(TODAY + dt.timedelta(days=75)),
            "description": "External sharing is on. Reviewed quarterly.",
        })
        overdue = [s for s in self.rows["Site"]
                   if s.get("nextReviewDate")
                   and dt.date.fromisoformat(s["nextReviewDate"]) < TODAY]
        unscheduled = [s for s in self.rows["Site"] if not s.get("nextReviewDate")]
        self.notes.append(
            f"{len(self.rows['Site'])} sites, {len(overdue)} past their review "
            f"date and {len(unscheduled)} with no review date at all, plus one "
            "client extranet with external sharing on")

    def _build_quick_links(self) -> None:
        links = [
            ("Timesheets", "Apps", True), ("Expenses", "Finance", True),
            ("Book leave", "HR", True), ("IT service desk", "IT", True),
            ("Drawing register", "Apps", False), ("Document templates", "Knowledge", False),
            ("Payslips", "HR", False), ("Room booking", "Apps", False),
            ("BIM standards", "Knowledge", False), ("RIBA plan of work", "External", False),
            ("Building regulations", "External", False), ("Password reset", "IT", False),
        ]
        for order, (title, category, pinned) in enumerate(links, start=1):
            self.add("QuickLink", {
                "code": f"LINK-{order:03d}",
                "title": title,
                "url": f"https://apps.{self.config.firm['domain']}/"
                       f"{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')}",
                "description": f"{title} for everyone at {self.config.name}.",
                "category": category,
                "sortOrder": order,
                "openInNewTab": category == "External",
                "isPinned": pinned,
                **self._audience(everyone=0.75),
            })

    def _build_news(self, people: list[dict[str, Any]]) -> None:
        categories = self.model["termSets"]["NewsCategory"]["terms"]
        authors = [p for p in people
                   if p["_dept"]["code"] in ("BUS", "PPL", "MKT", "OPS")] or people
        for index in range(1, 46):
            # Well over a year, so some published posts are stale — the clutter
            # that makes an intranet search result useless is the thing the
            # adoption page exists to count.
            written = self.date_between(TODAY - dt.timedelta(days=620), TODAY)
            status = self.pick_weighted([
                {"code": "published", "share": 0.72},
                {"code": "draft", "share": 0.12},
                {"code": "inReview", "share": 0.08},
                {"code": "archived", "share": 0.05},
                {"code": "scheduled", "share": 0.03}])["code"]
            if status == "scheduled":
                written = TODAY + dt.timedelta(days=self.random.randint(2, 21))
            category = self.random.choice(categories)
            published = status in ("published", "archived")
            self.add("NewsArticle", {
                "code": f"NEWS-{index:04d}",
                "title": f"{category}: {self.random.choice(NEWS_SUBJECTS)}",
                "summary": f"{category} update for {self.config.name}.",
                "body": f"<p>{category} update for {self.config.name}.</p>",
                "category": category,
                "writtenBy": self.random.choice(authors)["employeeNumber"],
                "status": status,
                "publishedOn": f"{self.iso(written)}T09:00:00Z"
                if published or status == "scheduled" else None,
                "expiresOn": f"{self.iso(written + dt.timedelta(days=365))}T09:00:00Z"
                if status == "archived" else None,
                "isFeatured": published and self.random.random() < 0.15,
                # A view count only means something on something published.
                "viewCount": self.random.randint(12, 950) if published else 0,
                "pageUrl": f"https://{self.config.intranet.get('domain')}"
                           f"/sites/hub/news/{index:04d}",
                **self._audience(),
            })
        featured = [n for n in self.rows["NewsArticle"] if n["isFeatured"]]
        if not featured:
            published = [n for n in self.rows["NewsArticle"]
                         if n["status"] == "published"]
            if published:
                published[0]["isFeatured"] = True
                self.notes.append(
                    "forced one news article to be featured, so the hub's hero "
                    "web part has something to show")

    def _build_announcements(self) -> None:
        items = [
            ("Studio closed for the bank holiday", "info", 0, 3),
            ("Planned network maintenance on Saturday", "warning", -1, 2),
            ("We have been shortlisted for a RIBA award", "success", -4, 10),
            ("Phishing campaign in progress — do not click", "critical", 0, 1),
            ("New BIM standards take effect next month", "info", -10, 25),
        ]
        for index, (title, severity, start_offset, days) in enumerate(items, start=1):
            start = TODAY + dt.timedelta(days=start_offset)
            self.add("Announcement", {
                "code": f"ANN-{index:03d}",
                "title": title,
                "message": f"{title}. See the intranet for details.",
                "severity": severity,
                "startOn": f"{self.iso(start)}T07:00:00Z",
                "endOn": f"{self.iso(start + dt.timedelta(days=days))}T19:00:00Z",
                "isDismissible": severity != "critical",
                **self._audience(everyone=0.8),
            })

    def _build_events(self, people: list[dict[str, Any]]) -> None:
        types = self.model["termSets"]["EventType"]["terms"]
        offices = [o["code"] for o in self.config.offices]
        registration_index = 0
        for index in range(1, 25):
            event_type = self.random.choice(types)
            start = self.date_between(TODAY - dt.timedelta(days=150),
                                      TODAY + dt.timedelta(days=90))
            past = start < TODAY
            capacity = self.random.choice([12, 20, 30, 40, 60, 120])
            needs_registration = self.random.random() < 0.7
            status = ("archived" if past and self.random.random() < 0.3
                      else "published")
            event = self.add("CompanyEvent", {
                "code": f"EVT-{index:04d}",
                "title": f"{event_type}: {self.random.choice(EVENT_SUBJECTS)}",
                "description": f"<p>{event_type} at {self.config.name}.</p>",
                "eventType": event_type,
                "startOn": f"{self.iso(start)}T09:30:00Z",
                "endOn": f"{self.iso(start)}T12:30:00Z",
                "isAllDay": False,
                "timeZone": self.config.firm.get("timeZone", "Europe/London"),
                "office": self.random.choice(offices),
                "venue": "Studio" if self.random.random() < 0.6 else "Online",
                "organizer": self.random.choice(people)["employeeNumber"],
                "capacity": capacity,
                "registrationRequired": needs_registration,
                "registrationDeadline": f"{self.iso(start - dt.timedelta(days=2))}"
                                        "T17:00:00Z" if needs_registration else None,
                # An architecture practice counts CPD hours, and a training
                # session that carries none is a training session nobody logs.
                "cpdHours": float(self.random.choice([1.0, 1.5, 2.0, 3.0]))
                if event_type in ("Training session", "Webinar", "Conference")
                else None,
                "status": status,
                **self._audience(everyone=0.55),
            })
            if not needs_registration:
                continue

            # Deliberately oversubscribe some events, so the waitlist is real.
            wanted = min(len(people),
                         int(capacity * self.random.uniform(0.3, 1.4)))
            for position, attendee in enumerate(
                    self.random.sample(people, wanted), start=1):
                registration_index += 1
                registered = start - dt.timedelta(days=self.random.randint(3, 30))
                # Position within *this* event. Using the running total made
                # every registration after the first event a waitlisting: 453
                # of 524, which is not an event, it is a queue.
                over = position > capacity
                if over:
                    status_key = "waitlisted"
                elif past:
                    status_key = self.pick_weighted([
                        {"code": "attended", "share": 0.74},
                        {"code": "noShow", "share": 0.16},
                        {"code": "cancelled", "share": 0.10}])["code"]
                else:
                    status_key = self.pick_weighted([
                        {"code": "registered", "share": 0.92},
                        {"code": "cancelled", "share": 0.08}])["code"]
                self.add("EventRegistration", {
                    "code": f"{event['code']}-R{position:05d}",
                    "name": f"{attendee['displayName']} — {event['title']}",
                    "event": event["code"],
                    "attendee": attendee["employeeNumber"],
                    "status": status_key,
                    "registeredOn": f"{self.iso(max(registered, TODAY - dt.timedelta(days=360)))}T10:00:00Z",
                    "checkedInOn": f"{self.iso(start)}T09:35:00Z"
                    if status_key == "attended" else None,
                    "dietaryRequirements": self.random.choice(
                        ["Vegetarian", "Vegan", "Gluten free", None, None, None]),
                })

    def _build_policies(self, config: dict[str, Any],
                        people: list[dict[str, Any]]) -> None:
        """Policies, and the attestation record that is Phase 5's exit criterion."""
        owners = [p for p in people if p["_dept"]["code"] in ("BUS", "PPL", "TEC")] \
            or people
        attestation_index = 0
        for position, policy in enumerate(config.get("policies") or []):
            requires = bool(policy.get("attest"))
            due_days = int(policy.get("days") or 30)
            # Every policy being long-standing made every unsigned attestation
            # overdue, and an attestation report whose only state is "overdue"
            # cannot show the difference between somebody who is late and
            # somebody who simply has not got to it yet. A third are issued
            # recently enough that their window is still open.
            if requires and position % 3 == 1:
                effective = TODAY - dt.timedelta(
                    days=self.random.randint(1, max(1, due_days - 2)))
            else:
                effective = TODAY - dt.timedelta(days=self.random.randint(90, 400))
            # Every fourth policy is overdue for review. A policy past its
            # own review date is a policy nobody owns any more, and it is the
            # finding the compliance page is for — left to chance it never
            # appeared at all.
            review = effective + dt.timedelta(
                days=200 if position % 4 == 2 else 365)
            row = self.add("PolicyDocument", {
                "code": policy["number"],
                "title": policy["title"],
                "documentNumber": policy["number"],
                "category": policy["category"],
                "policyOwner": self.random.choice(owners)["employeeNumber"],
                "approver": self.random.choice(owners)["employeeNumber"],
                "legalEntity": self.config.firm["code"],
                "versionLabel": f"{self.random.randint(1, 4)}.0",
                "status": "published",
                "effectiveDate": self.iso(effective),
                "nextReviewDate": self.iso(review),
                "summary": f"{policy['title']} for {self.config.name}.",
                "requiresAttestation": requires,
                "attestationDueDays": due_days if requires else None,
                "documentUrl": f"https://{config.get('domain')}/sites/hub/policies/"
                               f"{policy['number']}.pdf",
            })
            if not requires:
                continue

            # Everyone has to attest, and the report is only useful if some
            # have not. The due date is the effective date plus the policy's
            # own window, so "overdue" is a fact about the data rather than a
            # status somebody typed.
            due = effective + dt.timedelta(days=due_days)
            for person in people:
                attestation_index += 1
                joined = person.get("_joined")
                if joined and joined > due:
                    continue           # joined after the window closed
                roll = self.random.random()
                if roll < 0.82:
                    status = "completed"
                elif roll < 0.95:
                    status = "overdue" if due < TODAY else "pending"
                else:
                    status = "exempt"
                attested = None
                if status == "completed":
                    attested = self.date_between(
                        effective, min(due + dt.timedelta(days=5), TODAY))
                self.add("PolicyAttestation", {
                    "code": f"ATT-{attestation_index:06d}",
                    "name": f"{person['displayName']} — {policy['number']}",
                    "policy": policy["number"],
                    "employee": person["employeeNumber"],
                    "policyVersion": row["versionLabel"],
                    "status": status,
                    "dueDate": self.iso(due),
                    "attestedOn": f"{self.iso(attested)}T11:00:00Z"
                    if attested else None,
                    "method": self.pick_weighted([
                        {"code": "clickThrough", "share": 0.74},
                        {"code": "eSignature", "share": 0.18},
                        {"code": "trainingCompletion", "share": 0.08}])["code"]
                    if status == "completed" else None,
                })
        outstanding = [a for a in self.rows.get("PolicyAttestation", [])
                       if a["status"] in ("pending", "overdue")]
        overdue = [a for a in outstanding if a["status"] == "overdue"]
        self.notes.append(
            f"{len(self.rows.get('PolicyAttestation', []))} attestation(s), "
            f"{len(outstanding)} outstanding of which {len(overdue)} overdue, "
            "so the attestation report has something to chase")

    def _build_knowledge(self, people: list[dict[str, Any]]) -> None:
        categories = self.model["termSets"]["KnowledgeCategory"]["terms"]
        for index in range(1, 37):
            category = self.random.choice(categories)
            published = self.date_between(TODAY - dt.timedelta(days=500), TODAY)
            status = self.pick_weighted([
                {"code": "published", "share": 0.84},
                {"code": "draft", "share": 0.10},
                {"code": "archived", "share": 0.06}])["code"]
            helpful_yes = self.random.randint(0, 60)
            self.add("KnowledgeArticle", {
                "code": f"KB-{index:04d}",
                "title": f"{category}: {self.random.choice(KB_SUBJECTS)}",
                "summary": f"{category} guidance.",
                "body": f"<p>{category} guidance for {self.config.name}.</p>",
                "category": category,
                "articleOwner": self.random.choice(people)["employeeNumber"],
                "status": status,
                "publishedOn": f"{self.iso(published)}T09:00:00Z"
                if status != "draft" else None,
                # Some are overdue for review, which is the number that tells a
                # knowledge base from a graveyard.
                "nextReviewDate": self.iso(published + dt.timedelta(
                    days=self.random.choice([180, 365, 365, 540]))),
                "helpfulYes": helpful_yes,
                "helpfulNo": self.random.randint(0, max(1, helpful_yes // 4)),
            })
        stale = [k for k in self.rows["KnowledgeArticle"]
                 if dt.date.fromisoformat(k["nextReviewDate"]) < TODAY]
        self.notes.append(
            f"{len(stale)} of {len(self.rows['KnowledgeArticle'])} knowledge "
            "article(s) are past their review date")

    def _build_service_desk(self, config: dict[str, Any],
                            people: list[dict[str, Any]]) -> None:
        """The service desk, with SLA targets it sometimes misses."""
        categories = config.get("serviceCategories") or []
        if not categories:
            return
        for category in categories:
            assignees = [p for p in people
                         if p["_dept"]["code"] == category["unit"]] or people
            self.add("ServiceCategory", {
                "code": category["code"],
                "name": category["name"],
                "owningOrgUnit": category["unit"],
                "defaultAssignee": self.random.choice(assignees)["employeeNumber"],
                "targetResponseHours": int(category["response"]),
                "targetResolutionHours": int(category["resolve"]),
                "requiresApproval": category["code"] in ("IT_ACCESS", "IT_HW"),
                "description": f"{category['name']} requests.",
            })

        offices = [o["code"] for o in self.config.offices]
        for index in range(1, 221):
            category = self.random.choice(categories)
            assignees = [p for p in people
                         if p["_dept"]["code"] == category["unit"]] or people
            requester = self.random.choice(people)
            raised = (self.date_between(TODAY - dt.timedelta(days=180), TODAY)
                      .isoformat() + "T"
                      f"{self.random.randint(8, 17):02d}:"
                      f"{self.random.choice(['05', '17', '23', '41', '58'])}:00")
            raised_at = dt.datetime.fromisoformat(raised)
            priority = self.pick_weighted([
                {"code": "medium", "share": 0.52},
                {"code": "low", "share": 0.24},
                {"code": "high", "share": 0.19},
                {"code": "critical", "share": 0.05}])["code"]
            status = self.pick_weighted([
                {"code": "closed", "share": 0.52},
                {"code": "resolved", "share": 0.16},
                {"code": "inProgress", "share": 0.12},
                {"code": "assigned", "share": 0.09},
                {"code": "new", "share": 0.06},
                {"code": "waitingOnRequester", "share": 0.04},
                {"code": "cancelled", "share": 0.01}])["code"]
            settled = status in ("resolved", "closed")
            responded = status != "new"
            # One request in six misses its response target and one in five
            # its resolution target: an SLA nothing ever breaches is an SLA
            # nobody set.
            response_hours = (int(category["response"])
                              * (self.random.uniform(1.1, 3.0)
                                 if self.random.random() < 0.17
                                 else self.random.uniform(0.1, 0.9)))
            resolve_hours = (int(category["resolve"])
                             * (self.random.uniform(1.05, 2.4)
                                if self.random.random() < 0.2
                                else self.random.uniform(0.15, 0.95)))
            first_response = raised_at + dt.timedelta(hours=response_hours)
            resolved_on = raised_at + dt.timedelta(hours=resolve_hours)
            self.add("ServiceRequest", {
                "code": f"SR-{index:05d}",
                "title": f"{category['name']}: {self.random.choice(SR_SUBJECTS)}",
                "requester": requester["employeeNumber"],
                "category": category["code"],
                "description": f"{category['name']} request raised by "
                               f"{requester['displayName']}.",
                "priority": priority,
                "status": status,
                "channel": self.pick_weighted([
                    {"code": "portal", "share": 0.44},
                    {"code": "teams", "share": 0.26},
                    {"code": "email", "share": 0.14},
                    {"code": "walkUp", "share": 0.08},
                    {"code": "phone", "share": 0.05},
                    {"code": "virtualAgent", "share": 0.03}])["code"],
                "assignedTo": self.random.choice(assignees)["employeeNumber"]
                if status != "new" else None,
                "office": self.random.choice(offices),
                # raisedOn is when the requester asked, not when the row was
                # created: a ticket phoned in is typed up later, sometimes the
                # next morning, and measuring a response target from createdOn
                # measures the agent's typing. Every SLA measure starts here.
                "raisedOn": f"{raised}Z",
                "dueOn": (raised_at + dt.timedelta(
                    hours=int(category["response"]))).isoformat() + "Z",
                "firstRespondedOn": f"{first_response.isoformat()}Z"
                if responded else None,
                "resolvedOn": f"{resolved_on.isoformat()}Z" if settled else None,
                "resolution": "Resolved and confirmed with the requester."
                if settled else None,
                "satisfactionScore": self.pick_weighted([
                    {"code": 5, "share": 0.46}, {"code": 4, "share": 0.31},
                    {"code": 3, "share": 0.13}, {"code": 2, "share": 0.06},
                    {"code": 1, "share": 0.04}])["code"]
                if status == "closed" and self.random.random() < 0.6 else None,
            })

    def _build_recognition(self, people: list[dict[str, Any]]) -> None:
        values = self.model["termSets"]["CompanyValue"]["terms"]
        for index in range(1, 61):
            giver = self.random.choice(people)
            others = [p for p in people if p is not giver]
            if not others:
                continue
            recipients = self.random.sample(
                others, self.random.randint(1, min(3, len(others))))
            self.add("Recognition", {
                "code": f"REC-{index:04d}",
                "name": f"{giver['displayName']} thanked "
                        f"{recipients[0]['displayName']}"
                        + (f" and {len(recipients) - 1} other(s)"
                           if len(recipients) > 1 else ""),
                "givenBy": giver["employeeNumber"],
                "recipients": [p["employeeNumber"] for p in recipients],
                "message": f"Thank you for {self.random.choice(RECOGNITION_REASONS)}.",
                "companyValue": self.random.choice(values),
                "postedOn": f"{self.iso(self.date_between(TODAY - dt.timedelta(days=200), TODAY))}T15:00:00Z",
                "isPublic": self.random.random() < 0.88,
            })

    # -- Work Management -----------------------------------------------------
    def build_delivery(self) -> None:
        """Projects, backlog, resourcing and the timesheets behind them.

        This is the section the roadmap's Phase 3 exit criterion is about: more
        than ten thousand time entries, consistent enough that a utilisation
        report computed from them is worth reading. So the shape matters more
        than the volume — a firm where everybody is exactly 75% utilised proves
        nothing, and neither does one where the numbers are noise.

        It runs after `build_matters`, so on a profile that has the CRM it
        adds delivery data to the matters won pitches created, and on a
        work-only profile it creates the projects itself.
        """
        people = [p for p in self.employees if p["_left"] is None]
        if not people:
            self.warnings.append("no active staff, so no delivery data")
            return
        work = self.config.work
        if not work:
            self.warnings.append(
                f"{self.config.slug} has no 'work' section, so no delivery data")
            return

        currency = self.config.firm["currency"]
        self._build_delivery_reference(work, currency)
        projects = self._build_projects(work, people, currency)
        if not projects:
            self.warnings.append("no projects, so no delivery data")
            return
        self._build_phases(projects)
        members = self._build_project_members(projects, people, currency)
        if self.config.runs_sprints:
            self._build_sprints(projects, work)
        self._build_work_items(projects, members)
        self._build_milestones(projects, currency)
        self._build_timesheets(projects, members, people, currency)
        # After the timesheets, because the actuals come from them.
        self._roll_up_projects(projects)
        self._build_resourcing(projects, people)
        self._build_raid(projects, people, currency)
        self._build_status_reports(projects)

    def _build_delivery_reference(self, work: dict[str, Any],
                                  currency: str) -> None:
        """Roles, time codes, work item types and the phase definitions."""
        for role in work["roles"]:
            self.add("ProjectRole", {
                "code": role["code"], "name": role["name"],
                "defaultBillRate": float(role["billRate"]),
                "defaultCostRate": float(role["costRate"]),
                "currency": currency,
                "description": f"{role['name']} on a client engagement.",
            })
        for item in work["timeCodes"]:
            self.add("TimeCode", {
                "code": item["code"], "name": item["name"],
                "codeSet": self.config.firm["code"],
                "isBillableDefault": bool(item["billable"]),
                "description": f"{item['name']}, "
                               f"{'chargeable' if item['billable'] else 'not chargeable'}"
                               " to a client by default.",
            })
        for item in WORK_ITEM_MIX:
            self.add("WorkItemType", {
                "code": item["code"], "name": item["name"],
                "category": item["category"],
                # An epic or a feature holds children; a bug or a task does not.
                "allowsChildren": item["category"] in ("epic", "feature"),
                "description": f"{item['name']} on the backlog.",
            })

        # Phase definitions belong to a project type, so a new project of that
        # type gets the right phases without anyone retyping them.
        for project_type in work["projectTypes"]:
            for sequence, phase in enumerate(project_type["phases"], start=1):
                self.add("ProjectPhaseDefinition", {
                    "code": f"{project_type['code']}-{sequence:02d}",
                    "name": phase,
                    "projectType": project_type["code"],
                    "sequence": sequence,
                    "defaultFeePercent": round(
                        100.0 / len(project_type["phases"]), 2),
                    "framework": project_type["name"],
                })

    def _build_projects(self, work: dict[str, Any], people: list[dict[str, Any]],
                        currency: str) -> list[dict[str, Any]]:
        """The projects, reusing any the CRM hand-off already created."""
        existing_types = {row["code"] for row in self.rows.get("ProjectType", [])}
        for project_type in work["projectTypes"]:
            if project_type["code"] in existing_types:
                continue
            self.add("ProjectType", {
                "code": project_type["code"], "name": project_type["name"],
                "defaultBillingModel": project_type["model"],
                "requiresClient": not project_type.get("internal"),
                "isInternal": bool(project_type.get("internal")),
                "numberPrefix": project_type["code"][:4],
            })

        projects = list(self.rows.get("Project", []))
        if projects:
            self.notes.append(
                f"{len(projects)} project(s) came from won pitches; the delivery "
                "data is built on top of them rather than replacing them")

        managers = [p for p in people
                    if self.config.is_billable_department(p["_dept"]["code"])] or people
        clients = [a["code"] for a in self.rows.get("Account", [])]
        client_types = [t for t in work["projectTypes"] if not t.get("internal")]
        internal_types = [t for t in work["projectTypes"] if t.get("internal")]

        # Enough client work that the delivery staff have something to charge
        # to, and at least one project per billable person, which is what makes
        # a resourcing view non-trivial.
        billable = [p for p in people
                    if self.config.is_billable_department(p["_dept"]["code"])]
        wanted = max(8, round(len(billable) * 0.45))
        start_index = len(projects)
        for offset in range(max(0, wanted - len(projects))):
            index = start_index + offset + 1
            project_type = self.pick_weighted(
                [{"t": t, "share": 1.0} for t in client_types or work["projectTypes"]]
            )["t"]
            manager = self.random.choice(managers)
            started = self.date_between(TODAY - dt.timedelta(days=self.weeks * 7),
                                        TODAY - dt.timedelta(days=21))
            status = self.pick_weighted([
                {"code": "active", "share": 0.58},
                {"code": "completed", "share": 0.24},
                {"code": "onHold", "share": 0.10},
                {"code": "proposed", "share": 0.08}])["code"]
            projects.append(self.add("Project", {
                "code": f"PRJ-{index:05d}",
                "name": f"{self._project_title(project_type, clients, index)}",
                "projectType": project_type["code"],
                "client": self.random.choice(clients) if clients else None,
                "projectManager": manager["employeeNumber"],
                "orgUnit": manager["_dept"]["code"],
                "billingModel": project_type["model"],
                "currency": currency,
                "status": status,
                "health": self.pick_weighted([
                    {"code": "green", "share": 0.62},
                    {"code": "amber", "share": 0.27},
                    {"code": "red", "share": 0.11}])["code"],
                "startDate": self.iso(started),
                "targetEndDate": self.iso(started + dt.timedelta(
                    days=self.random.choice([60, 90, 120, 180, 270]))),
                "contractValue": float(self.random.randrange(40, 900) * 1000)
                if not project_type.get("internal") else None,
                "budgetHours": float(self.random.randrange(200, 4000)),
                "priority": self.pick_weighted([
                    {"code": "medium", "share": 0.52},
                    {"code": "high", "share": 0.28},
                    {"code": "low", "share": 0.14},
                    {"code": "critical", "share": 0.06}])["code"],
            }))

        # One internal project per department — including the billable ones,
        # because a fee earner's admin, training and business-development hours
        # are not chargeable to a client either. Without this, overhead time
        # would have to be logged against a client project, which is both
        # untrue and the kind of thing that gets a firm in trouble.
        if internal_types:
            internal_type = internal_types[0]
            for department in self.config.departments:
                head = next((p for p in people
                             if p["_dept"]["code"] == department["code"]), None)
                if head is None:
                    continue
                projects.append(self.add("Project", {
                    "code": f"INT-{department['code']}",
                    "name": f"Internal — {department['name']}",
                    "projectType": internal_type["code"],
                    "projectManager": head["employeeNumber"],
                    "orgUnit": department["code"],
                    "billingModel": "nonBillable",
                    "currency": currency,
                    "status": "active",
                    "health": "green",
                    "startDate": self.iso(
                        TODAY - dt.timedelta(days=self.weeks * 7)),
                    "budgetHours": float(self.random.randrange(400, 2000)),
                }))
        return projects

    def _project_title(self, project_type: dict[str, Any],
                       clients: list[str], index: int) -> str:
        account = next((a for a in self.rows.get("Account", [])
                        if a["code"] in clients), None) if clients else None
        if account:
            account = self.random.choice(self.rows["Account"])
            return f"{account['name']} — {project_type['name']}"
        return f"{project_type['name']} {index:03d}"

    def _build_phases(self, projects: list[dict[str, Any]]) -> None:
        """The phases of each project, from its type's definitions."""
        definitions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.rows.get("ProjectPhaseDefinition", []):
            definitions[row["projectType"]].append(row)

        for project in projects:
            phases = sorted(definitions.get(project["projectType"], []),
                            key=lambda r: r["sequence"])
            if not phases:
                continue
            # How far through the plan this project is. A completed project has
            # finished every phase; an active one is partway.
            if project["status"] in ("completed", "closed"):
                reached = len(phases)
            elif project["status"] == "proposed":
                reached = 0
            else:
                reached = self.random.randint(1, len(phases))
            started = dt.date.fromisoformat(project["startDate"])
            span = max(14, ((dt.date.fromisoformat(project["targetEndDate"])
                             - started).days if project.get("targetEndDate")
                            else 120) // len(phases))
            for position, phase in enumerate(phases):
                phase_start = started + dt.timedelta(days=span * position)
                if position < reached - 1:
                    status = "completed"
                elif position == reached - 1:
                    status = ("completed" if reached == len(phases)
                              and project["status"] in ("completed", "closed")
                              else "inProgress")
                else:
                    status = "notStarted"
                self.add("ProjectPhase", {
                    "code": f"{project['code']}-P{phase['sequence']:02d}",
                    "project": project["code"],
                    "definition": phase["code"],
                    "name": phase["name"],
                    "sequence": phase["sequence"],
                    "status": status,
                    "startDate": self.iso(phase_start),
                    "endDate": self.iso(phase_start + dt.timedelta(days=span)),
                    "actualEndDate": self.iso(phase_start + dt.timedelta(
                        days=span + self.random.randint(-5, 12)))
                    if status == "completed" else None,
                    "feePercent": phase["defaultFeePercent"],
                    "budgetHours": round(
                        (project.get("budgetHours") or 0) / len(phases), 1),
                    "currency": project["currency"],
                })

    def _build_project_members(self, projects: list[dict[str, Any]],
                               people: list[dict[str, Any]],
                               currency: str) -> dict[str, list[dict[str, Any]]]:
        """Who is on which project, with the rate they are charged at.

        Returns project code -> the people on it, because every later section
        needs it: a work item is assigned to somebody on the project, and time
        is logged by somebody on the project.
        """
        roles = self.rows.get("ProjectRole", [])
        by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
        # How much of each person is already committed. Drawing an allocation
        # per membership independently put 23 of 54 people over 100%, which is
        # not a firm, it is a spreadsheet nobody reconciled.
        committed: dict[str, float] = defaultdict(float)
        for project in projects:
            internal = project["billingModel"] == "nonBillable"
            if internal:
                # Everyone in the department, not a sample: an internal project
                # is where that department's overhead goes, so leaving somebody
                # off it would leave them with nowhere to log a week at all —
                # which showed up as fourteen staff logging no time.
                team = [p for p in people
                        if p["_dept"]["code"] == project["orgUnit"]]
            else:
                pool = [p for p in people
                        if self.config.is_billable_department(
                            p["_dept"]["code"])] or people
                size = min(len(pool), self.random.randint(3, 7))
                team = self.random.sample(pool, size)
                # Roughly one client project in six carries somebody from a
                # support function — a PMO running the plan, a finance analyst
                # on a billing workstream. Without them every support
                # department's utilisation reads exactly 0.0%, which looks
                # like a column nobody populates rather than like a firm.
                if self.random.random() < 0.17:
                    support = [p for p in people
                               if not self.config.is_billable_department(
                                   p["_dept"]["code"])]
                    if support:
                        team.append(self.random.choice(support))
            if not team:
                continue
            manager = next((p for p in people
                            if p["employeeNumber"] == project["projectManager"]),
                           None)
            if manager and manager not in team:
                team.append(manager)
            started = dt.date.fromisoformat(project["startDate"])
            for person in team:
                role = self.random.choice(roles)
                # The rate is the role's default, nudged by seniority. A rate
                # card that ignores who is doing the work is not a rate card.
                seniority = 1.0 + 0.05 * self.config.ladder.index(person["_rung"])
                # What is left of this person, rounded to a share somebody
                # would actually write down. A deliberate minority go over,
                # because over-allocation is the thing the resourcing page
                # exists to show.
                remaining = 100.0 - committed[person["employeeNumber"]]
                if self.random.random() < 0.06:
                    allocation = float(self.random.choice([20, 25, 40, 50]))
                else:
                    options = [a for a in (10, 20, 25, 40, 50, 60, 80, 100)
                               if a <= max(remaining, 10)]
                    allocation = float(self.random.choice(options or [10]))
                committed[person["employeeNumber"]] += allocation
                member = self.add("ProjectMember", {
                    "code": f"{project['code']}-{person['employeeNumber']}",
                    "name": f"{person['displayName']} — {role['name']}",
                    "project": project["code"],
                    "employee": person["employeeNumber"],
                    "role": role["code"],
                    "allocationPercent": allocation,
                    "startDate": self.iso(started),
                    "billRate": None if internal else round(
                        role["defaultBillRate"] * seniority, 2),
                    "costRate": round(role["defaultCostRate"] * seniority, 2),
                    "currency": currency,
                })
                member["_person"] = person
                member["_role"] = role
                by_project[project["code"]].append(member)
        return by_project

    def _build_sprints(self, projects: list[dict[str, Any]],
                       work: dict[str, Any]) -> None:
        """Iterations, for a firm that runs them."""
        length = int(work.get("sprintLengthDays", 14))
        for project in projects:
            if project["status"] in ("proposed", "cancelled"):
                continue
            started = dt.date.fromisoformat(project["startDate"])
            count = max(1, min(12, ((TODAY - started).days // length) + 1))
            for number in range(1, count + 1):
                sprint_start = started + dt.timedelta(days=length * (number - 1))
                sprint_end = sprint_start + dt.timedelta(days=length - 1)
                if sprint_end < TODAY:
                    status = "completed"
                elif sprint_start <= TODAY <= sprint_end:
                    status = "active"
                else:
                    status = "planned"
                self.add("Sprint", {
                    "code": f"{project['code']}-S{number:02d}",
                    "project": project["code"],
                    "name": f"Sprint {number}",
                    "goal": f"Sprint {number} of {project['name']}.",
                    "startDate": self.iso(sprint_start),
                    "endDate": self.iso(sprint_end),
                    "status": status,
                    "capacityHours": float(self.random.randrange(80, 400)),
                })

    def _build_work_items(self, projects: list[dict[str, Any]],
                          members: dict[str, list[dict[str, Any]]]) -> None:
        """The backlog, with a parent-child layer and real dependencies."""
        phases: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.rows.get("ProjectPhase", []):
            phases[row["project"]].append(row)
        sprints: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.rows.get("Sprint", []):
            sprints[row["project"]].append(row)

        index = 0
        for project in projects:
            team = members.get(project["code"], [])
            if not team:
                continue
            count = self.random.randint(8, 24)
            parents: list[str] = []
            created: list[dict[str, Any]] = []
            for _ in range(count):
                index += 1
                item_type = self.pick_weighted(WORK_ITEM_MIX)
                phase = (self.random.choice(phases[project["code"]])
                         if phases.get(project["code"]) else None)
                sprint = (self.random.choice(sprints[project["code"]])
                          if sprints.get(project["code"]) else None)
                assignee = self.random.choice(team)
                status = self.pick_weighted([
                    {"code": "done", "share": 0.42},
                    {"code": "inProgress", "share": 0.16},
                    {"code": "backlog", "share": 0.15},
                    {"code": "toDo", "share": 0.13},
                    {"code": "inReview", "share": 0.07},
                    {"code": "blocked", "share": 0.04},
                    {"code": "cancelled", "share": 0.03}])["code"]
                estimate = float(self.random.choice([2, 4, 8, 13, 21, 34]))
                due = self.date_between(
                    dt.date.fromisoformat(project["startDate"]),
                    TODAY + dt.timedelta(days=60))
                code = f"WI-{index:06d}"
                row = self.add("WorkItem", {
                    "code": code,
                    "title": f"{item_type['name']} {index}: "
                             f"{self.random.choice(WORK_ITEM_SUBJECTS)}",
                    "project": project["code"],
                    "phase": phase["code"] if phase else None,
                    "workItemType": item_type["code"],
                    # Only an epic or a feature may hold children, which is
                    # what allowsChildren on the type is for.
                    "parentWorkItem": (self.random.choice(parents)
                                       if parents and self.random.random() < 0.35
                                       else None),
                    "status": status,
                    "priority": self.pick_weighted([
                        {"code": "medium", "share": 0.5},
                        {"code": "high", "share": 0.26},
                        {"code": "low", "share": 0.17},
                        {"code": "critical", "share": 0.07}])["code"],
                    "assignedTo": assignee["employee"],
                    "reporter": project["projectManager"],
                    "sprint": sprint["code"] if sprint else None,
                    "dueDate": self.iso(due),
                    "completedOn": f"{self.iso(due)}T16:00:00Z"
                    if status == "done" else None,
                    "estimateHours": estimate,
                    "remainingHours": 0.0 if status == "done" else round(
                        estimate * self.random.uniform(0.1, 1.0), 1),
                    "storyPoints": estimate if self.config.runs_sprints else None,
                    "isBillable": project["billingModel"] != "nonBillable",
                })
                created.append(row)
                if item_type["category"] in ("epic", "feature"):
                    parents.append(code)

            # A handful of real dependencies, so a scheduling view has
            # something to draw. Always earlier-blocks-later, because a
            # dependency pointing backwards in the list is a cycle waiting to
            # be found by whoever renders it.
            for position in range(1, len(created)):
                if self.random.random() > 0.12:
                    continue
                predecessor = created[self.random.randrange(position)]
                successor = created[position]
                self.add("WorkItemDependency", {
                    "code": f"{predecessor['code']}-{successor['code']}",
                    "name": f"{predecessor['code']} blocks {successor['code']}",
                    "predecessor": predecessor["code"],
                    "successor": successor["code"],
                    "dependencyType": self.pick_weighted([
                        {"code": "finishToStart", "share": 0.74},
                        {"code": "startToStart", "share": 0.14},
                        {"code": "finishToFinish", "share": 0.09},
                        {"code": "startToFinish", "share": 0.03}])["code"],
                    "lagDays": self.random.choice([0, 0, 0, 1, 2, 5]),
                })

    def _build_milestones(self, projects: list[dict[str, Any]],
                          currency: str) -> None:
        """Milestones, and for a fixed-fee project the billing ones."""
        phases: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.rows.get("ProjectPhase", []):
            phases[row["project"]].append(row)

        for project in projects:
            billing = project["billingModel"] in ("fixedFee", "milestone",
                                                  "cappedFee", "percentageOfValue")
            value = project.get("contractValue") or 0.0
            project_phases = sorted(phases.get(project["code"], []),
                                    key=lambda r: r["sequence"])
            for phase in project_phases:
                due = dt.date.fromisoformat(phase["endDate"])
                if phase["status"] == "completed":
                    status = "achieved"
                elif due < TODAY:
                    status = self.pick_weighted([
                        {"code": "missed", "share": 0.62},
                        {"code": "achieved", "share": 0.38}])["code"]
                else:
                    status = "planned"
                self.add("Milestone", {
                    "code": f"{project['code']}-M{phase['sequence']:02d}",
                    "project": project["code"],
                    "phase": phase["code"],
                    "name": f"{phase['name']} complete",
                    "dueDate": self.iso(due),
                    "completedOn": self.iso(due + dt.timedelta(
                        days=self.random.randint(-4, 9)))
                    if status == "achieved" else None,
                    "status": status,
                    "isBillingMilestone": billing,
                    "billingAmount": round(
                        value * (phase["feePercent"] / 100.0), 2)
                    if billing and value else None,
                    "currency": currency,
                })

    def _build_timesheets(self, projects: list[dict[str, Any]],
                          members: dict[str, list[dict[str, Any]]],
                          people: list[dict[str, Any]], currency: str) -> None:
        """A week of time per person per week, and the entries inside it.

        This is the volume. It is also the only section where the numbers have
        to add up to something a report can be judged on, so the hours per week
        follow the firm's utilisation target rather than being uniform noise.
        """
        codes = self.config.work["timeCodes"]
        billable_codes = [c for c in codes if c["billable"]]
        internal_codes = [c for c in codes if not c["billable"]]
        weekly = float(self.config.work.get("weeklyHours", 37.5))
        target = float(self.config.work.get("utilisationTarget", 0.75))

        # Which projects each person can charge to, and at what rate.
        for_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for team in members.values():
            for member in team:
                for_person[member["employee"]].append(member)
        by_code = {p["code"]: p for p in projects}

        week_start = TODAY - dt.timedelta(days=TODAY.weekday() + 7 * self.weeks)
        sheet_index = 0
        entry_index = 0
        for person in people:
            number = person["employeeNumber"]
            assignments = for_person.get(number) or []
            if not assignments:
                continue
            is_billable = self.config.is_billable_department(
                person["_dept"]["code"])
            joined = person.get("_joined")
            for week in range(self.weeks):
                start = week_start + dt.timedelta(days=7 * week)
                end = start + dt.timedelta(days=4)
                if joined and end < joined:
                    continue
                # Not every week is a full week: holiday, illness, a
                # conference. A timesheet set with no gaps is not real.
                if self.random.random() < 0.07:
                    continue
                sheet_index += 1
                sheet_code = f"TS-{sheet_index:06d}"
                if end < TODAY - dt.timedelta(days=14):
                    sheet_status = self.pick_weighted([
                        {"code": "approved", "share": 0.93},
                        {"code": "rejected", "share": 0.07}])["code"]
                elif end < TODAY:
                    sheet_status = self.pick_weighted([
                        {"code": "approved", "share": 0.55},
                        {"code": "submitted", "share": 0.40},
                        {"code": "open", "share": 0.05}])["code"]
                else:
                    sheet_status = "open"

                entries = []
                total = 0.0
                billable_hours = 0.0
                count = self.random.randint(*ENTRIES_PER_WEEK)
                # The week's billable share varies around the firm's target,
                # which is what gives the utilisation report a distribution
                # rather than a single value.
                share = (min(0.98, max(0.0, self.random.gauss(target, 0.14)))
                         if is_billable else self.random.uniform(0.0, 0.15))
                for position in range(count):
                    entry_index += 1
                    day = start + dt.timedelta(days=self.random.randint(0, 4))
                    wants_billable = self.random.random() < share
                    chargeable = [a for a in assignments
                                  if by_code[a["project"]]["billingModel"]
                                  != "nonBillable"]
                    internal = [a for a in assignments
                                if by_code[a["project"]]["billingModel"]
                                == "nonBillable"]
                    pool = (chargeable if wants_billable and chargeable
                            else internal or assignments)
                    member = self.random.choice(pool)
                    project = by_code[member["project"]]
                    billable = (project["billingModel"] != "nonBillable"
                                and wants_billable)
                    code = self.pick_weighted(
                        billable_codes if billable else internal_codes or codes)
                    hours = round(min(
                        weekly / max(count, 1) * self.random.uniform(0.5, 1.6),
                        12.0), 2)
                    total += hours
                    if billable:
                        billable_hours += hours
                    rate = member.get("billRate")
                    entries.append({
                        "code": f"{sheet_code}-L{position + 1}",
                        "name": f"{code['name']} — {self.iso(day)}",
                        "timesheet": sheet_code,
                        "employee": number,
                        "project": project["code"],
                        "workItem": None,
                        "timeCode": code["code"],
                        "entryDate": self.iso(day),
                        "hours": hours,
                        "isBillable": billable,
                        "narrative": f"{code['name']} on {project['name']}.",
                        "status": self._entry_status(sheet_status, billable),
                        "billRate": rate if billable else None,
                        "billAmount": round(rate * hours, 2)
                        if billable and rate else None,
                        "currency": currency,
                    })

                self.add("Timesheet", {
                    "code": sheet_code,
                    "name": f"{person['displayName']} — week of {self.iso(start)}",
                    "employee": number,
                    "periodStart": self.iso(start),
                    "periodEnd": self.iso(start + dt.timedelta(days=6)),
                    "status": sheet_status,
                    "totalHours": round(total, 2),
                    "billableHours": round(billable_hours, 2),
                    "submittedOn": f"{self.iso(start + dt.timedelta(days=7))}"
                                   "T17:00:00Z"
                    if sheet_status != "open" else None,
                })
                for entry in entries:
                    self.add("TimeEntry", entry)

        self.notes.append(
            f"{len(self.rows['TimeEntry'])} time entries over "
            f"{len(self.rows['Timesheet'])} weekly timesheets, "
            f"{self.weeks} weeks of history")

    def _entry_status(self, sheet_status: str, billable: bool) -> str:
        """Where one line has got to, given the week it belongs to.

        Write-offs matter: a firm that never writes anything off is a firm
        whose fees are never challenged, and the write-off rate is one of the
        two numbers a finance director reads first. The demo had none, so the
        revenue page's write-off tiles read zero.
        """
        if sheet_status == "rejected":
            return "rejected"
        if sheet_status == "submitted":
            return "submitted"
        if sheet_status != "approved":
            return "draft"
        if not billable:
            return "approved"
        return self.pick_weighted([
            {"code": "billed", "share": 0.70},
            {"code": "approved", "share": 0.24},
            {"code": "writtenOff", "share": 0.06}])["code"]

    def _roll_up_projects(self, projects: list[dict[str, Any]]) -> None:
        """Put the hours actually logged back onto the project.

        Project carries actualHours, actualCost and percentComplete, and
        leaving them empty while thirteen thousand time entries exist is the
        kind of inconsistency a demo gets asked about in the first five
        minutes. The budget is then set from the actuals, so "hours against
        budget" lands in a band a delivery director would recognise instead of
        reading 265% across the whole firm.
        """
        hours: dict[str, float] = defaultdict(float)
        value: dict[str, float] = defaultdict(float)
        for entry in self.rows.get("TimeEntry", []):
            hours[entry["project"]] += entry["hours"]
            value[entry["project"]] += entry.get("billAmount") or 0.0
        phase_hours: dict[str, float] = defaultdict(float)
        for entry in self.rows.get("TimeEntry", []):
            if entry.get("phase"):
                phase_hours[entry["phase"]] += entry["hours"]

        over = 0
        for project in projects:
            logged = round(hours.get(project["code"], 0.0), 1)
            project["actualHours"] = logged
            project["actualCost"] = round(value.get(project["code"], 0.0), 2) or None
            # Most projects are inside their budget; a minority are over, and
            # a completed project is done rather than part-done.
            if project["status"] in ("completed", "closed"):
                project["percentComplete"] = 100.0
            elif project["status"] == "proposed":
                project["percentComplete"] = 0.0
            else:
                project["percentComplete"] = float(self.random.randrange(10, 95))
            if logged:
                # budget = logged / ratio, so a ratio above 1 means the budget
                # is smaller than the hours spent: that project is over. Most
                # are not — a firm where two projects in three overrun is a
                # firm with a pricing problem, not demo data.
                ratio = (self.random.uniform(1.02, 1.30)
                         if self.random.random() < 0.3
                         else self.random.uniform(0.70, 0.98))
                project["budgetHours"] = round(logged / ratio, 1)
                if ratio > 1:
                    over += 1
        if over:
            self.notes.append(
                f"{over} of {len(projects)} project(s) are over their budgeted "
                "hours, so the delivery page is not uniformly green")

    def _build_resourcing(self, projects: list[dict[str, Any]],
                          people: list[dict[str, Any]]) -> None:
        """Staffing requests, including ones nobody has filled."""
        roles = self.rows.get("ProjectRole", [])
        live = [p for p in projects if p["status"] in ("active", "proposed")]
        index = 0
        for project in live:
            if self.random.random() > 0.55:
                continue
            index += 1
            start = self.date_between(TODAY - dt.timedelta(days=30),
                                      TODAY + dt.timedelta(days=60))
            status = self.pick_weighted([
                {"code": "filled", "share": 0.42},
                {"code": "submitted", "share": 0.24},
                {"code": "partiallyFilled", "share": 0.18},
                {"code": "draft", "share": 0.10},
                {"code": "cancelled", "share": 0.06}])["code"]
            self.add("ResourceRequest", {
                "code": f"RR-{index:05d}",
                "name": f"{project['name']} — resourcing",
                "project": project["code"],
                "role": self.random.choice(roles)["code"],
                "requestedBy": project["projectManager"],
                "startDate": self.iso(start),
                "endDate": self.iso(start + dt.timedelta(
                    days=self.random.choice([30, 60, 90]))),
                "hoursRequired": float(self.random.randrange(40, 600)),
                "allocationPercent": float(self.random.choice([20, 40, 50, 80, 100])),
                "priority": self.pick_weighted([
                    {"code": "medium", "share": 0.5},
                    {"code": "high", "share": 0.33},
                    {"code": "low", "share": 0.11},
                    {"code": "critical", "share": 0.06}])["code"],
                "status": status,
                "assignedEmployee": self.random.choice(people)["employeeNumber"]
                if status in ("filled", "partiallyFilled") else None,
                "notes": "Raised from the delivery plan.",
            })
        unfilled = [r for r in self.rows.get("ResourceRequest", [])
                    if r["status"] in ("submitted", "partiallyFilled")]
        if not unfilled and self.rows.get("ResourceRequest"):
            self.rows["ResourceRequest"][0]["status"] = "submitted"
            self.rows["ResourceRequest"][0]["assignedEmployee"] = None
            self.notes.append(
                "forced one resource request to stay unfilled, so the "
                "resourcing gap on the dashboard is not zero")

    def _build_raid(self, projects: list[dict[str, Any]],
                    people: list[dict[str, Any]], currency: str) -> None:
        """The RAID log — and the severity score it implies."""
        index = 0
        for project in projects:
            if project["status"] in ("proposed", "cancelled"):
                continue
            for _ in range(self.random.randint(1, 5)):
                index += 1
                raid_type = self.pick_weighted([
                    {"code": "risk", "share": 0.34},
                    {"code": "issue", "share": 0.24},
                    {"code": "decision", "share": 0.16},
                    {"code": "dependency", "share": 0.12},
                    {"code": "assumption", "share": 0.08},
                    {"code": "changeRequest", "share": 0.06}])["code"]
                probability = self.pick_weighted([
                    {"code": "low", "share": 0.3}, {"code": "medium", "share": 0.4},
                    {"code": "high", "share": 0.22},
                    {"code": "veryHigh", "share": 0.08}])["code"]
                impact = self.pick_weighted([
                    {"code": "low", "share": 0.26}, {"code": "medium", "share": 0.38},
                    {"code": "high", "share": 0.26},
                    {"code": "veryHigh", "share": 0.10}])["code"]
                raised = self.date_between(
                    dt.date.fromisoformat(project["startDate"]), TODAY)
                status = self.pick_weighted([
                    {"code": "open", "share": 0.34},
                    {"code": "closed", "share": 0.24},
                    {"code": "inProgress", "share": 0.21},
                    {"code": "resolved", "share": 0.13},
                    {"code": "accepted", "share": 0.08}])["code"]
                closed = status in ("closed", "resolved")
                self.add("RaidItem", {
                    "code": f"RAID-{index:05d}",
                    "project": project["code"],
                    "raidType": raid_type,
                    "title": f"{raid_type.title()}: "
                             f"{self.random.choice(RAID_SUBJECTS)}",
                    "description": "Raised at the delivery review.",
                    "assignedTo": self.random.choice(people)["employeeNumber"],
                    "probability": probability,
                    "impact": impact,
                    # The score is probability x impact on the four-point
                    # scale, so a filter on "score >= 9" means something.
                    "severityScore": IMPACT_SCORE[probability] * IMPACT_SCORE[impact],
                    "status": status,
                    "raisedOn": self.iso(raised),
                    "dueDate": self.iso(raised + dt.timedelta(
                        days=self.random.choice([14, 30, 60]))),
                    "closedOn": self.iso(raised + dt.timedelta(
                        days=self.random.randint(3, 50))) if closed else None,
                    "mitigation": "Mitigation agreed with the client.",
                    "costImpact": float(self.random.randrange(2, 90) * 1000)
                    if raid_type in ("risk", "issue", "changeRequest") else None,
                    "scheduleImpactDays": self.random.choice([0, 2, 5, 10, 20])
                    if raid_type in ("risk", "issue", "dependency") else None,
                    "currency": currency,
                    "decidedBy": project["projectManager"]
                    if raid_type == "decision" else None,
                })

    def _build_status_reports(self, projects: list[dict[str, Any]]) -> None:
        """A weekly report per live project, for the last few weeks."""
        index = 0
        for project in projects:
            if project["status"] not in ("active", "onHold"):
                continue
            for weeks_ago in range(self.random.randint(1, 5)):
                index += 1
                period_end = TODAY - dt.timedelta(
                    days=TODAY.weekday() + 7 * weeks_ago)
                health = project.get("health") or "green"
                self.add("StatusReport", {
                    "code": f"SR-{index:05d}",
                    "name": f"{project['name']} — {self.iso(period_end)}",
                    "project": project["code"],
                    "periodEnd": self.iso(period_end),
                    "overallHealth": health,
                    "scheduleHealth": self.pick_weighted([
                        {"code": health, "share": 0.7},
                        {"code": "amber", "share": 0.2},
                        {"code": "green", "share": 0.1}])["code"],
                    "budgetHealth": self.pick_weighted([
                        {"code": health, "share": 0.7},
                        {"code": "green", "share": 0.2},
                        {"code": "red", "share": 0.1}])["code"],
                    "scopeHealth": health,
                    "percentComplete": float(self.random.randrange(5, 95)),
                    "summary": f"Delivery continues on {project['name']}.",
                    "accomplishments": "Phase work progressed as planned.",
                    "nextSteps": "Continue the current phase.",
                    "risksAndIssues": "See the RAID log.",
                    "reportedBy": project["projectManager"],
                    "publishedOn": f"{self.iso(period_end)}T18:00:00Z",
                })

    # -- Travel --------------------------------------------------------------
    def build_travel(self) -> None:
        """Trips, bookings, expense claims and the corporate card feed.

        Shaped so every page of the travel dashboard has something to say: a
        few people are away right now, some of them somewhere the firm classes
        as high risk, a minority of trips breach the policy, and a handful of
        card transactions are unexplained. A travel dashboard where nothing is
        out of policy and nothing is unmatched is a dashboard nobody needs.
        """
        travellers = [p for p in self.employees if p["_left"] is None]
        if not travellers:
            self.warnings.append("no active staff, so no travel was generated")
            return

        currency = self.config.firm["currency"]
        # The policy, the advisories and the per diem rates are all effective
        # dated: they are the version in force now, so they start at the
        # beginning of the current travel year and have no end.
        policy_from = self.iso(dt.date(TODAY.year, 1, 1))
        self.add("TravelPolicy", {
            "code": "POL-TRAVEL", "name": f"{self.config.name} travel policy",
            "legalEntity": self.config.firm["code"],
            "domesticCabinClass": "economy",
            "internationalCabinClass": "economy",
            "longHaulThresholdHours": 8,
            "longHaulCabinClass": "premiumEconomy",
            "maxHotelRatePerNight": 220.0,
            "currency": currency,
            "minAdvanceBookingDays": 14,
            "preApprovalRequired": True,
            "validFrom": policy_from,
        })

        for destination in DESTINATIONS:
            if destination["risk"] in ("low",):
                continue
            self.add("DestinationAdvisory", {
                "code": f"ADV-{destination['country']}",
                "title": f"{destination['city']} — {destination['risk']} risk",
                "country": destination["country"],
                "riskLevel": destination["risk"],
                "summary": f"Standing advisory for travel to {destination['city']}.",
                "requiresSecurityApproval": destination["risk"] in ("high", "extreme"),
                "validFrom": policy_from,
            })

        for destination in DESTINATIONS:
            self.add("PerDiemRate", {
                "code": f"PD-{destination['country']}-{destination['city'][:3].upper()}",
                "name": f"{destination['city']} per diem",
                "country": destination["country"], "city": destination["city"],
                "lodgingRate": float(min(destination["cost"], 260)),
                "mealsRate": 55.0, "incidentalsRate": 12.0,
                "currency": currency, "source": "HMRC published rates",
                "validFrom": policy_from,
            })

        trips = max(30, self.people)
        card_index = 0
        line_index = 0
        segment_index = 0
        booking_index = 0
        report_index = 0

        # Duty of care is the one tile a travel system is bought for, and the
        # one it is judged on in the first minute of a demo. Left to the draw,
        # "somebody risky, away right now" is the product of two small
        # probabilities and reads zero more often than not, so the first trip
        # is that case by construction.
        riskiest = max(DESTINATIONS, key=lambda d: RISK_RANK[d["risk"]])

        for index in range(1, trips + 1):
            traveller = self.random.choice(travellers)
            duty_of_care_case = index == 1
            destination = (riskiest if duty_of_care_case
                           else self.pick_weighted(DESTINATIONS))
            purpose = self.random.choice(
                ["CLIENT_MEETING", "CONFERENCE", "TRAINING", "INTERNAL", "BUS_DEV"])
            nights = self.random.randint(1, 4 if destination["hours"] < 4 else 6)

            # A spread across past, present and future, so "away now" is not zero.
            roll = 0.0 if duty_of_care_case else self.random.random()
            if roll < 0.10:
                # Departed, not yet back: nights - 1 at most, so the return
                # date is always still ahead and the trip is genuinely in
                # flight rather than landing today.
                departure = TODAY - dt.timedelta(
                    days=self.random.randint(0, nights - 1))
            elif roll < 0.62:
                departure = self.date_between(TODAY - dt.timedelta(days=300),
                                              TODAY - dt.timedelta(days=nights + 1))
            else:
                departure = self.date_between(TODAY + dt.timedelta(days=3),
                                              TODAY + dt.timedelta(days=120))
            ret = departure + dt.timedelta(days=nights)

            if ret < TODAY:
                status = self.pick_weighted([
                    {"code": "completed", "share": 0.86},
                    {"code": "cancelled", "share": 0.09},
                    {"code": "rejected", "share": 0.05}])["code"]
            elif departure <= TODAY <= ret:
                status = "inProgress"
            else:
                status = self.pick_weighted([
                    {"code": "booked", "share": 0.52},
                    {"code": "approved", "share": 0.20},
                    {"code": "submitted", "share": 0.18},
                    {"code": "booking", "share": 0.10}])["code"]

            # Booked late, or in a cabin above the class the policy allows for
            # that haul. Both are judged against the TravelPolicy row above
            # rather than against numbers repeated here.
            advance = self.pick_weighted([
                {"code": 3, "share": 0.03}, {"code": 7, "share": 0.04},
                {"code": 10, "share": 0.04}, {"code": 14, "share": 0.13},
                {"code": 21, "share": 0.22}, {"code": 30, "share": 0.24},
                {"code": 45, "share": 0.18}, {"code": 60, "share": 0.12},
            ])["code"]
            booked_on = departure - dt.timedelta(days=advance)
            long_haul = destination["hours"] >= 8
            if long_haul:
                cabin = self.pick_weighted([
                    {"code": "premiumEconomy", "share": 0.62},
                    {"code": "economy", "share": 0.33},
                    {"code": "business", "share": 0.05}])["code"]
                allowed = "premiumEconomy"
            else:
                cabin = self.pick_weighted([
                    {"code": "economy", "share": 0.95},
                    {"code": "business", "share": 0.05}])["code"]
                allowed = "economy"
            out_of_policy = (advance < 14
                             or CABIN_RANK[cabin] > CABIN_RANK[allowed])

            code = f"TRIP-{index:05d}"
            cost = float(destination["cost"] * 2 + nights * 180)
            self.add("TripRequest", {
                "code": code,
                "title": f"{destination['city']} — {purpose.replace('_', ' ').title()}",
                "traveller": traveller["employeeNumber"],
                "requestedBy": traveller["employeeNumber"],
                "purpose": purpose,
                "businessJustification":
                    f"{purpose.replace('_', ' ').title()} in {destination['city']}.",
                "tripType": "domestic" if destination["country"]
                            == self.config.firm["country"] else "international",
                "status": status,
                "departureDate": self.iso(departure),
                "returnDate": self.iso(ret),
                "origin": self.config.offices[0]["city"],
                "originCountry": self.config.firm["country"],
                "destination": destination["city"],
                "destinationCountry": destination["country"],
                "costCenter": f"CC-{traveller['_dept']['code']}",
                "currency": currency,
                "estimatedCost": cost,
                "actualCost": cost if status == "completed" else None,
                "isOutOfPolicy": out_of_policy,
                "visaRequired": destination["country"] in ("IN", "AE", "AU", "US"),
                "highestRiskLevel": destination["risk"],
                "isBillable": purpose == "CLIENT_MEETING",
                "submittedOn": f"{self.iso(booked_on - dt.timedelta(days=2))}T09:00:00Z",
            })

            if status in ("rejected", "submitted"):
                continue        # nothing is booked until it is approved

            # The outbound flight and the hotel.
            segment_index += 1
            self.add("TripSegment", {
                "code": f"{code}-S1",
                "name": f"{self.config.offices[0]['city']} to {destination['city']}",
                "trip": code, "sequence": 1, "segmentType": "flight",
                "startOn": f"{self.iso(departure)}T08:00:00Z",
                "endOn": f"{self.iso(departure)}T"
                         f"{min(8 + int(destination['hours']), 23):02d}:00:00Z",
                "fromLocation": self.config.offices[0]["city"],
                "toLocation": destination["city"],
            })
            booking_index += 1
            booking_status = ("cancelled" if status == "cancelled"
                              else "ticketed" if ret < TODAY else "confirmed")
            self.add("TravelBooking", {
                "code": f"{code}-B1",
                "name": f"Flight to {destination['city']}",
                "trip": code, "segment": f"{code}-S1",
                "bookingType": "flight", "status": booking_status,
                "channel": self.pick_weighted([
                    {"code": "travelManagementCompany", "share": 0.52},
                    {"code": "onlineBookingTool", "share": 0.30},
                    {"code": "directWithSupplier", "share": 0.11},
                    {"code": "bookedByTraveller", "share": 0.07}])["code"],
                "supplierName": "Skyline Airways",
                "fromLocation": self.config.offices[0]["city"],
                "toLocation": destination["city"],
                "startOn": f"{self.iso(departure)}T08:00:00Z",
                "endOn": f"{self.iso(ret)}T20:00:00Z",
                "cabinClass": cabin,
                "baseFare": float(destination["cost"] * 2 * 0.82),
                "taxesAndFees": float(destination["cost"] * 2 * 0.18),
                "totalCost": float(destination["cost"] * 2),
                "currency": currency,
                "isInPolicy": not out_of_policy,
                "isRefundable": cabin != "economy",
                "bookedOn": f"{self.iso(booked_on)}T11:00:00Z",
                "co2Kg": float(destination["co2"] * 2),
                "bookedBy": traveller["employeeNumber"],
            })

            if status != "completed":
                continue        # a claim follows the trip

            # The expense claim, and the card transactions behind it.
            report_index += 1
            report_code = f"EXP-{report_index:05d}"
            submitted = ret + dt.timedelta(days=self.random.randint(1, 21))
            if submitted > TODAY:
                submitted = TODAY
            claim_status = self.pick_weighted([
                {"code": "paid", "share": 0.62},
                {"code": "approved", "share": 0.16},
                {"code": "submitted", "share": 0.16},
                {"code": "rejected", "share": 0.06}])["code"]
            paid_on = (submitted + dt.timedelta(days=self.random.choice(
                [3, 5, 7, 9, 12, 16, 24, 31])) if claim_status == "paid" else None)
            if paid_on and paid_on > TODAY:
                paid_on = TODAY

            claim_total = 0.0
            reimbursable = 0.0
            lines = []
            for item in EXPENSE_MIX:
                if self.random.random() > 0.55:
                    continue
                nights_factor = nights if item.get("nightly") else 1
                unit = round(item["typical"]
                             * self.random.uniform(*EXPENSE_SPREAD), 2)
                amount = round(unit * nights_factor, 2)
                method = self.pick_weighted([
                    {"code": "corporateCard", "share": 0.62},
                    {"code": "personalCard", "share": 0.28},
                    {"code": "cash", "share": 0.10}])["code"]
                over_cap = unit > item["cap"]
                lines.append((item["code"], amount, method, over_cap))
                claim_total += amount
                if method != "corporateCard":
                    reimbursable += amount

            if not lines:
                continue

            self.add("ExpenseReport", {
                "code": report_code,
                "title": f"{destination['city']} — {self.iso(departure)}",
                "employee": traveller["employeeNumber"], "trip": code,
                "status": claim_status,
                "periodStart": self.iso(departure), "periodEnd": self.iso(ret),
                "currency": currency,
                "totalAmount": round(claim_total, 2),
                "reimbursableAmount": round(reimbursable, 2),
                "costCenter": f"CC-{traveller['_dept']['code']}",
                "submittedOn": f"{self.iso(submitted)}T17:00:00Z",
                "approvedOn": f"{self.iso(submitted + dt.timedelta(days=2))}T10:00:00Z"
                if claim_status in ("approved", "paid") else None,
                "paidOn": self.iso(paid_on),
            })

            for category, amount, method, over_cap in lines:
                line_index += 1
                spent_on = self.date_between(departure, ret)
                card_code = None
                if method == "corporateCard":
                    card_index += 1
                    card_code = f"CARD-{card_index:05d}"
                    self.add("CardTransaction", {
                        "code": card_code,
                        "name": f"{self.random.choice(MERCHANTS)} "
                                f"{self.iso(spent_on)}",
                        "cardholder": traveller["employeeNumber"],
                        "transactionDate": self.iso(spent_on),
                        "postedDate": self.iso(spent_on + dt.timedelta(days=2)),
                        "merchant": self.random.choice(MERCHANTS),
                        "amount": amount, "currency": currency,
                        "status": "matched",
                    })
                self.add("ExpenseLine", {
                    "code": f"{report_code}-L{line_index}",
                    "report": report_code,
                    "description": f"{category.replace('_', ' ').title()} in "
                                   f"{destination['city']}",
                    "expenseDate": self.iso(spent_on),
                    "category": category,
                    "merchant": self.random.choice(MERCHANTS),
                    "paymentMethod": method,
                    "originalAmount": amount, "originalCurrency": currency,
                    "amount": amount, "currency": currency,
                    "isReimbursable": method != "corporateCard",
                    "isBillable": purpose == "CLIENT_MEETING",
                    "isOutOfPolicy": over_cap,
                    "cardTransaction": card_code,
                })

        self.notes.append(
            f"one trip is in flight to {riskiest['city']} ({riskiest['risk']} "
            "risk) by construction, so the duty-of-care page is never zero")
        self._ensure_unmatched_card_spend(travellers, currency)

    def _ensure_unmatched_card_spend(self, travellers: list[dict[str, Any]],
                                     currency: str) -> None:
        """Corporate card spend nobody has explained yet.

        Every transaction generated above is matched to an expense line, so the
        card page would read zero on every tile. Unexplained company money is
        the whole reason that page exists, so some is created deliberately —
        including one old enough to be embarrassing.
        """
        wanted = max(6, self.people // 8)
        start = len(self.rows.get("CardTransaction", [])) + 1
        for offset in range(wanted):
            traveller = self.random.choice(travellers)
            age = (self.random.randint(60, 150) if offset == 0
                   else self.random.randint(3, 55))
            spent_on = TODAY - dt.timedelta(days=age)
            self.add("CardTransaction", {
                "code": f"CARD-{start + offset:05d}",
                "name": f"{self.random.choice(MERCHANTS)} {self.iso(spent_on)}",
                "cardholder": traveller["employeeNumber"],
                "transactionDate": self.iso(spent_on),
                "postedDate": self.iso(spent_on + dt.timedelta(days=2)),
                "merchant": self.random.choice(MERCHANTS),
                "amount": round(self.random.uniform(22, 480), 2),
                "currency": currency,
                "status": "disputed" if offset == 1 else "unmatched",
            })
        self.notes.append(
            f"{wanted} card transaction(s) left unmatched on purpose, the oldest "
            "over two months old, so the reconciliation page is not all zeros")

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
        # demodata adds a synthetic `code` natural key to tables that have no
        # such column, so an unknown `code` is expected. Where `code` is a real
        # column it must be checked like any other, which it was not.
        synthetic_code = "code" not in attributes
        for field, value in row.items():
            if field.startswith("_"):
                continue
            if field == "code" and synthetic_code:
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
            elif kind in ("multilookup", "multiterm"):
                self._check_multilookup(where, field, value, spec)
            elif kind == "term":
                self._check_term(where, field, value, spec)

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

    def _term_values(self, term_set: str) -> tuple[set[str], bool]:
        """Every term in a set, and whether the set accepts new ones."""
        spec = (self.model.get("termSets") or {}).get(term_set)
        if spec is None:
            return set(), True
        found: set[str] = set()

        def walk(items) -> None:
            for item in items or ():
                if isinstance(item, dict):
                    found.add(str(item.get("name") or item.get("code") or ""))
                    walk(item.get("children"))
                else:
                    found.add(str(item))

        walk(spec.get("terms"))
        return found - {""}, bool(spec.get("open"))

    def _check_term(self, where: str, field: str, value: Any,
                    spec: dict[str, Any]) -> None:
        """A single managed-metadata value.

        Unchecked until the intranet was built, which is the first module where
        most reference data is a term rather than a choice set.
        """
        term_set = spec.get("termSet")
        if not term_set:
            return
        available, is_open = self._term_values(term_set)
        if not available or is_open:
            return                      # an open set accepts anything
        if str(value) not in available:
            self.errors.append(
                f"{where}: {field} = '{value}' is not a term of {term_set}")

    def _check_multilookup(self, where: str, field: str, value: Any,
                           spec: dict[str, Any]) -> None:
        """A list of references, each of which has to resolve.

        Nothing checked these until the intranet was built, and the intranet's
        headline feature is audience targeting — which is carried entirely by
        multilookup columns. A dangling audience is worse than an empty one: it
        silently narrows who sees a policy, and the policy still looks
        published.
        """
        if not isinstance(value, (list, tuple)):
            self.errors.append(
                f"{where}: {field} is a multi-value column and must be a list, "
                f"not {type(value).__name__}")
            return
        if len(set(map(str, value))) != len(value):
            self.errors.append(f"{where}: {field} repeats a value")
        if spec["type"] == "multiterm":
            term_set = spec.get("termSet")
            if not term_set:
                return
            available, is_open = self._term_values(term_set)
            if not available or is_open:
                return
            for item in value:
                if str(item) not in available:
                    self.errors.append(
                        f"{where}: {field} includes '{item}', which is not a "
                        f"term of {term_set}")
            return
        target = spec.get("target")
        if not target:
            return
        available = self.keys.get(target, set())
        if not available:
            self.warnings.append(
                f"{where}: {field} points at {target}, which has no rows — "
                "load its reference data first")
            return
        for item in value:
            if str(item) not in available:
                self.errors.append(
                    f"{where}: {field} includes '{item}', which is not a "
                    f"{target} that exists")

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
