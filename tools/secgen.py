#!/usr/bin/env python3
"""Generate Dataverse security roles from security/*-roles.yaml.

Checks the role definitions against the resolved model, then writes the
Dataverse payloads and a readable access matrix. Nothing here touches a tenant.

What it checks
  · every table a role names exists in the profile and has a Dataverse table
    (one marked native or none has no privileges to grant). A standard:<table>
    resolves to that logical name, so prvReadaccount is emitted for Account.
  · a user-level depth on an organization-owned table is raised to Global,
    because Dataverse has no user-level rows there
  · a table no role can reach is reported, so a gap is visible
  · a table a role both grants and denies is an error
  · column security covers exactly the columns the model classifies more
    strictly than their table

Usage
    python tools/secgen.py --profile hr-starter --prefix wga
    python tools/secgen.py --profile hr-starter --prefix wga --roles security/phase1-roles.yaml
    python tools/secgen.py --profile hr-starter --prefix wga --strict
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dvgen import SENSITIVITY_RANK, logical  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
SECURITY_DIR = ROOT / "security"

# Our depth names -> Dataverse privilege depth.
DEPTHS = {
    "user": "Basic",
    "businessUnit": "Local",
    "parentChild": "Deep",
    "organization": "Global",
}
DEPTH_ORDER = ["user", "businessUnit", "parentChild", "organization"]

# Our privilege names -> the Dataverse privilege name prefix.
PRIVILEGES = {
    "create": "prvCreate",
    "read": "prvRead",
    "write": "prvWrite",
    "delete": "prvDelete",
    "append": "prvAppend",
    "appendTo": "prvAppendTo",
    "assign": "prvAssign",
    "share": "prvShare",
}
# Dataverse only supports a depth on these; the rest are all-or-nothing.
DEPTH_AWARE = {"create", "read", "write", "delete", "append", "appendTo", "assign", "share"}

SKIP_MODES = ("native", "none")


class SecError(Exception):
    pass


class RoleSet:
    def __init__(self, spec: dict[str, Any], model: dict[str, Any], prefix: str):
        self.spec = spec
        self.model = model
        self.prefix = prefix.lower()
        self.entities: dict[str, Any] = model.get("entities", {})
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.roles: dict[str, dict[str, Any]] = {}
        self.out_of_scope: dict[str, str] = {
            item["table"]: " ".join((item.get("why") or "").split())
            for item in (spec.get("outOfScope") or [])
        }
        self._build()

    # -- helpers
    def _dataverse_table(self, name: str) -> str | None:
        ent = self.entities.get(name)
        if ent is None:
            return None
        mode = (ent.get("platforms", {}) or {}).get("dataverse", "custom")
        if isinstance(mode, str) and mode.startswith("standard:"):
            return mode.split(":", 1)[1].strip().lower()
        if mode in SKIP_MODES:
            return None
        return logical(self.prefix, name)

    def _ownership(self, name: str) -> str:
        return (self.entities.get(name) or {}).get("ownership", "user")

    # -- build
    def _build(self) -> None:
        for role_spec in self.spec.get("roles", []):
            code = role_spec.get("code")
            if not code:
                self.errors.append("a role has no code")
                continue
            self.roles[code] = {
                "code": code,
                "name": role_spec.get("name", code),
                "description": " ".join((role_spec.get("description") or "").split()),
                "appliesTo": role_spec.get("appliesTo", ""),
                "inherits": role_spec.get("inherits"),
                "grants": {},            # table -> {privilege: depth}
                "denied": list((role_spec.get("denied") or {}).get("tables") or []),
                "deniedWhy": " ".join(((role_spec.get("denied") or {}).get("why") or "").split()),
                "rationale": [],
            }

        for code, role in self.roles.items():
            role_spec = next(r for r in self.spec["roles"] if r.get("code") == code)
            for rule in role_spec.get("rules", []):
                self._apply_rule(role, rule)
            self._check_denials(role)

        self._resolve_inheritance()
        self._check_coverage()
        self._check_column_profiles()

    def _apply_rule(self, role: dict[str, Any], rule: dict[str, Any]) -> None:
        depth = rule.get("depth", "user")
        if depth not in DEPTHS:
            self.errors.append(
                f"{role['code']}: depth '{depth}' is not one of {', '.join(DEPTHS)}")
            return
        privileges = rule.get("privileges") or []
        unknown = [p for p in privileges if p not in PRIVILEGES]
        if unknown:
            self.errors.append(
                f"{role['code']}: privilege(s) {', '.join(unknown)} are not valid")
            return

        for table in rule.get("tables") or []:
            if table not in self.entities:
                self.errors.append(
                    f"{role['code']}: table '{table}' is not in this profile")
                continue
            if self._dataverse_table(table) is None:
                self.warnings.append(
                    f"{role['code']}: '{table}' has no Dataverse table (it is marked "
                    "native or none) — privileges on it must be set by hand")
                continue

            effective = depth
            if depth != "organization" and self._ownership(table) == "organization":
                effective = "organization"
                self.notes.append(
                    f"{role['code']}: {table} is organization-owned, so {depth} depth "
                    "has no meaning — raised to organization"
                )

            grants = role["grants"].setdefault(table, {})
            for privilege in privileges:
                current = grants.get(privilege)
                # A later rule may widen a privilege but never narrow it.
                if current is None or DEPTH_ORDER.index(effective) > DEPTH_ORDER.index(current):
                    grants[privilege] = effective

            if rule.get("why"):
                role["rationale"].append(
                    {"tables": rule["tables"], "privileges": privileges,
                     "depth": effective, "why": " ".join(rule["why"].split())}
                )

    def _check_denials(self, role: dict[str, Any]) -> None:
        for table in role["denied"]:
            if table not in self.entities:
                self.errors.append(
                    f"{role['code']}: denied table '{table}' is not in this profile")
            elif table in role["grants"]:
                self.errors.append(
                    f"{role['code']}: '{table}' is both granted and denied"
                )

    def _resolve_inheritance(self) -> None:
        """A role that inherits another starts from its grants, then widens."""
        for code, role in self.roles.items():
            parent_code = role["inherits"]
            if not parent_code:
                continue
            parent = self.roles.get(parent_code)
            if parent is None:
                self.errors.append(f"{code}: inherits unknown role '{parent_code}'")
                continue
            merged: dict[str, dict[str, str]] = {}
            for table, privileges in parent["grants"].items():
                merged[table] = dict(privileges)
            for table, privileges in role["grants"].items():
                target = merged.setdefault(table, {})
                for privilege, depth in privileges.items():
                    current = target.get(privilege)
                    if current is None or DEPTH_ORDER.index(depth) > DEPTH_ORDER.index(current):
                        target[privilege] = depth
            # An explicit denial in the child removes what the parent granted
            # beyond the child's own scope, but keeps the parent's own-row access.
            for table in role["denied"]:
                if table in merged and table in parent["grants"]:
                    merged[table] = dict(parent["grants"][table])
                    self.notes.append(
                        f"{code}: denies {table} beyond what {parent_code} already grants "
                        f"({', '.join(f'{p} at {d}' for p, d in merged[table].items())})"
                    )
                elif table in merged:
                    del merged[table]
            role["effectiveGrants"] = merged
        for role in self.roles.values():
            role.setdefault("effectiveGrants", dict(role["grants"]))

    def _check_coverage(self) -> None:
        for name in self.out_of_scope:
            if name not in self.entities:
                self.errors.append(f"outOfScope lists '{name}', which is not in this profile")

        reachable = {t for role in self.roles.values() for t in role["effectiveGrants"]}
        # A table cannot be both deliberately ungranted and granted. When it is,
        # the generated matrix reads as though the roles disagree about it —
        # which is how the travel desk ended up unable to read a currency that
        # every traveller could.
        for name in sorted(set(self.out_of_scope) & reachable):
            holders = sorted(code for code, role in self.roles.items()
                             if name in role["effectiveGrants"])
            self.errors.append(
                f"'{name}' is declared outOfScope but granted by "
                f"{', '.join(holders)} — drop it from one or the other")
        for name, ent in sorted(self.entities.items()):
            if self._dataverse_table(name) is None or name in reachable:
                continue
            if name in self.out_of_scope:
                self.notes.append(f"{name} is out of scope for this phase: {self.out_of_scope[name]}")
                continue
            self.warnings.append(
                f"no role can reach '{name}' ({ent.get('sensitivity')}) — "
                "intentional, or a missing rule?"
            )

    def _check_column_profiles(self) -> None:
        """Column security must cover exactly the stricter-than-table columns."""
        expected: set[str] = set()
        for name, ent in self.entities.items():
            if self._dataverse_table(name) is None or name in self.out_of_scope:
                continue
            table_rank = SENSITIVITY_RANK.get(ent.get("sensitivity", "internal"), 1)
            for attr, spec in ent["attributes"].items():
                rank = SENSITIVITY_RANK.get(spec.get("sensitivity"), 0) if spec.get("sensitivity") else 0
                if spec.get("pii"):
                    rank = max(rank, SENSITIVITY_RANK["confidential"])
                if rank > table_rank:
                    expected.add(f"{name}.{attr}")

        declared: set[str] = set()
        for profile in self.spec.get("columnProfiles", []):
            for column in profile.get("columns", []):
                declared.add(column)
                table, _, attr = column.partition(".")
                if table not in self.entities:
                    self.errors.append(f"column profile: table '{table}' is not in this profile")
                elif attr not in self.entities[table]["attributes"]:
                    self.errors.append(f"column profile: '{column}' is not a column of {table}")
            for role_code in (profile.get("readFor") or []) + (profile.get("writeFor") or []):
                if role_code not in self.roles:
                    self.errors.append(
                        f"column profile '{profile.get('name')}': unknown role '{role_code}'")
            for role_code in (profile.get("readAlso") or {}):
                if role_code not in self.roles:
                    self.errors.append(
                        f"column profile '{profile.get('name')}': unknown role '{role_code}'")

        for column in sorted(expected - declared):
            self.errors.append(
                f"{column} is classified more strictly than its table but no column "
                "profile covers it"
            )
        for column in sorted(declared - expected):
            self.warnings.append(
                f"column profile covers {column}, which the model does not classify "
                "more strictly than its table — it will not be secured by dvgen"
            )

    # -- output
    def payloads(self) -> list[dict[str, Any]]:
        """One role record plus its privileges, for the Dataverse Web API."""
        out = []
        for code, role in self.roles.items():
            privileges = []
            for table in sorted(role["effectiveGrants"]):
                logical_name = self._dataverse_table(table)
                for privilege, depth in sorted(role["effectiveGrants"][table].items()):
                    privileges.append(
                        {
                            "Depth": DEPTHS[depth],
                            "PrivilegeName": f"{PRIVILEGES[privilege]}{logical_name}",
                            "_table": table,
                            "_privilege": privilege,
                        }
                    )
            out.append(
                {
                    "_code": code,
                    "role": {
                        "name": role["name"],
                        "description": role["description"][:2000],
                        "iscustomizable": {"Value": True},
                    },
                    # POST to /AddPrivilegesRole with the created role's id.
                    "addPrivileges": {"Privileges": privileges},
                    "privilegeCount": len(privileges),
                }
            )
        return out

    def matrix(self) -> str:
        """A markdown access matrix: one row per table, one column per role."""
        codes = list(self.roles)
        short = {"user": "own", "businessUnit": "unit", "parentChild": "unit+", "organization": "all"}
        lines = [
            "| Table | Sensitivity | " + " | ".join(self.roles[c]["name"].replace("HR – ", "")
                                                     for c in codes) + " |",
            "|---|---|" + "---|" * len(codes),
        ]
        for name in sorted(self.entities):
            if self._dataverse_table(name) is None or name in self.out_of_scope:
                continue
            ent = self.entities[name]
            cells = []
            for code in codes:
                grants = self.roles[code]["effectiveGrants"].get(name)
                if not grants:
                    cells.append("—")
                    continue
                # One group per depth, widest first. Showing only the widest
                # depth for the whole cell overstates the narrow privileges:
                # a project manager who reads their unit's time but creates
                # only their own rendered as "CRUD (unit)", which is a claim
                # the role does not make and the matrix is what a reviewer
                # approves.
                groups: dict[str, str] = {}
                for privilege, letter in (("create", "C"), ("read", "R"),
                                          ("write", "U"), ("delete", "D"),
                                          ("assign", "+a"), ("share", "+s")):
                    if privilege in grants:
                        groups[grants[privilege]] = groups.get(
                            grants[privilege], "") + letter
                parts = [
                    f"{letters or '·'} ({short[depth]})"
                    for depth, letters in sorted(
                        groups.items(), key=lambda kv: DEPTH_ORDER.index(kv[0]),
                        reverse=True)
                ]
                cells.append(" ".join(parts) or "—")
            lines.append(f"| {name} | {ent.get('sensitivity')} | " + " | ".join(cells) + " |")
        lines.append("")
        lines.append("C create · R read · U update · D delete · +a assign · +s share")
        lines.append("")
        lines.append("Scope: **own** = rows the user owns (Basic) · **unit** = their business "
                     "unit (Local) · **unit+** = their unit and those beneath it (Deep) · "
                     "**all** = the whole environment (Global).")
        return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Dataverse security roles.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--roles", help="roles YAML (default: security/phase1-roles.yaml)")
    parser.add_argument("--model", help="resolved model JSON (default: build/<profile>.model.json)")
    parser.add_argument("--out", help="output directory (default: build/dataverse/<profile>/security)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args(argv)

    roles_file = Path(args.roles) if args.roles else SECURITY_DIR / "phase1-roles.yaml"
    model_file = Path(args.model) if args.model else BUILD_DIR / f"{args.profile}.model.json"
    for path, hint in ((roles_file, "write the roles YAML first"),
                       (model_file, f"run: python tools/schematool.py export --profile {args.profile}")):
        if not path.exists():
            print(f"error: {path} not found — {hint}", file=sys.stderr)
            return 2

    spec = yaml.safe_load(roles_file.read_text(encoding="utf-8"))
    model = json.loads(model_file.read_text(encoding="utf-8"))
    role_set = RoleSet(spec, model, args.prefix)

    out_dir = Path(args.out) if args.out else BUILD_DIR / "dataverse" / args.profile / "security"
    (out_dir / "roles").mkdir(parents=True, exist_ok=True)
    payloads = role_set.payloads()
    written = set()
    for item in payloads:
        path = out_dir / "roles" / f"{item['_code']}.json"
        path.write_text(json.dumps(item, indent=2), encoding="utf-8")
        written.add(path.name)
    # A role file from an earlier run with a different roles file is not
    # harmless leftover: solgen packs whatever is in this directory, so three
    # HR roles once went into the travel solution. The directory is the
    # contract, so this run owns all of it.
    stale = sorted(path for path in (out_dir / "roles").glob("*.json")
                   if path.name not in written)
    for path in stale:
        path.unlink()
        role_set.notes.append(
            f"removed {path.name}, left over from an earlier run with a "
            "different roles file")
    (out_dir / "column-profiles.json").write_text(
        json.dumps(spec.get("columnProfiles", []), indent=2), encoding="utf-8")
    (out_dir / "matrix.md").write_text(role_set.matrix() + "\n", encoding="utf-8")
    (out_dir / "report.json").write_text(
        json.dumps(
            {
                "profile": args.profile,
                "prefix": args.prefix,
                "roles": [
                    {
                        "code": r["code"], "name": r["name"], "inherits": r["inherits"],
                        "tables": len(r["effectiveGrants"]),
                        "denied": r["denied"], "rationale": r["rationale"],
                    }
                    for r in role_set.roles.values()
                ],
                "outOfScope": role_set.out_of_scope,
                "errors": role_set.errors,
                "warnings": role_set.warnings,
                "notes": role_set.notes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Wrote {out_dir}")
    for item in payloads:
        role = role_set.roles[item["_code"]]
        print(f"  {role['name']}: {len(role['effectiveGrants'])} tables, "
              f"{item['privilegeCount']} privileges")
    for note in role_set.notes:
        print(f"  note: {note}")
    for warning in role_set.warnings:
        print(f"  warning: {warning}")
    for error in role_set.errors:
        print(f"  ERROR: {error}")

    if role_set.errors:
        return 1
    if args.strict and role_set.warnings:
        print("\nfailing because --strict was given and there are warnings")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Output was piped into something that closed early, e.g. `| head`.
        os._exit(0)
