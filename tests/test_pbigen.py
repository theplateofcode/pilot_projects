"""Tests for tools/pbigen.py. Run with: python -m unittest discover -s tests"""
import json
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import pbigen  # noqa: E402


def attr(kind, **extra):
    out = {"type": kind, "required": False, "physical": {"dataverse": "Text (100)"}}
    out.update(extra)
    return out


def native(kind, physical):
    return {"type": kind, "required": True, "trait": "base", "physical": {"dataverse": physical}}


def entity(attributes, description="A table.", dataverse="custom"):
    return {
        "layer": "hr", "description": description, "ownership": "user",
        "volume": "low", "sensitivity": "internal",
        "platforms": {"dataverse": dataverse}, "primary": "name", "keys": [],
        "attributes": attributes,
    }


def model(entities, enums=None):
    return {"profile": "t", "layers": {"hr": "1.0.0"}, "entities": entities,
            "enums": enums or {}, "termSets": {}, "terminology": {}, "seed": {}}


BASE = {
    "id": native("guid", "<table>id (native primary key)"),
    "createdBy": native("lookup", "createdby (native → systemuser)"),
    "owner": {"type": "lookup", "target": "Employee", "trait": "owned",
              "physical": {"dataverse": "ownerid (native, user or team) + owningbusinessunit"}},
}


def spec(**overrides):
    out = {
        "meta": {"name": "T", "prefix": "wga", "storageMode": "import",
                 "environmentParameter": "DataverseEnvironment",
                 "defaultEnvironment": "x.crm.dynamics.com", "culture": "en-GB"},
        "tables": [], "hide": [], "dateTable": {"name": "Date", "from": "2022-01-01"},
        "measures": [], "pages": [],
    }
    out.update(overrides)
    return out


class TestTypeMapping(unittest.TestCase):
    def _columns(self, **attributes):
        m = model({"T": entity({**BASE, **attributes}), "Employee": entity({**BASE})})
        b = pbigen.Builder(spec(tables=["T", "Employee"]), m, "wga")
        return b.columns["T"], b

    def test_scalar_types_map_to_power_bi_types(self):
        cols, _ = self._columns(
            title=attr("string"), n=attr("integer"), d=attr("decimal"),
            paid=attr("boolean"), start=attr("date"), at=attr("datetime"),
            amount=attr("money"), pct=attr("percent"))
        self.assertEqual(cols["title"]["dataType"], "string")
        self.assertEqual(cols["n"]["dataType"], "int64")
        self.assertEqual(cols["d"]["dataType"], "double")
        self.assertEqual(cols["paid"]["dataType"], "boolean")
        self.assertEqual(cols["start"]["dataType"], "dateTime")
        self.assertEqual(cols["start"]["formatString"], "Short Date")
        self.assertEqual(cols["at"]["formatString"], "General Date")
        self.assertEqual(cols["amount"]["dataType"], "decimal")
        self.assertEqual(cols["pct"]["formatString"], "0.0%")

    def test_numbers_summarise_but_text_does_not(self):
        cols, _ = self._columns(n=attr("integer"), title=attr("string"))
        self.assertEqual(cols["n"]["summarizeBy"], "sum")
        self.assertEqual(cols["title"]["summarizeBy"], "none")

    def test_read_only_number_does_not_summarise(self):
        cols, _ = self._columns(calc=attr("decimal", readOnly=True))
        self.assertEqual(cols["calc"]["summarizeBy"], "none")

    def test_lookup_becomes_a_hidden_guid_and_a_visible_label(self):
        cols, _ = self._columns(employee=attr("lookup", target="Employee"))
        self.assertTrue(cols["employee"]["isHidden"])
        self.assertEqual(cols["employee"]["sourceColumn"], "wga_employee")
        self.assertFalse(cols["employeename"]["isHidden"])
        self.assertEqual(cols["employeename"]["sourceColumn"], "wga_employeename")

    def test_choice_becomes_a_hidden_int_and_a_visible_label(self):
        m = model({"T": entity({**BASE, "status": attr("choice", enum="S")})},
                  enums={"S": {"values": [{"code": 1, "key": "draft", "label": "Draft"},
                                          {"code": 2, "key": "done", "label": "Done"},
                                          {"code": 9, "key": "old", "label": "Old",
                                           "deprecated": True}]}})
        b = pbigen.Builder(spec(tables=["T"]), m, "wga")
        cols = b.columns["T"]
        self.assertTrue(cols["status"]["isHidden"])
        self.assertEqual(cols["statusname"]["sourceColumn"], "wga_statusname")
        self.assertIn("Draft, Done", cols["statusname"]["description"])
        self.assertNotIn("Old", cols["statusname"]["description"])

    def test_native_columns_use_their_dataverse_names(self):
        cols, _ = self._columns()
        self.assertEqual(cols["createdBy"]["sourceColumn"], "createdby")
        self.assertEqual(cols["owner"]["sourceColumn"], "ownerid")

    def test_primary_key_resolves_to_the_table_id(self):
        cols, _ = self._columns()
        self.assertEqual(cols["id"]["sourceColumn"], "wga_tid")

    def test_plumbing_traits_are_hidden(self):
        cols, _ = self._columns()
        self.assertTrue(cols["createdBy"]["isHidden"])
        self.assertTrue(cols["createdByname"]["isHidden"])
        self.assertTrue(cols["owner"]["isHidden"])

    def test_effective_dating_stays_visible(self):
        cols, _ = self._columns(
            validFrom={"type": "date", "trait": "effectiveDated",
                       "physical": {"dataverse": "Date only"}})
        self.assertFalse(cols["validFrom"]["isHidden"])

    def test_unsupported_types_are_skipped_with_a_warning(self):
        cols, b = self._columns(photo=attr("image"), regarding=attr("polylookup"))
        self.assertNotIn("photo", cols)
        self.assertNotIn("regarding", cols)
        self.assertTrue(any("polylookup" in w for w in b.warnings))

    def test_standard_table_reads_from_its_own_name(self):
        m = model({"Account": entity(dict(BASE), dataverse="standard:account")})
        b = pbigen.Builder(spec(tables=["Account"]), m, "wga")
        self.assertIn('Item="account"', b.tmdl_table("Account"))


class TestRelationships(unittest.TestCase):
    MODEL = model({
        "Employee": entity(dict(BASE)),
        "LeaveType": entity(dict(BASE)),
        "LeaveRequest": entity({
            **BASE,
            "employee": attr("lookup", target="Employee", required=True),
            "coverBy": attr("lookup", target="Employee"),
            "leaveType": attr("lookup", target="LeaveType", required=True),
            "startDate": attr("date", required=True),
        }),
    })

    def _build(self):
        return pbigen.Builder(
            spec(tables=["Employee", "LeaveType", "LeaveRequest"]), self.MODEL, "wga")

    def test_audit_columns_never_become_relationships(self):
        rels = self._build().relationships
        self.assertFalse([r for r in rels if r["fromColumn"] in ("createdBy", "owner")])

    def test_the_column_named_after_its_target_wins(self):
        rels = {r["fromColumn"]: r for r in self._build().relationships
                if r["toTable"] == "Employee"}
        self.assertTrue(rels["employee"]["isActive"])
        self.assertFalse(rels["coverBy"]["isActive"])

    def test_the_losing_path_is_explained(self):
        b = self._build()
        self.assertTrue(any("coverBy" in n and "USERELATIONSHIP" in n for n in b.notes))

    def test_only_one_active_path_per_table_pair(self):
        rels = self._build().relationships
        pairs = [(r["fromTable"], r["toTable"]) for r in rels if r["isActive"]]
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_a_date_column_joins_the_date_table(self):
        rels = [r for r in self._build().relationships if r["toTable"] == "Date"]
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0]["fromColumn"], "startDate")
        self.assertTrue(rels[0]["isActive"])

    def test_a_second_date_role_is_inactive(self):
        m = model({
            "Employee": entity(dict(BASE)),
            "Employment": entity({**BASE, "startDate": attr("date", required=True),
                                  "endDate": attr("date")}),
        })
        b = pbigen.Builder(spec(tables=["Employee", "Employment"]), m, "wga")
        rels = {r["fromColumn"]: r for r in b.relationships if r["toTable"] == "Date"}
        self.assertTrue(rels["startDate"]["isActive"])
        self.assertFalse(rels["endDate"]["isActive"])

    def test_a_lookup_to_a_table_not_loaded_is_left_out(self):
        m = model({"Employee": entity(dict(BASE)),
                   "T": entity({**BASE, "grade": attr("lookup", target="Grade")}),
                   "Grade": entity(dict(BASE))})
        b = pbigen.Builder(spec(tables=["Employee", "T"]), m, "wga")
        self.assertFalse([r for r in b.relationships if r["toTable"] == "Grade"])


class TestDaxValidation(unittest.TestCase):
    MODEL = model({
        "Employee": entity(dict(BASE)),
        "LeaveRequest": entity({**BASE, "duration": attr("decimal"),
                                "employee": attr("lookup", target="Employee")}),
    })

    def _errors(self, measures, pages=None):
        b = pbigen.Builder(
            spec(tables=["Employee", "LeaveRequest"], measures=measures, pages=pages or []),
            self.MODEL, "wga")
        return b.errors

    def test_a_good_measure_passes(self):
        self.assertEqual(
            self._errors([{"name": "Days", "expression": "SUM( 'LeaveRequest'[duration] )"}]), [])

    def test_a_misspelt_column_is_an_error(self):
        errors = self._errors([{"name": "D", "expression": "SUM( 'LeaveRequest'[durationn] )"}])
        self.assertTrue(any("durationn" in e for e in errors))

    def test_a_table_the_spec_does_not_load_is_an_error(self):
        errors = self._errors([{"name": "P", "expression": "SUM( 'Compensation'[amount] )"}])
        self.assertTrue(any("does not load" in e for e in errors))

    def test_a_misspelt_measure_reference_is_an_error(self):
        errors = self._errors([
            {"name": "Days", "expression": "SUM( 'LeaveRequest'[duration] )"},
            {"name": "Twice", "expression": "[Dayss] * 2"}])
        self.assertTrue(any("[Dayss]" in e for e in errors))

    def test_a_good_measure_reference_passes(self):
        self.assertEqual(self._errors([
            {"name": "Days", "expression": "SUM( 'LeaveRequest'[duration] )"},
            {"name": "Twice", "expression": "[Days] * 2"}]), [])

    def test_a_duplicate_measure_name_is_an_error(self):
        errors = self._errors([{"name": "D", "expression": "1"}, {"name": "D", "expression": "2"}])
        self.assertTrue(any("defined twice" in e for e in errors))

    def test_date_table_columns_resolve(self):
        self.assertEqual(self._errors([
            {"name": "M", "expression": "MAX( 'Date'[Year Month] )"}]), [])

    def test_a_date_column_that_does_not_exist_is_an_error(self):
        errors = self._errors([{"name": "M", "expression": "MAX( 'Date'[Weekday] )"}])
        self.assertTrue(any("Weekday" in e for e in errors))


class TestPageValidation(unittest.TestCase):
    MODEL = model({
        "Employee": entity({**BASE, "displayName": attr("string"),
                            "entraObjectId": attr("string")}),
        "OrgUnit": entity({**BASE, "name": attr("string")}),
    })

    def _errors(self, pages, hide=None):
        b = pbigen.Builder(
            spec(tables=["Employee", "OrgUnit"], hide=hide or [],
                 measures=[{"name": "Headcount", "expression": "COUNTROWS( 'Employee' )"}],
                 pages=pages),
            self.MODEL, "wga")
        return b.errors

    def test_a_good_visual_passes(self):
        self.assertEqual(self._errors([{"name": "P", "visuals": [
            {"type": "barHorizontal", "title": "By dept", "measures": ["Headcount"],
             "axis": "OrgUnit.name"}]}]), [])

    def test_an_unknown_measure_on_a_visual_is_an_error(self):
        errors = self._errors([{"name": "P", "visuals": [
            {"type": "statTile", "title": "T", "measure": "Nope"}]}])
        self.assertTrue(any("'Nope' is not a measure" in e for e in errors))

    def test_an_unknown_axis_column_is_an_error(self):
        errors = self._errors([{"name": "P", "visuals": [
            {"type": "barHorizontal", "title": "T", "measures": ["Headcount"],
             "axis": "OrgUnit.nmae"}]}])
        self.assertTrue(any("OrgUnit.nmae" in e for e in errors))

    def test_a_hidden_column_cannot_be_put_on_a_visual(self):
        errors = self._errors(
            [{"name": "P", "visuals": [
                {"type": "barHorizontal", "title": "T", "measures": ["Headcount"],
                 "axis": "Employee.entraObjectId"}]}],
            hide=["Employee.entraObjectId"])
        self.assertTrue(any("is hidden" in e for e in errors))

    def test_a_field_without_a_table_is_an_error(self):
        errors = self._errors([{"name": "P", "visuals": [
            {"type": "table", "title": "T", "columns": ["displayName"]}]}])
        self.assertTrue(any("Table.Column" in e for e in errors))

    def test_hiding_an_unknown_column_is_an_error(self):
        errors = self._errors([], hide=["Employee.ghost"])
        self.assertTrue(any("Employee.ghost" in e for e in errors))

    def test_two_measures_of_different_format_on_one_axis_warn(self):
        b = pbigen.Builder(
            spec(tables=["Employee", "OrgUnit"],
                 measures=[{"name": "Count", "expression": "1", "format": "#,0"},
                           {"name": "Rate", "expression": "0.5", "format": "0.0%"}],
                 pages=[{"name": "P", "visuals": [
                     {"type": "barClustered", "title": "Mixed",
                      "measures": ["Count", "Rate"], "axis": "OrgUnit.name"}]}]),
            self.MODEL, "wga")
        self.assertTrue(any("two scales on one chart" in w for w in b.warnings))

    def test_two_measures_of_the_same_format_do_not_warn(self):
        b = pbigen.Builder(
            spec(tables=["Employee", "OrgUnit"],
                 measures=[{"name": "A", "expression": "1", "format": "#,0"},
                           {"name": "B", "expression": "2", "format": "#,0"}],
                 pages=[{"name": "P", "visuals": [
                     {"type": "barClustered", "title": "Same",
                      "measures": ["A", "B"], "axis": "OrgUnit.name"}]}]),
            self.MODEL, "wga")
        self.assertFalse(any("two scales" in w for w in b.warnings))


class TestTmdl(unittest.TestCase):
    MODEL = model({"Employee": entity({**BASE, "displayName": attr("string")},
                                      description="A person.")})

    def _builder(self, **kw):
        return pbigen.Builder(spec(tables=["Employee"], **kw), self.MODEL, "wga")

    def test_table_tmdl_has_a_partition_and_the_parameter(self):
        text = self._builder().tmdl_table("Employee")
        self.assertIn("table Employee", text)
        self.assertIn("partition Employee = m", text)
        self.assertIn("CommonDataService.Database(DataverseEnvironment", text)
        self.assertIn('Item="wga_employee"', text)

    def test_table_description_becomes_a_tmdl_comment(self):
        self.assertIn("/// A person.", self._builder().tmdl_table("Employee"))

    def test_measures_live_on_the_metrics_table(self):
        b = self._builder(measures=[{"name": "Headcount", "format": "#,0",
                                     "folder": "Headcount",
                                     "expression": "COUNTROWS( 'Employee' )"}])
        metrics = b.tmdl_metrics_table()
        self.assertIn("measure Headcount = COUNTROWS( 'Employee' )", metrics)
        self.assertIn("formatString: #,0", metrics)
        self.assertIn("displayFolder: Headcount", metrics)
        self.assertNotIn("measure Headcount", b.tmdl_table("Employee"))

    def test_a_multiline_measure_is_wrapped_in_a_tmdl_code_block(self):
        b = self._builder(measures=[{"name": "M", "expression": "VAR a = 1\nRETURN a"}])
        text = b.tmdl_metrics_table()
        self.assertIn("measure M = ```", text)
        self.assertIn("RETURN a", text)

    def test_a_name_with_spaces_is_quoted(self):
        b = self._builder(measures=[{"name": "Attrition %", "expression": "1"}])
        self.assertIn("measure 'Attrition %' = 1", b.tmdl_metrics_table())

    def test_date_table_is_calculated_and_marked_as_a_date_table(self):
        text = self._builder().tmdl_date_table()
        self.assertIn("dataCategory: Time", text)
        self.assertIn("partition Date = calculated", text)
        self.assertIn("CALENDAR( Start, End )", text)

    def test_fiscal_year_offset_follows_the_year_end_month(self):
        b = pbigen.Builder(
            spec(tables=["Employee"],
                 dateTable={"name": "Date", "from": "2022-01-01", "yearEndMonth": 3}),
            self.MODEL, "wga")
        self.assertIn("EDATE( [Date], 9 )", b.tmdl_date_table())

    def test_an_iso_string_start_date_works_as_well_as_a_yaml_date(self):
        import datetime as dt
        for value in ("2022-04-01", dt.date(2022, 4, 1)):
            b = pbigen.Builder(
                spec(tables=["Employee"], dateTable={"name": "Date", "from": value}),
                self.MODEL, "wga")
            self.assertIn("DATE(2022, 4, 1)", b.tmdl_date_table(), repr(value))

    def test_model_tmdl_lists_tables_and_relationships(self):
        text = self._builder().tmdl_model()
        self.assertIn("ref table Employee", text)
        self.assertIn("ref table Metrics", text)
        self.assertIn("ref table Date", text)
        self.assertIn("culture: en-GB", text)

    def test_inactive_relationships_are_marked_in_the_model(self):
        m = model({"Employee": entity(dict(BASE)),
                   "Employment": entity({**BASE, "startDate": attr("date"),
                                         "endDate": attr("date")})})
        b = pbigen.Builder(spec(tables=["Employee", "Employment"]), m, "wga")
        self.assertIn("isActive: false", b.tmdl_model())

    def test_escaping_leaves_plain_identifiers_alone(self):
        self.assertEqual(pbigen.Builder._escape("Employee"), "Employee")
        self.assertEqual(pbigen.Builder._escape("Year Month"), "'Year Month'")


class TestTheme(unittest.TestCase):
    def test_data_colours_are_the_validated_palette_in_order(self):
        colours = pbigen.theme(spec())["dataColors"]
        self.assertEqual(colours[:3], ["#2a78d6", "#eb6834", "#1baf7a"])
        self.assertEqual(len(colours), 8)

    def test_status_colours_are_separate_from_the_categorical_slots(self):
        t = pbigen.theme(spec())
        self.assertEqual(t["good"], "#008300")
        self.assertEqual(t["bad"], "#e34948")

    def test_bars_carry_data_labels_so_identity_is_not_colour_alone(self):
        t = pbigen.theme(spec())
        for visual in ("barChart", "clusteredBarChart", "columnChart"):
            self.assertTrue(t["visualStyles"][visual]["*"]["labels"][0]["show"], visual)

    def test_lines_do_not_label_every_point(self):
        t = pbigen.theme(spec())
        self.assertFalse(t["visualStyles"]["lineChart"]["*"]["labels"][0]["show"])
        self.assertEqual(
            t["visualStyles"]["lineChart"]["*"]["lineStyles"][0]["strokeWidth"], 2)

    def test_vertical_gridlines_are_off_and_horizontal_are_recessive(self):
        t = pbigen.theme(spec())["visualStyles"]["*"]["*"]
        self.assertFalse(t["categoryAxis"][0]["gridlineShow"])
        self.assertEqual(t["valueAxis"][0]["gridlineColor"]["solid"]["color"], "#e8e8e4")

    def test_theme_is_json_serialisable(self):
        json.dumps(pbigen.theme(spec()))


class TestRealDashboard(unittest.TestCase):
    """The committed dashboard spec must be valid against the real HR model."""

    @classmethod
    def setUpClass(cls):
        model_file = ROOT / "build" / "hr-starter.model.json"
        if not model_file.exists():
            raise unittest.SkipTest("run: python tools/schematool.py export --profile hr-starter")
        cls.spec = yaml.safe_load(
            (ROOT / "powerbi" / "hr-dashboard.yaml").read_text(encoding="utf-8"))
        cls.b = pbigen.Builder(cls.spec, json.loads(model_file.read_text(encoding="utf-8")), "wga")

    def test_no_errors(self):
        self.assertEqual(self.b.errors, [])

    def test_compensation_is_not_in_the_dataset_at_all(self):
        """The Phase 1 exit criterion has to hold in Power BI too."""
        self.assertNotIn("Compensation", self.b.columns)
        for measure in self.b.measures:
            self.assertNotIn("Compensation", measure["expression"], measure["name"])

    def test_pay_ranges_and_credential_numbers_are_hidden(self):
        for reference in ("JobRequisition.salaryMin", "JobRequisition.salaryMax",
                          "EmployeeCredential.credentialNumber"):
            table, _, column = reference.partition(".")
            self.assertTrue(self.b.columns[table][column]["isHidden"], reference)

    def test_every_measure_has_a_description_and_a_format(self):
        for measure in self.b.measures:
            self.assertTrue(measure["description"], measure["name"])
            self.assertTrue(measure["formatString"], measure["name"])

    def test_the_headcount_measure_respects_effective_dating(self):
        headcount = next(m for m in self.b.measures if m["name"] == "Headcount")
        self.assertIn("validTo", headcount["expression"])
        self.assertIn("isPrimary", headcount["expression"])

    def test_every_relationship_points_at_columns_that_exist(self):
        """A relationship naming a column the model does not have breaks the load."""
        for rel in self.b.relationships:
            self.assertIn(rel["fromColumn"], self.b.columns[rel["fromTable"]], rel["name"])
            self.assertIn(rel["toColumn"], self.b.columns[rel["toTable"]], rel["name"])

    def test_every_table_has_its_primary_key_column(self):
        for table in self.b.included:
            self.assertIn("id", self.b.columns[table], table)
            self.assertTrue(
                self.b.columns[table]["id"]["sourceColumn"].endswith("id"), table)

    def test_exactly_one_active_path_between_any_pair_of_tables(self):
        pairs = [(r["fromTable"], r["toTable"]) for r in self.b.relationships if r["isActive"]]
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_no_visual_uses_more_than_three_series(self):
        """Theme slots 1-3 are the ones validated under --pairs all."""
        for page in self.spec["pages"]:
            for visual in page["visuals"]:
                self.assertLessEqual(len(visual.get("measures") or []), 3,
                                     f"{page['name']}/{visual['title']}")

    def test_every_page_has_at_least_one_stat_tile(self):
        for page in self.spec["pages"]:
            types = [v.get("type") for v in page["visuals"]]
            self.assertIn("statTile", types, page["name"])


if __name__ == "__main__":
    unittest.main()
