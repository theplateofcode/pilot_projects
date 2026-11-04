#!/usr/bin/env python3
"""Generate a PnP provisioning template from a resolved model.

Reads the JSON that `schematool.py export` writes and emits a PnP site template
(site columns, content types, lists, views and term groups) that
`Invoke-PnPSiteTemplate` can apply. Nothing here touches a tenant.

SharePoint is the constrained platform, and the point of this generator is to
hit those limits in the build rather than in production:

  · **Lookup threshold.** A view may include at most 12 lookup-type columns —
    lookups, Person columns and managed metadata all count. Default views are
    built within that budget and the omissions are reported.
  · **List view threshold.** A list over 5,000 items needs its filter and sort
    columns indexed. Tables the model marks `volume: high` get indexes, and a
    list that cannot be indexed within SharePoint's own limit is reported.
  · **No column-level security.** A column classified more strictly than its
    table cannot be protected here at all. Each one is reported, because the
    honest answer is "put that data on Dataverse or in its own list".
  · **Internal names are 32 characters.** Longer ones are truncated to a stable
    name and the mapping is written out, since a flow or app that guesses the
    internal name will break.

Usage
    python tools/spgen.py --profile travel-starter --prefix pfx
    python tools/spgen.py --profile intranet-starter --prefix pfx --site Intranet
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

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"

PNP_NS = "http://schemas.dev.office.com/PnP/2022/09/ProvisioningSchema"
# A fixed namespace, so the same column always gets the same field id and a
# re-applied template updates rather than duplicating.
FIELD_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

MAX_INTERNAL_NAME = 32
LOOKUP_BUDGET = 12                 # lookup-type columns per view
VIEW_THRESHOLD = 5000              # items before a list needs indexed columns
MAX_INDEXES = 20                   # indexed columns per list

TEMPLATE_TYPES = {"list": 100, "library": 101, "pages": 850}
SKIP_MODES = ("native", "none", "termstore")

# Schema type -> SharePoint field type. The dictionary's physical.sharepoint is
# the intent; this is the mechanism.
FIELD_TYPES = {
    "string": "Text",
    "text": "Note",
    "richtext": "Note",
    "email": "Text",
    "phone": "Text",
    "url": "URL",
    "integer": "Number",
    "duration": "Number",
    "decimal": "Number",
    "percent": "Number",
    "money": "Currency",
    "boolean": "Boolean",
    "date": "DateTime",
    "datetime": "DateTime",
    "autonumber": "Text",
    "image": "Thumbnail",
    "choice": "Choice",
    "multichoice": "MultiChoice",
    "lookup": "Lookup",
    "parent": "Lookup",
    "multilookup": "LookupMulti",
    "term": "TaxonomyFieldType",
    "multiterm": "TaxonomyFieldTypeMulti",
}
# Field types that count against the lookup budget.
LOOKUP_LIKE = {"Lookup", "LookupMulti", "User", "UserMulti",
               "TaxonomyFieldType", "TaxonomyFieldTypeMulti"}
# Types with no SharePoint column at all.
SKIP_TYPES = {"guid", "file"}

ADDRESS_PARTS = (("Line1", 255), ("Line2", 255), ("City", 100),
                 ("StateProvince", 100), ("PostalCode", 40))

SENSITIVITY_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


class SpError(Exception):
    pass


def internal_name(prefix: str, attr: str, qualifier: str = "") -> tuple[str, bool]:
    """A stable internal name within SharePoint's 32-character limit.

    Returns (name, was_shortened). The qualifier is the entity name, used only
    when one attribute name needs more than one shape across the site.
    """
    head = f"{qualifier[0].upper()}{qualifier[1:]}" if qualifier else ""
    base = re.sub(r"[^0-9A-Za-z]", "",
                  f"{prefix}{head}{attr[0].upper()}{attr[1:]}")
    if len(base) <= MAX_INTERNAL_NAME:
        return base, False
    digest = uuid.uuid5(FIELD_NAMESPACE, base).hex[:4]
    return f"{base[:MAX_INTERNAL_NAME - 5]}_{digest}", True


def field_id(internal: str) -> str:
    """Stable id from the internal name: one site column, one id."""
    return "{" + str(uuid.uuid5(FIELD_NAMESPACE, internal)) + "}"


def content_type_id(index: int) -> str:
    """A content type under Item (0x01), with a stable GUID-shaped suffix."""
    return f"0x0100{uuid.uuid5(FIELD_NAMESPACE, f'ct{index}').hex.upper()}"


def split_pascal(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name)


class Generator:
    def __init__(self, model: dict[str, Any], prefix: str, site: str | None = None):
        self.model = model
        self.prefix = prefix.lower()
        self.site = site or str(model.get("profile", "site"))
        self.entities: dict[str, Any] = model.get("entities", {})
        self.enums: dict[str, Any] = model.get("enums", {})
        self.term_sets: dict[str, Any] = model.get("termSets", {})
        self.terminology: dict[str, Any] = model.get("terminology", {})
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.limits: list[dict[str, Any]] = []
        self.renamed: dict[str, str] = {}

        self.lists = [n for n in sorted(self.entities)
                      if (self.entities[n].get("platforms", {}) or {})
                      .get("sharepoint", "list") not in SKIP_MODES]
        # Entity -> {attr: {name, id, type, ...}}
        self.fields: dict[str, dict[str, dict[str, Any]]] = {}
        self._build_fields()
        self._check_limits()

    # -- naming
    def list_url(self, entity: str) -> str:
        mode = (self.entities[entity].get("platforms", {}) or {}).get("sharepoint", "list")
        folder = {"library": "", "pages": "SitePages"}.get(mode, "Lists")
        plural = self._labels(entity)[1].replace(" ", "")
        return f"{folder}/{plural}" if folder else plural

    def _labels(self, entity: str) -> tuple[str, str]:
        spec = self.entities[entity]
        term = self.terminology.get(entity) or {}
        singular = term.get("singular") or spec.get("label") or split_pascal(entity)
        plural = term.get("plural") or spec.get("plural") or f"{singular}s"
        return singular, plural

    def _column_label(self, entity: str, attr: str, spec: dict[str, Any]) -> str:
        term = self.terminology.get(f"{entity}.{attr}") or {}
        if isinstance(term, dict) and term.get("label"):
            return term["label"]
        return spec.get("label") or split_pascal(attr[0].upper() + attr[1:])

    # -- fields
    def _build_fields(self) -> None:
        """Two passes: work out each column's shape, then name them.

        A site column in SharePoint belongs to the site, not to one list, so
        `name` on Employee and `name` on OrgUnit should be the *same* column
        reused by both content types — that is what site columns are for, and
        SharePoint rejects two fields with one internal name and different ids.
        So the first pass computes each column's shape, the second gives one
        name per distinct shape and qualifies with the entity only where an
        attribute genuinely needs more than one.
        """
        draft: list[tuple[str, dict[str, Any]]] = []
        for entity in self.lists:
            for attr, column in self.entities[entity]["attributes"].items():
                for field in self._fields_for(entity, attr, column):
                    draft.append((entity, field))

        # attr -> {shape signature: [(entity, field), ...]}
        shapes: dict[str, dict[tuple, list[tuple[str, dict[str, Any]]]]] = {}
        for entity, field in draft:
            shapes.setdefault(field["attr"], {}).setdefault(
                self._shape(field), []).append((entity, field))

        for attr, by_shape in shapes.items():
            shared = len(by_shape) == 1
            for signature, members in by_shape.items():
                if shared:
                    name, shortened = internal_name(self.prefix, attr)
                    if len(members) > 1:
                        self.notes.append(
                            f"{attr}: one site column {name} shared by "
                            f"{len(members)} lists")
                else:
                    # The same attribute name with different shapes cannot be
                    # one site column, so each gets the entity in its name.
                    entity = members[0][0]
                    name, shortened = internal_name(self.prefix, attr, entity)
                    self.notes.append(
                        f"{attr} on {', '.join(e for e, _ in members)}: shape differs "
                        f"from the other lists' {attr}, so it is its own column {name}")
                identifier = field_id(name)
                for entity, field in members:
                    field["name"] = name
                    field["id"] = identifier
                    field["shared"] = shared and len(members) > 1
                    if shortened:
                        self.renamed[f"{entity}.{attr}"] = name

        for entity in self.lists:
            self.fields[entity] = {}
        for entity, field in draft:
            self.fields[entity][field["attr"]] = field

        if self.renamed:
            self.notes.append(
                f"{len(self.renamed)} internal name(s) exceeded "
                f"{MAX_INTERNAL_NAME} characters and were shortened; the mapping is in "
                "manifest.json, because a flow that guesses the internal name breaks")

    @staticmethod
    def _shape(field: dict[str, Any]) -> tuple:
        """What makes two columns of the same name the same site column."""
        return (field["type"], field["maxLength"], field["decimals"],
                field["target"], tuple(field["choices"] or ()), field["required"],
                field["unique"])

    def _fields_for(self, entity: str, attr: str,
                    column: dict[str, Any]) -> list[dict[str, Any]]:
        kind = column["type"]
        physical = str((column.get("physical") or {}).get("sharepoint") or "")

        if kind in SKIP_TYPES:
            return []
        if "(native" in physical or physical.strip() in ("—", "-"):
            if physical.strip() in ("—", "-"):
                self.notes.append(
                    f"{entity}.{attr}: the dictionary gives no SharePoint column "
                    "(use Author or a Person column) — omitted")
            return []

        if kind == "address":
            out = []
            label = self._column_label(entity, attr, column)
            for suffix, length in ADDRESS_PARTS:
                out.extend(self._simple_field(
                    entity, f"{attr}{suffix}", column, "Text",
                    f"{label} – {split_pascal(suffix)}", max_length=length))
            out.extend(self._simple_field(
                entity, f"{attr}Country", column, "Lookup",
                f"{label} – Country", target="Country"))
            return out

        if kind == "polylookup":
            targets = column.get("targets") or ["*"]
            scope = "any list" if "*" in targets else ", ".join(targets)
            self.notes.append(
                f"{entity}.{attr} references {scope}; SharePoint has no multi-list "
                "lookup, so it is a list title plus an item id that a flow resolves")
            label = self._column_label(entity, attr, column)
            return (self._simple_field(entity, f"{attr}List", column, "Text",
                                       f"{label} (list)", max_length=255)
                    + self._simple_field(entity, f"{attr}ItemId", column, "Number",
                                         f"{label} (item id)"))

        field_type = FIELD_TYPES.get(kind)
        if field_type is None:
            self.warnings.append(f"{entity}.{attr}: no SharePoint field for '{kind}'")
            return []

        target = column.get("target")
        if kind in ("lookup", "parent") and target == "Employee":
            field_type = "User"
        elif kind == "multilookup" and target == "Employee":
            field_type = "UserMulti"

        return self._simple_field(entity, attr, column, field_type,
                                  self._column_label(entity, attr, column), target=target)

    def _simple_field(self, entity: str, attr: str, column: dict[str, Any],
                      field_type: str, display: str,
                      target: str | None = None,
                      max_length: int | None = None) -> list[dict[str, Any]]:
        required = bool(column.get("required")) and not column.get("readOnly")
        return [{
            "attr": attr,
            "name": "",                 # assigned in the second pass
            "id": "",
            "type": field_type,
            "display": display,
            "description": " ".join(str(column.get("description") or "").split()),
            "required": required,
            "indexed": bool(column.get("indexed") or column.get("unique")),
            "unique": bool(column.get("unique")),
            "target": target if field_type in ("Lookup", "LookupMulti") else None,
            "maxLength": max_length or (int(column.get("length") or 255)
                                        if field_type == "Text" else None),
            "decimals": self._decimals(column),
            "choices": self._choices(column) if field_type in ("Choice", "MultiChoice") else None,
            "default": column.get("default"),
            "readOnly": bool(column.get("readOnly")),
            "shared": False,
        }]

    @staticmethod
    def _decimals(column: dict[str, Any]) -> int | None:
        if column["type"] == "decimal":
            return int(column.get("scale") or 2)
        if column["type"] == "percent":
            return 2
        if column["type"] in ("integer", "duration"):
            return 0
        return None

    def _choices(self, column: dict[str, Any]) -> list[str]:
        enum = self.enums.get(column.get("enum") or column.get("target") or "") or {}
        return [v.get("label") or split_pascal(v["key"]).title()
                for v in enum.get("values", []) if not v.get("deprecated")]

    # -- limits
    def _check_limits(self) -> None:
        for entity in self.lists:
            spec = self.entities[entity]
            fields = self.fields[entity]
            lookups = [f for f in fields.values() if f["type"] in LOOKUP_LIKE]
            if len(lookups) > LOOKUP_BUDGET:
                self.limits.append({
                    "list": entity, "limit": "lookup threshold",
                    "detail": f"{len(lookups)} lookup-type columns, but a view may show "
                              f"{LOOKUP_BUDGET}; the default view shows the first "
                              f"{LOOKUP_BUDGET} and the rest are on the form only",
                })
                self.warnings.append(
                    f"{entity}: {len(lookups)} lookup-type columns exceeds the view "
                    f"budget of {LOOKUP_BUDGET}")

            if spec.get("volume") == "high":
                self.limits.append({
                    "list": entity, "limit": "list view threshold",
                    "detail": f"marked volume: high, so it will pass {VIEW_THRESHOLD:,} "
                              "items; filter and sort columns are indexed and views are "
                              "filtered, but consider Dataverse or SQL instead",
                })

            indexed = [f for f in fields.values() if f["indexed"]]
            if len(indexed) > MAX_INDEXES:
                self.errors.append(
                    f"{entity}: {len(indexed)} indexed columns exceeds SharePoint's "
                    f"limit of {MAX_INDEXES}")

            table_rank = SENSITIVITY_RANK.get(spec.get("sensitivity", "internal"), 1)
            for attr, column in spec["attributes"].items():
                rank = SENSITIVITY_RANK.get(column.get("sensitivity"), 0) \
                    if column.get("sensitivity") else 0
                if column.get("pii"):
                    rank = max(rank, SENSITIVITY_RANK["confidential"])
                if rank > table_rank:
                    self.limits.append({
                        "list": entity, "limit": "no column security",
                        "detail": f"{attr} is classified "
                                  f"{column.get('sensitivity') or 'pii'} on an "
                                  f"{spec.get('sensitivity')} list, and SharePoint has no "
                                  "column-level security — move it to its own list with "
                                  "broken inheritance, or put this module on Dataverse",
                    })
            if spec.get("sensitivity") == "restricted":
                self.limits.append({
                    "list": entity, "limit": "restricted list",
                    "detail": "the whole list is restricted; break permission inheritance "
                              "on it and grant only the owning group",
                })

    # -- XML
    def template(self) -> ET.Element:
        ET.register_namespace("pnp", PNP_NS)
        root = ET.Element(f"{{{PNP_NS}}}Provisioning")
        preferences = ET.SubElement(root, f"{{{PNP_NS}}}Preferences")
        preferences.set("Generator", "pilot_projects tools/spgen.py")
        templates = ET.SubElement(root, f"{{{PNP_NS}}}Templates")
        templates.set("ID", "CONTAINER-TEMPLATE")
        template = ET.SubElement(templates, f"{{{PNP_NS}}}ProvisioningTemplate")
        template.set("ID", f"TEMPLATE-{re.sub(r'[^A-Z0-9]', '', self.site.upper())}")
        template.set("Version", "1")
        template.set("BaseSiteTemplate", "STS#3")
        template.set("DisplayName", self.site)

        self._site_fields(template)
        self._content_types(template)
        self._lists(template)
        self._term_groups(template)
        return root

    def _site_fields(self, template: ET.Element) -> None:
        """One element per site column, however many lists use it."""
        container = ET.SubElement(template, f"{{{PNP_NS}}}SiteFields")
        emitted: set[str] = set()
        for entity in self.lists:
            for field in self.fields[entity].values():
                if field["name"] in emitted:
                    continue
                emitted.add(field["name"])
                container.append(self._field_element(entity, field))

    def _field_element(self, entity: str, field: dict[str, Any]) -> ET.Element:
        element = ET.Element("Field")
        element.set("ID", field["id"])
        element.set("Name", field["name"])
        element.set("StaticName", field["name"])
        element.set("DisplayName", field["display"])
        element.set("Type", field["type"])
        element.set("Group", self.site if field["shared"]
                    else f"{self.site} – {entity}")
        element.set("Required", "TRUE" if field["required"] else "FALSE")
        if field["description"]:
            element.set("Description", field["description"])
        if field["indexed"]:
            element.set("Indexed", "TRUE")
        if field["unique"]:
            element.set("EnforceUniqueValues", "TRUE")
            element.set("Indexed", "TRUE")           # uniqueness requires an index
        if field["readOnly"]:
            element.set("ReadOnly", "TRUE")

        if field["type"] == "Text" and field["maxLength"]:
            element.set("MaxLength", str(min(field["maxLength"], 255)))
        if field["type"] == "Note":
            element.set("NumLines", "6")
            element.set("RichText", "TRUE" if field["attr"].endswith("Html") else "FALSE")
        if field["type"] in ("Number", "Currency") and field["decimals"] is not None:
            element.set("Decimals", str(field["decimals"]))
        if field["type"] == "Currency":
            element.set("LCID", "2057")              # en-GB
        if field["type"] == "DateTime":
            element.set("Format", "DateOnly"
                        if self._is_date_only(entity, field["attr"]) else "DateTime")
        if field["type"] in ("User", "UserMulti"):
            element.set("UserSelectionMode", "PeopleOnly")
            if field["type"] == "UserMulti":
                element.set("Mult", "TRUE")
        if field["type"] in ("Lookup", "LookupMulti") and field["target"]:
            target = field["target"]
            if target in self.fields:
                # PnP resolves a list by title with {listid:<title>}. The list's
                # folder path in braces is not a token it knows, so a lookup set
                # that way is created unbound.
                element.set("List", f"{{listid:{self._labels(target)[1]}}}")
                element.set("ShowField", self._display_field(target))
            else:
                self.warnings.append(
                    f"{entity}.{field['attr']}: lookup target {target} has no list — "
                    "the field is created unbound and must be pointed at a list by hand")
            if field["type"] == "LookupMulti":
                element.set("Mult", "TRUE")
        if field["choices"] is not None:
            choices = ET.SubElement(element, "CHOICES")
            for choice in field["choices"]:
                ET.SubElement(choices, "CHOICE").text = choice
            if field["default"] is not None:
                label = self._choice_label(entity, field["attr"], field["default"])
                if label:
                    ET.SubElement(element, "Default").text = label
        elif field["default"] is not None and field["type"] == "Boolean":
            ET.SubElement(element, "Default").text = "1" if field["default"] else "0"
        return element

    def _is_date_only(self, entity: str, attr: str) -> bool:
        spec = self.entities[entity]["attributes"].get(attr)
        if spec and spec["type"] == "date":
            return True
        return attr.endswith("Date") or attr in ("validFrom", "validTo")

    def _display_field(self, entity: str) -> str:
        primary = self.entities[entity].get("primary") or "name"
        field = self.fields.get(entity, {}).get(primary)
        return field["name"] if field else "Title"

    def _choice_label(self, entity: str, attr: str, key: Any) -> str | None:
        spec = self.entities[entity]["attributes"].get(attr) or {}
        enum = self.enums.get(spec.get("enum") or "") or {}
        for value in enum.get("values", []):
            if value.get("key") == key:
                return value.get("label") or split_pascal(value["key"]).title()
        return None

    def _content_types(self, template: ET.Element) -> None:
        container = ET.SubElement(template, f"{{{PNP_NS}}}ContentTypes")
        for index, entity in enumerate(self.lists, start=1):
            singular, _ = self._labels(entity)
            element = ET.SubElement(container, f"{{{PNP_NS}}}ContentType")
            element.set("ID", content_type_id(index))
            element.set("Name", singular)
            element.set("Group", self.site)
            element.set("Description",
                        " ".join(self.entities[entity].get("description", "").split()))
            refs = ET.SubElement(element, f"{{{PNP_NS}}}FieldRefs")
            for field in self.fields[entity].values():
                ref = ET.SubElement(refs, f"{{{PNP_NS}}}FieldRef")
                ref.set("ID", field["id"])
                ref.set("Name", field["name"])
                if field["required"]:
                    ref.set("Required", "true")

    def _lists(self, template: ET.Element) -> None:
        container = ET.SubElement(template, f"{{{PNP_NS}}}Lists")
        for index, entity in enumerate(self.lists, start=1):
            spec = self.entities[entity]
            mode = (spec.get("platforms", {}) or {}).get("sharepoint", "list")
            singular, plural = self._labels(entity)
            element = ET.SubElement(container, f"{{{PNP_NS}}}ListInstance")
            element.set("Title", plural)
            element.set("Url", self.list_url(entity))
            element.set("TemplateType", str(TEMPLATE_TYPES.get(mode, 100)))
            element.set("ContentTypesEnabled", "true")
            element.set("RemoveExistingContentTypes", "true")
            element.set("EnableVersioning", "true")
            element.set("MinorVersionLimit", "0")
            element.set("MaxVersionLimit", "50")
            element.set("Description", " ".join(spec.get("description", "").split()))
            if spec.get("sensitivity") in ("confidential", "restricted"):
                element.set("EnableAttachments", "false")

            bindings = ET.SubElement(element, f"{{{PNP_NS}}}ContentTypeBindings")
            binding = ET.SubElement(bindings, f"{{{PNP_NS}}}ContentTypeBinding")
            binding.set("ContentTypeID", content_type_id(index))
            binding.set("Default", "true")

            self._views(element, entity)

    def _views(self, list_element: ET.Element, entity: str) -> None:
        """A default view inside the lookup budget, plus a form-only remainder."""
        fields = list(self.fields[entity].values())
        primary = self.entities[entity].get("primary") or "name"

        chosen: list[dict[str, Any]] = []
        lookup_count = 0
        # The primary column first, then the rest in declaration order.
        ordered = ([f for f in fields if f["attr"] == primary]
                   + [f for f in fields if f["attr"] != primary])
        omitted: list[str] = []
        for field in ordered:
            if len(chosen) >= 12 and field["type"] not in LOOKUP_LIKE:
                omitted.append(field["name"])
                continue
            if field["type"] in LOOKUP_LIKE:
                if lookup_count >= LOOKUP_BUDGET:
                    omitted.append(field["name"])
                    continue
                lookup_count += 1
            chosen.append(field)

        if omitted:
            self.notes.append(
                f"{entity}: default view shows {len(chosen)} of {len(fields)} columns; "
                f"{len(omitted)} are on the form only, within the {LOOKUP_BUDGET}-lookup "
                "view budget")

        views = ET.SubElement(list_element, f"{{{PNP_NS}}}Views")
        views.set("RemoveExistingViews", "true")
        view = ET.SubElement(views, "View")
        view.set("DisplayName", "All items")
        view.set("DefaultView", "TRUE")
        view.set("Type", "HTML")
        view.set("Scope", "Recursive")
        row_limit = ET.SubElement(view, "RowLimit")
        row_limit.set("Paged", "TRUE")
        row_limit.text = "100"
        view_fields = ET.SubElement(view, "ViewFields")
        for field in chosen:
            ref = ET.SubElement(view_fields, "FieldRef")
            ref.set("Name", field["name"])

        # A high-volume list must sort and filter on indexed columns only.
        if self.entities[entity].get("volume") == "high":
            query = ET.SubElement(view, "Query")
            order = ET.SubElement(query, "OrderBy")
            ref = ET.SubElement(order, "FieldRef")
            ref.set("Name", "Created")
            ref.set("Ascending", "FALSE")
            self.notes.append(
                f"{entity}: high volume, so the default view sorts on Created, which "
                "SharePoint indexes itself")

    def _term_groups(self, template: ET.Element) -> None:
        wanted = {name: spec for name, spec in self.term_sets.items()}
        if not wanted:
            return
        groups = ET.SubElement(template, f"{{{PNP_NS}}}TermGroups")
        group = ET.SubElement(groups, f"{{{PNP_NS}}}TermGroup")
        group.set("Name", self.site)
        group.set("ID", str(uuid.uuid5(FIELD_NAMESPACE, f"group:{self.site}")))
        group.set("Description", "Managed metadata for the pilot's taxonomies.")
        for name, spec in sorted(wanted.items()):
            term_set = ET.SubElement(group, f"{{{PNP_NS}}}TermSet")
            term_set.set("Name", split_pascal(name))
            term_set.set("ID", str(uuid.uuid5(FIELD_NAMESPACE, f"termset:{name}")))
            term_set.set("IsOpenForTermCreation", "true" if spec.get("open") else "false")
            term_set.set("Description", " ".join(str(spec.get("description", "")).split()))
            self._terms(term_set, name, spec.get("terms") or [], ())

    def _terms(self, parent: ET.Element, set_name: str, terms: list[Any],
               path: tuple[str, ...]) -> None:
        for item in terms:
            if isinstance(item, str):
                name, children = item, []
            elif isinstance(item, dict):
                name, children = item.get("name", ""), item.get("children") or []
            else:
                self.warnings.append(f"term set {set_name}: unreadable term {item!r}")
                continue
            if not name:
                continue
            element = ET.SubElement(parent, f"{{{PNP_NS}}}Term")
            element.set("Name", name)
            element.set("ID", str(uuid.uuid5(
                FIELD_NAMESPACE, f"term:{set_name}:{'/'.join(path + (name,))}")))
            if children:
                container = ET.SubElement(element, f"{{{PNP_NS}}}Terms")
                self._terms(container, set_name, children, path + (name,))

    # -- reports
    def limits_report(self) -> str:
        lines = [
            f"# {self.site} — SharePoint limits",
            "",
            "Generated by `tools/spgen.py`. Each row is a place where the model asks for",
            "something SharePoint cannot do, or can only do with care. This is the input",
            "to the stack decision in `docs/02-tech-stack-strategy.md`: a module with many",
            "rows here belongs on Dataverse or SQL.",
            "",
        ]
        if not self.limits:
            lines.append("Nothing to report: this profile fits SharePoint as it stands.")
            return "\n".join(lines) + "\n"

        by_limit: dict[str, list[dict[str, Any]]] = {}
        for item in self.limits:
            by_limit.setdefault(item["limit"], []).append(item)
        for limit in sorted(by_limit):
            lines.append(f"## {limit} ({len(by_limit[limit])})")
            lines.append("")
            lines.append("| List | Detail |")
            lines.append("|---|---|")
            for item in by_limit[limit]:
                lines.append(f"| `{item['list']}` | {item['detail']} |")
            lines.append("")
        return "\n".join(lines) + "\n"

    def build(self) -> dict[str, Any]:
        root = self.template()
        ET.indent(root, space="  ")
        xml = ET.tostring(root, encoding="unicode", xml_declaration=True)
        distinct = {f["name"]: f for entity in self.lists
                    for f in self.fields[entity].values()}
        lookup_fields = sum(1 for f in distinct.values() if f["type"] in LOOKUP_LIKE)
        shared = sum(1 for f in distinct.values() if f["shared"])
        return {
            "profile": self.model.get("profile"),
            "site": self.site,
            "prefix": self.prefix,
            "counts": {
                "lists": len(self.lists),
                "siteColumns": len(distinct),
                "sharedColumns": shared,
                "fieldReferences": sum(len(self.fields[e]) for e in self.lists),
                "lookupFields": lookup_fields,
                "contentTypes": len(self.lists),
                "termSets": len(self.term_sets),
                "limits": len(self.limits),
                "renamed": len(self.renamed),
            },
            "xml": xml,
            "limitsReport": self.limits_report(),
            "renamed": self.renamed,
            "limits": self.limits,
            "notes": self.notes,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a PnP provisioning template from a resolved model.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--prefix", required=True, help="field name prefix, e.g. pfx")
    parser.add_argument("--site", help="site display name (default: the profile name)")
    parser.add_argument("--model", help="resolved model JSON (default: build/<profile>.model.json)")
    parser.add_argument("--out", help="output directory (default: build/sharepoint/<profile>)")
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
    generator = Generator(model, args.prefix, args.site)
    plan = generator.build()

    out_dir = Path(args.out) if args.out else BUILD_DIR / "sharepoint" / args.profile
    if not plan["errors"]:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "template.xml").write_text(plan["xml"], encoding="utf-8")
        (out_dir / "limits.md").write_text(plan["limitsReport"], encoding="utf-8")
        (out_dir / "manifest.json").write_text(json.dumps(
            {k: v for k, v in plan.items() if k not in ("xml", "limitsReport")},
            indent=2), encoding="utf-8")
        counts = plan["counts"]
        print(f"Wrote {out_dir}")
        print(f"  {counts['lists']} lists, {counts['contentTypes']} content types")
        print(f"  {counts['siteColumns']} site columns "
              f"({counts['sharedColumns']} shared across lists, "
              f"{counts['lookupFields']} lookup-type)")
        print(f"  {counts['fieldReferences']} field references from the content types")
        print(f"  {counts['termSets']} term sets")
        if counts["renamed"]:
            print(f"  {counts['renamed']} internal name(s) shortened — see manifest.json")
        if counts["limits"]:
            print(f"  {counts['limits']} SharePoint limit(s) to know about — see limits.md")
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
