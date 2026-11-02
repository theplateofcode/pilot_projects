"""Reference data (from the schema's own seeds) and demo data."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re

import yaml

from .client import DataverseError
from .model import Table
from .schema_steps import Deployer

# Natural-key columns, tried in order, when a reference points at a row by text.
NATURAL_KEYS = ["code", "externalId", "isoCode", "isoCode2", "isoCode3", "proposalNumber",
                "name", "displayName", "topic", "title"]

# Native lookups that accept several target tables: the bind name carries the table.
CUSTOMER_LOOKUPS = {"parentcustomerid", "customerid", "regardingobjectid"}

# Order reference data is loaded in: parents before children.
SEED_ORDER = ["Country", "Language", "Currency", "LeadSource", "SalesPipeline", "PipelineStage", "ServiceOffering"]

TODAY = dt.date.today()


def relative_dates(value):
    """Turn ``today+30`` / ``today-14`` into real dates so demo data never looks stale."""
    if isinstance(value, str):
        match = re.fullmatch(r"today\s*([+-])\s*(\d+)", value.strip())
        if match:
            days = int(match.group(2)) * (1 if match.group(1) == "+" else -1)
            return (TODAY + dt.timedelta(days=days)).isoformat()
        if value.strip() == "today":
            return TODAY.isoformat()
    return value


class DataDeployer(Deployer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.aliases: dict[str, tuple[str, str]] = {
            key.split(":", 1)[1]: tuple(value)
            for key, value in (self.state.data or {}).items()
            if key.startswith("alias:") and isinstance(value, list) and len(value) == 2
        }
        self._natural: dict[str, dict[str, str]] = {}   # table logical -> {key value -> id}

    # ------------------------------------------------------------ plumbing
    def entity_set(self, table: Table) -> str:
        if table.entity_set:
            return table.entity_set
        self.refresh_table_metadata()
        return table.entity_set or f"{table.logical}s"

    def natural_key_column(self, table: Table) -> tuple[str, str] | None:
        for candidate in NATURAL_KEYS:
            logical = table.logical_of(candidate)
            if logical:
                return candidate, logical
        return None

    def find_by_natural_key(self, table: Table, value) -> str | None:
        """Row id for a reference written as text (a code, an ISO code or a name)."""
        if value is None:
            return None
        text = str(value)
        if text.startswith("@"):
            alias = self.aliases.get(text[1:])
            return alias[1] if alias else None
        cache = self._natural.setdefault(table.logical, {})
        if text in cache:
            return cache[text]
        if self.dv.dry_run:
            return "00000000-0000-0000-0000-000000000000"
        natural = self.natural_key_column(table)
        primary_id = f"{table.logical}id"
        candidates = []
        if natural:
            candidates.append(natural[1])
        for extra in ("name", "fullname", "currencyname", "isocurrencycode"):
            logical = extra if not table.is_custom else None
            if logical and logical not in candidates:
                candidates.append(logical)
        if table.is_custom and table.primary_logical not in candidates:
            candidates.append(table.primary_logical)
        for column in candidates:
            escaped = text.replace("'", "''")
            try:
                row = self.dv.first(
                    f"{self.entity_set(table)}?$select={primary_id}&$filter={column} eq '{escaped}'"
                )
            except DataverseError:
                continue
            if row:
                cache[text] = row[primary_id]
                return row[primary_id]
        return None

    def build_body(self, table: Table, record: dict) -> dict | None:
        body: dict = {}
        for key, raw in record.items():
            if key.startswith("_"):
                continue
            value = relative_dates(raw)
            column = table.column(key)
            if column is None:
                if re.fullmatch(r"[a-z0-9_]+", key):
                    body[key] = value           # a native column written by logical name
                else:
                    self.log(f"  ! {table.name}: no column '{key}' — value dropped")
                continue
            if column.kind == "manytomany":
                continue
            if column.kind in ("lookup",) or (column.kind == "native" and column.spec.get("type") == "lookup"):
                target_name = column.spec.get("target") or ("Term" if column.spec.get("type") == "term" else None)
                if column.spec.get("type") == "term":
                    target_name = "Term"
                target = self.model.tables.get(target_name or "")
                if target is None:
                    continue
                row_id = self.find_by_natural_key(target, value)
                if not row_id:
                    self.log(f"  ! {table.name}.{key}: cannot find {target_name} '{value}' — left empty")
                    continue
                bind = column.logical
                if bind in CUSTOMER_LOOKUPS:
                    bind = f"{bind}_{target.logical}"
                body[f"{bind}@odata.bind"] = f"/{self.entity_set(target)}({row_id})"
                continue
            if column.spec.get("type") in ("choice", "multichoice"):
                enum_name = column.spec["enum"]
                if isinstance(value, list):
                    codes = [self.model.enum_value(enum_name, v) for v in value]
                    body[column.logical] = ",".join(str(c) for c in codes if c is not None)
                else:
                    code = self.model.enum_value(enum_name, value)
                    if code is None:
                        self.log(f"  ! {table.name}.{key}: unknown choice '{value}'")
                        continue
                    body[column.logical] = code
                continue
            body[column.logical] = value
        return body

    def label_for(self, table: Table, record: dict) -> str | None:
        """A readable primary name for tables whose label is normally generated."""
        parts = [str(record[k]) for k in ("_label",) if k in record]
        if parts:
            return parts[0]
        pieces = []
        for key, value in record.items():
            if key.startswith("_") or value in (None, ""):
                continue
            column = table.column(key)
            if column and column.kind == "lookup":
                pieces.append(str(value).lstrip("@").replace("_", " "))
            elif column and column.spec.get("type") == "choice":
                pieces.append(str(value))
        return " – ".join(pieces[:3]) if pieces else None

    def write_records(self, table_name: str, records: list[dict], source: str) -> None:
        table = self.model.tables.get(table_name)
        if table is None:
            self.log(f"  ! {source}: table '{table_name}' is not deployed — {len(records)} rows skipped")
            return
        if table_name == "Currency":
            self.write_currencies(records)
            return
        written = skipped = 0
        for record in records:
            alias = record.get("_alias") or self.fingerprint(table_name, record)
            if alias in self.aliases:
                skipped += 1
                continue
            key_value = self.dedupe_value(table, record)
            existing = (
                self.find_by_natural_key(table, key_value)
                if key_value and not self.dv.dry_run
                else None
            )
            if existing:
                self.remember_alias(alias, self.entity_set(table), existing)
                skipped += 1
                continue
            body = self.build_body(table, record)
            if table.is_custom and table.primary_logical not in body:
                label = self.label_for(table, record)
                if label:
                    body[table.primary_logical] = label[:200]
            with self.guard(f"{table_name} row {key_value or record.get('_alias') or ''}"):
                result = self.dv.post(self.entity_set(table), body)
                row_id = (result or {}).get("id")
                if row_id:
                    self.remember_alias(alias, self.entity_set(table), row_id)
                if key_value and row_id:
                    self._natural.setdefault(table.logical, {})[str(key_value)] = row_id
                written += 1
        self.count("data rows", written)
        self.log(f"  + {table_name}: {written} rows written, {skipped} already there")

    @staticmethod
    def fingerprint(table_name: str, record: dict) -> str:
        """Stable id for a demo row with no natural key, so re-runs do not duplicate it."""
        payload = json.dumps(
            {k: v for k, v in sorted(record.items()) if not k.startswith("_")},
            sort_keys=True, ensure_ascii=False, default=str,
        )
        return f"{table_name}#{hashlib.sha1(payload.encode('utf-8')).hexdigest()[:12]}"

    def dedupe_value(self, table: Table, record: dict):
        """The value that identifies this row again on a later run."""
        for candidate in NATURAL_KEYS:
            if candidate in record and table.logical_of(candidate):
                return record[candidate]
        primary = table.spec.get("primary")
        if primary and primary in record:
            return record[primary]
        return None

    def remember_alias(self, alias: str, entity_set: str, row_id: str) -> None:
        self.aliases[alias] = (entity_set, row_id)
        self.state.set(f"alias:{alias}", [entity_set, row_id])

    def write_currencies(self, records: list[dict]) -> None:
        """transactioncurrency is a standard table with its own column names."""
        written = skipped = 0
        for record in records:
            iso = record.get("isoCode") or record.get("isocurrencycode")
            if not iso:
                continue
            existing = None
            if not self.dv.dry_run:
                existing = self.dv.first(
                    f"transactioncurrencies?$select=transactioncurrencyid&$filter=isocurrencycode eq '{iso}'"
                )
            if existing:
                self._natural.setdefault("transactioncurrency", {})[iso] = existing["transactioncurrencyid"]
                skipped += 1
                continue
            with self.guard(f"currency {iso}"):
                result = self.dv.post(
                    "transactioncurrencies",
                    {
                        "currencyname": record.get("name", iso),
                        "isocurrencycode": iso,
                        "currencysymbol": record.get("symbol", iso),
                        "currencyprecision": int(record.get("decimalPlaces", 2)),
                        "exchangerate": float(record.get("exchangeRate", 1.0)),
                    },
                )
                row_id = (result or {}).get("id")
                if row_id:
                    self._natural.setdefault("transactioncurrency", {})[iso] = row_id
                written += 1
        self.count("data rows", written)
        self.log(f"  + Currency: {written} currencies written, {skipped} already there")

    # ------------------------------------------------------- reference data
    def load_reference_data(self) -> None:
        seeds = self.model.seed
        ordered = [name for name in SEED_ORDER if name in seeds]
        ordered += [name for name in seeds if name not in ordered]
        self.load_term_sets()
        for name in ordered:
            if name not in self.model.tables:
                continue
            self.write_records(name, seeds[name], "schema seed")

    def load_term_sets(self) -> None:
        if "TermSet" not in self.model.tables or "Term" not in self.model.tables:
            return
        term_sets = []
        terms = []
        for name, spec in self.model.term_sets.items():
            code = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).upper()
            term_sets.append(
                {
                    "code": code,
                    "name": re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name),
                    "description": spec.get("description", ""),
                    "isHierarchical": bool(spec.get("hierarchical")),
                    "isOpen": bool(spec.get("open")),
                }
            )
            for index, term in enumerate(spec.get("terms", []) or []):
                terms.append({"termSet": code, "name": term, "sortOrder": (index + 1) * 10, "isAvailableForTagging": True})
        self.write_records("TermSet", term_sets, "term sets")
        self.write_records("Term", terms, "terms")

    # ------------------------------------------------------------ demo data
    def load_demo_data(self, path: str) -> None:
        file_path = pathlib.Path(path)
        if not file_path.exists():
            self.log(f"  ! demo data file {file_path} not found")
            return
        document = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        for block in document.get("data", []) or []:
            table_name = block["table"]
            records = block.get("records", []) or []
            if table_name.startswith("native:"):
                self.write_native_records(table_name.split(":", 1)[1], records)
                continue
            self.write_records(table_name, records, str(file_path.name))

    def write_native_records(self, logical: str, records: list[dict]) -> None:
        """Activities (task, phone call, appointment) written straight to the native tables."""
        entity_sets = {
            "task": "tasks", "phonecall": "phonecalls", "appointment": "appointments",
            "email": "emails", "annotation": "annotations",
        }
        entity_set = entity_sets.get(logical, f"{logical}s")
        written = 0
        for record in records:
            body: dict = {}
            for key, raw in record.items():
                if key.startswith("_"):
                    continue
                value = relative_dates(raw)
                if key == "regarding":
                    target_name, _, reference = str(value).partition(":")
                    target = self.model.tables.get(target_name)
                    if not target:
                        continue
                    row_id = self.find_by_natural_key(target, reference)
                    if not row_id:
                        self.log(f"  ! activity regarding {value} not found — skipped")
                        body = {}
                        break
                    body[f"regardingobjectid_{target.logical}@odata.bind"] = (
                        f"/{self.entity_set(target)}({row_id})"
                    )
                    continue
                body[key] = value
            if not body:
                continue
            with self.guard(f"{logical} '{record.get('subject', '')}'"):
                self.dv.post(entity_set, body)
                written += 1
        self.count("data rows", written)
        self.log(f"  + {logical}: {written} rows written")
