"""Tests for tools/schematool.py. Run with: python -m unittest discover -s tests"""
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import schematool  # noqa: E402

CORE = """
module: { id: core, name: Core, kind: foundation, version: 1.0.0, description: Test core. }
entities:
  Currency:
    description: Currency.
    attributes:
      name: string(50)!
      code: { type: string(3)!, unique: true }
seed:
  Currency:
    - { code: USD, name: US Dollar }
"""


def load(files: dict, profiles: str = ""):
    """Load a throw-away schema folder and return (model, profiles)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        schemas = root / "schemas"
        schemas.mkdir()
        for name, text in files.items():
            (schemas / name).write_text(textwrap.dedent(text), encoding="utf-8")
        if profiles:
            (schemas / "profiles.yaml").write_text(textwrap.dedent(profiles), encoding="utf-8")
        with mock.patch.object(schematool, "ROOT", root), \
                mock.patch.object(schematool, "SCHEMA_DIR", schemas), \
                mock.patch.object(schematool, "PROFILES_FILE", schemas / "profiles.yaml"):
            model = schematool.Model().load()
            loaded = schematool.load_profiles(model)
    return model, loaded


def module(layer_id: str, deps: str, body: str) -> str:
    header = f"module: {{ id: {layer_id}, name: {layer_id}, kind: module, version: 1.0.0, dependsOn: [{deps}], description: Test. }}\n"
    return header + textwrap.dedent(body)


class RepositorySchemas(unittest.TestCase):
    def test_repository_schemas_are_valid(self):
        model = schematool.Model().load()
        schematool.load_profiles(model)
        self.assertEqual(model.errors, [])

    def test_generated_docs_are_up_to_date(self):
        model = schematool.Model().load()
        pages = schematool.render_docs(model, schematool.load_profiles(model))
        stale = [str(p) for p, text in pages.items() if not p.exists() or p.read_text(encoding="utf-8") != text]
        self.assertEqual(stale, [], "run: python tools/schematool.py docs")


class Rules(unittest.TestCase):
    def assertError(self, model, fragment):
        self.assertTrue(any(fragment in e for e in model.errors), f"no error containing {fragment!r} in {model.errors}")

    def test_valid_minimal_model(self):
        model, _ = load({"core.yaml": CORE})
        self.assertEqual(model.errors, [])

    def test_reference_to_layer_that_is_not_a_dependency(self):
        a = module("a", "core", """
            entities:
              Thing:
                description: Thing.
                attributes:
                  name: string!
                  other: lookup(Other)
            """)
        b = module("b", "core", """
            entities:
              Other:
                description: Other.
                attributes:
                  name: string!
            """)
        model, _ = load({"core.yaml": CORE, "a.yaml": a, "b.yaml": b})
        self.assertError(model, "which is not a dependency of 'a'")

    def test_money_needs_currency_lookup(self):
        a = module("a", "core", """
            entities:
              Invoice:
                description: Invoice.
                attributes:
                  name: string!
                  amount: money
            """)
        model, _ = load({"core.yaml": CORE, "a.yaml": a})
        self.assertError(model, "money needs a 'currency' lookup(Currency)")

    def test_reserved_column_name(self):
        a = module("a", "core", """
            entities:
              Note:
                description: Note.
                attributes:
                  name: string!
                  author: string
            """)
        model, _ = load({"core.yaml": CORE, "a.yaml": a})
        self.assertError(model, "'author' is a reserved column name")

    def test_seed_lookup_must_resolve(self):
        a = module("a", "core", """
            entities:
              Rate:
                description: Rate.
                attributes:
                  name: string!
                  code: { type: string(10)!, unique: true }
                  currency: lookup(Currency)
            seed:
              Rate:
                - { code: R1, name: Rate one, currency: EUR }
            """)
        model, _ = load({"core.yaml": CORE, "a.yaml": a})
        self.assertError(model, "no visible Currency seed row with code 'EUR'")

    def test_two_layers_cannot_add_the_same_column(self):
        a = module("a", "core", """
            extend:
              Currency:
                attributes:
                  region: string(20)
            """)
        b = module("b", "core", """
            extend:
              Currency:
                attributes:
                  region: string(20)
            """)
        model, _ = load({"core.yaml": CORE, "a.yaml": a, "b.yaml": b})
        self.assertError(model, "already added by layer 'a'")

    def test_profile_must_include_dependencies(self):
        a = module("a", "core", "entities: {}\n")
        model, _ = load({"core.yaml": CORE, "a.yaml": a}, profiles="""
            profiles:
              broken: { description: Missing core., layers: [a] }
            """)
        self.assertError(model, "'a' also needs: core")

    def test_dependency_cycle(self):
        a = module("a", "core, b", "entities: {}\n")
        b = module("b", "a", "entities: {}\n")
        model, _ = load({"core.yaml": CORE, "a.yaml": a, "b.yaml": b})
        self.assertError(model, "dependency cycle")


if __name__ == "__main__":
    unittest.main()
