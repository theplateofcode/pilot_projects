"""Tests for tools/ppcheck.py. Run with: python -m unittest discover -s tests"""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import ppcheck  # noqa: E402


class TestConfig(unittest.TestCase):
    def test_reports_every_missing_variable(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            cfg, missing = ppcheck.load_config()
        self.assertEqual(missing, ["PP_TENANT_ID", "PP_CLIENT_ID", "PP_CLIENT_SECRET"])
        self.assertEqual(cfg["tenant"], "")

    def test_reports_only_the_missing_one(self):
        env = {"PP_TENANT_ID": "t", "PP_CLIENT_ID": "c"}
        with mock.patch.dict("os.environ", env, clear=True):
            _, missing = ppcheck.load_config()
        self.assertEqual(missing, ["PP_CLIENT_SECRET"])

    def test_reads_all_three(self):
        env = {"PP_TENANT_ID": "t", "PP_CLIENT_ID": "c", "PP_CLIENT_SECRET": "s"}
        with mock.patch.dict("os.environ", env, clear=True):
            cfg, missing = ppcheck.load_config()
        self.assertEqual(missing, [])
        self.assertEqual(cfg, {"tenant": "t", "client_id": "c", "client_secret": "s"})

    def test_main_exits_2_without_credentials(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("sys.stdout", new=io.StringIO()) as out:
            code = ppcheck.main([])
        self.assertEqual(code, 2)
        self.assertIn("PP_TENANT_ID", out.getvalue())

    def test_main_json_mode_without_credentials_is_valid_json(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch("sys.stdout", new=io.StringIO()) as out:
            ppcheck.main(["--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["missing"], list(ppcheck.REQUIRED_VARS))


class TestTokenHints(unittest.TestCase):
    def test_maps_known_aadsts_codes(self):
        cases = {
            "AADSTS7000215: Invalid client secret": "secret",
            "AADSTS700016: Application with identifier": "app registration",
            "AADSTS90002: Tenant not found": "tenant",
            "AADSTS65001: no consent": "consent",
            "AADSTS7000222: expired secret": "expired",
        }
        for detail, expected in cases.items():
            self.assertIn(expected, ppcheck._token_hint(detail), detail)

    def test_unknown_code_gives_no_hint(self):
        self.assertEqual(ppcheck._token_hint("AADSTS999999: something new"), "")


class TestEnvironmentParsing(unittest.TestCase):
    PAYLOAD = {
        "value": [
            {
                "name": "env-guid-1",
                "properties": {
                    "displayName": "Woodgrove Dev",
                    "environmentSku": "Developer",
                    "azureRegion": "uksouth",
                    "provisioningState": "Succeeded",
                    "isDefault": False,
                    "linkedEnvironmentMetadata": {
                        "instanceUrl": "https://woodgrove-dev.crm11.dynamics.com/",
                        "version": "9.2.25100.1",
                    },
                },
            },
            {
                "name": "env-guid-2",
                "properties": {
                    "displayName": "Woodgrove Test",
                    "environmentType": "Sandbox",
                    "azureRegionHint": "uksouth",
                    "isDefault": True,
                },
            },
        ]
    }

    def test_extracts_name_sku_and_dataverse_url(self):
        envs = ppcheck._parse_environments(self.PAYLOAD)
        self.assertEqual(len(envs), 2)
        first = envs[0]
        self.assertEqual(first["name"], "Woodgrove Dev")
        self.assertEqual(first["type"], "Developer")
        self.assertEqual(first["region"], "uksouth")
        self.assertEqual(first["dataverseUrl"], "https://woodgrove-dev.crm11.dynamics.com/")
        self.assertEqual(first["dataverseVersion"], "9.2.25100.1")

    def test_environment_without_database_has_no_url(self):
        envs = ppcheck._parse_environments(self.PAYLOAD)
        second = envs[1]
        self.assertIsNone(second["dataverseUrl"])
        self.assertEqual(second["type"], "Sandbox")
        self.assertTrue(second["isDefault"])

    def test_empty_payload_is_empty_list(self):
        self.assertEqual(ppcheck._parse_environments({}), [])


class TestClaimDecoding(unittest.TestCase):
    def test_decodes_unpadded_payload(self):
        import base64

        claims = {"tid": "tenant-1", "appid": "app-1", "oid": "obj-1", "roles": ["X"]}
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        token = f"header.{body}.signature"
        self.assertEqual(ppcheck._decode_claims(token)["tid"], "tenant-1")
        self.assertEqual(ppcheck._decode_claims(token)["roles"], ["X"])

    def test_malformed_token_returns_empty(self):
        self.assertEqual(ppcheck._decode_claims("not-a-jwt"), {})


class TestChecksAgainstFakes(unittest.TestCase):
    CFG = {"tenant": "t", "client_id": "c", "client_secret": "s"}

    def test_login_surfaces_token_claims(self):
        import base64

        claims = {"tid": "tenant-1", "appid": "app-1", "oid": "obj-1", "aud": "bap"}
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        with mock.patch.object(ppcheck, "_request",
                               return_value=(200, {"access_token": f"h.{body}.s"})):
            result = ppcheck.check_login(self.CFG, 5)
        self.assertTrue(result["ok"])
        self.assertEqual(result["tenantId"], "tenant-1")
        self.assertEqual(result["appId"], "app-1")

    def test_bad_secret_raises_with_hint(self):
        error = (400, {"error_description": "AADSTS7000215: Invalid client secret provided"})
        with mock.patch.object(ppcheck, "_request", return_value=error):
            with self.assertRaises(ppcheck.CheckError) as ctx:
                ppcheck.check_login(self.CFG, 5)
        self.assertIn("secret", ctx.exception.hint)

    def test_powerbi_lists_workspaces(self):
        responses = [
            (200, {"access_token": "h.e30.s"}),
            (200, {"value": [{"name": "HR Analytics", "id": "w1",
                              "isOnDedicatedCapacity": False, "type": "Workspace"}]}),
        ]
        with mock.patch.object(ppcheck, "_request", side_effect=responses):
            result = ppcheck.check_powerbi(self.CFG, 5)
        self.assertEqual(result["workspaces"][0]["name"], "HR Analytics")

    def test_powerbi_401_explains_service_principal_setting(self):
        responses = [(200, {"access_token": "h.e30.s"}), (401, {"error": "Unauthorized"})]
        with mock.patch.object(ppcheck, "_request", side_effect=responses):
            with self.assertRaises(ppcheck.CheckError) as ctx:
                ppcheck.check_powerbi(self.CFG, 5)
        self.assertIn("service principals", ctx.exception.hint)

    def test_dataverse_whoami_returns_ids(self):
        responses = [
            (200, {"access_token": "h.e30.s"}),
            (200, {"UserId": "u1", "BusinessUnitId": "b1", "OrganizationId": "o1"}),
        ]
        env = {"name": "Dev", "dataverseUrl": "https://x.crm.dynamics.com/"}
        with mock.patch.object(ppcheck, "_request", side_effect=responses):
            result = ppcheck.check_dataverse(self.CFG, env, 5)
        self.assertEqual(result["userId"], "u1")
        self.assertEqual(result["url"], "https://x.crm.dynamics.com")

    def test_dataverse_without_database_is_an_error(self):
        with self.assertRaises(ppcheck.CheckError):
            ppcheck.check_dataverse(self.CFG, {"name": "NoDb"}, 5)

    def test_environments_tries_powerapps_when_bap_fails(self):
        # BAP token ok, BAP list 403; then PowerApps token ok, list 200.
        responses = [
            (200, {"access_token": "h.e30.s"}),
            (403, {"error": "Forbidden"}),
            (200, {"access_token": "h.e30.s"}),
            (200, {"value": [{"name": "e1", "properties": {"displayName": "Dev"}}]}),
        ]
        with mock.patch.object(ppcheck, "_request", side_effect=responses):
            result = ppcheck.check_environments(self.CFG, 5)
        self.assertEqual(result["source"], "powerapps")
        self.assertEqual(result["environments"][0]["name"], "Dev")

    def test_environments_reports_both_failures(self):
        responses = [
            (200, {"access_token": "h.e30.s"}),
            (403, {"error": "Forbidden"}),
            (200, {"access_token": "h.e30.s"}),
            (403, {"error": "Forbidden"}),
        ]
        with mock.patch.object(ppcheck, "_request", side_effect=responses):
            with self.assertRaises(ppcheck.CheckError) as ctx:
                ppcheck.check_environments(self.CFG, 5)
        self.assertIn("management application", ctx.exception.hint)

    def test_blocked_host_is_reported_as_unreachable(self):
        import urllib.error

        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("tunnel refused")):
            with self.assertRaises(ppcheck.CheckError) as ctx:
                ppcheck._request("https://api.powerbi.com/v1.0/myorg/groups", timeout=1)
        self.assertIn("cannot reach", ctx.exception.message)
        self.assertIn("network policy", ctx.exception.hint)


class TestFormatting(unittest.TestCase):
    def test_table_pads_columns(self):
        text = ppcheck._fmt_table([["a", "bbb"], ["cccc", "d"]], ["H1", "H2"])
        lines = text.splitlines()
        self.assertIn("H1", lines[0])
        self.assertEqual(len(lines), 4)


if __name__ == "__main__":
    unittest.main()
