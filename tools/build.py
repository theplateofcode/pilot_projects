#!/usr/bin/env python3
"""Run the whole generator pipeline for one profile.

Twelve tools is too many to remember the order of, and only two orderings
matter: everything needs the resolved model first, and `solgen` needs what the
Dataverse generators produced. This runs them in a correct order, stops at the
first failure, and prints what each one produced.

    python tools/build.py --profile hr-starter --prefix wga --solution WoodgroveHR
    python tools/build.py --profile law-firm --prefix fab --targets dataverse,powerbi
    python tools/build.py --profile travel-starter --prefix pfx --targets sharepoint
    python tools/build.py --profile hr-starter --prefix wga --list

Targets group the tools by what they are for, so a SharePoint pilot does not
have to run the Dataverse generators to get its template:

    schema      validate, regenerate the dictionary, export the model
    dataverse   dvgen, secgen, appgen, flowgen, solgen
    powerbi     pbigen
    sql         sqlgen
    sharepoint  spgen
    demo        demodata

Nothing here writes to a tenant. `dvdeploy.py` is the only tool that does, and
it is deliberately not in the pipeline.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
PYTHON = sys.executable


@dataclass
class Step:
    """One tool invocation."""
    name: str
    target: str
    args: list[str]
    why: str
    # A spec this step needs. Each declares the profile it was written for, so
    # running the Phase 1 roles against the Travel profile is skipped with a
    # reason rather than failing on tables that profile does not have.
    spec: Path | None = None
    strict: bool = True


TARGETS = ("schema", "dataverse", "powerbi", "sql", "sharepoint", "demo")


# Which spec belongs to which profile. A spec also declares its own profile, so
# this only has to say *where to look*; build.py skips one whose declaration
# disagrees, which is what keeps a Phase 1 spec out of a Phase 2 build.
SPECS = {
    "security": {"hr-starter": "phase1-roles.yaml", "law-firm-crm": "crm-roles.yaml"},
    "apps": {"hr-starter": "hr-admin.yaml", "law-firm-crm": "crm-intake.yaml"},
    "powerbi": {"hr-starter": "hr-dashboard.yaml", "law-firm-crm": "crm-dashboard.yaml"},
}


def spec_for(kind: str, profile: str) -> Path | None:
    """The spec file for this profile, if there is one."""
    name = SPECS[kind].get(profile)
    return (ROOT / kind / name) if name else None


def plan_steps(profile: str, prefix: str, solution: str | None,
               firm: str | None) -> list[Step]:
    solution = solution or f"{prefix.capitalize()}Solution"
    roles = spec_for("security", profile)
    app = spec_for("apps", profile)
    dashboard = spec_for("powerbi", profile)
    return [
        Step("validate", "schema", ["schematool.py", "validate"],
             "the YAML must be valid before anything reads it", strict=False),
        Step("docs --check", "schema", ["schematool.py", "docs", "--check"],
             "the data dictionary must match the YAML", strict=False),
        Step("export", "schema", ["schematool.py", "export", "--profile", profile],
             "every generator reads the resolved model, not the YAML", strict=False),

        Step("dvgen", "dataverse",
             ["dvgen.py", "--profile", profile, "--prefix", prefix,
              "--solution", solution],
             "tables, columns, relationships and keys", strict=False),
        Step("secgen", "dataverse",
             ["secgen.py", "--profile", profile, "--prefix", prefix]
             + (["--roles", str(roles)] if roles else []),
             "security roles and the access matrix", spec=roles),
        Step("appgen", "dataverse",
             ["appgen.py", "--profile", profile, "--prefix", prefix]
             + (["--app", str(app)] if app else [])
             + (["--roles", str(roles)] if roles else []),
             "site map, forms and views", spec=app),
        Step("flowgen", "dataverse",
             ["flowgen.py", "--profile", profile, "--prefix", prefix,
              "--solution", solution],
             "the approval and onboarding flows"),
        # Not --strict: solgen warns when a role or app was skipped for this
        # profile, which is the pipeline working as intended.
        Step("solgen", "dataverse",
             ["solgen.py", "--profile", profile, "--prefix", prefix,
              "--solution", solution],
             "one solution folder from everything above", strict=False),

        Step("pbigen", "powerbi",
             ["pbigen.py", "--profile", profile, "--prefix", prefix]
             + (["--spec", str(dashboard)] if dashboard else []),
             "the Power BI semantic model, measures and theme", spec=dashboard),

        Step("sqlgen", "sql", ["sqlgen.py", "--profile", profile],
             "Azure SQL migrations"),

        Step("spgen", "sharepoint",
             ["spgen.py", "--profile", profile, "--prefix", prefix],
             "a PnP site template and the SharePoint limits report",
             strict=False),

        Step("demodata", "demo",
             ["demodata.py", "--profile", profile]
             + (["--firm", firm] if firm else []),
             "demo rows for the fictional firm"),
    ]


def spec_profile(path: Path) -> str | None:
    """The profile a spec declares it was written for, if it says."""
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:                      # noqa: BLE001 — the tool will report it
        return None
    meta = data.get("meta") or data
    value = meta.get("profile")
    return str(value) if value else None


def skip_reason(step: Step, profile: str) -> str | None:
    """Why this step is not for this profile, if it is not."""
    if step.spec is None:
        if step.name in ("secgen", "appgen", "pbigen"):
            return f"no {step.name} spec is mapped to the '{profile}' profile"
        return None
    if not step.spec.exists():
        return f"no {step.spec.relative_to(ROOT)}"
    declared = spec_profile(step.spec)
    if declared and declared != profile:
        return (f"{step.spec.relative_to(ROOT)} is written for the "
                f"'{declared}' profile")
    return None


def run(step: Step, dry_run: bool) -> tuple[int, str, float]:
    command = [PYTHON, str(ROOT / "tools" / step.args[0])] + step.args[1:]
    if step.strict:
        command.append("--strict")
    if dry_run:
        return 0, " ".join(command[1:]), 0.0
    started = time.monotonic()
    result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
    elapsed = time.monotonic() - started
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode, output.strip(), elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the generator pipeline for one profile.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--solution", help="solution unique name for the Dataverse target")
    parser.add_argument("--firm", help="firm slug for the demo target")
    parser.add_argument("--targets", help=f"comma-separated: {', '.join(TARGETS)} "
                                          "(default: all of them)")
    parser.add_argument("--list", action="store_true",
                        help="print the commands without running them")
    parser.add_argument("--keep-going", action="store_true",
                        help="run every step even after one fails")
    args = parser.parse_args(argv)

    if not re.fullmatch(r"[a-z][a-z0-9]{1,7}", args.prefix.lower()):
        print(f"error: prefix '{args.prefix}' must be 2-8 characters, lowercase letters "
              "and digits, starting with a letter", file=sys.stderr)
        return 2

    wanted = [t.strip() for t in args.targets.split(",")] if args.targets else list(TARGETS)
    unknown = [t for t in wanted if t not in TARGETS]
    if unknown:
        print(f"error: unknown target(s) {', '.join(unknown)}; choose from "
              f"{', '.join(TARGETS)}", file=sys.stderr)
        return 2
    if "schema" not in wanted:
        wanted.insert(0, "schema")      # every generator needs the resolved model

    steps = [s for s in plan_steps(args.profile, args.prefix, args.solution, args.firm)
             if s.target in wanted]

    skipped: list[str] = []
    runnable: list[Step] = []
    for step in steps:
        reason = skip_reason(step, args.profile)
        if reason:
            skipped.append(f"{step.name}: {reason}")
            continue
        runnable.append(step)

    print(f"profile {args.profile}  prefix {args.prefix}  "
          f"targets {', '.join(wanted)}")
    print(f"{len(runnable)} step(s)" + (f", {len(skipped)} skipped" if skipped else ""))
    print("=" * 72)

    failed: list[str] = []
    for step in runnable:
        code, output, elapsed = run(step, args.list)
        if args.list:
            print(f"  {output}")
            continue
        marker = "ok  " if code == 0 else "FAIL"
        print(f"\n[{marker}] {step.name}  ({elapsed:.1f}s) — {step.why}")
        for line in output.splitlines():
            print(f"       {line}")
        if code != 0:
            failed.append(step.name)
            if not args.keep_going:
                print(f"\nstopped at {step.name}. Use --keep-going to run the rest.")
                break

    if args.list:
        for note in skipped:
            print(f"  (skipped) {note}")
        return 0

    print("\n" + "=" * 72)
    for note in skipped:
        print(f"skipped  {note}")
    if failed:
        print(f"{len(failed)} step(s) failed: {', '.join(failed)}")
        return 1

    print(f"all {len(runnable)} step(s) succeeded")
    outputs = [
        ("Dataverse plan", BUILD_DIR / "dataverse" / args.profile / "plan.json"),
        ("security matrix", BUILD_DIR / "dataverse" / args.profile / "security" / "matrix.md"),
        ("solution", BUILD_DIR / "solution" / (args.solution or "")),
        ("Power BI model", BUILD_DIR / "powerbi"),
        ("SQL migrations", BUILD_DIR / "sql" / args.profile),
        ("SharePoint limits", BUILD_DIR / "sharepoint" / args.profile / "limits.md"),
    ]
    existing = [(label, path) for label, path in outputs if path.exists()]
    if existing:
        print("\nwhere things landed:")
        for label, path in existing:
            print(f"  {label:18s} {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        os._exit(0)
