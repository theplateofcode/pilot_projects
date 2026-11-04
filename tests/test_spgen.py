"""Tests for tools/spgen.py. Run with: python -m unittest discover -s tests"""
import json
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import spgen  # noqa: E402

NS = {"pnp": spgen.PNP_NS}


def attr(kind, sharepoint="Single line of text", **extra):
    out = {"type": kind, "required": False, "physical": {"sharepoint": sharepoint}}
    out.update(extra)
    return out


BASE = {
    "id": {"type": "guid", "required": True, "trait": "base",
           "physical": {"sharepoint": "UniqueId (native; integer ID also exists)"}},
    "createdOn": {"type": "datetime", "required": True, "trait": "base",
                  "physical": {"sharepoint": "Created (native)"}},
    "createdBy": {"type": "lookup", "target": "Employee", "trait": "base",
                  "physical": {"sharepoint": "Author (native)"}},
}


def entity(attributes=None, sharepoint="list", volume="low", sensitivity="internal",
           description="A table.", keys=None, primary="name"):
    return {
        "layer": "hr", "description": description, "ownership": "user",
        "volume": volume, "sensitivity": sensitivity,
        "platforms": {"sharepoint": sharepoint, "dataverse": "custom"},
        "primary": primary, "keys": keys or [],
        "attributes": {**BASE, **(attributes or {"name": attr("string", required=True)})},
    }


def model(entities, enums=None, term_sets=None, terminology=None):
    return {"profile": "t", "layers": {"hr": "1.0.0"}, "entities": entities,
            "enums": enums or {}, "termSets": term_sets or {},
            "terminology": terminology or {}, "seed": {}}


def tree(generator):
    root = generator.template()
    return root


class TestNaming(unittest.TestCase):
    def test_internal_name_is_prefixed_pascal_case(self):
        self.assertEqual(spgen.internal_name("pfx", "startDate"), ("pfxStartDate", False))

    def test_a_long_name_is_shortened_within_32_characters(self):
        name, shortened = spgen.internal_name("pfx", "requiresAttachmentAfterSomeManyDays")
        self.assertTrue(shortened)
        self.assertLessEqual(len(name), 32)

    def test_shortening_is_stable(self):
        a = spgen.internal_name("pfx", "aVeryLongColumnNameThatWillNotFitAtAll")
        b = spgen.internal_name("pfx", "aVeryLongColumnNameThatWillNotFitAtAll")
        self.assertEqual(a, b)

    def test_a_qualifier_scopes_the_name_to_one_list(self):
        name, _ = spgen.internal_name("pfx", "status", "LeaveRequest")
        self.assertEqual(name, "pfxLeaveRequestStatus")

    def test_the_field_id_follows_the_internal_name(self):
        """A shared site column must have one id, whichever list refers to it."""
        self.assertEqual(spgen.field_id("pfxName"), spgen.field_id("pfxName"))
        self.assertNotEqual(spgen.field_id("pfxName"), spgen.field_id("pfxCode"))


class TestSiteColumnSharing(unittest.TestCase):
    def test_identical_columns_become_one_shared_site_column(self):
        g = spgen.Generator(model({
            "Employee": entity({"name": attr("string", required=True, length=200)}),
            "OrgUnit": entity({"name": attr("string", required=True, length=200)}),
        }), "pfx")
        self.assertEqual(g.fields["Employee"]["name"]["name"],
                         g.fields["OrgUnit"]["name"]["name"])
        self.assertEqual(g.fields["Employee"]["name"]["id"],
                         g.fields["OrgUnit"]["name"]["id"])
        self.assertTrue(g.fields["Employee"]["name"]["shared"])

    def test_columns_of_the_same_name_but_different_shape_stay_separate(self):
        g = spgen.Generator(model({
            "A": entity({"code": attr("string", length=20)}),
            "B": entity({"code": attr("integer", sharepoint="Number (0 dp)")}),
        }), "pfx")
        self.assertNotEqual(g.fields["A"]["code"]["name"], g.fields["B"]["code"]["name"])
        self.assertTrue(any("shape differs" in n for n in g.notes))

    def test_a_site_column_is_emitted_once_however_many_lists_use_it(self):
        g = spgen.Generator(model({
            "A": entity({"name": attr("string", required=True, length=200)}),
            "B": entity({"name": attr("string", required=True, length=200)}),
        }), "pfx")
        fields = tree(g).findall(".//pnp:SiteFields/Field", NS)
        names = [f.get("Name") for f in fields]
        self.assertEqual(len(names), len(set(names)))

    def test_a_shared_column_is_grouped_at_site_level(self):
        g = spgen.Generator(model({
            "A": entity({"name": attr("string", required=True, length=200)}),
            "B": entity({"name": attr("string", required=True, length=200)}),
        }), "pfx", site="Travel")
        field = next(f for f in tree(g).findall(".//pnp:SiteFields/Field", NS)
                     if f.get("Name") == "pfxName")
        self.assertEqual(field.get("Group"), "Travel")


class TestFieldTypes(unittest.TestCase):
    def _field(self, name, **attributes):
        g = spgen.Generator(model({"T": entity({"name": attr("string", required=True),
                                                **attributes})}), "pfx")
        element = next((f for f in tree(g).findall(".//pnp:SiteFields/Field", NS)
                        if f.get("Name") == f"pfx{name[0].upper()}{name[1:]}"), None)
        return element, g

    def test_text_carries_its_length(self):
        element, _ = self._field("title", title=attr("string", length=120))
        self.assertEqual(element.get("Type"), "Text")
        self.assertEqual(element.get("MaxLength"), "120")

    def test_text_longer_than_255_is_capped(self):
        element, _ = self._field("title", title=attr("string", length=2000))
        self.assertEqual(element.get("MaxLength"), "255")

    def test_a_date_is_date_only_and_a_datetime_is_not(self):
        element, _ = self._field("startDate", startDate=attr("date", sharepoint="Date (date only)"))
        self.assertEqual(element.get("Format"), "DateOnly")
        element, _ = self._field("submittedOn",
                                 submittedOn=attr("datetime", sharepoint="Date and time"))
        self.assertEqual(element.get("Format"), "DateTime")

    def test_decimal_precision_becomes_decimals(self):
        element, _ = self._field("fte", fte=attr("decimal", scale=2))
        self.assertEqual(element.get("Type"), "Number")
        self.assertEqual(element.get("Decimals"), "2")

    def test_an_integer_has_no_decimals(self):
        element, _ = self._field("count", count=attr("integer"))
        self.assertEqual(element.get("Decimals"), "0")

    def test_money_is_currency_in_sterling(self):
        element, _ = self._field("amount", amount=attr("money"))
        self.assertEqual(element.get("Type"), "Currency")
        self.assertEqual(element.get("LCID"), "2057")

    def test_a_lookup_at_a_person_becomes_a_person_column(self):
        g = spgen.Generator(model({
            "Employee": entity(),
            "T": entity({"name": attr("string", required=True),
                         "employee": attr("lookup", target="Employee", required=True)}),
        }), "pfx")
        self.assertEqual(g.fields["T"]["employee"]["type"], "User")
        element = next(f for f in tree(g).findall(".//pnp:SiteFields/Field", NS)
                       if f.get("Name") == "pfxEmployee")
        self.assertEqual(element.get("UserSelectionMode"), "PeopleOnly")

    def test_a_lookup_at_another_list_binds_to_it(self):
        g = spgen.Generator(model({
            "LeaveType": entity(),
            "T": entity({"name": attr("string", required=True),
                         "leaveType": attr("lookup", target="LeaveType", required=True)}),
        }), "pfx")
        element = next(f for f in tree(g).findall(".//pnp:SiteFields/Field", NS)
                       if f.get("Name") == "pfxLeaveType")
        self.assertEqual(element.get("Type"), "Lookup")
        self.assertEqual(element.get("List"), "{Lists/LeaveTypes}")

    def test_a_lookup_at_a_list_that_does_not_exist_is_reported(self):
        g = spgen.Generator(model({
            "T": entity({"name": attr("string", required=True),
                         "gone": attr("lookup", target="Nowhere")}),
        }), "pfx")
        tree(g)
        self.assertTrue(any("has no list" in w for w in g.warnings))

    def test_a_choice_carries_its_labels_and_default(self):
        g = spgen.Generator(model(
            {"T": entity({"name": attr("string", required=True),
                          "status": attr("choice", sharepoint="Choice", enum="S",
                                         default="submitted")})},
            enums={"S": {"values": [{"code": 1, "key": "draft", "label": "Draft"},
                                    {"code": 5, "key": "submitted", "label": "Submitted"},
                                    {"code": 9, "key": "old", "deprecated": True}]}}), "pfx")
        element = next(f for f in tree(g).findall(".//pnp:SiteFields/Field", NS)
                       if f.get("Name") == "pfxStatus")
        choices = [c.text for c in element.findall("CHOICES/CHOICE")]
        self.assertEqual(choices, ["Draft", "Submitted"])
        self.assertEqual(element.find("Default").text, "Submitted")

    def test_a_unique_column_enforces_uniqueness_and_is_indexed(self):
        element, _ = self._field("code", code=attr("string", unique=True))
        self.assertEqual(element.get("EnforceUniqueValues"), "TRUE")
        self.assertEqual(element.get("Indexed"), "TRUE")

    def test_a_read_only_column_is_not_required(self):
        element, _ = self._field("label", label=attr("string", required=True, readOnly=True))
        self.assertEqual(element.get("Required"), "FALSE")
        self.assertEqual(element.get("ReadOnly"), "TRUE")

    def test_native_columns_are_not_recreated(self):
        g = spgen.Generator(model({"T": entity()}), "pfx")
        names = {f["name"] for f in g.fields["T"].values()}
        self.assertNotIn("pfxCreatedOn", names)
        self.assertNotIn("pfxCreatedBy", names)
        self.assertNotIn("pfxId", names)

    def test_a_column_the_dictionary_withholds_is_omitted_with_a_note(self):
        g = spgen.Generator(model({"T": entity(
            {"name": attr("string", required=True),
             "owner": attr("lookup", target="Employee", sharepoint="—")})}), "pfx")
        self.assertNotIn("owner", g.fields["T"])
        self.assertTrue(any("no SharePoint column" in n for n in g.notes))

    def test_an_address_expands_to_parts_plus_a_country_lookup(self):
        g = spgen.Generator(model({"Country": entity(),
                                   "T": entity({"name": attr("string", required=True),
                                                "home": attr("address",
                                                             sharepoint="Text ×5 + Lookup → Country")})}),
                            "pfx")
        self.assertIn("homeLine1", g.fields["T"])
        self.assertIn("homeCountry", g.fields["T"])
        self.assertEqual(g.fields["T"]["homeCountry"]["type"], "Lookup")

    def test_a_polylookup_becomes_a_list_title_and_an_item_id(self):
        g = spgen.Generator(model({"T": entity(
            {"name": attr("string", required=True),
             "regarding": attr("polylookup", targets=["*"], required=True,
                               sharepoint="Text ×2 (list name + id)")})}), "pfx")
        self.assertEqual(g.fields["T"]["regardingList"]["type"], "Text")
        self.assertEqual(g.fields["T"]["regardingItemId"]["type"], "Number")
        self.assertTrue(any("no multi-list lookup" in n for n in g.notes))

    def test_an_unmapped_type_is_reported(self):
        _, g = self._field("x", x=attr("wormhole"))
        self.assertTrue(any("no SharePoint field" in w for w in g.warnings))


class TestLists(unittest.TestCase):
    def test_a_list_is_titled_with_the_plural_and_lives_under_lists(self):
        g = spgen.Generator(model({"LeaveRequest": entity()}), "pfx")
        element = tree(g).find(".//pnp:Lists/pnp:ListInstance", NS)
        self.assertEqual(element.get("Title"), "Leave Requests")
        self.assertEqual(element.get("Url"), "Lists/LeaveRequests")
        self.assertEqual(element.get("TemplateType"), "100")

    def test_a_library_and_a_pages_list_use_their_own_template_types(self):
        g = spgen.Generator(model({"Doc": entity(sharepoint="library"),
                                   "Page": entity(sharepoint="pages")}), "pfx")
        types = {e.get("Title"): e.get("TemplateType")
                 for e in tree(g).findall(".//pnp:Lists/pnp:ListInstance", NS)}
        self.assertEqual(types["Docs"], "101")
        self.assertEqual(types["Pages"], "850")

    def test_native_and_termstore_entities_get_no_list(self):
        g = spgen.Generator(model({"Kept": entity(),
                                   "Native": entity(sharepoint="native"),
                                   "Terms": entity(sharepoint="termstore")}), "pfx")
        self.assertEqual(g.lists, ["Kept"])

    def test_terminology_renames_the_list(self):
        g = spgen.Generator(model({"Project": entity()},
                                  terminology={"Project": {"singular": "Matter",
                                                           "plural": "Matters"}}), "pfx")
        self.assertEqual(tree(g).find(".//pnp:Lists/pnp:ListInstance", NS).get("Title"),
                         "Matters")

    def test_a_sensitive_list_disables_attachments(self):
        g = spgen.Generator(model({"Pay": entity(sensitivity="restricted")}), "pfx")
        self.assertEqual(tree(g).find(".//pnp:Lists/pnp:ListInstance", NS)
                         .get("EnableAttachments"), "false")

    def test_a_content_type_is_bound_to_each_list(self):
        g = spgen.Generator(model({"T": entity()}), "pfx")
        root = tree(g)
        content_type = root.find(".//pnp:ContentTypes/pnp:ContentType", NS)
        binding = root.find(".//pnp:Lists/pnp:ListInstance/pnp:ContentTypeBindings/"
                            "pnp:ContentTypeBinding", NS)
        self.assertEqual(binding.get("ContentTypeID"), content_type.get("ID"))
        self.assertEqual(binding.get("Default"), "true")


class TestViewsAndLimits(unittest.TestCase):
    def _many_lookups(self, count):
        targets = {f"T{i}": entity() for i in range(count)}
        attributes = {"name": attr("string", required=True)}
        for i in range(count):
            attributes[f"ref{i}"] = attr("lookup", target=f"T{i}")
        return spgen.Generator(model({**targets, "Big": entity(attributes)}), "pfx")

    def test_a_view_stays_inside_the_lookup_budget(self):
        g = self._many_lookups(18)
        root = tree(g)
        big = next(e for e in root.findall(".//pnp:Lists/pnp:ListInstance", NS)
                   if e.get("Title") == "Bigs")
        names = [r.get("Name") for r in big.findall("pnp:Views/View/ViewFields/FieldRef", NS)]
        types = {f.get("Name"): f.get("Type")
                 for f in root.findall(".//pnp:SiteFields/Field", NS)}
        lookups = sum(1 for n in names if types.get(n) in spgen.LOOKUP_LIKE)
        self.assertLessEqual(lookups, spgen.LOOKUP_BUDGET)

    def test_exceeding_the_lookup_budget_is_reported(self):
        g = self._many_lookups(18)
        self.assertTrue(any(l["limit"] == "lookup threshold" for l in g.limits))
        self.assertTrue(any("exceeds the view budget" in w for w in g.warnings))

    def test_a_high_volume_list_is_flagged_and_sorts_on_an_indexed_column(self):
        g = spgen.Generator(model({"Big": entity(volume="high")}), "pfx")
        root = tree(g)
        self.assertTrue(any(l["limit"] == "list view threshold" for l in g.limits))
        order = root.find(".//pnp:Lists/pnp:ListInstance/pnp:Views/View/Query/OrderBy/FieldRef",
                          NS)
        self.assertEqual(order.get("Name"), "Created")

    def test_a_column_stricter_than_its_list_is_reported_as_unprotectable(self):
        g = spgen.Generator(model({"T": entity(
            {"name": attr("string", required=True),
             "mobile": attr("phone", pii=True)})}), "pfx")
        gaps = [l for l in g.limits if l["limit"] == "no column security"]
        self.assertEqual(len(gaps), 1)
        self.assertIn("mobile", gaps[0]["detail"])

    def test_a_pii_column_on_a_restricted_list_is_not_reported(self):
        """The whole list is protected, so there is nothing SharePoint cannot do."""
        g = spgen.Generator(model({"T": entity(
            {"name": attr("string", required=True),
             "dob": attr("date", pii=True)}, sensitivity="restricted")}), "pfx")
        self.assertFalse([l for l in g.limits if l["limit"] == "no column security"])

    def test_a_restricted_list_is_told_to_break_inheritance(self):
        g = spgen.Generator(model({"Pay": entity(sensitivity="restricted")}), "pfx")
        self.assertTrue(any(l["limit"] == "restricted list" for l in g.limits))

    def test_too_many_indexed_columns_is_an_error(self):
        attributes = {"name": attr("string", required=True)}
        for i in range(25):
            attributes[f"c{i}"] = attr("string", indexed=True)
        g = spgen.Generator(model({"T": entity(attributes)}), "pfx")
        self.assertTrue(any("exceeds SharePoint's limit" in e for e in g.errors))

    def test_the_limits_report_is_empty_when_nothing_is_wrong(self):
        g = spgen.Generator(model({"T": entity()}), "pfx")
        self.assertIn("fits SharePoint as it stands", g.limits_report())


class TestTermStore(unittest.TestCase):
    def test_a_term_set_becomes_a_term_group_with_its_terms(self):
        g = spgen.Generator(model(
            {"T": entity()},
            term_sets={"ServiceLine": {"description": "Lines of service.", "open": False,
                                       "terms": ["Audit",
                                                 {"name": "Tax",
                                                  "children": ["Corporate", "Personal"]}]}}),
            "pfx", site="Woodgrove")
        root = tree(g)
        term_set = root.find(".//pnp:TermGroups/pnp:TermGroup/pnp:TermSet", NS)
        self.assertEqual(term_set.get("Name"), "Service Line")
        self.assertEqual(term_set.get("IsOpenForTermCreation"), "false")
        names = [t.get("Name") for t in root.iter(f"{{{spgen.PNP_NS}}}Term")]
        self.assertEqual(set(names), {"Audit", "Tax", "Corporate", "Personal"})

    def test_term_ids_are_stable_across_runs(self):
        spec = model({"T": entity()}, term_sets={"S": {"terms": ["One"]}})
        a = tree(spgen.Generator(spec, "pfx")).iter(f"{{{spgen.PNP_NS}}}Term")
        b = tree(spgen.Generator(spec, "pfx")).iter(f"{{{spgen.PNP_NS}}}Term")
        self.assertEqual([t.get("ID") for t in a], [t.get("ID") for t in b])

    def test_an_open_term_set_says_so(self):
        g = spgen.Generator(model({"T": entity()},
                                  term_sets={"S": {"open": True, "terms": ["One"]}}), "pfx")
        self.assertEqual(tree(g).find(".//pnp:TermGroups/pnp:TermGroup/pnp:TermSet", NS)
                         .get("IsOpenForTermCreation"), "true")


class TestCli(unittest.TestCase):
    def test_a_bad_prefix_is_rejected(self):
        for bad in ("P", "1abc", "toolongprefix", "ab-c"):
            self.assertEqual(spgen.main(["--profile", "x", "--prefix", bad]), 2, bad)


class TestRealProfiles(unittest.TestCase):
    """The template must be structurally sound for the profiles that use SharePoint."""

    PROFILES = ("travel-starter", "intranet-starter", "hr-starter")

    def _plan(self, profile):
        model_file = ROOT / "build" / f"{profile}.model.json"
        if not model_file.exists():
            self.skipTest(f"run: python tools/schematool.py export --profile {profile}")
        return spgen.Generator(
            json.loads(model_file.read_text(encoding="utf-8")), "pfx", profile).build()

    def test_every_profile_generates_valid_xml_without_errors(self):
        for profile in self.PROFILES:
            plan = self._plan(profile)
            self.assertEqual(plan["errors"], [], profile)
            ET.fromstring(plan["xml"])          # raises if malformed

    def test_no_duplicate_internal_names_or_ids(self):
        for profile in self.PROFILES:
            root = ET.fromstring(self._plan(profile)["xml"])
            fields = root.findall(".//pnp:SiteFields/Field", NS)
            names = [f.get("Name") for f in fields]
            ids = [f.get("ID") for f in fields]
            self.assertEqual(len(names), len(set(names)), profile)
            self.assertEqual(len(ids), len(set(ids)), profile)

    def test_every_internal_name_fits_sharepoints_limit(self):
        for profile in self.PROFILES:
            root = ET.fromstring(self._plan(profile)["xml"])
            for field in root.findall(".//pnp:SiteFields/Field", NS):
                self.assertLessEqual(len(field.get("Name")), spgen.MAX_INTERNAL_NAME,
                                     f"{profile}: {field.get('Name')}")

    def test_every_field_reference_resolves_to_a_site_column(self):
        for profile in self.PROFILES:
            root = ET.fromstring(self._plan(profile)["xml"])
            known = {f.get("Name") for f in root.findall(".//pnp:SiteFields/Field", NS)}
            for content_type in root.findall(".//pnp:ContentTypes/pnp:ContentType", NS):
                for ref in content_type.findall("pnp:FieldRefs/pnp:FieldRef", NS):
                    self.assertIn(ref.get("Name"), known,
                                  f"{profile}: {content_type.get('Name')}")

    def test_every_view_field_resolves_and_stays_in_budget(self):
        for profile in self.PROFILES:
            root = ET.fromstring(self._plan(profile)["xml"])
            fields = {f.get("Name"): f.get("Type")
                      for f in root.findall(".//pnp:SiteFields/Field", NS)}
            for list_instance in root.findall(".//pnp:Lists/pnp:ListInstance", NS):
                for view in list_instance.findall("pnp:Views/View", NS):
                    names = [r.get("Name") for r in view.findall("ViewFields/FieldRef", NS)]
                    for name in names:
                        self.assertIn(name, fields,
                                      f"{profile}: {list_instance.get('Title')}")
                    lookups = sum(1 for n in names if fields.get(n) in spgen.LOOKUP_LIKE)
                    self.assertLessEqual(lookups, spgen.LOOKUP_BUDGET,
                                         f"{profile}: {list_instance.get('Title')}")

    def test_every_lookup_points_at_a_list_in_the_template(self):
        for profile in self.PROFILES:
            root = ET.fromstring(self._plan(profile)["xml"])
            urls = {l.get("Url") for l in root.findall(".//pnp:Lists/pnp:ListInstance", NS)}
            for field in root.findall(".//pnp:SiteFields/Field", NS):
                if field.get("Type") in ("Lookup", "LookupMulti") and field.get("List"):
                    self.assertIn(field.get("List").strip("{}"), urls,
                                  f"{profile}: {field.get('Name')}")

    def test_hr_reports_more_limits_than_travel(self):
        """Which is the evidence for putting HR on Dataverse and Travel on SharePoint."""
        hr = self._plan("hr-starter")
        travel = self._plan("travel-starter")
        hr_restricted = [l for l in hr["limits"] if l["limit"] == "restricted list"]
        travel_restricted = [l for l in travel["limits"] if l["limit"] == "restricted list"]
        self.assertGreater(len(hr_restricted), len(travel_restricted))


if __name__ == "__main__":
    unittest.main()
