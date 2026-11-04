"""Tests for tools/sqlgen.py. Run with: python -m unittest discover -s tests

The strongest test here does not inspect strings: it transpiles the generated
T-SQL to SQLite and executes it, so the DDL has to be real. That proves the
statement order works, every column name is consistent between the table and
its indexes, and every foreign key resolves — none of which a string assertion
would catch.
"""
import json
import re
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import sqlgen  # noqa: E402

MODEL_FILE = ROOT / "build" / "hr-starter.model.json"

try:
    import sqlglot
except ImportError:                     # pragma: no cover
    sqlglot = None


def attr(kind, **extra):
    out = {"type": kind, "required": False, "physical": {"sql": "nvarchar(100)"}}
    out.update(extra)
    return out


BASE = {
    "id": {"type": "guid", "required": True, "trait": "base",
           "physical": {"sql": "uniqueidentifier PK (NEWSEQUENTIALID)"}},
    "createdOn": {"type": "datetime", "required": True, "trait": "base",
                  "physical": {"sql": "datetime2 (UTC)"}},
    "rowVersion": {"type": "integer", "required": True, "trait": "base",
                   "physical": {"sql": "rowversion"}},
}


def entity(attributes=None, layer="hr", description="A table.", sql="table"):
    return {
        "layer": layer, "description": description, "ownership": "user",
        "volume": "low", "sensitivity": "internal",
        "platforms": {"sql": sql, "dataverse": "custom"},
        "primary": "name", "keys": [],
        "attributes": {**BASE, **(attributes or {"name": attr("string", required=True)})},
    }


def model(entities, enums=None, seed=None):
    return {"profile": "t", "layers": {"hr": "1.0.0"}, "entities": entities,
            "enums": enums or {}, "termSets": {}, "terminology": {}, "seed": seed or {}}


class TestIdentifiers(unittest.TestCase):
    def test_identifiers_are_bracket_quoted(self):
        self.assertEqual(sqlgen.quote("Leave Request"), "[Leave Request]")

    def test_a_closing_bracket_is_rejected_rather_than_escaped(self):
        with self.assertRaises(sqlgen.SqlError):
            sqlgen.quote("bad]name")

    def test_text_literals_are_unicode_and_escape_quotes(self):
        self.assertEqual(sqlgen.literal("O'Brien"), "N'O''Brien'")

    def test_booleans_become_bits(self):
        self.assertEqual(sqlgen.literal(True), "1")
        self.assertEqual(sqlgen.literal(False), "0")

    def test_none_becomes_null(self):
        self.assertEqual(sqlgen.literal(None), "NULL")

    def test_an_over_long_name_is_reported(self):
        g = sqlgen.Generator(model({"A" * 200: entity()}))
        self.assertTrue(any("exceeds 128" in e for e in g.errors))


class TestSchemas(unittest.TestCase):
    def test_one_schema_per_layer_by_default(self):
        g = sqlgen.Generator(model({"Employee": entity(layer="core"),
                                    "LeaveRequest": entity(layer="hr")}))
        self.assertEqual(g.schemas(), ["core", "hr", "ref"])
        self.assertEqual(g.ref("LeaveRequest"), "[hr].[LeaveRequest]")

    def test_flat_puts_everything_in_dbo(self):
        g = sqlgen.Generator(model({"Employee": entity(layer="core")}), flat=True)
        self.assertEqual(g.schemas(), ["dbo"])
        self.assertEqual(g.ref("Employee"), "[dbo].[Employee]")

    def test_a_table_marked_none_is_not_created(self):
        g = sqlgen.Generator(model({"Gone": entity(sql="none"), "Kept": entity()}))
        self.assertEqual(g.tables, ["Kept"])


class TestTypes(unittest.TestCase):
    def _sql(self, **attributes):
        g = sqlgen.Generator(model({"T": entity({"name": attr("string", required=True),
                                                 **attributes})}))
        return g.sql_tables(), g

    def test_decimal_uses_the_model_precision_and_scale(self):
        text, _ = self._sql(fte=attr("decimal", precision=4, scale=2))
        self.assertIn("[fte] decimal(4,2)", text)

    def test_scale_greater_than_precision_is_an_error(self):
        _, g = self._sql(bad=attr("decimal", precision=2, scale=5))
        self.assertTrue(any("exceeds precision" in e for e in g.errors))

    def test_a_small_choice_uses_tinyint_and_a_large_one_int(self):
        g = sqlgen.Generator(model(
            {"T": entity({"name": attr("string", required=True),
                          "small": attr("choice", enum="Small"),
                          "big": attr("choice", enum="Big")})},
            enums={"Small": {"values": [{"code": 1, "key": "a"}]},
                   "Big": {"values": [{"code": 900, "key": "b"}]}}))
        text = g.sql_tables()
        self.assertIn("[small] tinyint", text)
        self.assertIn("[big] int", text)

    def test_long_text_becomes_nvarchar_max(self):
        text, _ = self._sql(note=attr("text"))
        self.assertIn("[note] nvarchar(max)", text)

    def test_money_is_decimal_19_4(self):
        text, _ = self._sql(amount=attr("money"))
        self.assertIn("[amount] decimal(19,4)", text)

    def test_rowversion_is_emitted_as_the_sql_type(self):
        text, _ = self._sql()
        self.assertIn("[rowVersion] rowversion NOT NULL", text)

    def test_the_primary_key_defaults_to_newsequentialid(self):
        text, _ = self._sql()
        self.assertIn("DEFAULT (NEWSEQUENTIALID())", text)
        self.assertIn("CONSTRAINT [PK_T] PRIMARY KEY ([id])", text)

    def test_created_on_defaults_to_utc_now(self):
        text, _ = self._sql()
        self.assertIn("[createdOn] datetime2(3) NOT NULL CONSTRAINT "
                      "[DF_T_createdOn] DEFAULT (SYSUTCDATETIME())", text)

    def test_a_lookup_becomes_an_id_column(self):
        g = sqlgen.Generator(model({"Employee": entity(), "T": entity(
            {"name": attr("string", required=True),
             "employee": attr("lookup", target="Employee", required=True)})}))
        self.assertIn("[employeeId] uniqueidentifier NOT NULL", g.sql_tables())

    def test_a_read_only_required_column_is_nullable(self):
        """A server-composed label cannot be supplied on insert."""
        text, _ = self._sql(label=attr("string", required=True, readOnly=True))
        self.assertIn("[label] nvarchar(100) NULL", text)

    def test_an_unmapped_type_is_reported(self):
        _, g = self._sql(mystery=attr("wormhole"))
        self.assertTrue(any("no SQL type" in w for w in g.warnings))

    def test_an_address_expands_to_parts_plus_a_country_key(self):
        g = sqlgen.Generator(model({"Country": entity(), "T": entity(
            {"name": attr("string", required=True), "home": attr("address")})}))
        text = g.sql_tables()
        self.assertIn("[homeLine1] nvarchar(250) NULL", text)
        self.assertIn("[homeCountryId] uniqueidentifier NULL", text)
        self.assertTrue(any(fk["column"] == "homeCountryId" for fk in g.foreign_keys))


class TestConstraints(unittest.TestCase):
    def test_a_choice_default_resolves_to_its_code(self):
        g = sqlgen.Generator(model(
            {"T": entity({"name": attr("string", required=True),
                          "status": attr("choice", enum="S", default="submitted")})},
            enums={"S": {"values": [{"code": 1, "key": "draft"},
                                    {"code": 5, "key": "submitted"}]}}))
        self.assertIn("DEFAULT (5)", g.sql_tables())

    def test_a_default_that_is_not_an_enum_value_is_reported(self):
        g = sqlgen.Generator(model(
            {"T": entity({"name": attr("string", required=True),
                          "status": attr("choice", enum="S", default="ghost")})},
            enums={"S": {"values": [{"code": 1, "key": "draft"}]}}))
        g.sql_tables()
        self.assertTrue(any("is not a value of" in w for w in g.warnings))

    def test_min_and_max_become_a_check_constraint(self):
        g = sqlgen.Generator(model({"T": entity(
            {"name": attr("string", required=True),
             "month": attr("integer", min=1, max=12)})}))
        text = g.sql_tables()
        self.assertIn("CONSTRAINT [CK_T_month] CHECK ([month] IS NULL OR "
                      "([month] >= 1 AND [month] <= 12))", text)

    def test_a_percent_is_bounded_without_being_asked(self):
        g = sqlgen.Generator(model({"T": entity(
            {"name": attr("string", required=True), "pct": attr("percent")})}))
        self.assertIn("[pct] >= 0 AND [pct] <= 100", g.sql_tables())

    def test_effective_dating_checks_the_range(self):
        g = sqlgen.Generator(model({"T": entity(
            {"name": attr("string", required=True),
             "validFrom": attr("date", required=True), "validTo": attr("date")})}))
        self.assertIn("CONSTRAINT [CK_T_validRange] CHECK "
                      "([validTo] IS NULL OR [validTo] >= [validFrom])", g.sql_tables())

    def test_money_requires_a_currency_on_the_row(self):
        g = sqlgen.Generator(model({"Currency": entity(), "T": entity(
            {"name": attr("string", required=True), "amount": attr("money"),
             "currency": attr("lookup", target="Currency")})}))
        self.assertIn("CONSTRAINT [CK_T_currency] CHECK (NOT ([amount] IS NOT NULL) "
                      "OR [currencyId] IS NOT NULL)", g.sql_tables())

    def test_a_polylookup_keeps_its_pair_coherent(self):
        g = sqlgen.Generator(model({"T": entity(
            {"name": attr("string", required=True),
             "regarding": attr("polylookup", targets=["*"], required=True)})}))
        text = g.sql_tables()
        self.assertIn("[regardingTable] nvarchar(128) NOT NULL", text)
        self.assertIn("[regardingId] uniqueidentifier NOT NULL", text)
        self.assertIn("CK_T_regarding_pair", text)
        self.assertTrue(any("no foreign key can be declared" in n for n in g.notes))


class TestForeignKeys(unittest.TestCase):
    def test_a_required_lookup_restricts_delete_and_optional_sets_null(self):
        g = sqlgen.Generator(model({"Employee": entity(), "T": entity(
            {"name": attr("string", required=True),
             "must": attr("lookup", target="Employee", required=True),
             "may": attr("lookup", target="Employee")})}))
        g.sql_tables()
        text = g.sql_foreign_keys()
        self.assertIn("FOREIGN KEY ([mustId]) REFERENCES [hr].[Employee] ([id]) "
                      "ON DELETE NO ACTION", text)
        self.assertIn("FOREIGN KEY ([mayId]) REFERENCES [hr].[Employee] ([id]) "
                      "ON DELETE SET NULL", text)

    def test_a_parent_cascades(self):
        g = sqlgen.Generator(model({"Request": entity(), "Step": entity(
            {"name": attr("string", required=True),
             "request": attr("parent", target="Request", required=True)})}))
        g.sql_tables()
        self.assertIn("ON DELETE CASCADE", g.sql_foreign_keys())

    def test_a_self_reference_stays_no_action(self):
        """SQL Server rejects a cascade cycle."""
        g = sqlgen.Generator(model({"OrgUnit": entity(
            {"name": attr("string", required=True),
             "parentOrgUnit": attr("lookup", target="OrgUnit")})}))
        g.sql_tables()
        text = g.sql_foreign_keys()
        self.assertIn("FOREIGN KEY ([parentOrgUnitId])", text)
        self.assertIn("ON DELETE NO ACTION", text)
        self.assertTrue(any("references its own table" in n for n in g.notes))

    def test_a_choice_points_at_its_reference_table(self):
        g = sqlgen.Generator(model(
            {"T": entity({"name": attr("string", required=True),
                          "status": attr("choice", enum="S")})},
            enums={"S": {"values": [{"code": 1, "key": "draft"}]}}))
        g.sql_tables()
        self.assertIn("FOREIGN KEY ([status]) REFERENCES [ref].[S] ([code])",
                      g.sql_foreign_keys())

    def test_a_lookup_to_a_table_with_no_sql_keeps_the_column_without_a_key(self):
        g = sqlgen.Generator(model({"Gone": entity(sql="none"), "T": entity(
            {"name": attr("string", required=True),
             "ref": attr("lookup", target="Gone")})}))
        text = g.sql_tables()
        self.assertIn("[refId] uniqueidentifier", text)
        self.assertTrue(any("no SQL table" in w for w in g.warnings))

    def test_a_multilookup_becomes_a_junction_table(self):
        g = sqlgen.Generator(model({"OrgUnit": entity(), "T": entity(
            {"name": attr("string", required=True),
             "units": attr("multilookup", target="OrgUnit")})}))
        text = g.sql_tables()
        self.assertIn("CREATE TABLE [hr].[T_Units] (", text)
        self.assertIn("PRIMARY KEY ([TId], [OrgUnitId])", text)


class TestReferenceTables(unittest.TestCase):
    def test_only_enums_in_use_get_a_table(self):
        g = sqlgen.Generator(model(
            {"T": entity({"name": attr("string", required=True),
                          "status": attr("choice", enum="Used")})},
            enums={"Used": {"values": [{"code": 1, "key": "a", "label": "A"}]},
                   "Unused": {"values": [{"code": 1, "key": "b"}]}}))
        text = g.sql_reference()
        self.assertIn("[ref].[Used]", text)
        self.assertNotIn("[ref].[Unused]", text)

    def test_a_deprecated_value_is_kept_and_flagged(self):
        """Codes are permanent, so the row stays and the flag carries the news."""
        g = sqlgen.Generator(model(
            {"T": entity({"name": attr("string", required=True),
                          "status": attr("choice", enum="S")})},
            enums={"S": {"values": [{"code": 1, "key": "a", "label": "A"},
                                    {"code": 9, "key": "old", "label": "Old",
                                     "deprecated": True}]}}))
        text = g.sql_reference()
        self.assertIn("N'old'", text)
        self.assertRegex(text, r"N'old', N'Old', 1")

    def test_a_missing_enum_is_an_error(self):
        g = sqlgen.Generator(model({"T": entity(
            {"name": attr("string", required=True), "status": attr("choice", enum="Nope")})}))
        g.sql_reference()
        self.assertTrue(any("not defined" in e for e in g.errors))


class TestIndexes(unittest.TestCase):
    def test_a_declared_key_becomes_a_filtered_unique_index(self):
        spec = entity({"name": attr("string", required=True),
                       "workEmail": attr("email", required=True)})
        spec["keys"] = [["workEmail"]]
        g = sqlgen.Generator(model({"Employee": spec}))
        text = g.sql_indexes()
        self.assertIn("CREATE UNIQUE INDEX [UQ_Employee_workEmail]", text)
        self.assertIn("WHERE [workEmail] IS NOT NULL", text)

    def test_a_unique_column_becomes_a_unique_index(self):
        g = sqlgen.Generator(model({"T": entity(
            {"name": attr("string", required=True), "code": attr("string", unique=True)})}))
        self.assertIn("CREATE UNIQUE INDEX [UQ_T_code]", g.sql_indexes())

    def test_every_lookup_is_indexed(self):
        g = sqlgen.Generator(model({"Employee": entity(), "T": entity(
            {"name": attr("string", required=True),
             "employee": attr("lookup", target="Employee")})}))
        self.assertIn("CREATE INDEX [IX_T_employee] ON [hr].[T] ([employeeId])",
                      g.sql_indexes())

    def test_a_parent_gives_one_current_row_per_parent(self):
        g = sqlgen.Generator(model({"Employee": entity(), "JobAssignment": entity(
            {"name": attr("string", required=True),
             "employee": attr("parent", target="Employee", required=True),
             "isPrimary": attr("boolean"),
             "validFrom": attr("date", required=True), "validTo": attr("date")})}))
        text = g.sql_indexes()
        self.assertIn("CREATE UNIQUE INDEX [UX_JobAssignment_current]", text)
        self.assertIn("WHERE [validTo] IS NULL AND [isPrimary] = 1", text)

    def test_effective_dating_without_a_parent_is_left_alone(self):
        """LeavePolicy is scoped by entity, country and employment type too."""
        g = sqlgen.Generator(model({"LeaveType": entity(), "LeavePolicy": entity(
            {"name": attr("string", required=True),
             "leaveType": attr("lookup", target="LeaveType", required=True),
             "validFrom": attr("date", required=True), "validTo": attr("date")})}))
        self.assertNotIn("UX_LeavePolicy_current", g.sql_indexes())
        self.assertTrue(any("no parent column" in n for n in g.notes))


class TestSeed(unittest.TestCase):
    def test_seed_rows_merge_on_their_natural_key(self):
        g = sqlgen.Generator(model(
            {"Currency": entity({"name": attr("string", required=True),
                                 "isoCode": attr("string", unique=True),
                                 "symbol": attr("string")})},
            seed={"Currency": [{"isoCode": "GBP", "name": "Pound", "symbol": "£"}]}))
        text = g.sql_seed()
        self.assertIn("MERGE [hr].[Currency] AS t", text)
        self.assertIn("WHEN MATCHED THEN UPDATE SET", text)
        self.assertIn("N'GBP'", text)

    def test_a_seed_lookup_resolves_by_subquery(self):
        g = sqlgen.Generator(model(
            {"JobFamily": entity({"name": attr("string", required=True),
                                  "code": attr("string", unique=True)}),
             "JobProfile": entity({"name": attr("string", required=True),
                                   "code": attr("string", unique=True),
                                   "jobFamily": attr("lookup", target="JobFamily")})},
            seed={"JobProfile": [{"code": "P1", "name": "Profile", "jobFamily": "DELIVERY"}]}))
        self.assertIn("(SELECT [id] FROM [hr].[JobFamily] WHERE [code] = N'DELIVERY')",
                      g.sql_seed())

    def test_a_seed_column_that_does_not_exist_is_reported(self):
        g = sqlgen.Generator(model(
            {"Currency": entity({"name": attr("string", required=True),
                                 "code": attr("string", unique=True)})},
            seed={"Currency": [{"code": "GBP", "name": "Pound", "colour": "green"}]}))
        g.sql_seed()
        self.assertTrue(any("'colour' is not a column" in w for w in g.warnings))

    def test_seed_order_puts_a_lookup_target_first(self):
        g = sqlgen.Generator(model(
            {"JobFamily": entity({"name": attr("string", required=True),
                                  "code": attr("string")}),
             "JobProfile": entity({"name": attr("string", required=True),
                                   "code": attr("string"),
                                   "jobFamily": attr("lookup", target="JobFamily")})},
            seed={"JobProfile": [{"code": "P1", "name": "P"}],
                  "JobFamily": [{"code": "F1", "name": "F"}]}))
        order = g._seed_order()
        self.assertLess(order.index("JobFamily"), order.index("JobProfile"))


class TestViews(unittest.TestCase):
    def test_an_effective_dated_table_gets_a_current_view(self):
        g = sqlgen.Generator(model({"T": entity(
            {"name": attr("string", required=True),
             "validFrom": attr("date", required=True), "validTo": attr("date")})}))
        text = g.sql_views()
        self.assertIn("CREATE OR ALTER VIEW [hr].[TCurrent]", text)
        self.assertIn("[validTo] IS NULL", text)

    def test_a_plain_table_gets_no_view(self):
        g = sqlgen.Generator(model({"T": entity()}))
        self.assertIn("No effective-dated tables", g.sql_views())


@unittest.skipIf(sqlglot is None, "sqlglot is not installed")
class TestGeneratedSqlActuallyRuns(unittest.TestCase):
    """Transpile the real output to SQLite and execute it.

    SQLite is not the target, but it is a real parser and executor. If the DDL
    runs there, the statement order works, the column names agree between each
    table and its indexes, and nothing references something that does not exist.
    """

    GUARD = re.compile(r"^IF\s+(OBJECT_ID|INDEXPROPERTY|SCHEMA_ID)\b.*?\bIS NULL\s*",
                       re.I | re.S)

    @classmethod
    def setUpClass(cls):
        if not MODEL_FILE.exists():
            raise unittest.SkipTest("run: python tools/schematool.py export --profile hr-starter")
        model_json = json.loads(MODEL_FILE.read_text(encoding="utf-8"))
        cls.plan = sqlgen.Generator(model_json, flat=True).build()
        if cls.plan["errors"]:
            raise AssertionError(cls.plan["errors"])

        cls.con = sqlite3.connect(":memory:")
        cls.failures: dict[str, list[str]] = {}
        for name in ("002_reference.sql", "003_tables.sql", "005_indexes.sql"):
            cls.failures[name] = cls._run(cls.plan["files"][name])
        cls.tables = {r[0] for r in cls.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        cls.columns = {t: {r[1] for r in cls.con.execute(f'PRAGMA table_info("{t}")')}
                       for t in cls.tables}

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "con"):
            cls.con.close()

    @classmethod
    def _statements(cls, text: str) -> list[str]:
        body = "\n".join(l for l in text.splitlines() if not l.strip().startswith("--"))
        return [s.strip() for s in body.split(";\n") if s.strip()]

    @classmethod
    def _desugar(cls, statement: str) -> str:
        """Make T-SQL runnable on SQLite without changing what it declares."""
        statement = cls.GUARD.sub("", statement)
        statement = statement.replace("[dbo].", "")                  # no schemas
        statement = re.sub(r"\bnvarchar\(max\)", "TEXT", statement, flags=re.I)
        statement = re.sub(r"\browversion\b", "INTEGER", statement, flags=re.I)
        statement = statement.replace("NEWSEQUENTIALID()", "NULL")
        statement = statement.replace("SYSUTCDATETIME()", "CURRENT_TIMESTAMP")
        return statement

    @classmethod
    def _run(cls, text: str) -> list[str]:
        failures = []
        for statement in cls._statements(text):
            if re.match(r"^(EXEC|MERGE)\b", statement, re.I):
                continue                   # not DDL; covered by their own tests
            try:
                sql = sqlglot.transpile(cls._desugar(statement), read="tsql",
                                        write="sqlite")[0]
                cls.con.execute(sql)
            except Exception as exc:       # noqa: BLE001 — the message is the point
                failures.append(f"{statement[:100]!r}: {exc}")
        return failures

    def test_reference_tables_execute(self):
        self.assertEqual(self.failures["002_reference.sql"], [])

    def test_every_table_executes(self):
        self.assertEqual(self.failures["003_tables.sql"], [])
        self.assertGreaterEqual(len(self.tables), 50)

    def test_every_index_executes(self):
        """An index naming a column the table does not have fails here."""
        self.assertEqual(self.failures["005_indexes.sql"], [])

    def test_every_foreign_key_resolves(self):
        pattern = re.compile(
            r"ALTER TABLE \[(\w+)\]\.\[(\w+)\] ADD CONSTRAINT \[([^\]]+)\] "
            r"FOREIGN KEY \(\[([^\]]+)\]\) REFERENCES \[(\w+)\]\.\[(\w+)\] \(\[(\w+)\]\)")
        broken, checked = [], 0
        for statement in self._statements(self.plan["files"]["004_foreign_keys.sql"]):
            match = pattern.search(statement)
            if not match:
                continue
            checked += 1
            _, source, name, source_column, _, target, target_column = match.groups()
            if source not in self.columns:
                broken.append(f"{name}: no table {source}")
            elif source_column not in self.columns[source]:
                broken.append(f"{name}: {source} has no column {source_column}")
            if target not in self.columns:
                broken.append(f"{name}: no target table {target}")
            elif target_column not in self.columns[target]:
                broken.append(f"{name}: {target} has no column {target_column}")
        self.assertEqual(broken, [])
        self.assertGreater(checked, 300)

    def test_every_view_executes(self):
        failures = []
        for statement in self._statements(self.plan["files"]["007_views.sql"]):
            sql = self._desugar(statement).replace("CREATE OR ALTER VIEW", "CREATE VIEW")
            try:
                self.con.execute(sqlglot.transpile(sql, read="tsql", write="sqlite")[0])
            except Exception as exc:       # noqa: BLE001
                failures.append(f"{statement[:80]!r}: {exc}")
        self.assertEqual(failures, [])

    def test_no_duplicate_constraint_names(self):
        self.assertEqual([e for e in self.plan["errors"] if "duplicate" in e], [])

    def test_the_real_profile_has_no_warnings(self):
        self.assertEqual(self.plan["warnings"], [])


class TestRealProfiles(unittest.TestCase):
    def test_every_profile_generates_without_errors(self):
        found = 0
        for model_file in sorted((ROOT / "build").glob("*.model.json")):
            found += 1
            plan = sqlgen.Generator(
                json.loads(model_file.read_text(encoding="utf-8"))).build()
            self.assertEqual(plan["errors"], [], model_file.name)
            self.assertEqual(plan["warnings"], [], model_file.name)
        if not found:
            self.skipTest("run: python tools/schematool.py export --profile hr-starter")


if __name__ == "__main__":
    unittest.main()
