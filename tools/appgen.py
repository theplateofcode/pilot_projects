#!/usr/bin/env python3
"""Generate a model-driven app from the schema and an app spec.

Reads `apps/<name>.yaml` (the site map and any overrides) plus the resolved
model, and emits the app's site map, a main form per table and a default view
per table. Building fifty forms by clicking is days of work that a column rename
then undoes; generating them means the YAML stays the single source of truth.

    sitemap.xml          areas, groups and sub-areas
    AppModule.xml        the app and the components it carries
    Forms/<Entity>.xml   a main form, with its columns grouped by purpose
    Views/<Entity>.xml   a default view (savedquery) with FetchXML and layout

The form layout is derived rather than arbitrary:

    Key information   required columns — what cannot be saved without
    Validity          validFrom / validTo, for an effective-dated table
    Details           everything else
    Sensitive         columns classified more strictly than their table, in
                      their own section so a column security profile maps to it
    Integration       externalId / sourceSystem / lastSyncedOn, collapsed
    Audit             created / modified, collapsed and read-only

Usage
    python tools/appgen.py --profile hr-starter --prefix wga
    python tools/appgen.py --profile hr-starter --prefix wga --app apps/hr-admin.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
APPS_DIR = ROOT / "apps"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dvgen import (SENSITIVITY_RANK, logical, native_currency_column,  # noqa: E402
                   split_pascal)

NAMESPACE = uuid.UUID("9d5c1b7e-4f2a-4c8d-b1e6-7a3f9c2d5e80")

# Columns Dataverse provides, which a form refers to by their native names.
NATIVE = re.compile(r"\(native\b")
AUDIT_COLUMNS = ("createdon", "createdby", "modifiedon", "modifiedby")

# Types with no form control worth placing.
SKIP_TYPES = {"guid", "polylookup", "multilookup", "multiterm", "address"}

# Platform-managed columns that do not belong on a form body. statecode and
# statuscode appear in the record's own header, and versionnumber is a
# concurrency token nobody should see or set.
NOT_ON_A_FORM = ("statecode", "statuscode", "versionnumber")

# A section per purpose, in the order they appear on the form.
SECTIONS = ("key", "validity", "details", "sensitive", "integration", "audit")
SECTION_LABELS = {
    "key": "Key information",
    "validity": "Validity",
    "details": "Details",
    "sensitive": "Sensitive",
    "integration": "Integration",
    "audit": "Audit",
}
COLLAPSED = ("integration", "audit")

# How many columns a view shows before it stops being readable on a laptop.
MAX_VIEW_COLUMNS = 8


def stable_id(*parts: str) -> str:
    return "{" + str(uuid.uuid5(NAMESPACE, ":".join(parts))) + "}"


class AppError(Exception):
    pass


class Generator:
    def __init__(self, spec: dict[str, Any], model: dict[str, Any], prefix: str,
                 roles: dict[str, Any] | None = None):
        self.spec = spec
        self.model = model
        self.prefix = prefix.lower()
        self.meta = spec.get("meta", {})
        self.entities: dict[str, Any] = model.get("entities", {})
        self.enums: dict[str, Any] = model.get("enums", {})
        self.terminology: dict[str, Any] = model.get("terminology", {})
        self.roles = roles or {}
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.tables: list[str] = []
        self._collect()

    # -- collection
    def _collect(self) -> None:
        seen: set[str] = set()
        for area in self.spec.get("areas") or []:
            for group in area.get("groups") or []:
                for table in group.get("tables") or []:
                    if table not in self.entities:
                        self.errors.append(
                            f"area '{area.get('id')}' group '{group.get('id')}': "
                            f"'{table}' is not in this profile")
                        continue
                    mode = (self.entities[table].get("platforms", {}) or {}) \
                        .get("dataverse", "custom")
                    if mode == "none":
                        self.errors.append(
                            f"'{table}' has no Dataverse table, so it cannot be on a "
                            "site map")
                        continue
                    if table in seen:
                        self.warnings.append(
                            f"'{table}' appears more than once in the site map")
                        continue
                    seen.add(table)
                    self.tables.append(table)

        role = self.meta.get("role")
        if role and self.roles:
            grants = (self.roles.get(role) or {}).get("effectiveGrants", {})
            if grants:
                for table in self.tables:
                    if table not in grants:
                        self.warnings.append(
                            f"'{table}' is on the site map but the '{role}' role cannot "
                            "read it, so the sub-area will be empty")
            else:
                self.notes.append(f"role '{role}' has no grants to check against")

        unplaced = [
            name for name, spec in sorted(self.entities.items())
            if name not in seen
            and (spec.get("platforms", {}) or {}).get("dataverse", "custom") == "custom"
        ]
        for name in unplaced:
            self.notes.append(f"{name} is not on the site map — reachable only "
                              "through a related record")

    # -- labels
    def labels(self, entity: str) -> tuple[str, str]:
        spec = self.entities[entity]
        term = self.terminology.get(entity) or {}
        singular = term.get("singular") or spec.get("label") or split_pascal(entity)
        plural = term.get("plural") or spec.get("plural") or f"{singular}s"
        return singular, plural

    def column_label(self, entity: str, attr: str, spec: dict[str, Any]) -> str:
        term = self.terminology.get(f"{entity}.{attr}") or {}
        if isinstance(term, dict) and term.get("label"):
            return term["label"]
        return spec.get("label") or split_pascal(attr[0].upper() + attr[1:])

    def physical(self, entity: str, attr: str, spec: dict[str, Any]) -> str | None:
        """The Dataverse column name a form control binds to.

        Where the table has a money column, its currency lookup is the native
        transactioncurrencyid that the money column creates — the same rule
        dvgen and pbigen apply, so all three agree on the column name. Binding
        a control to wga_currency on such a table would bind it to nothing.
        """
        marker = str((spec.get("physical") or {}).get("dataverse") or "")
        if NATIVE.search(marker):
            token = marker.split()[0]
            if token == "<table>id":
                return None                       # the key is never on a form
            return token if re.fullmatch(r"[a-z][a-z0-9_]*", token) else None
        attributes = self.entities.get(entity, {}).get("attributes", {})
        if (attr == native_currency_column(attributes)
                and spec.get("target") == "Currency"):
            return "transactioncurrencyid"
        return logical(self.prefix, attr)

    # -- form layout
    def section_of(self, entity: str, attr: str, spec: dict[str, Any]) -> str:
        trait = spec.get("trait")
        if trait == "base" and self.physical(entity, attr, spec) in AUDIT_COLUMNS:
            return "audit"
        if trait in ("base", "owned"):
            # Platform plumbing: present, but never the first thing on a form.
            return "details"
        if trait == "integration":
            return "integration"
        if trait == "effectiveDated":
            return "validity"
        table_rank = SENSITIVITY_RANK.get(
            self.entities[entity].get("sensitivity", "internal"), 1)
        rank = SENSITIVITY_RANK.get(spec.get("sensitivity"), 0) if spec.get("sensitivity") else 0
        if spec.get("pii"):
            rank = max(rank, SENSITIVITY_RANK["confidential"])
        if rank > table_rank:
            return "sensitive"
        if spec.get("required") and not spec.get("readOnly"):
            return "key"
        return "details"

    def form_columns(self, entity: str) -> dict[str, list[dict[str, Any]]]:
        """Columns grouped into sections, honouring any declared order."""
        spec = self.entities[entity]
        override = (self.spec.get("forms") or {}).get(entity) or {}
        declared = list(override.get("order") or [])
        for name in declared:
            if name not in spec["attributes"]:
                self.errors.append(
                    f"form override for {entity}: '{name}' is not a column")

        sections: dict[str, list[dict[str, Any]]] = {key: [] for key in SECTIONS}
        ordered = ([a for a in declared if a in spec["attributes"]]
                   + [a for a in spec["attributes"] if a not in declared])
        for attr in ordered:
            column = spec["attributes"][attr]
            if column["type"] in SKIP_TYPES:
                continue
            physical = self.physical(entity, attr, column)
            if physical is None or physical in NOT_ON_A_FORM:
                continue
            # A declared order means the author wants these together and first,
            # but only for columns they actually named.
            section = ("key" if attr in declared and not column.get("trait")
                       else self.section_of(entity, attr, column))
            sections[section].append({
                "attr": attr,
                "name": physical,
                "label": self.column_label(entity, attr, column),
                "required": bool(column.get("required")) and not column.get("readOnly"),
                "readOnly": bool(column.get("readOnly") or column.get("calculated")
                                 or section == "audit"),
                "rows": 4 if column["type"] in ("text", "richtext") else 1,
            })
        return {key: value for key, value in sections.items() if value}

    # -- XML
    def sitemap(self) -> ET.Element:
        root = ET.Element("SiteMap")
        for area in self.spec.get("areas") or []:
            area_element = ET.SubElement(root, "Area")
            area_element.set("Id", area["id"])
            area_element.set("ShowGroups", "true")
            if area.get("icon"):
                area_element.set("ResourceId", area["icon"])
            self._titles(area_element, area["name"])
            for group in area.get("groups") or []:
                group_element = ET.SubElement(area_element, "Group")
                group_element.set("Id", group["id"])
                self._titles(group_element, group["name"])
                if group.get("note"):
                    group_element.set("Description", " ".join(group["note"].split()))
                for table in group.get("tables") or []:
                    if table not in self.tables:
                        continue
                    sub = ET.SubElement(group_element, "SubArea")
                    sub.set("Id", f"{group['id']}_{table.lower()}")
                    sub.set("Entity", self._logical_table(table))
                    sub.set("Client", "All")
                    sub.set("AvailableOffline", "false")
                    self._titles(sub, self.labels(table)[1])
        return root

    def _logical_table(self, entity: str) -> str:
        mode = (self.entities[entity].get("platforms", {}) or {}).get("dataverse", "custom")
        if isinstance(mode, str) and mode.startswith("standard:"):
            return mode.split(":", 1)[1].strip().lower()
        return logical(self.prefix, entity)

    @staticmethod
    def _titles(parent: ET.Element, text: str) -> None:
        titles = ET.SubElement(parent, "Titles")
        title = ET.SubElement(titles, "Title")
        title.set("LCID", "1033")
        title.set("Title", text)

    def app_module(self) -> ET.Element:
        root = ET.Element("AppModule")
        name = self.meta.get("name", "App")
        unique = re.sub(r"[^A-Za-z0-9]", "", name)
        ET.SubElement(root, "UniqueName").text = f"{self.prefix}_{unique}"
        ET.SubElement(root, "Name").text = name
        ET.SubElement(root, "Description").text = \
            " ".join(str(self.meta.get("description", "")).split())
        ET.SubElement(root, "ClientType").text = "4"
        ET.SubElement(root, "NavigationType").text = "1"
        ET.SubElement(root, "AppModuleId").text = stable_id("appmodule", name)
        components = ET.SubElement(root, "AppModuleComponents")
        for table in self.tables:
            component = ET.SubElement(components, "AppModuleComponent")
            component.set("type", "1")            # entity
            component.set("schemaName", self._logical_table(table))
        for table in self.tables:
            for kind, type_code in (("form", "60"), ("view", "26")):
                component = ET.SubElement(components, "AppModuleComponent")
                component.set("type", type_code)
                component.set("id", stable_id(kind, table))
        return root

    def form(self, entity: str) -> ET.Element:
        sections = self.form_columns(entity)
        singular, _ = self.labels(entity)
        root = ET.Element("form")
        tabs = ET.SubElement(root, "tabs")
        tab = ET.SubElement(tabs, "tab")
        tab.set("name", "general")
        tab.set("id", stable_id("tab", entity, "general"))
        tab.set("verticallayout", "true")
        tab.set("expanded", "true")
        self._form_labels(tab, singular)
        columns = ET.SubElement(tab, "columns")
        column = ET.SubElement(columns, "column")
        column.set("width", "100%")
        container = ET.SubElement(column, "sections")

        for key in SECTIONS:
            if key not in sections:
                continue
            section = ET.SubElement(container, "section")
            section.set("name", key)
            section.set("id", stable_id("section", entity, key))
            section.set("showlabel", "true")
            section.set("columns", "1" if key in ("key", "validity") else "2")
            if key in COLLAPSED:
                section.set("visible", "true")
                section.set("locklevel", "0")
            self._form_labels(section, SECTION_LABELS[key])
            rows = ET.SubElement(section, "rows")
            for field in sections[key]:
                row = ET.SubElement(rows, "row")
                cell = ET.SubElement(row, "cell")
                cell.set("id", stable_id("cell", entity, field["attr"]))
                cell.set("showlabel", "true")
                if field["rows"] > 1:
                    cell.set("rowspan", str(field["rows"]))
                self._form_labels(cell, field["label"])
                control = ET.SubElement(cell, "control")
                control.set("id", field["name"])
                control.set("datafieldname", field["name"])
                control.set("classid", "{4273EDBD-AC1D-40d3-9FB2-095C621B552D}")
                if field["readOnly"]:
                    control.set("disabled", "true")
        return root

    @staticmethod
    def _form_labels(parent: ET.Element, text: str) -> None:
        labels = ET.SubElement(parent, "labels")
        label = ET.SubElement(labels, "label")
        label.set("description", text)
        label.set("languagecode", "1033")

    def view(self, entity: str) -> ET.Element:
        spec = self.entities[entity]
        override = (self.spec.get("views") or {}).get(entity) or {}
        table = self._logical_table(entity)
        primary = spec.get("primary") or "name"

        declared = list(override.get("columns") or [])
        for name in declared:
            if name not in spec["attributes"]:
                self.errors.append(
                    f"view override for {entity}: '{name}' is not a column")
        chosen = [a for a in declared if a in spec["attributes"]]
        if not chosen:
            chosen = self._default_view_columns(entity, primary)
        if len(chosen) > MAX_VIEW_COLUMNS:
            self.notes.append(
                f"{entity}: view trimmed from {len(chosen)} to {MAX_VIEW_COLUMNS} "
                "columns so it stays readable")
            chosen = chosen[:MAX_VIEW_COLUMNS]

        sort = override.get("sort") or [[primary, "asc"]]
        for item in sort:
            if item[0] not in spec["attributes"]:
                self.errors.append(
                    f"view override for {entity}: cannot sort on '{item[0]}'")

        root = ET.Element("savedquery")
        ET.SubElement(root, "savedqueryid").text = stable_id("view", entity)
        ET.SubElement(root, "name").text = f"Active {self.labels(entity)[1]}"
        ET.SubElement(root, "returnedtypecode").text = table
        ET.SubElement(root, "querytype").text = "0"
        ET.SubElement(root, "isdefault").text = "1"
        ET.SubElement(root, "isquickfindquery").text = "0"

        fetch = ET.SubElement(root, "fetchxml")
        fetch_root = ET.Element("fetch")
        fetch_root.set("version", "1.0")
        fetch_root.set("mapping", "logical")
        entity_element = ET.SubElement(fetch_root, "entity")
        entity_element.set("name", table)
        for attr in chosen:
            physical = self.physical(entity, attr, spec["attributes"][attr])
            if physical is None:
                continue
            attribute = ET.SubElement(entity_element, "attribute")
            attribute.set("name", physical)
        for attr, direction in sort:
            column = spec["attributes"].get(attr)
            if column is None:
                continue                    # already reported above
            physical = self.physical(entity, attr, column)
            if physical is None:
                continue
            order = ET.SubElement(entity_element, "order")
            order.set("attribute", physical)
            order.set("descending", "true" if str(direction).lower().startswith("desc")
                      else "false")
        # Active rows only: statecode 0 is Active on every Dataverse table.
        filter_element = ET.SubElement(entity_element, "filter")
        filter_element.set("type", "and")
        condition = ET.SubElement(filter_element, "condition")
        condition.set("attribute", "statecode")
        condition.set("operator", "eq")
        condition.set("value", "0")
        fetch.text = ET.tostring(fetch_root, encoding="unicode")

        layout = ET.SubElement(root, "layoutxml")
        grid = ET.Element("grid")
        grid.set("name", "resultset")
        grid.set("object", "1")
        primary_column = spec["attributes"].get(primary)
        grid.set("jump", (self.physical(entity, primary, primary_column)
                          if primary_column else None) or "createdon")
        grid.set("select", "1")
        grid.set("preview", "1")
        grid.set("icon", "1")
        row = ET.SubElement(grid, "row")
        row.set("name", "result")
        row.set("id", f"{table}id")
        # The first column is widest; the rest share what is left.
        widths = ["200"] + ["125"] * (len(chosen) - 1)
        for attr, width in zip(chosen, widths):
            physical = self.physical(entity, attr, spec["attributes"][attr])
            if physical is None:
                continue
            cell = ET.SubElement(row, "cell")
            cell.set("name", physical)
            cell.set("width", width)
        layout.text = ET.tostring(grid, encoding="unicode")
        return root

    def _default_view_columns(self, entity: str, primary: str) -> list[str]:
        """The primary column, then the columns that identify a row."""
        attributes = self.entities[entity]["attributes"]
        chosen = [primary] if primary in attributes else []
        # Required lookups and codes identify a row; long text never does.
        for attr, spec in attributes.items():
            if attr in chosen or spec.get("trait") or spec["type"] in SKIP_TYPES:
                continue
            if spec["type"] in ("text", "richtext", "file", "image"):
                continue
            if self.section_of(entity, attr, spec) == "sensitive":
                continue
            if spec.get("required") or spec["type"] in ("lookup", "parent", "choice"):
                chosen.append(attr)
        for attr, spec in attributes.items():
            if len(chosen) >= MAX_VIEW_COLUMNS:
                break
            if attr in chosen or spec.get("trait") or spec["type"] in SKIP_TYPES:
                continue
            if spec["type"] in ("text", "richtext", "file", "image"):
                continue
            if self.section_of(entity, attr, spec) == "sensitive":
                continue
            chosen.append(attr)
        return chosen

    # -- build
    def build(self) -> dict[str, Any]:
        forms: dict[str, str] = {}
        views: dict[str, str] = {}
        if not self.errors:
            for table in self.tables:
                forms[table] = self._pretty(self.form(table))
                views[table] = self._pretty(self.view(table))
        sitemap = self._pretty(self.sitemap()) if not self.errors else ""
        app = self._pretty(self.app_module()) if not self.errors else ""
        sensitive_sections = sum(
            1 for table in self.tables if "sensitive" in self.form_columns(table))
        return {
            "profile": self.model.get("profile"),
            "app": self.meta.get("name"),
            "prefix": self.prefix,
            "counts": {
                "areas": len(self.spec.get("areas") or []),
                "groups": sum(len(a.get("groups") or [])
                              for a in self.spec.get("areas") or []),
                "tables": len(self.tables),
                "forms": len(forms),
                "views": len(views),
                "sensitiveSections": sensitive_sections,
                "notOnTheSiteMap": len([n for n in self.notes
                                        if "not on the site map" in n]),
            },
            "sitemap": sitemap,
            "appModule": app,
            "forms": forms,
            "views": views,
            "notes": self.notes,
            "warnings": self.warnings,
            "errors": self.errors,
        }

    @staticmethod
    def _pretty(element: ET.Element) -> str:
        ET.indent(element, space="  ")
        return ET.tostring(element, encoding="unicode", xml_declaration=True)


def load_roles(profile: str, prefix: str) -> dict[str, Any]:
    """The security roles, so the generator can check the app against them."""
    roles_file = ROOT / "security" / "phase1-roles.yaml"
    model_file = BUILD_DIR / f"{profile}.model.json"
    if not (roles_file.exists() and model_file.exists()):
        return {}
    try:
        import secgen
        role_set = secgen.RoleSet(
            yaml.safe_load(roles_file.read_text(encoding="utf-8")),
            json.loads(model_file.read_text(encoding="utf-8")), prefix)
        return role_set.roles
    except Exception:                      # noqa: BLE001 — the check is optional
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a model-driven app.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--app", help="app YAML (default: apps/hr-admin.yaml)")
    parser.add_argument("--model", help="resolved model JSON (default: build/<profile>.model.json)")
    parser.add_argument("--out", help="output directory (default: build/apps/<app>)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args(argv)

    if not re.fullmatch(r"[a-z][a-z0-9]{1,7}", args.prefix.lower()):
        print(f"error: prefix '{args.prefix}' must be 2-8 characters, lowercase letters "
              "and digits, starting with a letter", file=sys.stderr)
        return 2

    app_file = Path(args.app) if args.app else APPS_DIR / "hr-admin.yaml"
    model_file = Path(args.model) if args.model else BUILD_DIR / f"{args.profile}.model.json"
    for path, hint in ((app_file, "write the app YAML first"),
                       (model_file, f"run: python tools/schematool.py export "
                                    f"--profile {args.profile}")):
        if not path.exists():
            print(f"error: {path} not found — {hint}", file=sys.stderr)
            return 2

    spec = yaml.safe_load(app_file.read_text(encoding="utf-8"))
    model = json.loads(model_file.read_text(encoding="utf-8"))
    generator = Generator(spec, model, args.prefix, load_roles(args.profile, args.prefix))
    plan = generator.build()

    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(plan["app"] or "app")).strip("-").lower()
    out_dir = Path(args.out) if args.out else BUILD_DIR / "apps" / slug
    if not plan["errors"]:
        (out_dir / "Forms").mkdir(parents=True, exist_ok=True)
        (out_dir / "Views").mkdir(parents=True, exist_ok=True)
        (out_dir / "sitemap.xml").write_text(plan["sitemap"], encoding="utf-8")
        (out_dir / "AppModule.xml").write_text(plan["appModule"], encoding="utf-8")
        for table, xml in plan["forms"].items():
            (out_dir / "Forms" / f"{table}.xml").write_text(xml, encoding="utf-8")
        for table, xml in plan["views"].items():
            (out_dir / "Views" / f"{table}.xml").write_text(xml, encoding="utf-8")
        (out_dir / "manifest.json").write_text(json.dumps(
            {k: v for k, v in plan.items()
             if k not in ("sitemap", "appModule", "forms", "views")}
            | {"tables": generator.tables}, indent=2), encoding="utf-8")
        counts = plan["counts"]
        print(f"Wrote {out_dir}")
        print(f"  {counts['areas']} areas, {counts['groups']} groups, "
              f"{counts['tables']} tables")
        print(f"  {counts['forms']} forms, {counts['views']} views")
        print(f"  {counts['sensitiveSections']} form(s) with a Sensitive section")
        if counts["notOnTheSiteMap"]:
            print(f"  {counts['notOnTheSiteMap']} table(s) reachable only through a "
                  "related record — see manifest.json")
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
