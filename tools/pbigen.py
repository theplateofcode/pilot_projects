#!/usr/bin/env python3
"""Build a Power BI project (PBIP) from a dashboard spec and the resolved model.

The semantic model, its relationships and the theme are generated; the report
canvas is emitted as a page specification to lay out in Power BI Desktop, which
is the one part no file format lets us generate reliably.

The useful part is the checking. Every DAX reference is resolved against the
columns the generator itself produced, so a column renamed in the YAML schema
fails this build rather than a refresh three weeks later:

  · 'Table'[Column] must name a table in the model and a column it really has
  · [Measure] must name a measure in the spec
  · a measure may not reference a table the spec does not load
  · a visual may not name an unknown measure, column or page field
  · a hidden column may not be used by a visual

Usage
    python tools/pbigen.py --profile hr-starter --prefix wga
    python tools/pbigen.py --profile hr-starter --prefix wga --spec powerbi/hr-dashboard.yaml
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

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dvgen import logical, split_pascal  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"
POWERBI_DIR = ROOT / "powerbi"

# Schema type -> (TMDL dataType, formatString, summarizeBy)
TYPE_MAP: dict[str, tuple[str, str | None, str]] = {
    "string": ("string", None, "none"),
    "text": ("string", None, "none"),
    "richtext": ("string", None, "none"),
    "email": ("string", None, "none"),
    "phone": ("string", None, "none"),
    "url": ("string", None, "none"),
    "autonumber": ("string", None, "none"),
    "guid": ("string", None, "none"),
    "integer": ("int64", "#,0", "sum"),
    "duration": ("int64", "#,0", "sum"),
    "decimal": ("double", "#,0.00", "sum"),
    "percent": ("double", "0.0%", "none"),
    "money": ("decimal", "\\£#,0.00;(\\£#,0.00)", "sum"),
    "boolean": ("boolean", None, "none"),
    "date": ("dateTime", "Short Date", "none"),
    "datetime": ("dateTime", "General Date", "none"),
}
# Types with no useful representation over the TDS endpoint, and why.
SKIP_REASONS = {
    "file": "binary content, not a reportable value",
    "image": "binary content, not a reportable value",
    "address": "a composite; load the parts individually if a report needs them",
    "multilookup": "a many-to-many intersect table, which the TDS endpoint does not expose",
    "multiterm": "as multilookup",
    "multichoice": "a delimited value the TDS endpoint does not split",
}
SKIP_TYPES = set(SKIP_REASONS)

# The theme's first three categorical slots are the ones that pass every
# colour-vision and contrast gate under all-pairs comparison. A fourth puts
# yellow beside orange, which does not.
MAX_SERIES = 3

# Trait columns that are pure plumbing: hidden from the field list, though a
# measure may still reference them. integration, classified and effectiveDated
# are business facts (validFrom/validTo, sensitivity) and stay visible.
PLUMBING_TRAITS = {"base", "owned"}

# The Dataverse TDS endpoint exposes a choice as an integer plus a `<col>name`
# label column, and a lookup as a GUID plus a `<col>name` display column.
LABEL_SUFFIX = "name"

DAX_COLUMN = re.compile(r"'([^']+)'\[([^\]]+)\]")
DAX_MEASURE = re.compile(r"(?<!\])\[([^\]]+)\]")
DAX_FUNCTIONS = re.compile(r"\b([A-Z][A-Z0-9_.]*)\s*\(")

# Date table columns the generator always creates.
DATE_COLUMNS = {
    "Date": ("dateTime", "Short Date"),
    "Year": ("int64", "#0"),
    "Quarter": ("string", None),
    "Month": ("string", None),
    "Month Number": ("int64", "#0"),
    "Year Month": ("string", None),
    "Year Month Sort": ("int64", "#0"),
    "Fiscal Year": ("string", None),
    "Fiscal Quarter": ("string", None),
    "Is Past": ("boolean", None),
}


class SpecError(Exception):
    pass


def _as_date(value: Any) -> dt.date:
    """YAML gives a date for an unquoted 2022-01-01 and a string if it is quoted."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


class Builder:
    def __init__(self, spec: dict[str, Any], model: dict[str, Any], prefix: str):
        self.spec = spec
        self.model = model
        self.prefix = prefix.lower()
        self.meta = spec.get("meta", {})
        self.entities: dict[str, Any] = model.get("entities", {})
        self.enums: dict[str, Any] = model.get("enums", {})
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.included: list[str] = list(spec.get("tables") or [])
        self.date_table = (spec.get("dateTable") or {}).get("name", "Date")
        self.hidden: set[str] = set(spec.get("hide") or [])
        # Table -> {column name -> {dataType, formatString, sourceColumn, ...}}
        self.columns: dict[str, dict[str, dict[str, Any]]] = {}
        self.relationships: list[dict[str, Any]] = []
        self.measures: list[dict[str, Any]] = []
        self._build()

    # -- model ----------------------------------------------------------------
    def _build(self) -> None:
        for name in self.included:
            if name not in self.entities:
                self.errors.append(f"table '{name}' is not in this profile")
        self.included = [n for n in self.included if n in self.entities]

        for name in self.included:
            self.columns[name] = self._table_columns(name)
        self.columns[self.date_table] = {
            col: {"dataType": dtype, "formatString": fmt, "sourceColumn": col,
                  "summarizeBy": "none", "isHidden": col.endswith("Sort")}
            for col, (dtype, fmt) in DATE_COLUMNS.items()
        }

        self._build_relationships()
        self._build_measures()
        self._check_hidden()
        self._check_pages()

    def _table_columns(self, name: str) -> dict[str, dict[str, Any]]:
        ent = self.entities[name]
        table_logical = self._logical_table(name)
        out: dict[str, dict[str, Any]] = {}

        for attr, spec in ent["attributes"].items():
            kind = spec["type"]
            if kind in SKIP_TYPES:
                # Known to have no useful shape over the TDS endpoint. Recorded
                # rather than warned, so --strict stays usable, but the omission
                # is in model-report.json where someone can see it.
                self.notes.append(
                    f"{name}.{attr} ({kind}) is not loaded: {SKIP_REASONS[kind]}")
                continue

            source = self._source_column(name, attr, spec)
            if source is None:
                continue

            plumbing = spec.get("trait") in PLUMBING_TRAITS

            if kind in ("lookup", "parent", "term"):
                # The GUID, hidden — only relationships use it.
                out[attr] = {
                    "dataType": "string", "formatString": None, "sourceColumn": source,
                    "summarizeBy": "none", "isHidden": True,
                    "description": spec.get("description", ""),
                }
                # The display name, shown unless the lookup is plumbing.
                label_name = f"{attr}{LABEL_SUFFIX}"
                out[label_name] = {
                    "dataType": "string", "formatString": None,
                    "sourceColumn": f"{source}{LABEL_SUFFIX}",
                    "summarizeBy": "none", "isHidden": plumbing,
                    "description": f"Display name of {split_pascal(attr).lower()}.",
                }
                continue

            if kind == "polylookup":
                # dvgen builds these as two text columns, so that is what the
                # TDS endpoint serves. Hidden: a report filters on the record's
                # own table, not on a stringly-typed reference to it.
                for part, label_suffix, length in (("Table", "table", 100), ("Id", "id", 36)):
                    out[f"{attr}{part}"] = {
                        "dataType": "string", "formatString": None,
                        "sourceColumn": logical(self.prefix, f"{attr}{part}"),
                        "summarizeBy": "none", "isHidden": True,
                        "description": f"The {label_suffix} of the record this row is about.",
                    }
                continue

            if kind == "choice":
                out[attr] = {
                    "dataType": "int64", "formatString": "#0", "sourceColumn": source,
                    "summarizeBy": "none", "isHidden": True,
                    "description": spec.get("description", ""),
                }
                out[f"{attr}{LABEL_SUFFIX}"] = {
                    "dataType": "string", "formatString": None,
                    "sourceColumn": f"{source}{LABEL_SUFFIX}",
                    "summarizeBy": "none", "isHidden": plumbing,
                    "description": self._choice_description(spec),
                }
                continue

            if kind not in TYPE_MAP:
                self.warnings.append(f"{name}.{attr}: no Power BI type for '{kind}' — skipped")
                continue

            dtype, fmt, summarize = TYPE_MAP[kind]
            out[attr] = {
                "dataType": dtype, "formatString": fmt, "sourceColumn": source,
                "summarizeBy": summarize if not spec.get("readOnly") else "none",
                "isHidden": plumbing or f"{name}.{attr}" in self.hidden,
                "description": spec.get("description", ""),
            }
        if not out:
            self.errors.append(f"{name}: no usable columns")
        return out

    def _source_column(self, entity: str, attr: str, spec: dict[str, Any]) -> str | None:
        """The TDS endpoint column name, honouring the native Dataverse columns.

        The dictionary writes a native column as its real name plus a note:
        "createdon (native)", "ownerid (native, user or team) + owningbusinessunit",
        and the primary key as the placeholder "<table>id (native primary key)".
        """
        physical = str((spec.get("physical") or {}).get("dataverse") or "")
        if "(native" not in physical:
            return logical(self.prefix, attr)
        token = physical.split()[0]
        if token == "<table>id":
            return f"{self._logical_table(entity)}id"
        if re.fullmatch(r"[a-z][a-z0-9_]*", token):
            return token
        self.warnings.append(
            f"{entity}.{attr}: cannot work out the native column name from "
            f"'{physical}' — skipped")
        return None

    def _logical_table(self, name: str) -> str:
        mode = (self.entities[name].get("platforms", {}) or {}).get("dataverse", "custom")
        if isinstance(mode, str) and mode.startswith("standard:"):
            return mode.split(":", 1)[1].strip().lower()
        return logical(self.prefix, name)

    def _choice_description(self, spec: dict[str, Any]) -> str:
        enum = self.enums.get(spec.get("enum") or "") or {}
        values = [v.get("label") or v.get("key") for v in enum.get("values", [])
                  if not v.get("deprecated")]
        if not values:
            return spec.get("description", "")
        return f"One of: {', '.join(values)}."

    def _build_relationships(self) -> None:
        """A business lookup between two loaded tables becomes a relationship.

        Trait columns are left out: createdBy, modifiedBy and owner point at
        Employee on every table, and a report model does not analyse by who
        touched a row. Including them would also crowd out the real
        relationship, since only one path between a pair may be active.
        """
        candidates: list[dict[str, Any]] = []
        for name in self.included:
            for attr, spec in self.entities[name]["attributes"].items():
                if spec["type"] not in ("lookup", "parent"):
                    continue
                if spec.get("trait"):
                    continue
                target = spec.get("target")
                if target not in self.columns or target == name:
                    continue
                if attr not in self.columns[name]:
                    continue
                target_key = self._primary_key_column(target)
                if target_key is None or target_key not in self.columns[target]:
                    self.errors.append(
                        f"{name}.{attr}: {target} has no primary key column in the model, "
                        "so the relationship cannot be created")
                    continue
                candidates.append(
                    {
                        "name": f"{name}_{attr}_{target}",
                        "fromTable": name, "fromColumn": attr,
                        "toTable": target, "toColumn": target_key,
                        "crossFilteringBehavior": "oneDirection",
                        "isActive": True,
                        "_priority": self._priority(attr, target, spec),
                    }
                )

        # Date relationships, one role-playing path per date column.
        for table, column, active in (
            ("Employment", "startDate", True),
            ("Employment", "endDate", False),
            ("LeaveRequest", "startDate", True),
            ("JobApplication", "appliedOn", True),
            ("JobRequisition", "postedOn", True),
            ("EmployeeCredential", "expiresOn", True),
            ("TrainingRecord", "completedOn", True),
            ("PerformanceReview", "completedOn", True),
            ("ApprovalStep", "decidedOn", True),
        ):
            if column in self.columns.get(table, {}):
                candidates.append(
                    {
                        "name": f"{table}_{column}_{self.date_table}",
                        "fromTable": table, "fromColumn": column,
                        "toTable": self.date_table, "toColumn": "Date",
                        "crossFilteringBehavior": "oneDirection",
                        "isActive": active,
                        "_priority": 100 if active else -1,
                    }
                )

        self.relationships = self._choose_active(candidates)

    @staticmethod
    def _priority(attr: str, target: str, spec: dict[str, Any]) -> int:
        """How strongly this column deserves to be the active path to its target.

        The column named after its target is the subject of the row —
        LeaveRequest.employee is whose leave it is, while coverBy is incidental —
        so it outranks every other path to the same table.
        """
        score = 0
        if attr.lower() == target.lower():
            score += 50
        if spec["type"] == "parent":
            score += 20
        if spec.get("required"):
            score += 10
        return score

    def _choose_active(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Only one active relationship may exist between a pair of tables."""
        best: dict[tuple[str, str], dict[str, Any]] = {}
        for rel in candidates:
            if not rel["isActive"]:
                continue
            pair = (rel["fromTable"], rel["toTable"])
            incumbent = best.get(pair)
            if incumbent is None or rel["_priority"] > incumbent["_priority"]:
                best[pair] = rel

        out = []
        for rel in candidates:
            pair = (rel["fromTable"], rel["toTable"])
            if rel["isActive"] and best.get(pair) is not rel:
                rel["isActive"] = False
                self.notes.append(
                    f"{rel['name']}: inactive — {best[pair]['fromColumn']} is the active path "
                    f"from {rel['fromTable']} to {rel['toTable']}; "
                    "reach this one with USERELATIONSHIP"
                )
            del rel["_priority"]
            out.append(rel)
        return out

    def _primary_key_column(self, table: str) -> str | None:
        for attr, spec in self.entities[table]["attributes"].items():
            if spec["type"] == "guid":
                return attr
        return None

    # -- measures -------------------------------------------------------------
    def _build_measures(self) -> None:
        names: set[str] = set()
        for item in self.spec.get("measures") or []:
            name = item.get("name")
            if not name:
                self.errors.append("a measure has no name")
                continue
            if name in names:
                self.errors.append(f"measure '{name}' is defined twice")
            names.add(name)
            self.measures.append(
                {
                    "name": name,
                    "expression": (item.get("expression") or "").strip(),
                    "formatString": item.get("format"),
                    "displayFolder": item.get("folder"),
                    "description": " ".join((item.get("description") or "").split()),
                }
            )

        for measure in self.measures:
            self._check_dax(measure["name"], measure["expression"], names)

    def _check_dax(self, where: str, expression: str, measure_names: set[str]) -> None:
        for table, column in DAX_COLUMN.findall(expression):
            if table not in self.columns:
                self.errors.append(
                    f"{where}: references table '{table}', which the spec does not load")
            elif column not in self.columns[table]:
                near = ", ".join(sorted(self.columns[table])[:6])
                self.errors.append(
                    f"{where}: '{table}'[{column}] is not a column of {table} (has: {near}…)")

        # A [Name] not preceded by ] is a measure reference.
        without_columns = DAX_COLUMN.sub("", expression)
        for name in DAX_MEASURE.findall(without_columns):
            if name in measure_names:
                continue
            if name in {"Date"}:                     # a bare table-less reference
                continue
            self.errors.append(f"{where}: [{name}] is not a measure in this spec")

    # -- pages ----------------------------------------------------------------
    def _check_hidden(self) -> None:
        for reference in self.hidden:
            table, _, column = reference.partition(".")
            if table not in self.columns:
                self.errors.append(f"hide: '{table}' is not a loaded table")
            elif column not in self.columns[table]:
                self.errors.append(f"hide: '{reference}' is not a column of {table}")

    def _check_pages(self) -> None:
        measure_names = {m["name"] for m in self.measures}
        for page in self.spec.get("pages") or []:
            page_name = page.get("name", "?")
            for visual in page.get("visuals") or []:
                title = visual.get("title", "?")
                where = f"page '{page_name}' visual '{title}'"
                wanted = list(visual.get("measures") or [])
                if visual.get("measure"):
                    wanted.append(visual["measure"])
                for name in wanted:
                    if name not in measure_names:
                        self.errors.append(f"{where}: '{name}' is not a measure in this spec")
                for field in ([visual["axis"]] if visual.get("axis") else []) + \
                             list(visual.get("columns") or []):
                    self._check_field(where, field)
                series = len(visual.get("measures") or [])
                if series > MAX_SERIES:
                    self.warnings.append(
                        f"{where}: {series} series, but only the first {MAX_SERIES} theme "
                        "slots pass the colour-vision and contrast gates under all-pairs "
                        "comparison — fold the rest into 'Other' or use small multiples")
                if visual.get("type") == "barClustered" and len(visual.get("measures") or []) > 1:
                    formats = {next((m["formatString"] for m in self.measures
                                     if m["name"] == n), None)
                               for n in visual["measures"]}
                    if len(formats) > 1:
                        self.warnings.append(
                            f"{where}: measures have different formats {formats} on one axis — "
                            "two scales on one chart; split it or index them to a common base")

    def _check_field(self, where: str, field: str) -> None:
        table, _, column = field.partition(".")
        if not column:
            self.errors.append(f"{where}: field '{field}' must be written Table.Column")
        elif table not in self.columns:
            self.errors.append(f"{where}: table '{table}' is not loaded")
        elif column not in self.columns[table]:
            self.errors.append(f"{where}: '{field}' is not a column of {table}")
        elif self.columns[table][column].get("isHidden"):
            self.errors.append(f"{where}: '{field}' is hidden and cannot be put on a visual")

    # -- TMDL -----------------------------------------------------------------
    def tmdl_table(self, name: str) -> str:
        lines = [f"table {self._escape(name)}", ""]
        ent = self.entities.get(name)
        if ent:
            lines.append(f"\t/// {' '.join(ent['description'].split())}")
        lines.append("")

        for column, spec in self.columns[name].items():
            lines.extend(self._tmdl_column(column, spec))

        for measure in [m for m in self.measures if self._measure_home(m) == name]:
            lines.extend(self._tmdl_measure(measure))

        lines.extend(self._tmdl_partition(name))
        return "\n".join(lines) + "\n"

    def _tmdl_column(self, column: str, spec: dict[str, Any]) -> list[str]:
        lines = [f"\tcolumn {self._escape(column)}"]
        if spec.get("description"):
            lines.insert(0, f"\t/// {' '.join(str(spec['description']).split())}")
        lines.append(f"\t\tdataType: {spec['dataType']}")
        if spec.get("formatString"):
            lines.append(f"\t\tformatString: {spec['formatString']}")
        lines.append(f"\t\tsummarizeBy: {spec.get('summarizeBy', 'none')}")
        lines.append(f"\t\tsourceColumn: {spec['sourceColumn']}")
        if spec.get("isHidden"):
            lines.append("\t\tisHidden")
        lines.append("")
        lines.append("\t\tannotation SummarizationSetBy = Automatic")
        lines.append("")
        return lines

    def _tmdl_measure(self, measure: dict[str, Any]) -> list[str]:
        lines = []
        if measure["description"]:
            lines.append(f"\t/// {measure['description']}")
        expression = measure["expression"]
        if "\n" in expression:
            lines.append(f"\tmeasure {self._escape(measure['name'])} = ```")
            for line in expression.splitlines():
                lines.append(f"\t\t\t{line}")
            lines.append("\t\t\t```")
        else:
            lines.append(f"\tmeasure {self._escape(measure['name'])} = {expression}")
        if measure["formatString"]:
            lines.append(f"\t\tformatString: {measure['formatString']}")
        if measure["displayFolder"]:
            lines.append(f"\t\tdisplayFolder: {measure['displayFolder']}")
        lines.append("")
        return lines

    def _measure_home(self, measure: dict[str, Any]) -> str:
        """Every measure lives on the Metrics table, so the field list stays tidy."""
        return "Metrics"

    def _tmdl_partition(self, name: str) -> list[str]:
        source = self._logical_table(name)
        parameter = self.meta.get("environmentParameter", "DataverseEnvironment")
        mode = self.meta.get("storageMode", "import")
        expression = [
            "\t\t\tlet",
            f"\t\t\t    Source = CommonDataService.Database({parameter}, "
            "[CreateNavigationProperties=false]),",
            f'\t\t\t    Table = Source{{[Schema="dbo", Item="{source}"]}}[Data]',
            "\t\t\tin",
            "\t\t\t    Table",
        ]
        return [
            f"\tpartition {self._escape(name)} = m",
            f"\t\tmode: {mode}",
            "\t\tsource = ```",
            *expression,
            "\t\t\t```",
            "",
        ]

    def tmdl_metrics_table(self) -> str:
        lines = ["table Metrics", "", "\t/// Every measure in the model. The table holds no data.", ""]
        lines.append("\tcolumn _placeholder")
        lines.append("\t\tdataType: string")
        lines.append("\t\tisHidden")
        lines.append("\t\tsummarizeBy: none")
        lines.append("\t\tsourceColumn: _placeholder")
        lines.append("")
        for measure in self.measures:
            lines.extend(self._tmdl_measure(measure))
        lines.append('\tpartition Metrics = m')
        lines.append("\t\tmode: import")
        lines.append("\t\tsource = ```")
        lines.append('\t\t\tlet')
        lines.append('\t\t\t    Source = #table({"_placeholder"}, {}),')
        lines.append('\t\t\t    Typed = Table.TransformColumnTypes(Source, '
                     '{{"_placeholder", type text}})')
        lines.append("\t\t\tin")
        lines.append("\t\t\t    Typed")
        lines.append("\t\t\t```")
        lines.append("")
        return "\n".join(lines) + "\n"

    def tmdl_date_table(self) -> str:
        cfg = self.spec.get("dateTable") or {}
        start = _as_date(cfg.get("from", "2022-01-01"))
        year_end = int(cfg.get("yearEndMonth", 12))
        fiscal_offset = (12 - year_end) % 12
        expression = f"""
            VAR Start = DATE({start.year}, {start.month}, {start.day})
            VAR End = DATE( YEAR( TODAY() ) + 1, 12, 31 )
            RETURN
            ADDCOLUMNS(
                CALENDAR( Start, End ),
                "Year", YEAR( [Date] ),
                "Quarter", "Q" & FORMAT( [Date], "Q" ),
                "Month", FORMAT( [Date], "MMM" ),
                "Month Number", MONTH( [Date] ),
                "Year Month", FORMAT( [Date], "MMM yyyy" ),
                "Year Month Sort", YEAR( [Date] ) * 100 + MONTH( [Date] ),
                "Fiscal Year", "FY" & FORMAT( EDATE( [Date], {fiscal_offset} ), "yy" ),
                "Fiscal Quarter", "Q" & FORMAT( EDATE( [Date], {fiscal_offset} ), "Q" ),
                "Is Past", [Date] <= TODAY()
            )
        """.strip()

        lines = [f"table {self.date_table}", "",
                 f"\t/// Calendar from {start.isoformat()} to the end of next year. "
                 f"Fiscal year ends in month {year_end}.", ""]
        if cfg.get("markAsDateTable", True):
            lines.append("\tdataCategory: Time")
            lines.append("")
        for column, spec in self.columns[self.date_table].items():
            lines.extend(self._tmdl_column(column, {**spec, "sourceColumn": column}))
        lines.append(f"\tpartition {self.date_table} = calculated")
        lines.append("\t\tmode: import")
        lines.append("\t\tsource = ```")
        for line in expression.splitlines():
            lines.append(f"\t\t\t{line.strip()}")
        lines.append("\t\t\t```")
        lines.append("")
        lines.append(f"\tannotation PBI_ResultType = Table")
        lines.append("")
        return "\n".join(lines) + "\n"

    def tmdl_model(self) -> str:
        culture = self.meta.get("culture", "en-GB")
        lines = ["model Model", f"\tculture: {culture}",
                 "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
                 "\tdiscourageImplicitMeasures", "\tsourceQueryCulture: " + culture, ""]
        for name in [self.date_table, "Metrics", *self.included]:
            lines.append(f"ref table {self._escape(name)}")
        lines.append("")
        lines.append("ref cultureInfo " + culture)
        lines.append("")
        for rel in self.relationships:
            lines.append(f"relationship {rel['name']}")
            if not rel["isActive"]:
                lines.append("\tisActive: false")
            lines.append(f"\tfromColumn: {self._escape(rel['fromTable'])}."
                         f"{self._escape(rel['fromColumn'])}")
            lines.append(f"\ttoColumn: {self._escape(rel['toTable'])}."
                         f"{self._escape(rel['toColumn'])}")
            lines.append("")
        return "\n".join(lines) + "\n"

    def tmdl_expressions(self) -> str:
        parameter = self.meta.get("environmentParameter", "DataverseEnvironment")
        default = self.meta.get("defaultEnvironment", "")
        return (
            f"/// The Dataverse TDS endpoint host, e.g. {default}. Override it per\n"
            f"/// environment in the Power BI service under semantic model settings.\n"
            f"expression {parameter} = \"{default}\" meta "
            "[IsParameterQuery=true, Type=\"Text\", IsParameterQueryRequired=true]\n"
            "\tlineageTag: dataverse-environment\n"
            "\tannotation PBI_NavigationStepName = Navigation\n"
        )

    @staticmethod
    def _escape(name: str) -> str:
        """TMDL quotes a name that is not a bare identifier."""
        return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else f"'{name}'"


# --- theme -------------------------------------------------------------------
def theme(spec: dict[str, Any]) -> dict[str, Any]:
    """A Power BI theme built on the validated palette.

    Categorical slots 1-3 (blue, orange, aqua) pass every hard gate in both
    modes under --pairs all; no visual in the spec uses more than three series.
    Light-mode aqua sits below 3:1 on the surface, so the relief rule applies
    and data labels are on by default for bar charts.
    """
    return {
        "name": "Woodgrove HR",
        "dataColors": [
            "#2a78d6", "#eb6834", "#1baf7a", "#eda100",
            "#e87ba4", "#008300", "#6250d6", "#e34948",
        ],
        "good": "#008300",
        "neutral": "#eda100",
        "bad": "#e34948",
        "maximum": "#0d366b",
        "center": "#eda100",
        "minimum": "#cde2fb",
        "null": "#c3c2b7",
        "background": "#fcfcfb",
        "secondaryBackground": "#f2f2f0",
        "tableAccent": "#2a78d6",
        "foreground": "#0b0b0b",
        "foregroundNeutralSecondary": "#52514e",
        "foregroundNeutralTertiary": "#6f6e6a",
        "textClasses": {
            "title": {"fontSize": 14, "fontFace": "Segoe UI Semibold", "color": "#0b0b0b"},
            "label": {"fontSize": 10, "fontFace": "Segoe UI", "color": "#52514e"},
            "callout": {"fontSize": 32, "fontFace": "Segoe UI Light", "color": "#0b0b0b"},
        },
        "visualStyles": {
            "*": {
                "*": {
                    # Recessive grid and axes; the data carries the ink.
                    "background": [{"show": True, "color": {"solid": {"color": "#fcfcfb"}},
                                    "transparency": 0}],
                    "border": [{"show": False}],
                    "visualHeader": [{"show": True, "background": {"solid": {"color": "#fcfcfb"}}}],
                    "title": [{"show": True, "fontSize": 14, "fontColor":
                               {"solid": {"color": "#0b0b0b"}}, "alignment": "left"}],
                    "categoryAxis": [{"show": True, "gridlineShow": False,
                                      "labelColor": {"solid": {"color": "#52514e"}},
                                      "fontSize": 10}],
                    "valueAxis": [{"show": True, "gridlineShow": True,
                                   "gridlineColor": {"solid": {"color": "#e8e8e4"}},
                                   "gridlineThickness": 1,
                                   "labelColor": {"solid": {"color": "#52514e"}},
                                   "fontSize": 10}],
                    "legend": [{"show": True, "position": "TopLeft", "showTitle": False,
                                "labelColor": {"solid": {"color": "#52514e"}}, "fontSize": 10}],
                }
            },
            # Relief for the light-mode contrast warning on slot 3.
            "barChart": {"*": {"labels": [{"show": True, "fontSize": 10,
                                           "color": {"solid": {"color": "#0b0b0b"}}}]}},
            "clusteredBarChart": {"*": {"labels": [{"show": True, "fontSize": 10,
                                                    "color": {"solid": {"color": "#0b0b0b"}}}]}},
            "columnChart": {"*": {"labels": [{"show": True, "fontSize": 10,
                                              "color": {"solid": {"color": "#0b0b0b"}}}]}},
            # Lines carry no label per point; 2px stroke, markers only on hover.
            "lineChart": {"*": {
                "labels": [{"show": False}],
                "lineStyles": [{"strokeWidth": 2, "showMarker": False}],
            }},
            "card": {"*": {
                "labels": [{"fontSize": 32, "color": {"solid": {"color": "#0b0b0b"}}}],
                "categoryLabels": [{"fontSize": 11,
                                    "color": {"solid": {"color": "#52514e"}}}],
            }},
            "tableEx": {"*": {
                "grid": [{"gridVertical": False, "gridHorizontal": True,
                          "gridHorizontalColor": {"solid": {"color": "#e8e8e4"}},
                          "rowPadding": 4}],
                "columnHeaders": [{"fontColor": {"solid": {"color": "#0b0b0b"}},
                                   "backColor": {"solid": {"color": "#f2f2f0"}}}],
            }},
        },
    }


# --- writing -----------------------------------------------------------------
def write_project(builder: Builder, out_dir: Path, spec: dict[str, Any]) -> None:
    name = spec.get("meta", {}).get("name", "Report")
    slug = re.sub(r"[^A-Za-z0-9]+", "", name) or "Report"
    model_dir = out_dir / f"{slug}.SemanticModel"
    report_dir = out_dir / f"{slug}.Report"
    definition = model_dir / "definition"
    (definition / "tables").mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / f"{slug}.pbip").write_text(json.dumps({
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{slug}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    }, indent=2), encoding="utf-8")

    (model_dir / ".platform").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "SemanticModel", "displayName": name},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000000"},
    }, indent=2), encoding="utf-8")
    (model_dir / "definition.pbism").write_text(json.dumps({
        "version": "4.0", "settings": {}}, indent=2), encoding="utf-8")

    (definition / "database.tmdl").write_text(
        "database\n\tcompatibilityLevel: 1567\n", encoding="utf-8")
    (definition / "model.tmdl").write_text(builder.tmdl_model(), encoding="utf-8")
    (definition / "expressions.tmdl").write_text(builder.tmdl_expressions(), encoding="utf-8")
    (definition / "tables" / f"{builder.date_table}.tmdl").write_text(
        builder.tmdl_date_table(), encoding="utf-8")
    (definition / "tables" / "Metrics.tmdl").write_text(
        builder.tmdl_metrics_table(), encoding="utf-8")
    for table in builder.included:
        (definition / "tables" / f"{table}.tmdl").write_text(
            builder.tmdl_table(table), encoding="utf-8")

    (report_dir / ".platform").write_text(json.dumps({
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": "Report", "displayName": name},
        "config": {"version": "2.0", "logicalId": "00000000-0000-0000-0000-000000000001"},
    }, indent=2), encoding="utf-8")
    (report_dir / "definition.pbir").write_text(json.dumps({
        "version": "4.0",
        "datasetReference": {"byPath": {"path": f"../{slug}.SemanticModel"}},
    }, indent=2), encoding="utf-8")

    themes = report_dir / "StaticResources" / "SharedResources" / "BaseThemes"
    themes.mkdir(parents=True, exist_ok=True)
    (themes / "WoodgroveHR.json").write_text(
        json.dumps(theme(spec), indent=2), encoding="utf-8")

    (out_dir / "pages.md").write_text(page_spec(builder, spec), encoding="utf-8")
    (out_dir / "model-report.json").write_text(json.dumps({
        "tables": {t: sorted(c for c, s in builder.columns[t].items() if not s.get("isHidden"))
                   for t in builder.columns},
        "hiddenColumns": sorted(
            f"{t}.{c}" for t in builder.columns for c, s in builder.columns[t].items()
            if s.get("isHidden")),
        "relationships": builder.relationships,
        "measures": [{"name": m["name"], "folder": m["displayFolder"]} for m in builder.measures],
        "errors": builder.errors,
        "warnings": builder.warnings,
        "notes": builder.notes,
    }, indent=2), encoding="utf-8")


def page_spec(builder: Builder, spec: dict[str, Any]) -> str:
    """The report canvas, as a layout to build in Desktop."""
    lines = [
        f"# {spec['meta']['name']} — report pages",
        "",
        "Generated by `tools/pbigen.py` from `powerbi/hr-dashboard.yaml`. The semantic",
        "model, relationships, measures and theme are generated; lay the visuals out in",
        "Power BI Desktop against this list, then commit the saved `.Report` folder.",
        "",
        "Apply the theme first: **View → Themes → Browse for themes →**",
        "`StaticResources/SharedResources/BaseThemes/WoodgroveHR.json`.",
        "",
        "Design rules this layout follows, from the data-visualisation guidance:",
        "",
        "- One scale per chart. No visual puts two different units on one axis.",
        "- A single-series chart has no legend — its title names the series.",
        "- Two or more series always carry a legend, and bars carry data labels, so",
        "  identity is never colour alone.",
        "- Categorical colour comes from theme slots 1–3 in fixed order, never cycled.",
        "- Ordered things (pipeline stages) use the sequential blue ramp, not hues.",
        "- No visual carries more than three series. Slots 1–3 are the ones that pass",
        "  every colour-vision and contrast gate under all-pairs comparison; a fourth",
        "  would put yellow next to orange, which fails them. Past three, fold the rest",
        "  into \"Other\" or use small multiples.",
        "- Where a page is operational rather than a summary, it also carries a table",
        "  view, so a value can be read exactly and not just compared.",
        "",
    ]
    for page in spec.get("pages") or []:
        lines.append(f"## {page['name']}")
        lines.append("")
        if page.get("description"):
            lines.append(f"{' '.join(page['description'].split())}")
            lines.append("")
        tiles = [v for v in page["visuals"] if v.get("type") == "statTile"]
        if tiles:
            lines.append("**Stat tiles** (one row across the top, cards):")
            lines.append("")
            lines.append("| Tile | Measure | Conditional formatting |")
            lines.append("|---|---|---|")
            for tile in tiles:
                formatting = ("good/warning/critical from the status palette, with an icon"
                              if tile.get("status") else "none")
                lines.append(f"| {tile['title']} | `{tile['measure']}` | {formatting} |")
            lines.append("")
        others = [v for v in page["visuals"] if v.get("type") != "statTile"]
        if others:
            lines.append("**Visuals:**")
            lines.append("")
            for visual in others:
                lines.append(f"- **{visual['title']}** — `{visual['type']}`")
                if visual.get("measures"):
                    lines.append(f"  - values: {', '.join('`' + m + '`' for m in visual['measures'])}")
                if visual.get("axis"):
                    lines.append(f"  - axis: `{visual['axis']}`")
                if visual.get("columns"):
                    lines.append(f"  - columns: {', '.join('`' + c + '`' for c in visual['columns'])}")
                legend = len(visual.get("measures") or []) > 1
                lines.append(f"  - legend: {'yes' if legend else 'no (single series)'}")
                if visual.get("dataLabels"):
                    lines.append("  - data labels: on")
                if visual.get("note"):
                    lines.append(f"  - note: {' '.join(visual['note'].split())}")
            lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a Power BI project from a dashboard spec.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--spec", help="dashboard YAML (default: powerbi/hr-dashboard.yaml)")
    parser.add_argument("--model", help="resolved model JSON (default: build/<profile>.model.json)")
    parser.add_argument("--out", help="output directory (default: build/powerbi/<name>)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args(argv)

    spec_file = Path(args.spec) if args.spec else POWERBI_DIR / "hr-dashboard.yaml"
    model_file = Path(args.model) if args.model else BUILD_DIR / f"{args.profile}.model.json"
    for path, hint in ((spec_file, "write the dashboard YAML first"),
                       (model_file, f"run: python tools/schematool.py export --profile {args.profile}")):
        if not path.exists():
            print(f"error: {path} not found — {hint}", file=sys.stderr)
            return 2

    spec = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
    model = json.loads(model_file.read_text(encoding="utf-8"))
    builder = Builder(spec, model, args.prefix)

    slug = re.sub(r"[^A-Za-z0-9]+", "", spec["meta"]["name"]) or "Report"
    out_dir = Path(args.out) if args.out else BUILD_DIR / "powerbi" / slug
    if not builder.errors:
        write_project(builder, out_dir, spec)
        print(f"Wrote {out_dir}")
        print(f"  {len(builder.included)} source tables + Date + Metrics")
        print(f"  {sum(len(c) for c in builder.columns.values())} columns "
              f"({len([1 for t in builder.columns for s in builder.columns[t].values() if s.get('isHidden')])} hidden)")
        print(f"  {len(builder.measures)} measures")
        active = sum(1 for r in builder.relationships if r["isActive"])
        print(f"  {len(builder.relationships)} relationships ({active} active)")
        print(f"  {sum(len(p['visuals']) for p in spec.get('pages') or [])} visuals across "
              f"{len(spec.get('pages') or [])} pages")
    if builder.notes:
        inactive = len([n for n in builder.notes if "USERELATIONSHIP" in n])
        skipped = len(builder.notes) - inactive
        if inactive:
            print(f"  {inactive} inactive role-playing relationship(s) — see model-report.json")
        if skipped:
            print(f"  {skipped} column(s) not loaded by design — see model-report.json")
    for warning in builder.warnings:
        print(f"  warning: {warning}")
    for error in builder.errors:
        print(f"  ERROR: {error}")

    if builder.errors:
        return 1
    if args.strict and builder.warnings:
        print("\nfailing because --strict was given and there are warnings")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # Output was piped into something that closed early, e.g. `| head`.
        os._exit(0)
