#!/usr/bin/env python3
"""Generate SQL migrations from a resolved model.

Targets Azure SQL / SQL Server. Reads the JSON that `schematool.py export`
writes and emits ordered migration files plus a manifest.

The file order removes a dependency problem the same way the Dataverse plan
does. Every table is created before any foreign key, so pairs that reference
each other need no topological sort — `Employee.orgUnit` points at `OrgUnit`
while `OrgUnit.head` points back at `Employee`, which inline constraints cannot
express in either order.

    001_schemas.sql        one schema per layer, so module independence is visible
    002_reference.sql      a lookup table per choice set, with its values
    003_tables.sql         every table: columns, primary key, defaults, checks
    004_foreign_keys.sql   every foreign key, as ALTER statements
    005_indexes.sql        unique constraints from keys, indexes from hints,
                           and filtered indexes that enforce effective dating
    006_seed.sql           the schema's own reference data
    007_views.sql          current-row views over the effective-dated tables

What SQL gives that the other platforms do not, and this generator uses:

  · a choice is a foreign key to a lookup table, so an invalid value cannot be
    stored at all rather than being checked by the app
  · effective dating is enforced by a filtered unique index, so "exactly one
    current row per person" is a constraint and not a convention
  · money carries a currency foreign key, checked

Usage
    python tools/sqlgen.py --profile hr-starter
    python tools/sqlgen.py --profile accounting-firm --flat
    python tools/sqlgen.py --profile hr-starter --out build/sql/hr
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"

# Entity-level platform values that mean "no table here".
SKIP_MODES = ("none",)

# Reserved words we must bracket. SQL Server allows [x] for anything, so we
# bracket every identifier rather than keeping a word list that goes stale.
MAX_IDENTIFIER = 128

# Schema type -> SQL Server column type. The dictionary's physical.sql is the
# source of truth where it gives a concrete type; this is the fallback and the
# place where precision is derived.
SIMPLE_TYPES = {
    "string": lambda s: f"nvarchar({min(int(s.get('length') or 100), 4000)})",
    "text": lambda s: "nvarchar(max)",
    "richtext": lambda s: "nvarchar(max)",
    "email": lambda s: "nvarchar(320)",
    "phone": lambda s: "nvarchar(50)",
    "url": lambda s: "nvarchar(2000)",
    "integer": lambda s: "int",
    "duration": lambda s: "int",
    "percent": lambda s: "decimal(5,2)",
    "money": lambda s: "decimal(19,4)",
    "boolean": lambda s: "bit",
    "date": lambda s: "date",
    "datetime": lambda s: "datetime2(3)",
    "guid": lambda s: "uniqueidentifier",
    "file": lambda s: "nvarchar(2000)",
    "image": lambda s: "nvarchar(2000)",
    "autonumber": lambda s: f"nvarchar({min(int(s.get('length') or 30), 400)})",
}

ADDRESS_PARTS = (
    ("Line1", "nvarchar(250)"),
    ("Line2", "nvarchar(250)"),
    ("City", "nvarchar(100)"),
    ("StateProvince", "nvarchar(100)"),
    ("PostalCode", "nvarchar(40)"),
)

# Columns the base trait provides, which SQL stores itself rather than
# inheriting from a platform.
ROWVERSION_COLUMN = "rowVersion"

ON_DELETE_SQL = {
    "cascade": "ON DELETE CASCADE",
    "setNull": "ON DELETE SET NULL",
    "restrict": "ON DELETE NO ACTION",
}


class SqlError(Exception):
    pass


def quote(name: str) -> str:
    """Bracket-quote an identifier, rejecting the one character that can escape it."""
    if "]" in name:
        raise SqlError(f"identifier {name!r} contains a closing bracket")
    return f"[{name}]"


def literal(value: Any) -> str:
    """A SQL literal. Text is N-prefixed so Unicode survives."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return f"'{value.isoformat()}'"
    return "N'" + str(value).replace("'", "''") + "'"


class Generator:
    def __init__(self, model: dict[str, Any], flat: bool = False,
                 reference_schema: str = "ref"):
        self.model = model
        self.entities: dict[str, Any] = model.get("entities", {})
        self.enums: dict[str, Any] = model.get("enums", {})
        self.term_sets: dict[str, Any] = model.get("termSets", {})
        self.seeds: dict[str, Any] = model.get("seed", {})
        self.flat = flat
        self.reference_schema = "dbo" if flat else reference_schema
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.tables = [n for n in sorted(self.entities)
                       if (self.entities[n].get("platforms", {}) or {})
                       .get("sql", "table") not in SKIP_MODES]
        self.junctions: list[dict[str, Any]] = []
        self.foreign_keys: list[dict[str, Any]] = []
        self._check_identifiers()

    # -- naming
    def schema_of(self, entity: str) -> str:
        if self.flat:
            return "dbo"
        layer = self.entities[entity].get("layer", "core")
        return re.sub(r"[^0-9a-zA-Z]", "_", layer)

    def ref(self, entity: str) -> str:
        return f"{quote(self.schema_of(entity))}.{quote(entity)}"

    def ref_table(self, name: str) -> str:
        return f"{quote(self.reference_schema)}.{quote(name)}"

    def schemas(self) -> list[str]:
        if self.flat:
            return ["dbo"]
        return sorted({self.schema_of(e) for e in self.tables} | {self.reference_schema})

    def _check_identifiers(self) -> None:
        for entity in self.tables:
            if len(entity) > MAX_IDENTIFIER:
                self.errors.append(f"table name {entity} exceeds {MAX_IDENTIFIER} characters")
            for attr in self.entities[entity]["attributes"]:
                if len(attr) > MAX_IDENTIFIER:
                    self.errors.append(f"{entity}.{attr} exceeds {MAX_IDENTIFIER} characters")

    # -- type mapping
    def column_type(self, entity: str, attr: str, spec: dict[str, Any]) -> str | None:
        kind = spec["type"]
        if kind == "decimal":
            precision = int(spec.get("precision") or 18)
            scale = int(spec.get("scale") or 2)
            if scale > precision:
                self.errors.append(
                    f"{entity}.{attr}: scale {scale} exceeds precision {precision}")
                return None
            return f"decimal({precision},{scale})"
        if kind in ("choice",):
            # Narrow where the codes allow it; the FK keeps it honest either way.
            enum = self.enums.get(spec.get("enum") or "") or {}
            codes = [v.get("code", 0) for v in enum.get("values", [])]
            return "tinyint" if codes and max(codes) <= 255 and min(codes) >= 0 else "int"
        if kind in ("lookup", "parent", "term"):
            return "uniqueidentifier"
        if kind in SIMPLE_TYPES:
            return SIMPLE_TYPES[kind](spec)
        return None

    # -- files
    def sql_schemas(self) -> str:
        lines = [self._header("Schemas", "One schema per layer, so a module's tables "
                                         "are visibly its own.")]
        for schema in self.schemas():
            lines.append(
                f"IF SCHEMA_ID('{schema}') IS NULL EXEC('CREATE SCHEMA {quote(schema)}');")
        return "\n".join(lines) + "\n"

    def sql_reference(self) -> str:
        """A lookup table per choice set, so an invalid value cannot be stored."""
        lines = [self._header(
            "Reference tables",
            "One table per choice set. A choice column is a foreign key to these, so "
            "an invalid value is rejected by the database rather than by the app. "
            "Codes are permanent; a deprecated value stays and is flagged.")]
        used = self._used_enums()
        for name in sorted(used):
            enum = self.enums.get(name)
            if enum is None:
                self.errors.append(f"enum {name} is referenced but not defined")
                continue
            table = self.ref_table(name)
            lines.append(f"\nIF OBJECT_ID('{self.reference_schema}.{name}') IS NULL")
            lines.append(f"CREATE TABLE {table} (")
            lines.append("    [code] int NOT NULL,")
            lines.append("    [key] nvarchar(100) NOT NULL,")
            lines.append("    [label] nvarchar(200) NOT NULL,")
            lines.append("    [isDeprecated] bit NOT NULL CONSTRAINT "
                         f"{quote(f'DF_{name}_isDeprecated')} DEFAULT (0),")
            lines.append(f"    CONSTRAINT {quote(f'PK_{name}')} PRIMARY KEY ([code]),")
            lines.append(f"    CONSTRAINT {quote(f'UQ_{name}_key')} UNIQUE ([key])")
            lines.append(");")
            for value in enum.get("values", []):
                lines.append(
                    f"MERGE {table} AS t USING (SELECT {literal(value['code'])} AS [code]) AS s "
                    "ON t.[code] = s.[code] WHEN MATCHED THEN UPDATE SET "
                    f"[key] = {literal(value['key'])}, "
                    f"[label] = {literal(value.get('label') or value['key'])}, "
                    f"[isDeprecated] = {literal(bool(value.get('deprecated')))} "
                    "WHEN NOT MATCHED THEN INSERT ([code], [key], [label], [isDeprecated]) "
                    f"VALUES ({literal(value['code'])}, {literal(value['key'])}, "
                    f"{literal(value.get('label') or value['key'])}, "
                    f"{literal(bool(value.get('deprecated')))});")
        return "\n".join(lines) + "\n"

    def _used_enums(self) -> set[str]:
        used = set()
        for entity in self.tables:
            for spec in self.entities[entity]["attributes"].values():
                if spec["type"] in ("choice", "multichoice"):
                    name = spec.get("enum") or spec.get("target")
                    if name:
                        used.add(name)
        return used

    def sql_tables(self) -> str:
        lines = [self._header(
            "Tables",
            "Columns, primary keys, defaults and check constraints. No foreign keys "
            "yet: tables that reference each other (Employee.orgUnit and OrgUnit.head) "
            "cannot both be created with inline constraints, in either order.")]
        for entity in self.tables:
            lines.append("")
            lines.extend(self._table(entity))
        for junction in self.junctions:
            lines.append("")
            lines.extend(self._junction_table(junction))
        return "\n".join(lines) + "\n"

    def _table(self, entity: str) -> list[str]:
        spec = self.entities[entity]
        columns: list[str] = []
        constraints: list[str] = []
        primary = None

        for attr, column in spec["attributes"].items():
            kind = column["type"]

            if kind == "guid" and column.get("trait") == "base" and attr == "id":
                primary = attr
                columns.append(
                    f"    {quote(attr)} uniqueidentifier NOT NULL CONSTRAINT "
                    f"{quote(f'DF_{entity}_{attr}')} DEFAULT (NEWSEQUENTIALID())")
                continue

            if attr == ROWVERSION_COLUMN:
                columns.append(f"    {quote(attr)} rowversion NOT NULL")
                continue

            if kind == "address":
                for suffix, sql_type in ADDRESS_PARTS:
                    columns.append(f"    {quote(attr + suffix)} {sql_type} NULL")
                columns.append(f"    {quote(attr + 'CountryId')} uniqueidentifier NULL")
                self.foreign_keys.append(
                    {"entity": entity, "column": attr + "CountryId", "target": "Country",
                     "onDelete": "setNull", "reason": "address country"})
                continue

            if kind == "polylookup":
                targets = column.get("targets") or ["*"]
                scope = "any table" if "*" in targets else ", ".join(targets)
                columns.append(f"    {quote(attr + 'Table')} nvarchar(128) "
                               f"{'NOT NULL' if column.get('required') else 'NULL'}")
                columns.append(f"    {quote(attr + 'Id')} uniqueidentifier "
                               f"{'NOT NULL' if column.get('required') else 'NULL'}")
                # No foreign key is possible, so at least keep the pair coherent.
                constraints.append(
                    f"    CONSTRAINT {quote(f'CK_{entity}_{attr}_pair')} CHECK ("
                    f"({quote(attr + 'Table')} IS NULL AND {quote(attr + 'Id')} IS NULL) OR "
                    f"({quote(attr + 'Table')} IS NOT NULL AND {quote(attr + 'Id')} IS NOT NULL))")
                self.notes.append(
                    f"{entity}.{attr} references {scope}, so no foreign key can be "
                    "declared; the pair is kept coherent by a check constraint")
                continue

            if kind in ("multilookup", "multiterm"):
                target = "Term" if kind == "multiterm" else column.get("target")
                if target in self.entities:
                    self.junctions.append(
                        {"entity": entity, "column": attr, "target": target})
                else:
                    self.warnings.append(
                        f"{entity}.{attr}: target {target} is not a table — skipped")
                continue

            if kind == "multichoice":
                enum = column.get("enum") or column.get("target")
                self.junctions.append(
                    {"entity": entity, "column": attr, "enum": enum})
                continue

            sql_type = self.column_type(entity, attr, column)
            if sql_type is None:
                self.warnings.append(f"{entity}.{attr}: no SQL type for '{kind}' — skipped")
                continue

            name = attr + "Id" if kind in ("lookup", "parent", "term") else attr
            required = bool(column.get("required")) and not column.get("readOnly")
            nullable = "NOT NULL" if required or kind == "parent" else "NULL"
            line = f"    {quote(name)} {sql_type} {nullable}"

            default = self._default(entity, attr, column, kind)
            if default:
                line += f" CONSTRAINT {quote(f'DF_{entity}_{name}')} DEFAULT ({default})"
            columns.append(line)

            if kind in ("lookup", "parent", "term"):
                target = "Term" if kind == "term" else column.get("target")
                if target in self.entities and (
                        self.entities[target].get("platforms", {}) or {}
                ).get("sql", "table") not in SKIP_MODES:
                    on_delete = column.get("onDelete") or (
                        "cascade" if kind == "parent"
                        else ("restrict" if required else "setNull"))
                    self.foreign_keys.append(
                        {"entity": entity, "column": name, "target": target,
                         "onDelete": on_delete, "self": target == entity})
                else:
                    self.warnings.append(
                        f"{entity}.{attr}: target {target} has no SQL table — "
                        "column kept, no foreign key")
            elif kind == "choice":
                enum = column.get("enum") or column.get("target")
                if enum in self.enums:
                    self.foreign_keys.append(
                        {"entity": entity, "column": name, "enumTarget": enum,
                         "onDelete": "restrict"})

            check = self._check(entity, name, column, kind)
            if check:
                constraints.append(check)

        if primary is None:
            self.errors.append(f"{entity}: no id column, so no primary key")
            return []
        constraints.insert(
            0, f"    CONSTRAINT {quote(f'PK_{entity}')} PRIMARY KEY ({quote(primary)})")

        # Effective dating: a closed row must end on or after it starts.
        attributes = spec["attributes"]
        if "validFrom" in attributes and "validTo" in attributes:
            constraints.append(
                f"    CONSTRAINT {quote(f'CK_{entity}_validRange')} CHECK ("
                "[validTo] IS NULL OR [validTo] >= [validFrom])")

        # Money needs a currency on the row.
        money = [a for a, c in attributes.items() if c["type"] == "money"]
        if money and "currency" in attributes:
            amounts = " OR ".join(f"{quote(a)} IS NOT NULL" for a in money)
            constraints.append(
                f"    CONSTRAINT {quote(f'CK_{entity}_currency')} CHECK ("
                f"NOT ({amounts}) OR [currencyId] IS NOT NULL)")

        out = [f"IF OBJECT_ID('{self.schema_of(entity)}.{entity}') IS NULL",
               f"CREATE TABLE {self.ref(entity)} ("]
        out.append(",\n".join(columns + constraints))
        out.append(");")
        if spec.get("description"):
            out.append(self._extended_property(entity, spec["description"]))
        return out

    def _default(self, entity: str, attr: str, column: dict[str, Any], kind: str) -> str | None:
        if column.get("default") is None:
            if attr == "createdOn" or attr == "modifiedOn":
                return "SYSUTCDATETIME()"
            return None
        value = column["default"]
        if kind == "choice":
            enum = self.enums.get(column.get("enum") or "") or {}
            for item in enum.get("values", []):
                if item.get("key") == value:
                    return str(item["code"])
            self.warnings.append(
                f"{entity}.{attr}: default '{value}' is not a value of "
                f"{column.get('enum')} — no default emitted")
            return None
        if kind == "boolean":
            return "1" if value else "0"
        if kind in ("integer", "decimal", "percent", "money", "duration"):
            return str(value)
        return literal(value)

    def _check(self, entity: str, name: str, column: dict[str, Any],
               kind: str) -> str | None:
        bounds = []
        if column.get("min") is not None:
            bounds.append(f"{quote(name)} >= {column['min']}")
        if column.get("max") is not None:
            bounds.append(f"{quote(name)} <= {column['max']}")
        if kind == "percent" and not bounds:
            bounds = [f"{quote(name)} >= 0", f"{quote(name)} <= 100"]
        if not bounds:
            return None
        clause = " AND ".join(bounds)
        return (f"    CONSTRAINT {quote(f'CK_{entity}_{name}')} CHECK ("
                f"{quote(name)} IS NULL OR ({clause}))")

    def _extended_property(self, entity: str, description: str) -> str:
        text = " ".join(description.split())
        return (f"EXEC sp_addextendedproperty 'MS_Description', {literal(text)}, "
                f"'SCHEMA', {literal(self.schema_of(entity))}, 'TABLE', {literal(entity)};")

    def _junction_table(self, junction: dict[str, Any]) -> list[str]:
        entity, column = junction["entity"], junction["column"]
        name = f"{entity}_{column[0].upper()}{column[1:]}"
        schema = self.schema_of(entity)
        if junction.get("enum"):
            target_column, target_type = "code", "int"
        else:
            target_column, target_type = f"{junction['target']}Id", "uniqueidentifier"
        out = [f"IF OBJECT_ID('{schema}.{name}') IS NULL",
               f"CREATE TABLE {quote(schema)}.{quote(name)} (",
               f"    {quote(entity + 'Id')} uniqueidentifier NOT NULL,",
               f"    {quote(target_column)} {target_type} NOT NULL,",
               f"    CONSTRAINT {quote(f'PK_{name}')} PRIMARY KEY "
               f"({quote(entity + 'Id')}, {quote(target_column)})",
               ");"]
        self.foreign_keys.append(
            {"entity": entity, "column": entity + "Id", "target": entity,
             "onDelete": "cascade", "junction": name, "schema": schema})
        if junction.get("enum"):
            self.foreign_keys.append(
                {"entity": entity, "column": target_column, "enumTarget": junction["enum"],
                 "onDelete": "restrict", "junction": name, "schema": schema})
        else:
            self.foreign_keys.append(
                {"entity": entity, "column": target_column, "target": junction["target"],
                 "onDelete": "restrict", "junction": name, "schema": schema})
        return out

    def sql_foreign_keys(self) -> str:
        lines = [self._header(
            "Foreign keys",
            "Added after every table exists, so circular references are not a problem. "
            "A choice column points at its reference table, which is what makes an "
            "invalid choice impossible to store.")]
        seen: set[str] = set()
        for fk in self.foreign_keys:
            if fk.get("junction"):
                source = f"{quote(fk['schema'])}.{quote(fk['junction'])}"
                constraint = f"FK_{fk['junction']}_{fk['column']}"
            else:
                source = self.ref(fk["entity"])
                constraint = f"FK_{fk['entity']}_{fk['column']}"
            if constraint in seen:
                self.errors.append(f"duplicate foreign key name {constraint}")
                continue
            seen.add(constraint)

            if fk.get("enumTarget"):
                target = self.ref_table(fk["enumTarget"])
                target_column = "code"
            else:
                target = self.ref(fk["target"])
                target_column = "id"

            # A self-reference or a cascade path that revisits a table would make
            # SQL Server reject the constraint, so those stay NO ACTION.
            action = ON_DELETE_SQL[fk.get("onDelete", "restrict")]
            if fk.get("self") and action != "ON DELETE NO ACTION":
                action = "ON DELETE NO ACTION"
                self.notes.append(
                    f"{fk['entity']}.{fk['column']} references its own table, so "
                    "the delete rule stays NO ACTION; SQL Server rejects a cycle")
            lines.append(
                f"IF OBJECT_ID('{constraint}', 'F') IS NULL "
                f"ALTER TABLE {source} ADD CONSTRAINT {quote(constraint)} "
                f"FOREIGN KEY ({quote(fk['column'])}) REFERENCES {target} "
                f"({quote(target_column)}) {action};")
        return "\n".join(lines) + "\n"

    def sql_indexes(self) -> str:
        lines = [self._header(
            "Indexes",
            "Unique constraints from the model's declared keys, indexes from its hints, "
            "an index on every foreign key, and filtered unique indexes that make "
            "effective dating a constraint rather than a convention.")]
        for entity in self.tables:
            spec = self.entities[entity]
            attributes = spec["attributes"]

            for key in spec.get("keys") or []:
                columns = [self._physical(attributes, c) for c in key]
                if any(c is None for c in columns):
                    continue
                name = f"UQ_{entity}_{'_'.join(key)}"
                lines.append(
                    f"IF INDEXPROPERTY(OBJECT_ID('{self.schema_of(entity)}.{entity}'), "
                    f"'{name}', 'IndexID') IS NULL\n"
                    f"CREATE UNIQUE INDEX {quote(name)} ON {self.ref(entity)} "
                    f"({', '.join(quote(c) for c in columns)}) "
                    f"WHERE {' AND '.join(f'{quote(c)} IS NOT NULL' for c in columns)};")

            for attr, column in attributes.items():
                physical = self._physical(attributes, attr)
                if physical is None:
                    continue
                if column.get("unique") and [attr] not in (spec.get("keys") or []):
                    name = f"UQ_{entity}_{attr}"
                    lines.append(
                        f"IF INDEXPROPERTY(OBJECT_ID('{self.schema_of(entity)}.{entity}'), "
                        f"'{name}', 'IndexID') IS NULL\n"
                        f"CREATE UNIQUE INDEX {quote(name)} ON {self.ref(entity)} "
                        f"({quote(physical)}) WHERE {quote(physical)} IS NOT NULL;")
                elif column.get("indexed") or column["type"] in ("lookup", "parent", "term"):
                    name = f"IX_{entity}_{attr}"
                    lines.append(
                        f"IF INDEXPROPERTY(OBJECT_ID('{self.schema_of(entity)}.{entity}'), "
                        f"'{name}', 'IndexID') IS NULL\n"
                        f"CREATE INDEX {quote(name)} ON {self.ref(entity)} "
                        f"({quote(physical)});")

            lines.extend(self._effective_dating_index(entity, attributes))
            lines.extend(self._reporting_indexes(entity, spec, attributes))
        return "\n".join(lines) + "\n"

    def _reporting_indexes(self, entity: str, spec: dict[str, Any],
                           attributes: dict[str, Any]) -> list[str]:
        """Index what a high-volume table is actually filtered by.

        Every foreign key is indexed above, which serves a join. It does not
        serve a report, and a report is what a high-volume table exists to feed:
        the filter is always a date range and a state. TimeEntry is marked
        volume: high, carries fourteen thousand demo rows, and had an index on
        all ten of its lookups and none on entryDate or status — so the one
        query the delivery dashboard runs was a full scan.

        Narrow on purpose: the row's own required date, and the column the
        model's convention calls `status`. Not every date (a due date is not
        what you filter a period by) and not every choice (priority is
        low-cardinality and an index on it earns nothing).
        """
        if spec.get("volume") != "high":
            return []
        wanted = [
            attr for attr, column in attributes.items()
            if not column.get("trait")
            and ((column["type"] in ("date", "datetime") and column.get("required"))
                 or attr == "status")
        ]
        lines = []
        for attr in wanted:
            physical = self._physical(attributes, attr)
            if physical is None or attributes[attr].get("unique"):
                continue
            if attributes[attr].get("indexed"):
                continue                   # already written above
            name = f"IX_{entity}_{attr}"
            lines.append(
                f"IF INDEXPROPERTY(OBJECT_ID('{self.schema_of(entity)}.{entity}'), "
                f"'{name}', 'IndexID') IS NULL\n"
                f"CREATE INDEX {quote(name)} ON {self.ref(entity)} "
                f"({quote(physical)});")
        if lines:
            self.notes.append(
                f"{entity} is volume: high, so "
                f"{', '.join(wanted)} {'is' if len(wanted) == 1 else 'are'} "
                "indexed for reporting as well as its foreign keys")
        return lines

    def _effective_dating_index(self, entity: str,
                                attributes: dict[str, Any]) -> list[str]:
        """One current row per subject, enforced by the database.

        Only a parent() column identifies the subject unambiguously: it is the
        row this one belongs to, so "one open row per parent" is the rule the
        model means. A plain lookup is not enough. LeavePolicy is effective
        dated and its leaveType is an ordinary lookup, but a policy is scoped by
        legal entity, country and employment type as well — one current row per
        leave type would stop two legal entities each having their own annual
        leave policy, which is the whole point of the table.
        """
        if "validTo" not in attributes or "validFrom" not in attributes:
            return []
        parents = [a for a, c in attributes.items() if c["type"] == "parent"]
        if not parents:
            self.notes.append(
                f"{entity} is effective-dated but has no parent column, so its subject "
                "is ambiguous and no one-current-row index was created — add one by "
                "hand if the real key is known")
            return []
        if len(parents) > 1:
            self.warnings.append(
                f"{entity} has more than one parent column ({', '.join(parents)}); "
                "no one-current-row index was created")
            return []

        subject = parents[0] + "Id"
        predicates = ["[validTo] IS NULL"]
        if "isPrimary" in attributes:
            predicates.append("[isPrimary] = 1")
        name = f"UX_{entity}_current"
        self.notes.append(
            f"{entity}: at most one row per {subject} with validTo IS NULL"
            + (" and isPrimary = 1" if "isPrimary" in attributes else ""))
        return [
            f"IF INDEXPROPERTY(OBJECT_ID('{self.schema_of(entity)}.{entity}'), "
            f"'{name}', 'IndexID') IS NULL\n"
            f"CREATE UNIQUE INDEX {quote(name)} ON {self.ref(entity)} "
            f"({quote(subject)}) WHERE {' AND '.join(predicates)};"
        ]

    @staticmethod
    def _physical(attributes: dict[str, Any], attr: str) -> str | None:
        spec = attributes.get(attr)
        if spec is None:
            return None
        if spec["type"] in ("lookup", "parent", "term"):
            return attr + "Id"
        if spec["type"] in ("multilookup", "multiterm", "multichoice", "address",
                            "polylookup"):
            return None
        return attr

    def sql_seed(self) -> str:
        lines = [self._header(
            "Reference data",
            "The schema's own seed rows. MERGE on the natural key, so running this "
            "again updates rather than duplicating.")]
        order = self._seed_order()
        for entity in order:
            rows = self.seeds.get(entity) or []
            if not rows or entity not in self.entities:
                continue
            attributes = self.entities[entity]["attributes"]
            natural = self._natural_key(entity, rows[0])
            if natural is None:
                self.warnings.append(
                    f"seed {entity}: no natural key column — rows skipped")
                continue
            lines.append(f"\n-- {entity} ({len(rows)} rows)")
            for row in rows:
                lines.append(self._merge(entity, attributes, row, natural))
        return "\n".join(lines) + "\n"

    def _seed_order(self) -> list[str]:
        remaining = {e for e in self.seeds if e in self.entities}
        ordered: list[str] = []
        while remaining:
            ready = []
            for entity in sorted(remaining):
                targets = {
                    spec.get("target")
                    for spec in self.entities[entity]["attributes"].values()
                    if spec["type"] in ("lookup", "parent") and not spec.get("trait")
                }
                if not (targets & remaining - {entity}):
                    ready.append(entity)
            if not ready:
                ready = [sorted(remaining)[0]]
            ordered.extend(ready)
            remaining -= set(ready)
        return ordered

    @staticmethod
    def _natural_key(entity: str, row: dict[str, Any]) -> str | None:
        for candidate in ("code", "isoCode", "isoCode2", "isoCode3"):
            if candidate in row:
                return candidate
        return None

    def _merge(self, entity: str, attributes: dict[str, Any], row: dict[str, Any],
               natural: str) -> str:
        sets, insert_columns, insert_values = [], [], []
        for field, value in row.items():
            spec = attributes.get(field)
            if spec is None:
                self.warnings.append(f"seed {entity}: '{field}' is not a column")
                continue
            kind = spec["type"]
            if kind in ("lookup", "parent", "term"):
                target = "Term" if kind == "term" else spec.get("target")
                column = field + "Id"
                expression = self._lookup_subquery(target, value)
            elif kind == "choice":
                enum = spec.get("enum") or spec.get("target")
                column = field
                expression = self._enum_subquery(enum, value)
            elif kind in ("multilookup", "multiterm", "multichoice", "address",
                          "polylookup"):
                continue
            else:
                column, expression = field, literal(value)
            if field != natural:
                sets.append(f"{quote(column)} = {expression}")
            insert_columns.append(quote(column))
            insert_values.append(expression)

        key_expression = literal(row[natural])
        matched = f"UPDATE SET {', '.join(sets)}" if sets else "UPDATE SET " \
                  f"{quote(natural)} = {key_expression}"
        return (f"MERGE {self.ref(entity)} AS t USING (SELECT {key_expression} AS "
                f"{quote(natural)}) AS s ON t.{quote(natural)} = s.{quote(natural)} "
                f"WHEN MATCHED THEN {matched} "
                f"WHEN NOT MATCHED THEN INSERT ({', '.join(insert_columns)}) "
                f"VALUES ({', '.join(insert_values)});")

    def _lookup_subquery(self, target: str, value: Any) -> str:
        if target not in self.entities:
            return "NULL"
        attributes = self.entities[target]["attributes"]
        for candidate in ("code", "isoCode", "isoCode2", "name"):
            if candidate in attributes:
                return (f"(SELECT [id] FROM {self.ref(target)} WHERE "
                        f"{quote(candidate)} = {literal(value)})")
        return "NULL"

    def _enum_subquery(self, enum: str, value: Any) -> str:
        for item in (self.enums.get(enum) or {}).get("values", []):
            if item.get("key") == value:
                return str(item["code"])
        self.warnings.append(f"seed value '{value}' is not a member of {enum}")
        return "NULL"

    def sql_views(self) -> str:
        """A current-row view per effective-dated table, so apps need no date logic."""
        lines = [self._header(
            "Views",
            "One view per effective-dated table, selecting the open row. An app that "
            "wants today's job assignment reads the view and carries no date logic.")]
        made = 0
        for entity in self.tables:
            attributes = self.entities[entity]["attributes"]
            if "validTo" not in attributes or "validFrom" not in attributes:
                continue
            name = f"{entity}Current"
            predicates = ["[validTo] IS NULL", "[validFrom] <= CAST(SYSUTCDATETIME() AS date)"]
            lines.append(
                f"\nCREATE OR ALTER VIEW {quote(self.schema_of(entity))}.{quote(name)} AS\n"
                f"SELECT * FROM {self.ref(entity)} WHERE {' AND '.join(predicates)};")
            made += 1
        if not made:
            lines.append("\n-- No effective-dated tables in this profile.")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _header(title: str, note: str) -> str:
        wrapped = []
        line = "--"
        for word in note.split():
            if len(line) + len(word) + 1 > 78:
                wrapped.append(line)
                line = "--"
            line += " " + word
        wrapped.append(line)
        return f"-- {title}\n" + "\n".join(wrapped) + "\n"

    # -- the whole thing
    def build(self) -> dict[str, Any]:
        # Order matters: tables fills junctions and foreign_keys.
        files = {
            "001_schemas.sql": self.sql_schemas(),
            "002_reference.sql": self.sql_reference(),
            "003_tables.sql": self.sql_tables(),
        }
        files["004_foreign_keys.sql"] = self.sql_foreign_keys()
        files["005_indexes.sql"] = self.sql_indexes()
        files["006_seed.sql"] = self.sql_seed()
        files["007_views.sql"] = self.sql_views()
        return {
            "profile": self.model.get("profile"),
            "dialect": "sqlserver",
            "flat": self.flat,
            "schemas": self.schemas(),
            "counts": {
                "tables": len(self.tables),
                "junctions": len(self.junctions),
                "referenceTables": len(self._used_enums()),
                "foreignKeys": len(self.foreign_keys),
                "seedRows": sum(len(r) for r in self.seeds.values()),
            },
            "files": files,
            "notes": self.notes,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate SQL migrations from a resolved model.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--flat", action="store_true",
                        help="put every table in dbo instead of one schema per layer")
    parser.add_argument("--model", help="resolved model JSON (default: build/<profile>.model.json)")
    parser.add_argument("--out", help="output directory (default: build/sql/<profile>)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args(argv)

    model_file = Path(args.model) if args.model else BUILD_DIR / f"{args.profile}.model.json"
    if not model_file.exists():
        print(f"error: {model_file} not found — run: python tools/schematool.py export "
              f"--profile {args.profile}", file=sys.stderr)
        return 2

    model = json.loads(model_file.read_text(encoding="utf-8"))
    generator = Generator(model, args.flat)
    plan = generator.build()

    out_dir = Path(args.out) if args.out else BUILD_DIR / "sql" / args.profile
    if not plan["errors"]:
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, text in plan["files"].items():
            (out_dir / name).write_text(text, encoding="utf-8")
        (out_dir / "manifest.json").write_text(json.dumps(
            {k: v for k, v in plan.items() if k != "files"}
            | {"files": sorted(plan["files"])}, indent=2), encoding="utf-8")
        counts = plan["counts"]
        print(f"Wrote {out_dir}")
        print(f"  {len(plan['schemas'])} schema(s): {', '.join(plan['schemas'])}")
        print(f"  {counts['tables']} tables + {counts['junctions']} junction tables")
        print(f"  {counts['referenceTables']} reference tables for the choice sets")
        print(f"  {counts['foreignKeys']} foreign keys")
        print(f"  {counts['seedRows']} seed rows")
        if plan["notes"]:
            print(f"  {len(plan['notes'])} note(s) — see manifest.json")
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
