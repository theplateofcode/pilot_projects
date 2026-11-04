#!/usr/bin/env python3
"""Generate Dataverse metadata from a resolved model.

Reads the JSON that `schematool.py export` writes and turns it into Dataverse
Web API payloads plus an ordered deployment plan. `dvdeploy.py` applies the
plan; nothing here touches a tenant.

The plan is deliberately ordered so no step depends on a step after it:

    1. optionsets     global choice sets, from the model's enums
    2. tables         every custom table with just its primary name column
    3. columns        all non-relationship columns
    4. relationships  one-to-many (lookups and parents), then many-to-many
    5. keys           alternate keys, once the columns they cover exist

Creating all tables before any lookup removes the need to topologically sort
tables that reference each other (OrgUnit.parentOrgUnit, Employee.manager).

Usage
    python tools/dvgen.py --profile hr-starter --prefix wga
    python tools/dvgen.py --profile hr-starter --prefix wga --layers core,hr
    python tools/dvgen.py --profile accounting-firm --prefix wga --out build/dv
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"

# Dataverse identifier limits.
MAX_ENTITY_SCHEMA = 50
MAX_ATTR_SCHEMA = 50
MAX_RELATIONSHIP_SCHEMA = 100

# Entity-level platform values that mean "do not create this table".
SKIP_ENTITY_MODES = ("native", "none")

# A trait column already provided by Dataverse: the dictionary records it as
# "createdon (native)", "ownerid (native, user or team) + owningbusinessunit".
NATIVE_COLUMN = re.compile(r"\(native\b")

# Schema type -> how we build the Dataverse attribute.
SIMPLE_TYPES = {
    "string", "text", "richtext", "email", "phone", "url", "integer",
    "decimal", "percent", "money", "boolean", "date", "datetime", "duration",
    "autonumber", "file", "image",
}
RELATIONSHIP_TYPES = {"lookup", "parent", "multilookup", "term", "multiterm", "polylookup"}

# The six physical columns an `address` composite expands into.
ADDRESS_PARTS = (
    ("line1", "Line 1", 250),
    ("line2", "Line 2", 250),
    ("city", "City", 100),
    ("stateProvince", "State/Province", 100),
    ("postalCode", "Postal Code", 40),
)

# Classification ranking. A column is given Dataverse column security only when
# its own classification is STRICTER than its table's: a wholly restricted table
# (Compensation, Offer) is protected by security roles instead, which is cheaper
# to run and to maintain than field security on every column.
SENSITIVITY_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
PII_IMPLIES = "confidential"

CASCADE_PARENTAL = {
    "Assign": "Cascade", "Delete": "Cascade", "Merge": "Cascade",
    "Reparent": "Cascade", "Share": "Cascade", "Unshare": "Cascade",
}
CASCADE_RESTRICT = {
    "Assign": "NoCascade", "Delete": "Restrict", "Merge": "NoCascade",
    "Reparent": "NoCascade", "Share": "NoCascade", "Unshare": "NoCascade",
}
CASCADE_REMOVE_LINK = {
    "Assign": "NoCascade", "Delete": "RemoveLink", "Merge": "NoCascade",
    "Reparent": "NoCascade", "Share": "NoCascade", "Unshare": "NoCascade",
}


class GenError(Exception):
    pass


# --- naming ------------------------------------------------------------------
def logical(prefix: str, name: str) -> str:
    """camelCase or PascalCase -> prefix_lowercasename (Dataverse logical name)."""
    return f"{prefix}_{re.sub(r'[^0-9a-zA-Z]', '', name).lower()}"


def label(text: str) -> dict[str, Any]:
    """A Dataverse localized label in English (LCID 1033)."""
    return {
        "@odata.type": "Microsoft.Dynamics.CRM.Label",
        "LocalizedLabels": [
            {
                "@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel",
                "Label": text,
                "LanguageCode": 1033,
            }
        ],
    }


def split_pascal(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name)


# --- the generator -----------------------------------------------------------
class Generator:
    def __init__(self, model: dict[str, Any], prefix: str, layers: list[str] | None = None,
                 solution: str | None = None):
        self.model = model
        self.prefix = prefix.lower()
        self.solution = solution
        self.layers = layers
        self.entities: dict[str, Any] = model.get("entities", {})
        self.enums: dict[str, Any] = model.get("enums", {})
        self.terminology: dict[str, Any] = model.get("terminology", {})
        self.warnings: list[str] = []
        self.manual: list[dict[str, Any]] = []
        self.native_mappings: list[dict[str, Any]] = []
        self.address_lookups: list[dict[str, Any]] = []
        # Entity name -> Dataverse logical name, for every entity we can point at.
        self.table_names: dict[str, str] = {}
        self.created: set[str] = set()
        self._map_tables()

    # -- table name resolution
    def _map_tables(self) -> None:
        for name, ent in self.entities.items():
            mode = (ent.get("platforms", {}) or {}).get("dataverse", "custom")
            if isinstance(mode, str) and mode.startswith("standard:"):
                self.table_names[name] = mode.split(":", 1)[1].strip().lower()
            elif mode == "native":
                # A Dataverse-provided table whose name matches the concept.
                self.table_names[name] = self._native_name(name)
            elif mode == "none":
                continue
            else:
                self.table_names[name] = logical(self.prefix, name)
                if self._in_scope(name):
                    self.created.add(name)

    @staticmethod
    def _native_name(name: str) -> str:
        known = {"ExchangeRate": "exchangerate", "AuditEvent": "audit",
                 "AppSetting": "organization"}
        return known.get(name, name.lower())

    def _in_scope(self, entity_name: str) -> bool:
        if self.layers is None:
            return True
        return self.entities[entity_name].get("layer") in self.layers

    def _entity_label(self, name: str, ent: dict[str, Any]) -> tuple[str, str]:
        term = self.terminology.get(name) or {}
        singular = term.get("singular") or ent.get("label") or split_pascal(name)
        plural = term.get("plural") or ent.get("plural") or f"{singular}s"
        return singular, plural

    def _column_label(self, entity: str, attr: str, spec: dict[str, Any]) -> str:
        term = self.terminology.get(f"{entity}.{attr}") or {}
        if isinstance(term, dict) and term.get("label"):
            return term["label"]
        return spec.get("label") or split_pascal(attr[0].upper() + attr[1:])

    # -- classification
    def _needs_column_security(self, entity: str, spec: dict[str, Any]) -> bool:
        """True when this column is classified more strictly than its table."""
        table_rank = SENSITIVITY_RANK.get(
            self.entities[entity].get("sensitivity", "internal"), 1)
        column = spec.get("sensitivity")
        column_rank = SENSITIVITY_RANK.get(column, 0) if column else 0
        if spec.get("pii"):
            column_rank = max(column_rank, SENSITIVITY_RANK[PII_IMPLIES])
        return column_rank > table_rank

    # -- money
    def _native_currency_column(self, entity: str) -> str | None:
        """The currency lookup Dataverse provides itself, if this table has money.

        Adding a Money column makes Dataverse create `transactioncurrencyid` and
        a `_base` shadow column. The schema's own `currency` lookup on such a
        table is that same relationship, so creating a second lookup to
        transactioncurrency would leave the money column reading the wrong one.
        """
        ent = self.entities[entity]
        money_fields = [a for a, s in ent["attributes"].items() if s["type"] == "money"]
        if not money_fields:
            return None
        named = {s.get("currencyField") for s in ent["attributes"].values()
                 if s["type"] == "money" and s.get("currencyField")}
        if named:
            return sorted(named)[0]
        return "currency" if "currency" in ent["attributes"] else None

    # -- skipping rules
    @staticmethod
    def _is_native_column(spec: dict[str, Any]) -> bool:
        physical = (spec.get("physical") or {}).get("dataverse") or spec.get("dataverse") or ""
        return bool(NATIVE_COLUMN.search(str(physical)))

    # -- option sets
    def option_sets(self) -> list[dict[str, Any]]:
        """Global choice sets for every enum the in-scope tables actually use."""
        used: set[str] = set()
        for name in sorted(self.created):
            for spec in self.entities[name]["attributes"].values():
                if spec["type"] in ("choice", "multichoice") and not self._is_native_column(spec):
                    target = spec.get("enum") or spec.get("target")
                    if target:
                        used.add(target)
                elif spec.get("trait") == "classified" and spec["type"] == "choice":
                    used.add(spec.get("enum") or "SensitivityLevel")

        payloads = []
        for enum_name in sorted(used):
            enum = self.enums.get(enum_name)
            if not enum:
                self.warnings.append(f"enum {enum_name} is referenced but not defined")
                continue
            options = []
            for value in enum.get("values", []):
                if value.get("deprecated"):
                    continue
                options.append(
                    {
                        "Value": value["code"],
                        "Label": label(value.get("label") or split_pascal(value["key"]).title()),
                        "Description": label(value.get("description") or ""),
                    }
                )
            payloads.append(
                {
                    "_name": enum_name,
                    "_logicalName": logical(self.prefix, enum_name),
                    "payload": {
                        "@odata.type": "Microsoft.Dynamics.CRM.OptionSetMetadata",
                        "Name": logical(self.prefix, enum_name),
                        "DisplayName": label(split_pascal(enum_name)),
                        "Description": label(enum.get("description", "")),
                        "OptionSetType": "Picklist",
                        "IsGlobal": True,
                        "IsCustomOptionSet": True,
                        "Options": options,
                    },
                }
            )
        return payloads

    # -- tables
    def tables(self) -> list[dict[str, Any]]:
        out = []
        for name in sorted(self.created):
            ent = self.entities[name]
            schema_name = logical(self.prefix, name)
            if len(schema_name) > MAX_ENTITY_SCHEMA:
                self.warnings.append(
                    f"table {name}: logical name '{schema_name}' is "
                    f"{len(schema_name)} characters (Dataverse allows {MAX_ENTITY_SCHEMA})"
                )
            singular, plural = self._entity_label(name, ent)
            primary = self._primary_attribute(name, ent)
            out.append(
                {
                    "_name": name,
                    "_logicalName": schema_name,
                    "payload": {
                        "@odata.type": "Microsoft.Dynamics.CRM.EntityMetadata",
                        "SchemaName": schema_name,
                        "LogicalName": schema_name,
                        "DisplayName": label(singular),
                        "DisplayCollectionName": label(plural),
                        "Description": label(ent.get("description", "")),
                        "OwnershipType": (
                            "UserOwned" if ent.get("ownership", "user") == "user" else "OrganizationOwned"
                        ),
                        "IsActivity": False,
                        "HasActivities": False,
                        "HasNotes": ent.get("volume") != "high",
                        "IsAuditEnabled": {"Value": ent.get("sensitivity") in ("confidential", "restricted")},
                        "IsValidForQueue": {"Value": False},
                        "Attributes": [primary],
                    },
                }
            )
        return out

    def _primary_attribute(self, name: str, ent: dict[str, Any]) -> dict[str, Any]:
        """Dataverse requires exactly one primary string column, created with the table."""
        primary = ent.get("primary") or "name"
        if primary == "auto":
            attr_name, max_len, display = "name", 400, "Name"
            description = "Read-only label composed by a flow or plug-in."
        else:
            spec = ent["attributes"].get(primary, {})
            if spec.get("type") not in ("string", "autonumber", None):
                self.warnings.append(
                    f"{name}: primary column '{primary}' is {spec.get('type')}; "
                    "Dataverse primary columns must be text — created as text"
                )
            attr_name = primary
            max_len = int(spec.get("length") or 200)
            display = self._column_label(name, primary, spec)
            description = spec.get("description", "")

        payload = {
            "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
            "SchemaName": logical(self.prefix, attr_name),
            "LogicalName": logical(self.prefix, attr_name),
            "DisplayName": label(display),
            "Description": label(description),
            "RequiredLevel": {"Value": "ApplicationRequired"},
            "MaxLength": min(max(max_len, 1), 4000),
            "FormatName": {"Value": "Text"},
            "IsPrimaryName": True,
        }
        if primary != "auto":
            spec = ent["attributes"].get(primary, {})
            if spec.get("type") == "autonumber" and spec.get("format"):
                payload["AutoNumberFormat"] = spec["format"]
        return payload

    # -- columns
    def columns(self) -> list[dict[str, Any]]:
        out = []
        for name in sorted(self.created):
            ent = self.entities[name]
            primary = ent.get("primary") or "name"
            attrs = []
            for attr_name, spec in ent["attributes"].items():
                if attr_name == primary or self._is_native_column(spec):
                    continue
                if spec["type"] in RELATIONSHIP_TYPES:
                    continue            # created as relationships, later in the plan
                if spec["type"] == "guid":
                    continue            # the native primary key
                if spec["type"] == "address":
                    attrs.extend(self._address_columns(name, attr_name, spec))
                    continue
                payload = self._column(name, attr_name, spec)
                if payload:
                    attrs.append(payload)
            if attrs:
                out.append({"_name": name, "_logicalName": logical(self.prefix, name), "attributes": attrs})
        return out

    def _address_columns(self, entity: str, attr: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
        base = self._column_label(entity, attr, spec)
        out = []
        for part, part_label, length in ADDRESS_PARTS:
            out.append(
                self._string_payload(
                    f"{attr}{part[0].upper()}{part[1:]}",
                    f"{base} – {part_label}",
                    spec.get("description", ""),
                    length,
                    required=False,
                )
            )
        # The country part is a lookup, built in the relationship pass.
        self.address_lookups.append(
            {"entity": entity, "column": f"{attr}Country", "label": f"{base} – Country"}
        )
        return out

    def _string_payload(self, attr_name: str, display: str, description: str,
                        length: int, required: bool, fmt: str = "Text") -> dict[str, Any]:
        return {
            "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
            "SchemaName": logical(self.prefix, attr_name),
            "LogicalName": logical(self.prefix, attr_name),
            "DisplayName": label(display),
            "Description": label(description),
            "RequiredLevel": {"Value": "ApplicationRequired" if required else "None"},
            "MaxLength": min(max(int(length), 1), 4000),
            "FormatName": {"Value": fmt},
        }

    def _column(self, entity: str, attr: str, spec: dict[str, Any]) -> dict[str, Any] | None:
        kind = spec["type"]
        schema_name = logical(self.prefix, attr)
        if len(schema_name) > MAX_ATTR_SCHEMA:
            self.warnings.append(
                f"{entity}.{attr}: column name '{schema_name}' is {len(schema_name)} "
                f"characters (Dataverse allows {MAX_ATTR_SCHEMA})"
            )
        display = self._column_label(entity, attr, spec)
        description = spec.get("description", "")
        required = bool(spec.get("required"))
        if spec.get("readOnly") or spec.get("calculated"):
            required = False            # a column the server fills cannot be required on save

        common = {
            "SchemaName": schema_name,
            "LogicalName": schema_name,
            "DisplayName": label(display),
            "Description": label(description),
            "RequiredLevel": {"Value": "ApplicationRequired" if required else "None"},
        }
        if self._needs_column_security(entity, spec):
            common["IsSecured"] = True          # needs a column security profile

        if kind in ("string", "email", "phone", "url"):
            fmt = {"string": "Text", "email": "Email", "phone": "Phone", "url": "Url"}[kind]
            payload = {**common, "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
                       "MaxLength": min(max(int(spec.get("length") or 100), 1), 4000),
                       "FormatName": {"Value": fmt}}
            return payload

        if kind in ("text", "richtext"):
            return {**common, "@odata.type": "Microsoft.Dynamics.CRM.MemoAttributeMetadata",
                    "MaxLength": 100000,
                    "FormatName": {"Value": "RichText" if kind == "richtext" else "TextArea"}}

        if kind == "integer":
            return {**common, "@odata.type": "Microsoft.Dynamics.CRM.IntegerAttributeMetadata",
                    "MinValue": int(spec.get("min", -2147483648)),
                    "MaxValue": int(spec.get("max", 2147483647)),
                    "Format": "None"}

        if kind == "duration":
            return {**common, "@odata.type": "Microsoft.Dynamics.CRM.IntegerAttributeMetadata",
                    "MinValue": 0, "MaxValue": 2147483647, "Format": "Duration"}

        if kind == "decimal":
            precision = int(spec.get("scale", spec.get("precision_scale", 2)) or 2)
            return {**common, "@odata.type": "Microsoft.Dynamics.CRM.DecimalAttributeMetadata",
                    "Precision": min(max(precision, 0), 10),
                    "MinValue": float(spec.get("min", -100000000000)),
                    "MaxValue": float(spec.get("max", 100000000000))}

        if kind == "percent":
            return {**common, "@odata.type": "Microsoft.Dynamics.CRM.DecimalAttributeMetadata",
                    "Precision": 2, "MinValue": 0, "MaxValue": 100}

        if kind == "money":
            return {**common, "@odata.type": "Microsoft.Dynamics.CRM.MoneyAttributeMetadata",
                    "Precision": 2, "PrecisionSource": 2,
                    "MinValue": -922337203685477.0, "MaxValue": 922337203685477.0}

        if kind == "boolean":
            default = bool(spec.get("default", False))
            return {**common, "@odata.type": "Microsoft.Dynamics.CRM.BooleanAttributeMetadata",
                    "DefaultValue": default,
                    "OptionSet": {
                        "@odata.type": "Microsoft.Dynamics.CRM.BooleanOptionSetMetadata",
                        "TrueOption": {"Value": 1, "Label": label("Yes")},
                        "FalseOption": {"Value": 0, "Label": label("No")},
                    }}

        if kind in ("date", "datetime"):
            return {**common, "@odata.type": "Microsoft.Dynamics.CRM.DateTimeAttributeMetadata",
                    "Format": "DateOnly" if kind == "date" else "DateAndTime",
                    "DateTimeBehavior": {"Value": "DateOnly" if kind == "date" else "UserLocal"}}

        if kind in ("choice", "multichoice"):
            enum_name = spec.get("enum") or spec.get("target")
            if not enum_name:
                self.warnings.append(f"{entity}.{attr}: choice column with no enum")
                return None
            odata = ("Microsoft.Dynamics.CRM.MultiSelectPicklistAttributeMetadata"
                     if kind == "multichoice" else
                     "Microsoft.Dynamics.CRM.PicklistAttributeMetadata")
            payload = {**common, "@odata.type": odata,
                       "GlobalOptionSet@odata.bind":
                           f"/GlobalOptionSetDefinitions(Name='{logical(self.prefix, enum_name)}')"}
            default_key = spec.get("default")
            if default_key is not None:
                code = self._enum_code(enum_name, default_key)
                if code is not None:
                    payload["DefaultFormValue"] = code
            return payload

        if kind == "autonumber":
            payload = self._string_payload(attr, display, description,
                                           int(spec.get("length") or 100), required)
            if spec.get("format"):
                payload["AutoNumberFormat"] = spec["format"]
            return payload

        if kind == "file":
            return {**common, "@odata.type": "Microsoft.Dynamics.CRM.FileAttributeMetadata",
                    "MaxSizeInKB": 32768}

        if kind == "image":
            return {**common, "@odata.type": "Microsoft.Dynamics.CRM.ImageAttributeMetadata",
                    "MaxSizeInKB": 10240, "IsPrimaryImage": True}

        self.warnings.append(f"{entity}.{attr}: no Dataverse mapping for type '{kind}'")
        return None

    def _enum_code(self, enum_name: str, key: Any) -> int | None:
        enum = self.enums.get(enum_name) or {}
        for value in enum.get("values", []):
            if value.get("key") == key:
                return value.get("code")
        return None

    # -- alternate keys
    def keys(self) -> list[dict[str, Any]]:
        """Alternate keys from the entity's declared keys and its unique columns.

        Dataverse builds a supporting index per key asynchronously, so these are
        applied after the columns they cover exist.
        """
        out = []
        for name in sorted(self.created):
            ent = self.entities[name]
            table = logical(self.prefix, name)
            key_sets: list[list[str]] = [list(k) for k in (ent.get("keys") or [])]
            for attr, spec in ent["attributes"].items():
                if spec.get("unique") and [attr] not in key_sets:
                    key_sets.append([attr])

            payloads = []
            for columns in key_sets:
                missing = [c for c in columns if c not in ent["attributes"]]
                if missing:
                    self.warnings.append(
                        f"{name}: key references unknown column(s) {', '.join(missing)}")
                    continue
                unsupported = [c for c in columns
                               if ent["attributes"][c]["type"] in ("text", "richtext", "file", "image")]
                if unsupported:
                    self.warnings.append(
                        f"{name}: alternate key on {', '.join(unsupported)} is not supported "
                        "by Dataverse — skipped")
                    continue
                schema_name = f"{self.prefix}_{table.split('_', 1)[1]}_{'_'.join(c.lower() for c in columns)}_key"
                payloads.append(
                    {
                        "@odata.type": "Microsoft.Dynamics.CRM.EntityKeyMetadata",
                        "SchemaName": schema_name[:MAX_ATTR_SCHEMA + 20],
                        "DisplayName": label(" + ".join(split_pascal(c).title() for c in columns)),
                        "KeyAttributes": [logical(self.prefix, c) for c in columns],
                    }
                )
            if payloads:
                out.append({"_name": name, "_logicalName": table, "keys": payloads})
        return out

    # -- relationships
    def relationships(self) -> dict[str, list[dict[str, Any]]]:
        one_to_many: list[dict[str, Any]] = []
        many_to_many: list[dict[str, Any]] = []

        for name in sorted(self.created):
            ent = self.entities[name]
            child = logical(self.prefix, name)
            native_currency = self._native_currency_column(name)
            for attr, spec in ent["attributes"].items():
                kind = spec["type"]
                if kind not in RELATIONSHIP_TYPES or self._is_native_column(spec):
                    continue
                if attr == native_currency and spec.get("target") == "Currency":
                    # Provided by the Money column as transactioncurrencyid.
                    self.native_mappings.append(
                        {"entity": name, "column": attr, "mapsTo": "transactioncurrencyid"})
                    continue

                if kind in ("term", "multiterm"):
                    target_entity, target = "Term", self.table_names.get("Term")
                elif kind == "polylookup":
                    self._record_polylookup(name, attr, spec)
                    continue
                else:
                    target_entity = spec.get("target")
                    target = self.table_names.get(target_entity) if target_entity else None

                if not target:
                    self.warnings.append(
                        f"{name}.{attr}: target '{target_entity}' is not a Dataverse table — skipped"
                    )
                    continue

                if kind in ("multilookup", "multiterm"):
                    many_to_many.append(self._many_to_many(name, child, attr, target_entity, target))
                else:
                    one_to_many.append(
                        self._one_to_many(name, child, attr, spec, target_entity, target)
                    )

        country = self.table_names.get("Country")
        for item in self.address_lookups:
            if not country:
                self.warnings.append(
                    f"{item['entity']}.{item['column']}: Country is not a Dataverse table — skipped")
                continue
            one_to_many.append(
                self._one_to_many(
                    item["entity"],
                    logical(self.prefix, item["entity"]),
                    item["column"],
                    {"type": "lookup", "target": "Country", "required": False,
                     "label": item["label"],
                     "description": "Country part of the composite address."},
                    "Country",
                    country,
                )
            )

        return {"oneToMany": one_to_many, "manyToMany": many_to_many}

    def _one_to_many(self, entity: str, child: str, attr: str, spec: dict[str, Any],
                     target_entity: str, target: str) -> dict[str, Any]:
        schema_name = f"{self.prefix}_{child.split('_', 1)[1]}_{attr.lower()}_{target}"
        if len(schema_name) > MAX_RELATIONSHIP_SCHEMA:
            schema_name = schema_name[:MAX_RELATIONSHIP_SCHEMA]
        parental = spec["type"] == "parent"
        on_delete = spec.get("onDelete") or ("cascade" if parental else
                                             ("restrict" if spec.get("required") else "setNull"))
        cascade = {"cascade": CASCADE_PARENTAL, "restrict": CASCADE_RESTRICT,
                   "setNull": CASCADE_REMOVE_LINK}[on_delete]
        lookup_schema = logical(self.prefix, attr)
        return {
            "_entity": entity,
            "_column": attr,
            "_target": target_entity,
            "payload": {
                "@odata.type": "Microsoft.Dynamics.CRM.OneToManyRelationshipMetadata",
                "SchemaName": schema_name,
                "ReferencedEntity": target,
                "ReferencingEntity": child,
                "RelationshipType": "OneToManyRelationship",
                "IsHierarchical": target == child and attr.lower().startswith("parent"),
                "CascadeConfiguration": cascade,
                "AssociatedMenuConfiguration": {
                    "Behavior": "UseCollectionName",
                    "Group": "Details",
                    "Order": 10000,
                    "IsCustomizable": {"Value": True},
                },
                "Lookup": {
                    "@odata.type": "Microsoft.Dynamics.CRM.LookupAttributeMetadata",
                    "SchemaName": lookup_schema,
                    "LogicalName": lookup_schema,
                    "DisplayName": label(self._column_label(entity, attr, spec)),
                    "Description": label(spec.get("description", "")),
                    "RequiredLevel": {
                        "Value": "ApplicationRequired" if (spec.get("required") or parental) else "None"
                    },
                    **({"IsSecured": True}
                       if self._needs_column_security(entity, spec) else {}),
                },
            },
        }

    def _many_to_many(self, entity: str, child: str, attr: str,
                      target_entity: str, target: str) -> dict[str, Any]:
        schema_name = f"{self.prefix}_{child.split('_', 1)[1]}_{attr.lower()}"[:MAX_RELATIONSHIP_SCHEMA]
        return {
            "_entity": entity,
            "_column": attr,
            "_target": target_entity,
            "payload": {
                "@odata.type": "Microsoft.Dynamics.CRM.ManyToManyRelationshipMetadata",
                "SchemaName": schema_name,
                "IntersectEntityName": schema_name,
                "Entity1LogicalName": child,
                "Entity2LogicalName": target,
                "Entity1AssociatedMenuConfiguration": {
                    "Behavior": "UseCollectionName", "Group": "Details", "Order": 10000},
                "Entity2AssociatedMenuConfiguration": {
                    "Behavior": "UseCollectionName", "Group": "Details", "Order": 10000},
            },
        }

    def _record_polylookup(self, entity: str, attr: str, spec: dict[str, Any]) -> None:
        """Multi-table lookups cannot be created through the metadata API."""
        targets = spec.get("targets") or spec.get("target") or "*"
        if isinstance(targets, str):
            targets = [t.strip() for t in targets.split("|")] if targets != "*" else ["<any table>"]
        self.manual.append(
            {
                "step": "multi-table lookup",
                "entity": entity,
                "column": attr,
                "detail": "create a polymorphic lookup in the maker portal, or one lookup per "
                          f"target: {', '.join(targets)}",
            }
        )

    # -- the plan
    def build(self) -> dict[str, Any]:
        option_sets = self.option_sets()
        tables = self.tables()
        columns = self.columns()
        rels = self.relationships()          # fills address_lookups -> must follow columns()
        alt_keys = self.keys()
        column_count = sum(len(t["attributes"]) for t in columns)
        return {
            "profile": self.model.get("profile"),
            "prefix": self.prefix,
            "solution": self.solution,
            "layers": self.layers or sorted({e.get("layer") for e in self.entities.values()}),
            "counts": {
                "optionSets": len(option_sets),
                "tables": len(tables),
                "columns": column_count + len(tables),     # + one primary column per table
                "oneToMany": len(rels["oneToMany"]),
                "manyToMany": len(rels["manyToMany"]),
                "alternateKeys": sum(len(k["keys"]) for k in alt_keys),
                "manualSteps": len(self.manual),
                "nativeMappings": len(self.native_mappings),
            },
            "optionSets": option_sets,
            "tables": tables,
            "columns": columns,
            "relationships": rels,
            "alternateKeys": alt_keys,
            "manualSteps": self.manual,
            "nativeMappings": self.native_mappings,
            "warnings": self.warnings,
        }


# --- output ------------------------------------------------------------------
def write_plan(plan: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("optionsets", "tables", "columns", "relationships", "keys"):
        (out_dir / sub).mkdir(exist_ok=True)

    for item in plan["optionSets"]:
        (out_dir / "optionsets" / f"{item['_name']}.json").write_text(
            json.dumps(item["payload"], indent=2), encoding="utf-8")
    for item in plan["tables"]:
        (out_dir / "tables" / f"{item['_name']}.json").write_text(
            json.dumps(item["payload"], indent=2), encoding="utf-8")
    for item in plan["columns"]:
        (out_dir / "columns" / f"{item['_name']}.json").write_text(
            json.dumps(item["attributes"], indent=2), encoding="utf-8")
    for item in plan["alternateKeys"]:
        (out_dir / "keys" / f"{item['_name']}.json").write_text(
            json.dumps(item["keys"], indent=2), encoding="utf-8")
    (out_dir / "relationships" / "one-to-many.json").write_text(
        json.dumps([r["payload"] for r in plan["relationships"]["oneToMany"]], indent=2), encoding="utf-8")
    (out_dir / "relationships" / "many-to-many.json").write_text(
        json.dumps([r["payload"] for r in plan["relationships"]["manyToMany"]], indent=2), encoding="utf-8")
    (out_dir / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Dataverse metadata from a resolved model.")
    parser.add_argument("--profile", required=True, help="profile name (its export must exist in build/)")
    parser.add_argument("--prefix", required=True, help="publisher customisation prefix, e.g. wga")
    parser.add_argument("--layers", help="comma-separated layers to build (default: all in the profile)")
    parser.add_argument("--solution", help="solution unique name to add the components to")
    parser.add_argument("--model", help="path to the resolved model JSON (default: build/<profile>.model.json)")
    parser.add_argument("--out", help="output directory (default: build/dataverse/<profile>)")
    args = parser.parse_args(argv)

    if not re.fullmatch(r"[a-z][a-z0-9]{1,7}", args.prefix.lower()):
        print(f"error: prefix '{args.prefix}' must be 2-8 characters, lowercase letters and digits, "
              "starting with a letter", file=sys.stderr)
        return 2

    model_path = Path(args.model) if args.model else BUILD_DIR / f"{args.profile}.model.json"
    if not model_path.exists():
        print(f"error: {model_path} not found — run: python tools/schematool.py export "
              f"--profile {args.profile}", file=sys.stderr)
        return 2

    model = json.loads(model_path.read_text(encoding="utf-8"))
    layers = [l.strip() for l in args.layers.split(",")] if args.layers else None
    if layers:
        known = set(model.get("layers", {}))
        unknown = [l for l in layers if l not in known]
        if unknown:
            print(f"error: layer(s) {', '.join(unknown)} are not in profile '{args.profile}' "
                  f"(it has: {', '.join(sorted(known))})", file=sys.stderr)
            return 2

    generator = Generator(model, args.prefix, layers, args.solution)
    plan = generator.build()
    out_dir = Path(args.out) if args.out else BUILD_DIR / "dataverse" / args.profile
    write_plan(plan, out_dir)

    counts = plan["counts"]
    print(f"Wrote {out_dir}")
    print(f"  {counts['optionSets']} global choice sets")
    print(f"  {counts['tables']} tables, {counts['columns']} columns")
    print(f"  {counts['oneToMany']} one-to-many, {counts['manyToMany']} many-to-many relationships")
    print(f"  {counts['alternateKeys']} alternate keys")
    if plan["nativeMappings"]:
        print(f"  {counts['nativeMappings']} column(s) mapped to native Dataverse columns")
    if plan["manualSteps"]:
        print(f"  {counts['manualSteps']} manual step(s) — see plan.json manualSteps")
    for warning in plan["warnings"]:
        print(f"  warning: {warning}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
