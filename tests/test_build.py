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
                               (ROOT / "apps" / "hr-admin.yaml", "hr-starter")):
            if path.exists():
                self.assertEqual(build.spec_profile(path), expected, path.name)

    def test_a_step_is_skipped_when_its_spec_is_for_another_profile(self):
        step = next(s for s in build.plan_steps("travel-starter", "pfx", "S", None)
                    if s.name == "secgen")
        reason = build.skip_reason(step, "travel-starter")
        self.assertIsNotNone(reason)
        self.assertIn("hr-starter", reason)

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
