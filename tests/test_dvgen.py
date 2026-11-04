"""Tests for tools/dvgen.py. Run with: python -m unittest discover -s tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import dvgen  # noqa: E402


def attrs(**overrides):
    """A base-trait set like the resolved model produces, plus overrides."""
    base = {
        "id": {"type": "guid", "required": True, "trait": "base",
               "physical": {"dataverse": "<table>id (native primary key)"}},
        "createdOn": {"type": "datetime", "required": True, "trait": "base",
                      "physical": {"dataverse": "createdon (native)"}},
        "owner": {"type": "lookup", "target": "Employee", "trait": "owned",
                  "physical": {"dataverse": "ownerid (native, user or team) + owningbusinessunit"}},
    }
    base.update(overrides)
    return base


def model(entities, enums=None, terminology=None):
    return {
        "profile": "test",
        "layers": {"core": "1.0.0"},
        "entities": entities,
        "enums": enums or {},
        "termSets": {},
        "terminology": terminology or {},
        "seed": {},
    }


def entity(description="A table.", **kwargs):
    out = {
        "layer": "core",
        "description": description,
        "ownership": "user",
        "volume": "low",
        "sensitivity": "internal",
        "platforms": {"dataverse": "custom", "sharepoint": "list", "sql": "table"},
        "primary": "name",
        "keys": [],
        "attributes": attrs(name={"type": "string", "length": 200, "required": True}),
    }
    out.update(kwargs)
    return out


class TestNaming(unittest.TestCase):
    def test_logical_name_lowercases_and_prefixes(self):
        self.assertEqual(dvgen.logical("wga", "LeaveRequest"), "wga_leaverequest")
        self.assertEqual(dvgen.logical("wga", "entraObjectId"), "wga_entraobjectid")

    def test_label_is_english_localized(self):
        lbl = dvgen.label("Leave Request")
        self.assertEqual(lbl["LocalizedLabels"][0]["Label"], "Leave Request")
        self.assertEqual(lbl["LocalizedLabels"][0]["LanguageCode"], 1033)

    def test_split_pascal(self):
        self.assertEqual(dvgen.split_pascal("LeaveRequest"), "Leave Request")


class TestTableMapping(unittest.TestCase):
    def test_standard_tables_are_not_created(self):
        m = model({
            "Account": entity(platforms={"dataverse": "standard:account"}),
            "LeaveRequest": entity(),
        })
        gen = dvgen.Generator(m, "wga")
        self.assertEqual(gen.table_names["Account"], "account")
        self.assertNotIn("Account", gen.created)
        self.assertIn("LeaveRequest", gen.created)

    def test_native_and_none_tables_are_not_created(self):
        m = model({
            "AuditEvent": entity(platforms={"dataverse": "native"}),
            "Gone": entity(platforms={"dataverse": "none"}),
        })
        gen = dvgen.Generator(m, "wga")
        self.assertEqual(gen.created, set())
        self.assertNotIn("Gone", gen.table_names)

    def test_ownership_maps_to_dataverse_ownership_type(self):
        m = model({"A": entity(ownership="user"), "B": entity(ownership="organization")})
        tables = {t["_name"]: t["payload"] for t in dvgen.Generator(m, "wga").tables()}
        self.assertEqual(tables["A"]["OwnershipType"], "UserOwned")
        self.assertEqual(tables["B"]["OwnershipType"], "OrganizationOwned")

    def test_sensitive_tables_get_auditing(self):
        m = model({"A": entity(sensitivity="internal"), "B": entity(sensitivity="restricted")})
        tables = {t["_name"]: t["payload"] for t in dvgen.Generator(m, "wga").tables()}
        self.assertFalse(tables["A"]["IsAuditEnabled"]["Value"])
        self.assertTrue(tables["B"]["IsAuditEnabled"]["Value"])

    def test_primary_column_is_created_with_the_table(self):
        m = model({"LeaveRequest": entity()})
        payload = dvgen.Generator(m, "wga").tables()[0]["payload"]
        primary = payload["Attributes"][0]
        self.assertTrue(primary["IsPrimaryName"])
        self.assertEqual(primary["LogicalName"], "wga_name")
        self.assertEqual(primary["MaxLength"], 200)

    def test_auto_primary_creates_a_read_only_name_column(self):
        m = model({"LeaveBalance": entity(primary="auto")})
        primary = dvgen.Generator(m, "wga").tables()[0]["payload"]["Attributes"][0]
        self.assertEqual(primary["LogicalName"], "wga_name")
        self.assertEqual(primary["MaxLength"], 400)

    def test_non_text_primary_is_reported(self):
        m = model({"X": entity(primary="count",
                               attributes=attrs(count={"type": "integer"}))})
        gen = dvgen.Generator(m, "wga")
        gen.tables()
        self.assertTrue(any("primary column" in w for w in gen.warnings))

    def test_long_table_name_is_reported(self):
        long_name = "A" * 60
        m = model({long_name: entity()})
        gen = dvgen.Generator(m, "wga")
        gen.tables()
        self.assertTrue(any("Dataverse allows 50" in w for w in gen.warnings))

    def test_terminology_overrides_the_display_name(self):
        m = model({"Project": entity()},
                  terminology={"Project": {"singular": "Matter", "plural": "Matters"}})
        payload = dvgen.Generator(m, "wga").tables()[0]["payload"]
        self.assertEqual(payload["DisplayName"]["LocalizedLabels"][0]["Label"], "Matter")
        self.assertEqual(payload["DisplayCollectionName"]["LocalizedLabels"][0]["Label"], "Matters")


class TestColumnMapping(unittest.TestCase):
    def _columns(self, **attributes):
        m = model({"T": entity(attributes=attrs(name={"type": "string", "length": 100,
                                                      "required": True}, **attributes))})
        gen = dvgen.Generator(m, "wga")
        cols = gen.columns()
        return {c["LogicalName"]: c for c in (cols[0]["attributes"] if cols else [])}, gen

    def test_native_trait_columns_are_skipped(self):
        cols, _ = self._columns()
        self.assertNotIn("wga_id", cols)
        self.assertNotIn("wga_createdon", cols)
        self.assertNotIn("wga_owner", cols)

    def test_text_formats(self):
        cols, _ = self._columns(
            note={"type": "text"}, body={"type": "richtext"},
            mail={"type": "email"}, tel={"type": "phone"}, link={"type": "url"})
        self.assertEqual(cols["wga_note"]["FormatName"]["Value"], "TextArea")
        self.assertEqual(cols["wga_body"]["FormatName"]["Value"], "RichText")
        self.assertEqual(cols["wga_mail"]["FormatName"]["Value"], "Email")
        self.assertEqual(cols["wga_tel"]["FormatName"]["Value"], "Phone")
        self.assertEqual(cols["wga_link"]["FormatName"]["Value"], "Url")

    def test_date_only_keeps_the_date_only_behaviour(self):
        cols, _ = self._columns(startDate={"type": "date"}, submittedOn={"type": "datetime"})
        self.assertEqual(cols["wga_startdate"]["DateTimeBehavior"]["Value"], "DateOnly")
        self.assertEqual(cols["wga_startdate"]["Format"], "DateOnly")
        self.assertEqual(cols["wga_submittedon"]["DateTimeBehavior"]["Value"], "UserLocal")

    def test_decimal_uses_the_schema_scale(self):
        cols, _ = self._columns(fte={"type": "decimal", "precision": 4, "scale": 2},
                                rate={"type": "decimal", "precision": 18, "scale": 10})
        self.assertEqual(cols["wga_fte"]["Precision"], 2)
        self.assertEqual(cols["wga_rate"]["Precision"], 10)

    def test_percent_is_bounded_0_to_100(self):
        cols, _ = self._columns(pct={"type": "percent"})
        self.assertEqual((cols["wga_pct"]["MinValue"], cols["wga_pct"]["MaxValue"]), (0, 100))

    def test_duration_uses_the_duration_format(self):
        cols, _ = self._columns(mins={"type": "duration"})
        self.assertEqual(cols["wga_mins"]["Format"], "Duration")
        self.assertEqual(cols["wga_mins"]["MinValue"], 0)

    def test_integer_respects_min_and_max(self):
        cols, _ = self._columns(month={"type": "integer", "min": 1, "max": 12})
        self.assertEqual((cols["wga_month"]["MinValue"], cols["wga_month"]["MaxValue"]), (1, 12))

    def test_boolean_default_and_option_set(self):
        cols, _ = self._columns(isPaid={"type": "boolean", "default": True})
        self.assertTrue(cols["wga_ispaid"]["DefaultValue"])
        self.assertEqual(cols["wga_ispaid"]["OptionSet"]["TrueOption"]["Value"], 1)

    def test_choice_binds_to_the_global_option_set_and_default(self):
        m = model(
            {"T": entity(attributes=attrs(
                name={"type": "string", "required": True},
                status={"type": "choice", "enum": "LeaveStatus", "required": True,
                        "default": "draft"}))},
            enums={"LeaveStatus": {"values": [
                {"code": 1, "key": "draft", "label": "Draft"},
                {"code": 2, "key": "submitted", "label": "Submitted"}]}},
        )
        gen = dvgen.Generator(m, "wga")
        col = gen.columns()[0]["attributes"][0]
        self.assertEqual(col["GlobalOptionSet@odata.bind"],
                         "/GlobalOptionSetDefinitions(Name='wga_leavestatus')")
        self.assertEqual(col["DefaultFormValue"], 1)

    def test_autonumber_carries_its_format(self):
        cols, _ = self._columns(num={"type": "autonumber", "format": "LVE-{SEQNUM:6}"})
        self.assertEqual(cols["wga_num"]["AutoNumberFormat"], "LVE-{SEQNUM:6}")

    def test_calculated_columns_are_never_required(self):
        cols, _ = self._columns(available={"type": "decimal", "required": True,
                                           "calculated": "a + b"})
        self.assertEqual(cols["wga_available"]["RequiredLevel"]["Value"], "None")

    def test_address_expands_into_five_text_columns_and_a_lookup(self):
        m = model({"Office": entity(attributes=attrs(
            name={"type": "string", "required": True}, address={"type": "address"}))})
        gen = dvgen.Generator(m, "wga")
        cols = {c["LogicalName"] for c in gen.columns()[0]["attributes"]}
        self.assertIn("wga_addressline1", cols)
        self.assertIn("wga_addresspostalcode", cols)
        self.assertEqual(len(gen.address_lookups), 1)
        self.assertEqual(gen.address_lookups[0]["column"], "addressCountry")

    def test_unknown_type_is_reported_not_silently_dropped(self):
        _, gen = self._columns(mystery={"type": "wormhole"})
        self.assertTrue(any("no Dataverse mapping" in w for w in gen.warnings))

    def test_long_column_name_is_reported(self):
        _, gen = self._columns(**{"a" * 60: {"type": "string"}})
        self.assertTrue(any("Dataverse allows 50" in w for w in gen.warnings))


class TestColumnSecurity(unittest.TestCase):
    def _secured(self, table_sensitivity, column_spec):
        m = model({"T": entity(sensitivity=table_sensitivity, attributes=attrs(
            name={"type": "string", "required": True}, field=column_spec))})
        cols = dvgen.Generator(m, "wga").columns()[0]["attributes"]
        return any(c.get("IsSecured") for c in cols if c["LogicalName"] == "wga_field")

    def test_column_stricter_than_its_table_is_secured(self):
        self.assertTrue(self._secured("internal", {"type": "money", "sensitivity": "confidential"}))

    def test_pii_on_an_internal_table_is_secured(self):
        self.assertTrue(self._secured("internal", {"type": "phone", "pii": True}))

    def test_pii_on_a_restricted_table_is_left_to_security_roles(self):
        self.assertFalse(self._secured("restricted", {"type": "string", "pii": True}))

    def test_pii_on_a_confidential_table_is_left_to_security_roles(self):
        self.assertFalse(self._secured("confidential", {"type": "string", "pii": True}))

    def test_unclassified_column_is_not_secured(self):
        self.assertFalse(self._secured("internal", {"type": "string"}))


class TestRelationships(unittest.TestCase):
    def test_lookup_targets_the_standard_table_name(self):
        m = model({
            "Account": entity(platforms={"dataverse": "standard:account"}),
            "Candidate": entity(attributes=attrs(
                name={"type": "string", "required": True},
                agency={"type": "lookup", "target": "Account"})),
        })
        rels = dvgen.Generator(m, "wga").relationships()["oneToMany"]
        self.assertEqual(rels[0]["payload"]["ReferencedEntity"], "account")
        self.assertEqual(rels[0]["payload"]["Lookup"]["LogicalName"], "wga_agency")

    def test_parent_cascades_on_delete(self):
        m = model({
            "ApprovalRequest": entity(),
            "ApprovalStep": entity(attributes=attrs(
                name={"type": "string", "required": True},
                request={"type": "parent", "target": "ApprovalRequest", "required": True})),
        })
        rels = dvgen.Generator(m, "wga").relationships()["oneToMany"]
        step = next(r for r in rels if r["_column"] == "request")
        self.assertEqual(step["payload"]["CascadeConfiguration"]["Delete"], "Cascade")
        self.assertEqual(step["payload"]["Lookup"]["RequiredLevel"]["Value"], "ApplicationRequired")

    def test_required_lookup_restricts_delete_and_optional_removes_the_link(self):
        m = model({
            "Employee": entity(),
            "T": entity(attributes=attrs(
                name={"type": "string", "required": True},
                must={"type": "lookup", "target": "Employee", "required": True},
                may={"type": "lookup", "target": "Employee"})),
        })
        rels = {r["_column"]: r["payload"] for r in
                dvgen.Generator(m, "wga").relationships()["oneToMany"]}
        self.assertEqual(rels["must"]["CascadeConfiguration"]["Delete"], "Restrict")
        self.assertEqual(rels["may"]["CascadeConfiguration"]["Delete"], "RemoveLink")

    def test_explicit_on_delete_wins(self):
        m = model({"Employee": entity(), "T": entity(attributes=attrs(
            name={"type": "string", "required": True},
            who={"type": "lookup", "target": "Employee", "required": True,
                 "onDelete": "setNull"}))})
        rel = next(r for r in dvgen.Generator(m, "wga").relationships()["oneToMany"]
                   if r["_column"] == "who")
        self.assertEqual(rel["payload"]["CascadeConfiguration"]["Delete"], "RemoveLink")

    def test_self_referencing_parent_lookup_is_hierarchical(self):
        m = model({"OrgUnit": entity(attributes=attrs(
            name={"type": "string", "required": True},
            parentOrgUnit={"type": "lookup", "target": "OrgUnit"}))})
        rel = dvgen.Generator(m, "wga").relationships()["oneToMany"][0]
        self.assertTrue(rel["payload"]["IsHierarchical"])

    def test_multilookup_becomes_many_to_many(self):
        m = model({"OrgUnit": entity(), "T": entity(attributes=attrs(
            name={"type": "string", "required": True},
            units={"type": "multilookup", "target": "OrgUnit"}))})
        rels = dvgen.Generator(m, "wga").relationships()
        self.assertEqual(len(rels["manyToMany"]), 1)
        self.assertEqual(rels["manyToMany"][0]["payload"]["Entity2LogicalName"], "wga_orgunit")

    def test_term_columns_point_at_the_term_table(self):
        m = model({"Term": entity(), "T": entity(attributes=attrs(
            name={"type": "string", "required": True},
            line={"type": "term", "target": "ServiceLine"}))})
        rels = dvgen.Generator(m, "wga").relationships()["oneToMany"]
        self.assertEqual(rels[0]["payload"]["ReferencedEntity"], "wga_term")

    def test_polylookup_becomes_two_text_columns(self):
        """The dictionary maps it to "Text x2 (table name + id)", not a lookup."""
        m = model({"T": entity(attributes=attrs(
            name={"type": "string", "required": True},
            regarding={"type": "polylookup", "targets": ["*"], "required": True}))})
        gen = dvgen.Generator(m, "wga")
        cols = {c["LogicalName"]: c for c in gen.columns()[0]["attributes"]}
        self.assertEqual(cols["wga_regardingtable"]["MaxLength"], 100)
        self.assertEqual(cols["wga_regardingid"]["MaxLength"], 36)
        self.assertEqual(cols["wga_regardingid"]["RequiredLevel"]["Value"],
                         "ApplicationRequired")
        self.assertEqual(gen.relationships()["oneToMany"], [])
        self.assertEqual(gen.manual, [])

    def test_polylookup_scope_is_recorded_as_a_native_mapping(self):
        m = model({"A": entity(), "B": entity(), "T": entity(attributes=attrs(
            name={"type": "string", "required": True},
            regarding={"type": "polylookup", "targets": ["A", "B"]}))})
        gen = dvgen.Generator(m, "wga")
        cols = {c["LogicalName"]: c for c in
                next(t for t in gen.columns() if t["_name"] == "T")["attributes"]}
        self.assertIn("A, B", cols["wga_regardingid"]["Description"]
                      ["LocalizedLabels"][0]["Label"])
        self.assertTrue(any(m["column"] == "regarding" for m in gen.native_mappings))

    def test_lookup_to_a_non_dataverse_table_is_reported(self):
        m = model({"Gone": entity(platforms={"dataverse": "none"}),
                   "T": entity(attributes=attrs(
                       name={"type": "string", "required": True},
                       ref={"type": "lookup", "target": "Gone"}))})
        gen = dvgen.Generator(m, "wga")
        gen.relationships()
        self.assertTrue(any("not a Dataverse table" in w for w in gen.warnings))


class TestMoneyCurrency(unittest.TestCase):
    def _gen(self, **attributes):
        m = model({
            "Currency": entity(platforms={"dataverse": "standard:transactioncurrency"}),
            "T": entity(attributes=attrs(name={"type": "string", "required": True}, **attributes)),
        })
        return dvgen.Generator(m, "wga")

    def test_currency_lookup_on_a_money_table_maps_to_the_native_column(self):
        gen = self._gen(amount={"type": "money", "required": True},
                        currency={"type": "lookup", "target": "Currency", "required": True})
        rels = gen.relationships()["oneToMany"]
        self.assertEqual(rels, [])
        self.assertEqual(gen.native_mappings[0]["mapsTo"], "transactioncurrencyid")

    def test_currency_lookup_without_money_stays_a_real_lookup(self):
        gen = self._gen(defaultCurrency={"type": "lookup", "target": "Currency"})
        rels = gen.relationships()["oneToMany"]
        self.assertEqual(len(rels), 1)
        self.assertEqual(gen.native_mappings, [])

    def test_currency_field_override_is_honoured(self):
        gen = self._gen(
            amount={"type": "money", "currencyField": "originalCurrency"},
            originalCurrency={"type": "lookup", "target": "Currency"},
            currency={"type": "lookup", "target": "Currency"})
        kept = {r["_column"] for r in gen.relationships()["oneToMany"]}
        mapped = {m["column"] for m in gen.native_mappings}
        self.assertEqual(mapped, {"originalCurrency"})
        self.assertIn("currency", kept)


class TestAlternateKeys(unittest.TestCase):
    def test_declared_keys_and_unique_columns_become_keys(self):
        m = model({"Employee": entity(
            keys=[["workEmail"]],
            attributes=attrs(
                name={"type": "string", "required": True},
                workEmail={"type": "email", "required": True},
                employeeNumber={"type": "string", "unique": True}))})
        keys = dvgen.Generator(m, "wga").keys()[0]["keys"]
        names = {k["SchemaName"] for k in keys}
        self.assertEqual(names, {"wga_employee_workemail_key",
                                 "wga_employee_employeenumber_key"})

    def test_a_unique_column_already_declared_is_not_duplicated(self):
        m = model({"T": entity(keys=[["code"]], attributes=attrs(
            name={"type": "string", "required": True},
            code={"type": "string", "unique": True}))})
        self.assertEqual(len(dvgen.Generator(m, "wga").keys()[0]["keys"]), 1)

    def test_composite_key_lists_every_column(self):
        m = model({"T": entity(keys=[["employee", "periodStart"]], attributes=attrs(
            name={"type": "string", "required": True},
            employee={"type": "string"}, periodStart={"type": "date"}))})
        key = dvgen.Generator(m, "wga").keys()[0]["keys"][0]
        self.assertEqual(key["KeyAttributes"], ["wga_employee", "wga_periodstart"])

    def test_key_on_an_unsupported_type_is_reported(self):
        m = model({"T": entity(keys=[["note"]], attributes=attrs(
            name={"type": "string", "required": True}, note={"type": "text"}))})
        gen = dvgen.Generator(m, "wga")
        self.assertEqual(gen.keys(), [])
        self.assertTrue(any("not supported" in w for w in gen.warnings))

    def test_key_on_an_unknown_column_is_reported(self):
        m = model({"T": entity(keys=[["nope"]])})
        gen = dvgen.Generator(m, "wga")
        gen.keys()
        self.assertTrue(any("unknown column" in w for w in gen.warnings))


class TestOptionSets(unittest.TestCase):
    def test_only_enums_in_use_are_emitted(self):
        m = model(
            {"T": entity(attributes=attrs(
                name={"type": "string", "required": True},
                status={"type": "choice", "enum": "Used"}))},
            enums={"Used": {"values": [{"code": 1, "key": "a", "label": "A"}]},
                   "Unused": {"values": [{"code": 1, "key": "b", "label": "B"}]}},
        )
        sets = dvgen.Generator(m, "wga").option_sets()
        self.assertEqual([s["_name"] for s in sets], ["Used"])

    def test_deprecated_values_are_left_out(self):
        m = model(
            {"T": entity(attributes=attrs(
                name={"type": "string", "required": True},
                status={"type": "choice", "enum": "E"}))},
            enums={"E": {"values": [{"code": 1, "key": "a", "label": "A"},
                                    {"code": 9, "key": "old", "label": "Old",
                                     "deprecated": True}]}},
        )
        options = dvgen.Generator(m, "wga").option_sets()[0]["payload"]["Options"]
        self.assertEqual([o["Value"] for o in options], [1])

    def test_missing_enum_is_reported(self):
        m = model({"T": entity(attributes=attrs(
            name={"type": "string", "required": True},
            status={"type": "choice", "enum": "Nope"}))})
        gen = dvgen.Generator(m, "wga")
        gen.option_sets()
        self.assertTrue(any("not defined" in w for w in gen.warnings))

    def test_option_set_is_global(self):
        m = model(
            {"T": entity(attributes=attrs(
                name={"type": "string", "required": True},
                status={"type": "choice", "enum": "E"}))},
            enums={"E": {"description": "d", "values": [{"code": 1, "key": "a", "label": "A"}]}},
        )
        payload = dvgen.Generator(m, "wga").option_sets()[0]["payload"]
        self.assertTrue(payload["IsGlobal"])
        self.assertEqual(payload["Name"], "wga_e")


class TestLayerFilterAndPlan(unittest.TestCase):
    def test_layer_filter_limits_what_is_created(self):
        m = model({"InCore": entity(), "InHr": entity(layer="hr")})
        gen = dvgen.Generator(m, "wga", layers=["core"])
        self.assertEqual(gen.created, {"InCore"})
        # Still addressable as a lookup target.
        self.assertEqual(gen.table_names["InHr"], "wga_inhr")

    def test_plan_counts_match_its_contents(self):
        m = model({"Employee": entity(), "T": entity(attributes=attrs(
            name={"type": "string", "required": True},
            who={"type": "lookup", "target": "Employee"},
            n={"type": "integer"}))})
        plan = dvgen.Generator(m, "wga").build()
        self.assertEqual(plan["counts"]["tables"], len(plan["tables"]))
        self.assertEqual(plan["counts"]["oneToMany"], len(plan["relationships"]["oneToMany"]))
        self.assertEqual(
            plan["counts"]["columns"],
            sum(len(t["attributes"]) for t in plan["columns"]) + len(plan["tables"]))

    def test_prefix_validation_rejects_bad_prefixes(self):
        for bad in ("W", "1abc", "toolongprefix", "ab-c", ""):
            self.assertEqual(dvgen.main(["--profile", "x", "--prefix", bad]), 2, bad)


if __name__ == "__main__":
    unittest.main()
