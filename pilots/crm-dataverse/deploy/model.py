"""Canonical model -> Dataverse metadata.

Reads the resolved model produced by ``tools/schematool.py export`` and turns it
into Dataverse Web API metadata payloads. Every physical decision already lives
in the schema (``attributes[x].physical.dataverse``); this module only translates
it. Anything mapped to a native Dataverse column or table is skipped, so the
standard ``account``, ``contact`` and ``transactioncurrency`` tables are extended
rather than duplicated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

LANG = 1033
MANAGED_REQUIRED = "canmodifyrequirementlevelsettings"

# Types the generator understands. Anything else is reported, not guessed at.
SUPPORTED = {
    "string", "text", "richtext", "email", "phone", "url", "integer", "decimal",
    "percent", "money", "boolean", "date", "datetime", "duration", "choice",
    "multichoice", "lookup", "parent", "multilookup", "term", "multiterm",
    "address", "autonumber", "image", "polylookup", "file", "guid",
}


def label(text: str, language: int = LANG) -> dict:
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.Label",
        "LocalizedLabels": [
            {
                "@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                "Label": text or "",
                "LanguageCode": language,
            }
        ],
    }


def required_level(required: bool, recommended: bool = False) -> dict:
    value = "ApplicationRequired" if required else ("Recommended" if recommended else "None")
    return {"Value": value, "CanBeChanged": True, "ManagedPropertyLogicalName": MANAGED_REQUIRED}


def split_words(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ").strip()


def title_label(name: str) -> str:
    words = split_words(name)
    return words[:1].upper() + words[1:]


def pascal(name: str) -> str:
    return name[:1].upper() + name[1:]


def truncate(name: str, limit: int) -> str:
    """Keep names inside Dataverse limits without losing uniqueness."""
    if len(name) <= limit:
        return name
    digest = f"{abs(hash(name)) % 0xFFFF:04x}"
    return name[: limit - 5] + "_" + digest


# --------------------------------------------------------------------------- model

@dataclass
class Column:
    """One Dataverse column (or relationship) derived from one schema attribute."""

    entity: str                 # canonical entity name, e.g. "Opportunity"
    name: str                   # canonical attribute name, e.g. "estimatedValue"
    logical: str                # Dataverse logical name
    schema: str                 # Dataverse schema name
    display: str
    spec: dict                  # the schema attribute
    kind: str                   # attribute | lookup | manytomany | skipped
    payload: dict | None = None
    relationship: dict | None = None
    note: str = ""

    @property
    def type(self) -> str:
        return self.spec.get("type", "")

    @property
    def required(self) -> bool:
        return bool(self.spec.get("required"))

    @property
    def read_only(self) -> bool:
        return bool(self.spec.get("readOnly"))


@dataclass
class Table:
    name: str                   # canonical name
    spec: dict
    logical: str
    schema: str
    display: str
    plural: str
    is_custom: bool
    primary_logical: str
    primary_display: str
    columns: list[Column] = field(default_factory=list)
    natives: list[Column] = field(default_factory=list)
    keys: list[list[str]] = field(default_factory=list)
    object_type_code: int | None = None
    entity_set: str | None = None

    def column(self, canonical_name: str) -> Column | None:
        for col in self.columns + self.natives:
            if col.name == canonical_name:
                return col
        return None

    def logical_of(self, canonical_name: str) -> str | None:
        """Dataverse logical name of a canonical attribute, or None if it is not deployed."""
        col = self.column(canonical_name)
        if col and col.kind in ("attribute", "lookup", "native"):
            return col.logical
        return None


class ScopeError(RuntimeError):
    pass


class CrmModel:
    """The resolved model, scoped and mapped for one Dataverse deployment."""

    def __init__(self, model: dict, prefix: str, include: Iterable[str], exclude: Iterable[str] = ()):
        self.raw = model
        self.prefix = prefix.lower().strip("_")
        self.enums: dict[str, dict] = model.get("enums", {})
        self.term_sets: dict[str, dict] = model.get("termSets", {})
        self.seed: dict[str, list] = model.get("seed", {})
        self.exclude = set(exclude)
        self.warnings: list[str] = []
        self.tables: dict[str, Table] = {}
        self.used_enums: set[str] = set()
        self._entities = model["entities"]
        self._scope = self._resolve_scope(include)
        for name in self._scope:
            self.tables[name] = self._build_table(name)
        self._map_columns()

    # ------------------------------------------------------------------ scope
    def _dataverse_mapping(self, name: str) -> str:
        return self._entities[name]["platforms"]["dataverse"]

    def _resolve_scope(self, include: Iterable[str]) -> list[str]:
        wanted = [n for n in include if n not in self.exclude]
        for name in wanted:
            if name not in self._entities:
                raise ScopeError(f"table '{name}' is not in this profile's model")
        scope = list(dict.fromkeys(wanted))
        # Pull in anything a required lookup points at; a model is only
        # deployable if every mandatory parent exists.
        added = True
        while added:
            added = False
            for name in list(scope):
                entity = self._entities[name]
                if self._dataverse_mapping(name) == "native":
                    continue
                for attr_name, attr in entity["attributes"].items():
                    if self._is_native(attr):
                        continue
                    if attr.get("type") not in ("lookup", "parent", "term", "multiterm"):
                        continue
                    targets = [attr["target"]] if attr.get("target") else []
                    if attr.get("type") in ("term", "multiterm"):
                        targets = ["Term", "TermSet"]
                    for target in targets:
                        if target in scope or target not in self._entities:
                            continue
                        if not attr.get("required") and attr.get("type") != "parent":
                            continue
                        if target in self.exclude:
                            raise ScopeError(
                                f"{name}.{attr_name} is a required reference to '{target}', "
                                f"which is excluded — include it or make the column optional"
                            )
                        scope.append(target)
                        added = True
        scope = [n for n in scope if self._dataverse_mapping(n) != "native"]
        return sorted(scope, key=lambda n: (not self._entities[n]["platforms"]["dataverse"].startswith("standard"), n))

    def _is_native(self, attr: dict) -> bool:
        physical = (attr.get("physical", {}) or {}).get("dataverse", "") or ""
        return "(native" in physical or attr.get("trait") == "base" or attr.get("trait") == "owned"

    # ----------------------------------------------------------------- tables
    def _build_table(self, name: str) -> Table:
        entity = self._entities[name]
        mapping = self._dataverse_mapping(name)
        is_custom = mapping == "custom"
        logical = f"{self.prefix}_{name.lower()}" if is_custom else mapping.split(":", 1)[1]
        primary_attr = entity.get("primary") or "name"
        if is_custom:
            if primary_attr == "auto" or primary_attr not in entity["attributes"]:
                primary_logical = f"{self.prefix}_name"
                primary_display = "Name"
            else:
                primary_logical = f"{self.prefix}_{primary_attr.lower()}"
                primary_display = title_label(primary_attr)
        else:
            primary_logical = {"account": "name", "contact": "fullname", "transactioncurrency": "currencyname"}.get(
                logical, "name"
            )
            primary_display = title_label(primary_attr)
        return Table(
            name=name,
            spec=entity,
            logical=logical,
            schema=f"{self.prefix}_{pascal(name)}" if is_custom else logical,
            display=entity.get("label") or title_label(name),
            plural=entity.get("plural") or (entity.get("label") or title_label(name)) + "s",
            is_custom=is_custom,
            primary_logical=primary_logical,
            primary_display=primary_display,
            keys=[k for k in entity.get("keys", [])],
        )

    # ---------------------------------------------------------------- columns
    def _map_columns(self) -> None:
        for table in self.tables.values():
            for attr_name, attr in table.spec["attributes"].items():
                if self._is_native(attr):
                    native = self._native_column(table, attr_name, attr)
                    if native:
                        table.natives.append(native)
                    continue
                if attr.get("type") == "guid":
                    continue
                if not table.is_custom and attr_name == table.spec.get("primary"):
                    continue
                for column in self._map_column(table, attr_name, attr):
                    table.columns.append(column)

    def _native_column(self, table: Table, attr_name: str, attr: dict) -> Column | None:
        """Record the native Dataverse column a schema attribute maps onto.

        Nothing is created for these, but views and forms can still refer to them
        by their canonical name (``Account.name`` -> ``name``).
        """
        physical = (attr.get("physical", {}) or {}).get("dataverse", "") or ""
        token = physical.split(" ", 1)[0].strip()
        if not token or "*" in token:
            return None
        token = token.replace("<table>", table.logical)
        if not re.fullmatch(r"[a-z0-9_]+", token):
            return None
        return Column(
            entity=table.name, name=attr_name, logical=token, schema=token,
            display=attr.get("label") or title_label(attr_name), spec=attr,
            kind="native", note=physical,
        )

    def _column_names(self, attr_name: str, suffix: str = "") -> tuple[str, str]:
        base = f"{attr_name}{suffix}"
        logical = truncate(f"{self.prefix}_{base.lower()}", 50)
        schema = truncate(f"{self.prefix}_{pascal(base)}", 50)
        return logical, schema

    def _map_column(self, table: Table, attr_name: str, attr: dict) -> list[Column]:
        kind = attr.get("type")
        if kind not in SUPPORTED:
            self.warnings.append(f"{table.name}.{attr_name}: unsupported type '{kind}' — skipped")
            return []

        if kind == "address":
            return self._address_columns(table, attr_name, attr)
        if kind == "polylookup":
            return self._polylookup_columns(table, attr_name, attr)
        if kind in ("multilookup", "multiterm"):
            return self._manytomany_column(table, attr_name, attr)
        if kind in ("lookup", "parent", "term"):
            return self._lookup_column(table, attr_name, attr)

        logical, schema = self._column_names(attr_name)
        display = attr.get("label") or title_label(attr_name)
        payload = self._simple_payload(table, attr_name, attr, logical, schema, display)
        if payload is None:
            return []
        is_primary = table.is_custom and logical == table.primary_logical
        column = Column(
            entity=table.name, name=attr_name, logical=logical, schema=schema, display=display,
            spec=attr, kind="attribute", payload=payload,
        )
        if is_primary:
            column.note = "primary name column (created with the table)"
        return [column]

    def _simple_payload(self, table, attr_name, attr, logical, schema, display) -> dict | None:
        kind = attr["type"]
        common = {
            "SchemaName": schema,
            "LogicalName": logical,
            "DisplayName": label(display),
            "Description": label(attr.get("description") or ""),
            "RequiredLevel": required_level(bool(attr.get("required")) and not attr.get("readOnly")),
        }

        if kind in ("string", "email", "phone", "url", "autonumber"):
            fmt = {"email": "Email", "phone": "Phone", "url": "Url"}.get(kind, "Text")
            payload = {
                "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
                "MaxLength": int(attr.get("length") or (30 if kind == "autonumber" else 100)),
                "FormatName": {"Value": fmt},
                **common,
            }
            if kind == "autonumber":
                payload["AutoNumberFormat"] = attr.get("format") or "{SEQNUM:6}"
                payload["MaxLength"] = max(payload["MaxLength"], 30)
            return payload

        if kind in ("text", "richtext"):
            return {
                "@odata.type": "Microsoft.Dynamics.CRM.MemoAttributeMetadata",
                "MaxLength": int(attr.get("length") or 4000),
                "FormatName": {"Value": "TextArea"},
                "IsRichTextEnabled": kind == "richtext",
                **common,
            }

        if kind == "integer" or kind == "duration":
            return {
                "@odata.type": "Microsoft.Dynamics.CRM.IntegerAttributeMetadata",
                "Format": "Duration" if kind == "duration" else "None",
                "MinValue": int(attr.get("min", 0 if kind == "duration" else -2147483648)),
                "MaxValue": int(attr.get("max", 2147483647)),
                **common,
            }

        if kind in ("decimal", "percent"):
            precision = 2 if kind == "percent" else int(attr.get("scale", 2))
            return {
                "@odata.type": "Microsoft.Dynamics.CRM.DecimalAttributeMetadata",
                "Precision": precision,
                "MinValue": float(attr.get("min", 0 if kind == "percent" else -100000000000)),
                "MaxValue": float(attr.get("max", 100 if kind == "percent" else 100000000000)),
                **common,
            }

        if kind == "money":
            return {
                "@odata.type": "Microsoft.Dynamics.CRM.MoneyAttributeMetadata",
                "Precision": 2,
                "PrecisionSource": 2,
                "ImeMode": "Disabled",
                "MinValue": 0.0,
                "MaxValue": 1000000000000.0,
                **common,
            }

        if kind == "boolean":
            default = bool(attr.get("default", False))
            return {
                "@odata.type": "Microsoft.Dynamics.CRM.BooleanAttributeMetadata",
                "DefaultValue": default,
                "OptionSet": {
                    "@odata.type": "Microsoft.Dynamics.CRM.BooleanOptionSetMetadata",
                    "TrueOption": {"Value": 1, "Label": label("Yes")},
                    "FalseOption": {"Value": 0, "Label": label("No")},
                },
                **common,
            }

        if kind in ("date", "datetime"):
            date_only = kind == "date"
            return {
                "@odata.type": "Microsoft.Dynamics.CRM.DateTimeAttributeMetadata",
                "Format": "DateOnly" if date_only else "DateAndTime",
                "DateTimeBehavior": {"Value": "DateOnly" if date_only else "UserLocal"},
                **common,
            }

        if kind in ("choice", "multichoice"):
            enum_name = attr["enum"]
            self.used_enums.add(enum_name)
            payload = {
                "@odata.type": "Microsoft.Dynamics.CRM."
                + ("MultiSelectPicklistAttributeMetadata" if kind == "multichoice" else "PicklistAttributeMetadata"),
                "_globalOptionSet": self.option_set_name(enum_name),
                **common,
            }
            default_key = attr.get("default")
            if default_key is not None and kind == "choice":
                value = self.enum_value(enum_name, default_key)
                if value is not None:
                    payload["DefaultFormValue"] = value
            return payload

        if kind == "image":
            return {
                "@odata.type": "Microsoft.Dynamics.CRM.ImageAttributeMetadata",
                "MaxSizeInKB": 10240,
                "IsPrimaryImage": True,
                **common,
            }

        if kind == "file":
            return {
                "@odata.type": "Microsoft.Dynamics.CRM.FileAttributeMetadata",
                "MaxSizeInKB": 32768,
                **common,
            }

        self.warnings.append(f"{table.name}.{attr_name}: no mapping for '{kind}' — skipped")
        return None

    # --------------------------------------------------------- relationships
    def _target_table(self, table: Table, attr_name: str, attr: dict, target: str) -> Table | None:
        if target in self.tables:
            return self.tables[target]
        self.warnings.append(
            f"{table.name}.{attr_name}: target '{target}' is not deployed — column skipped"
        )
        return None

    def _lookup_column(self, table: Table, attr_name: str, attr: dict) -> list[Column]:
        parental = attr["type"] == "parent"
        target_name = "Term" if attr["type"] == "term" else attr.get("target")
        target = self._target_table(table, attr_name, attr, target_name)
        if target is None:
            return []
        logical, schema = self._column_names(attr_name)
        display = attr.get("label") or title_label(attr_name)
        description = attr.get("description") or ""
        if attr["type"] == "term":
            description = (description + f" Term from the {attr['termSet']} set.").strip()

        on_delete = "cascade" if parental else (attr.get("onDelete") or ("restrict" if attr.get("required") else "setNull"))
        cascade = {
            "cascade": {"Assign": "Cascade", "Delete": "Cascade", "Merge": "NoCascade",
                        "Reparent": "Cascade", "Share": "Cascade", "Unshare": "Cascade"},
            "restrict": {"Assign": "NoCascade", "Delete": "Restrict", "Merge": "NoCascade",
                         "Reparent": "NoCascade", "Share": "NoCascade", "Unshare": "NoCascade"},
            "setNull": {"Assign": "NoCascade", "Delete": "RemoveLink", "Merge": "NoCascade",
                        "Reparent": "NoCascade", "Share": "NoCascade", "Unshare": "NoCascade"},
        }[on_delete]

        relationship_name = truncate(f"{self.prefix}_{table.name.lower()}_{attr_name.lower()}_{target.logical}", 100)
        relationship = {
            "@odata.type": "Microsoft.Dynamics.CRM.OneToManyRelationshipMetadata",
            "SchemaName": relationship_name,
            "ReferencedEntity": target.logical,
            "ReferencingEntity": table.logical,
            "CascadeConfiguration": cascade,
            "AssociatedMenuConfiguration": {
                "Behavior": "UseCollectionName",
                "Group": "Details",
                "Label": label(table.plural),
                "Order": 10000,
                "IsCustomizable": True,
            },
            "IsValidForAdvancedFind": True,
            "Lookup": {
                "@odata.type": "Microsoft.Dynamics.CRM.LookupAttributeMetadata",
                "SchemaName": schema,
                "LogicalName": logical,
                "DisplayName": label(display),
                "Description": label(description),
                "RequiredLevel": required_level(bool(attr.get("required"))),
            },
        }
        return [
            Column(
                entity=table.name, name=attr_name, logical=logical, schema=schema, display=display,
                spec=attr, kind="lookup", relationship=relationship,
                note="parental" if parental else "",
            )
        ]

    def _manytomany_column(self, table: Table, attr_name: str, attr: dict) -> list[Column]:
        target_name = "Term" if attr["type"] == "multiterm" else attr.get("target")
        target = self._target_table(table, attr_name, attr, target_name)
        if target is None:
            return []
        schema = truncate(f"{self.prefix}_{table.name.lower()}_{attr_name.lower()}_{target.logical}", 100)
        intersect = truncate(f"{self.prefix}_{table.name.lower()}_{attr_name.lower()}", 50)
        relationship = {
            "@odata.type": "Microsoft.Dynamics.CRM.ManyToManyRelationshipMetadata",
            "SchemaName": schema,
            "IntersectEntityName": intersect,
            "Entity1LogicalName": table.logical,
            "Entity2LogicalName": target.logical,
            "Entity1AssociatedMenuConfiguration": {
                "Behavior": "UseLabel", "Group": "Details",
                "Label": label(attr.get("label") or title_label(attr_name)), "Order": 10000,
            },
            "Entity2AssociatedMenuConfiguration": {
                "Behavior": "UseLabel", "Group": "Details", "Label": label(table.plural), "Order": 10000,
            },
        }
        return [
            Column(
                entity=table.name, name=attr_name, logical=intersect, schema=schema,
                display=attr.get("label") or title_label(attr_name), spec=attr,
                kind="manytomany", relationship=relationship,
                note=f"N:N {table.logical} ↔ {target.logical}",
            )
        ]

    def _address_columns(self, table: Table, attr_name: str, attr: dict) -> list[Column]:
        """`address` is a composite: five text columns plus a Country lookup."""
        parts = [
            ("Line1", "Line 1", 250), ("Line2", "Line 2", 250), ("City", "City", 100),
            ("StateProvince", "State / province", 100), ("PostalCode", "Postal code", 20),
        ]
        columns: list[Column] = []
        for suffix, display_suffix, length in parts:
            logical, schema = self._column_names(attr_name, suffix)
            display = f"{attr.get('label') or title_label(attr_name)} – {display_suffix}"
            columns.append(
                Column(
                    entity=table.name, name=f"{attr_name}{suffix}", logical=logical, schema=schema,
                    display=display, spec={"type": "string", "length": length, "pii": attr.get("pii")},
                    kind="attribute",
                    payload={
                        "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
                        "SchemaName": schema, "LogicalName": logical, "MaxLength": length,
                        "FormatName": {"Value": "Text"}, "DisplayName": label(display),
                        "Description": label(f"Part of the {attr_name} address."),
                        "RequiredLevel": required_level(False),
                    },
                )
            )
        country = {"type": "lookup", "target": "Country", "label": f"{title_label(attr_name)} – Country"}
        columns.extend(self._lookup_column(table, f"{attr_name}Country", country))
        return columns

    def _polylookup_columns(self, table: Table, attr_name: str, attr: dict) -> list[Column]:
        """`polylookup(*)` is stored as table name + id, exactly as the schema prescribes."""
        targets = attr.get("targets") or ["*"]
        if targets != ["*"]:
            columns: list[Column] = []
            for target in targets:
                spec = {
                    "type": "lookup", "target": target, "required": False,
                    "label": f"{title_label(attr_name)} ({title_label(target)})",
                    "description": f"One of the {attr_name} targets ({', '.join(targets)}).",
                }
                columns.extend(self._lookup_column(table, f"{attr_name}{target}", spec))
            self.warnings.append(
                f"{table.name}.{attr_name}: multi-table lookup deployed as one lookup per target "
                f"({', '.join(targets)})"
            )
            return columns

        columns = []
        for suffix, display, length, description in (
            ("Table", "Regarding table", 100, "Logical name of the table this row refers to."),
            ("Id", "Regarding id", 50, "Primary key of the row this row refers to."),
        ):
            logical, schema = self._column_names(attr_name, suffix)
            columns.append(
                Column(
                    entity=table.name, name=f"{attr_name}{suffix}", logical=logical, schema=schema,
                    display=f"{title_label(attr_name)} – {display}", spec={"type": "string", "length": length},
                    kind="attribute",
                    payload={
                        "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
                        "SchemaName": schema, "LogicalName": logical, "MaxLength": length,
                        "FormatName": {"Value": "Text"},
                        "DisplayName": label(f"{title_label(attr_name)} – {display}"),
                        "Description": label(description), "RequiredLevel": required_level(False),
                    },
                )
            )
        return columns

    # ------------------------------------------------------------------ enums
    def option_set_name(self, enum_name: str) -> str:
        return truncate(f"{self.prefix}_{enum_name.lower()}", 50)

    def enum_value(self, enum_name: str, key: str) -> int | None:
        for value in self.enums.get(enum_name, {}).get("values", []):
            if value["key"] == key:
                return int(value["code"])
        return None

    def option_set_payload(self, enum_name: str) -> dict:
        enum = self.enums[enum_name]
        return {
            "@odata.type": "Microsoft.Dynamics.CRM.OptionSetMetadata",
            "Name": self.option_set_name(enum_name),
            "DisplayName": label(title_label(enum_name)),
            "Description": label(enum.get("description") or ""),
            "OptionSetType": "Picklist",
            "IsGlobal": True,
            "Options": [
                {
                    "Value": int(value["code"]),
                    "Label": label(value.get("label") or title_label(value["key"])),
                    "Description": label(""),
                }
                for value in enum["values"]
                if not value.get("deprecated")
            ],
        }

    # ----------------------------------------------------------------- tables
    def table_payload(self, table: Table) -> dict:
        spec = table.spec
        primary_length = 200
        primary_column = {
            "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
            "SchemaName": truncate(f"{self.prefix}_{pascal(table.primary_logical.split('_', 1)[1])}", 50),
            "LogicalName": table.primary_logical,
            "MaxLength": primary_length,
            "FormatName": {"Value": "Text"},
            "IsPrimaryName": True,
            "DisplayName": label(table.primary_display),
            "Description": label("Primary name column."),
            "RequiredLevel": required_level(True),
        }
        primary_spec = spec["attributes"].get(spec.get("primary") or "name", {})
        if spec.get("primary") == "auto" or primary_spec.get("readOnly"):
            # A generated label: optional in Dataverse so rows can be created before a
            # flow or plug-in fills it in.
            primary_column["RequiredLevel"] = required_level(False)
            primary_column["Description"] = label(
                primary_spec.get("description") or "Generated label — filled by a flow or plug-in."
            )
        return {
            "@odata.type": "Microsoft.Dynamics.CRM.EntityMetadata",
            "SchemaName": table.schema,
            "LogicalName": table.logical,
            "DisplayName": label(table.display),
            "DisplayCollectionName": label(table.plural),
            "Description": label(spec.get("description") or ""),
            "OwnershipType": "OrganizationOwned" if spec.get("ownership") == "organization" else "UserOwned",
            "IsActivity": False,
            "HasActivities": spec.get("volume") != "low" or table.name in ("Account", "Contact", "Lead", "Opportunity"),
            "HasNotes": True,
            "IsAuditEnabled": {"Value": spec.get("sensitivity") in ("confidential", "restricted"),
                               "CanBeChanged": True, "ManagedPropertyLogicalName": "canmodifyauditsettings"},
            "IsDuplicateDetectionEnabled": {"Value": False, "CanBeChanged": True,
                                            "ManagedPropertyLogicalName": "canmodifyduplicatedetectionsettings"},
            "IsValidForQueue": {"Value": False, "CanBeChanged": True,
                                "ManagedPropertyLogicalName": "canmodifyqueuesettings"},
            "Attributes": [primary_column],
        }

    def key_payloads(self, table: Table) -> list[dict]:
        payloads = []
        for key in table.keys:
            logicals = []
            for part in key:
                logical = table.logical_of(part)
                if not logical:
                    logicals = []
                    self.warnings.append(
                        f"{table.name}: alternate key on {key} skipped — '{part}' is not a deployed column"
                    )
                    break
                logicals.append(logical)
            if not logicals:
                continue
            name = truncate(f"{self.prefix}_{table.name.lower()}_{'_'.join(k.lower() for k in key)}_key", 100)
            payloads.append(
                {
                    "@odata.type": "Microsoft.Dynamics.CRM.EntityKeyMetadata",
                    "SchemaName": name,
                    "DisplayName": label(f"{table.display}: {', '.join(title_label(k) for k in key)}"),
                    "KeyAttributes": logicals,
                }
            )
        return payloads

    # ------------------------------------------------------------------ views
    @property
    def custom_tables(self) -> list[Table]:
        return [t for t in self.tables.values() if t.is_custom]

    @property
    def standard_tables(self) -> list[Table]:
        return [t for t in self.tables.values() if not t.is_custom]

    def summary(self) -> str:
        columns = sum(len([c for c in t.columns if c.kind != "skipped"]) for t in self.tables.values())
        return (
            f"{len(self.custom_tables)} new tables, {len(self.standard_tables)} standard tables extended, "
            f"{columns} columns, {len(self.used_enums)} choice sets"
        )


def load_model(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
