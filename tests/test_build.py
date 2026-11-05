"""Tests for tools/build.py. Run with: python -m unittest discover -s tests"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build  # noqa: E402


class TestThePlan(unittest.TestCase):
    def _steps(self, profile="hr-starter", prefix="wga", solution="S", firm=None):
        return build.plan_steps(profile, prefix, solution, firm)

    def test_every_step_names_a_tool_that_exists(self):
        for step in self._steps():
            self.assertTrue((ROOT / "tools" / step.args[0]).exists(), step.args[0])

    def test_every_step_belongs_to_a_known_target(self):
        for step in self._steps():
            self.assertIn(step.target, build.TARGETS, step.name)

    def test_every_step_says_why_it_runs(self):
        for step in self._steps():
            self.assertTrue(step.why, step.name)

    def test_the_resolved_model_is_exported_before_any_generator(self):
        steps = self._steps()
        export = next(i for i, s in enumerate(steps) if s.name == "export")
        for index, step in enumerate(steps):
            if step.target != "schema":
                self.assertGreater(index, export, step.name)

    def test_solgen_runs_after_everything_it_assembles(self):
        steps = [s.name for s in self._steps()]
        solgen = steps.index("solgen")
        for name in ("dvgen", "secgen", "appgen", "flowgen"):
            self.assertLess(steps.index(name), solgen, name)

    def test_the_firm_is_passed_through_when_given(self):
        step = next(s for s in self._steps(firm="fabrikam") if s.name == "demodata")
        self.assertIn("--firm", step.args)
        self.assertIn("fabrikam", step.args)

    def test_no_firm_means_the_profile_default(self):
        step = next(s for s in self._steps() if s.name == "demodata")
        self.assertNotIn("--firm", step.args)

    def test_deploying_to_a_tenant_is_not_in_the_pipeline(self):
        """dvdeploy writes to a real environment, so it is never run for you."""
        for step in self._steps():
            self.assertNotIn("dvdeploy.py", step.args[0])

    def test_the_solution_name_defaults_rather_than_crashing(self):
        steps = build.plan_steps("hr-starter", "wga", None, None)
        solgen = next(s for s in steps if s.name == "solgen")
        self.assertIn("--solution", solgen.args)


class TestSpecSkipping(unittest.TestCase):
    def test_a_spec_declares_the_profile_it_is_for(self):
        for path, expected in ((ROOT / "security" / "phase1-roles.yaml", "hr-starter"),
                               (ROOT / "powerbi" / "hr-dashboard.yaml", "hr-starter"),
                               (ROOT / "apps" / "hr-admin.yaml", "hr-starter"),
                               (ROOT / "security" / "travel-roles.yaml", "travel-starter"),
                               (ROOT / "powerbi" / "travel-dashboard.yaml", "travel-starter"),
                               (ROOT / "apps" / "travel-desk.yaml", "travel-starter"),
                               (ROOT / "security" / "work-roles.yaml", "work-starter"),
                               (ROOT / "powerbi" / "work-dashboard.yaml", "work-starter"),
                               (ROOT / "apps" / "project-delivery.yaml", "work-starter"),
                               (ROOT / "security" / "intranet-roles.yaml", "intranet-starter"),
                               (ROOT / "powerbi" / "intranet-dashboard.yaml", "intranet-starter"),
                               (ROOT / "apps" / "intranet-admin.yaml", "intranet-starter")):
            if path.exists():
                self.assertEqual(build.spec_profile(path), expected, path.name)

    def test_a_step_with_no_spec_for_this_profile_is_skipped(self):
        """A profile with no roles file gives secgen nothing to run, and the
        skip says which profile it found nothing for.

        The profile is one SPECS does not map rather than a named pilot: this
        test used to name the next pilot due to be built, so it failed the day
        that pilot got its roles file — which is a test about the build driver
        breaking because the project made progress.
        """
        unmapped = next(
            (p for p in ("crm-starter", "general-firm", "accounting-firm")
             if p not in build.SPECS["security"]), None)
        self.assertIsNotNone(unmapped, "every profile now has a roles spec; "
                                       "point this test at a new one")
        step = next(s for s in build.plan_steps(unmapped, "pfx", "S", None)
                    if s.name == "secgen")
        reason = build.skip_reason(step, unmapped)
        self.assertIsNotNone(reason)
        self.assertIn(unmapped, reason)

    def test_every_mapped_profile_runs_its_own_specs(self):
        """The other half: a profile SPECS maps must not be skipped, and must
        get its own file rather than another phase's."""
        for kind, step_name in (("security", "secgen"), ("apps", "appgen"),
                                ("powerbi", "pbigen")):
            for profile, filename in build.SPECS[kind].items():
                step = next(s for s in build.plan_steps(profile, "pfx", "S", None)
                            if s.name == step_name)
                self.assertIsNone(build.skip_reason(step, profile),
                                  f"{profile}/{step_name}")
                self.assertEqual(step.spec.name, filename, f"{profile}/{kind}")

    def test_each_profile_with_a_spec_gets_its_own(self):
        for profile, expected in (("hr-starter", "phase1-roles.yaml"),
                                  ("law-firm-crm", "crm-roles.yaml"),
                                  ("travel-starter", "travel-roles.yaml"),
                                  ("work-starter", "work-roles.yaml"),
                                  ("intranet-starter", "intranet-roles.yaml")):
            step = next(s for s in build.plan_steps(profile, "wga", "S", None)
                        if s.name == "secgen")
            self.assertIsNotNone(step.spec, profile)
            self.assertEqual(step.spec.name, expected)
            self.assertIsNone(build.skip_reason(step, profile), profile)

    def test_a_specs_own_declaration_still_wins(self):
        """Belt and braces: the mapping says where to look, the spec says who for."""
        step = build.Step("secgen", "dataverse", ["secgen.py"], "why",
                          spec=ROOT / "security" / "phase1-roles.yaml")
        self.assertIn("hr-starter", build.skip_reason(step, "law-firm-crm") or "")

    def test_a_step_is_not_skipped_for_its_own_profile(self):
        step = next(s for s in build.plan_steps("hr-starter", "wga", "S", None)
                    if s.name == "secgen")
        self.assertIsNone(build.skip_reason(step, "hr-starter"))

    def test_a_step_with_no_spec_is_never_skipped(self):
        step = next(s for s in build.plan_steps("hr-starter", "wga", "S", None)
                    if s.name == "sqlgen")
        self.assertIsNone(build.skip_reason(step, "hr-starter"))

    def test_a_missing_spec_is_skipped_with_a_reason(self):
        step = build.Step("x", "dataverse", ["sqlgen.py"], "why",
                          spec=ROOT / "nowhere" / "absent.yaml")
        self.assertIn("no ", build.skip_reason(step, "hr-starter"))

    def test_an_unreadable_spec_does_not_crash_the_planner(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write("{{{ not yaml")
            path = Path(handle.name)
        try:
            self.assertIsNone(build.spec_profile(path))
        finally:
            path.unlink()


class TestCli(unittest.TestCase):
    def test_a_bad_prefix_is_rejected(self):
        self.assertEqual(build.main(
            ["--profile", "hr-starter", "--prefix", "X"]), 2)

    def test_an_unknown_target_is_rejected(self):
        self.assertEqual(build.main(
            ["--profile", "hr-starter", "--prefix", "wga", "--targets", "wormhole"]), 2)

    def test_list_prints_the_commands_without_running_them(self):
        import io
        from unittest import mock
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            code = build.main(["--profile", "hr-starter", "--prefix", "wga",
                               "--targets", "sql", "--list"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("sqlgen.py", text)
        self.assertIn("schematool.py export", text)   # schema is always included

    def test_schema_is_added_even_when_not_asked_for(self):
        import io
        from unittest import mock
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            build.main(["--profile", "hr-starter", "--prefix", "wga",
                        "--targets", "sql", "--list"])
        self.assertIn("schema", out.getvalue())


if __name__ == "__main__":
    unittest.main()
