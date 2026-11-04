#!/usr/bin/env python3
"""Check Power Platform / Power BI access for the pilot projects.

Confirms the service principal can sign in, then inventories what it can see:
Power Platform environments, Dataverse instances and Power BI workspaces.
Run it before any build phase; it prints exactly what is missing and why.

Credentials come from the environment (never from the command line, so they
stay out of shell history and CI logs):

    PP_TENANT_ID      Entra tenant GUID (or domain, e.g. contoso.onmicrosoft.com)
    PP_CLIENT_ID      app registration (client) ID
    PP_CLIENT_SECRET  client secret value

Usage
    python tools/ppcheck.py                 # login + full inventory
    python tools/ppcheck.py --json          # machine-readable report
    python tools/ppcheck.py --only login    # just the sign-in test
    python tools/ppcheck.py --timeout 60

Exit codes
    0  everything checked succeeded
    1  a check failed (bad credentials, missing consent, empty inventory)
    2  configuration missing (no credentials set)
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# --- what we talk to ---------------------------------------------------------
# Each resource needs its own token: Entra issues per-audience access tokens.
AUTHORITY = "https://login.microsoftonline.com"

SCOPE_BAP = "https://api.bap.microsoft.com/.default"          # environment admin
SCOPE_POWERAPPS = "https://service.powerapps.com/.default"     # maker APIs
SCOPE_POWERBI = "https://analysis.windows.net/powerbi/api/.default"
SCOPE_GRAPH = "https://graph.microsoft.com/.default"

BAP_ENVIRONMENTS = (
    "https://api.bap.microsoft.com/providers/Microsoft.BusinessAppPlatform"
    "/scopes/admin/environments?api-version=2020-10-01"
)
POWERAPPS_ENVIRONMENTS = (
    "https://api.powerapps.com/providers/Microsoft.PowerApps"
    "/environments?api-version=2016-11-01"
)
POWERBI_WORKSPACES = "https://api.powerbi.com/v1.0/myorg/groups"
GRAPH_ORG = "https://graph.microsoft.com/v1.0/organization"

REQUIRED_VARS = ("PP_TENANT_ID", "PP_CLIENT_ID", "PP_CLIENT_SECRET")

TICK, CROSS, WARN, DOT = "PASS", "FAIL", "WARN", "  ·"


class CheckError(Exception):
    """A check failed in a way worth reporting verbatim to the user."""

    def __init__(self, message: str, detail: str = "", hint: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.hint = hint


# --- plumbing ----------------------------------------------------------------
def _request(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any] | str]:
    """One HTTP call. Returns (status, parsed-json-or-text); never raises on 4xx/5xx."""
    hdrs = {"Accept": "application/json", "User-Agent": "pilot-projects-ppcheck/1.0"}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if headers:
        hdrs.update(headers)

    req = urllib.request.Request(url, method=method, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:                      # 4xx / 5xx
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except urllib.error.URLError as exc:
        # No response at all: DNS, TLS, or an egress proxy refusing the host.
        raise CheckError(
            f"cannot reach {urllib.parse.urlsplit(url).netloc}",
            detail=str(exc.reason),
            hint="the host is likely blocked by this environment's network policy",
        ) from exc
    except socket.timeout as exc:
        raise CheckError(f"timed out calling {url}", detail=str(exc)) from exc

    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def _aad_error(payload: dict[str, Any] | str) -> str:
    if isinstance(payload, dict):
        return str(payload.get("error_description") or payload.get("error") or payload)
    return str(payload)[:400]


def get_token(tenant: str, client_id: str, secret: str, scope: str, timeout: int) -> str:
    """Client-credentials token for one resource."""
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": secret,
            "scope": scope,
        }
    ).encode()
    status, payload = _request(
        f"{AUTHORITY}/{tenant}/oauth2/v2.0/token",
        method="POST",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=timeout,
    )
    if status != 200 or not isinstance(payload, dict) or "access_token" not in payload:
        raise CheckError(
            f"token request for {scope} failed (HTTP {status})",
            detail=_aad_error(payload),
            hint=_token_hint(_aad_error(payload)),
        )
    return payload["access_token"]


def _token_hint(detail: str) -> str:
    """Turn the common AADSTS codes into the one thing to go and fix."""
    table = {
        "AADSTS7000215": "the client secret is wrong or expired — create a new one",
        "AADSTS700016": "the app registration does not exist in this tenant — check PP_CLIENT_ID",
        "AADSTS900023": "the tenant was not found — check PP_TENANT_ID",
        "AADSTS90002": "the tenant was not found — check PP_TENANT_ID",
        "AADSTS500011": "the resource is not registered in the tenant — the API's service principal is missing",
        "AADSTS65001": "no admin consent for this resource — grant it in Entra ID",
        "AADSTS7000222": "the client secret has expired — create a new one",
    }
    for code, hint in table.items():
        if code in detail:
            return hint
    return ""


# --- individual checks -------------------------------------------------------
def check_login(cfg: dict[str, str], timeout: int) -> dict[str, Any]:
    """Prove the service principal exists and the secret is valid."""
    token = get_token(cfg["tenant"], cfg["client_id"], cfg["client_secret"], SCOPE_BAP, timeout)
    claims = _decode_claims(token)
    return {
        "ok": True,
        "tenantId": claims.get("tid"),
        "appId": claims.get("appid") or claims.get("azp"),
        "objectId": claims.get("oid"),
        "audience": claims.get("aud"),
        "roles": claims.get("roles", []),
        "expiresAt": claims.get("exp"),
    }


def _decode_claims(jwt: str) -> dict[str, Any]:
    """Read the (unverified) payload of our own freshly issued token, for display only."""
    import base64

    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def check_environments(cfg: dict[str, str], timeout: int) -> dict[str, Any]:
    """List Power Platform environments, with the Dataverse URL of each."""
    errors: list[str] = []
    for scope, url, shape in (
        (SCOPE_BAP, BAP_ENVIRONMENTS, "bap"),
        (SCOPE_POWERAPPS, POWERAPPS_ENVIRONMENTS, "powerapps"),
    ):
        try:
            token = get_token(cfg["tenant"], cfg["client_id"], cfg["client_secret"], scope, timeout)
            status, payload = _request(url, token=token, timeout=timeout)
        except CheckError as exc:
            errors.append(f"{shape}: {exc.message} ({exc.detail})" if exc.detail else f"{shape}: {exc.message}")
            continue

        if status == 200 and isinstance(payload, dict):
            return {"ok": True, "source": shape, "environments": _parse_environments(payload)}
        errors.append(f"{shape}: HTTP {status} {_aad_error(payload)[:200]}")

    raise CheckError(
        "could not list Power Platform environments",
        detail="; ".join(errors),
        hint="the app needs the Power Platform Administrator role (or an environment-level role), "
             "and must be registered as a management application: "
             "New-PowerAppManagementApp -ApplicationId <client-id>",
    )


def _parse_environments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for env in payload.get("value", []):
        props = env.get("properties", {}) or {}
        linked = (props.get("linkedEnvironmentMetadata") or {})
        out.append(
            {
                "name": props.get("displayName") or env.get("name"),
                "id": env.get("name") or env.get("id"),
                "type": (props.get("environmentSku") or props.get("environmentType") or "unknown"),
                "region": props.get("azureRegion") or props.get("azureRegionHint"),
                "state": (props.get("provisioningState") or props.get("states", {})
                          .get("management", {}).get("id")),
                "dataverseUrl": linked.get("instanceUrl"),
                "dataverseVersion": linked.get("version"),
                "isDefault": bool(props.get("isDefault")),
            }
        )
    return out


def check_dataverse(cfg: dict[str, str], env: dict[str, Any], timeout: int) -> dict[str, Any]:
    """Confirm the service principal is an application user inside one Dataverse org."""
    url = (env.get("dataverseUrl") or "").rstrip("/")
    if not url:
        raise CheckError(f"environment '{env.get('name')}' has no Dataverse database")

    token = get_token(cfg["tenant"], cfg["client_id"], cfg["client_secret"], f"{url}/.default", timeout)
    status, payload = _request(f"{url}/api/data/v9.2/WhoAmI", token=token, timeout=timeout)
    if status != 200 or not isinstance(payload, dict):
        raise CheckError(
            f"WhoAmI failed against {url} (HTTP {status})",
            detail=_aad_error(payload),
            hint="add the service principal as an application user in that environment "
                 "and give it a security role",
        )
    return {
        "ok": True,
        "environment": env.get("name"),
        "url": url,
        "userId": payload.get("UserId"),
        "businessUnitId": payload.get("BusinessUnitId"),
        "organizationId": payload.get("OrganizationId"),
    }


def check_powerbi(cfg: dict[str, str], timeout: int) -> dict[str, Any]:
    """List the Power BI workspaces the service principal can see."""
    token = get_token(cfg["tenant"], cfg["client_id"], cfg["client_secret"], SCOPE_POWERBI, timeout)
    status, payload = _request(POWERBI_WORKSPACES, token=token, timeout=timeout)
    if status == 401:
        raise CheckError(
            "Power BI rejected the token (HTTP 401)",
            detail=_aad_error(payload),
            hint="enable 'Allow service principals to use Power BI APIs' in the Fabric admin portal "
                 "and add the app to the allowed security group",
        )
    if status != 200 or not isinstance(payload, dict):
        raise CheckError(
            f"could not list Power BI workspaces (HTTP {status})",
            detail=_aad_error(payload),
            hint="the app must be a member of at least one workspace",
        )
    groups = [
        {
            "name": g.get("name"),
            "id": g.get("id"),
            "type": g.get("type"),
            "capacityId": g.get("capacityId"),
            "isOnDedicatedCapacity": g.get("isOnDedicatedCapacity"),
        }
        for g in payload.get("value", [])
    ]
    return {"ok": True, "workspaces": groups}


def check_tenant(cfg: dict[str, str], timeout: int) -> dict[str, Any]:
    """Name the tenant, so the operator can see which one they are pointed at."""
    token = get_token(cfg["tenant"], cfg["client_id"], cfg["client_secret"], SCOPE_GRAPH, timeout)
    status, payload = _request(GRAPH_ORG, token=token, timeout=timeout)
    if status != 200 or not isinstance(payload, dict):
        raise CheckError(
            f"could not read tenant details (HTTP {status})",
            detail=_aad_error(payload),
            hint="grant the app Organization.Read.All (application) if you want this check",
        )
    orgs = payload.get("value", [])
    if not orgs:
        raise CheckError("Graph returned no organization")
    org = orgs[0]
    return {
        "ok": True,
        "displayName": org.get("displayName"),
        "tenantId": org.get("id"),
        "domains": [d.get("name") for d in org.get("verifiedDomains", []) if d.get("name")],
    }


# --- config ------------------------------------------------------------------
def load_config() -> tuple[dict[str, str], list[str]]:
    missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    cfg = {
        "tenant": os.environ.get("PP_TENANT_ID", ""),
        "client_id": os.environ.get("PP_CLIENT_ID", ""),
        "client_secret": os.environ.get("PP_CLIENT_SECRET", ""),
    }
    return cfg, missing


# --- reporting ---------------------------------------------------------------
def _fmt_table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = lambda cells: "  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
    out = [line(headers), "  " + "  ".join("-" * w for w in widths)]
    out += [line(r) for r in rows]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check Power Platform and Power BI access for the pilot projects."
    )
    parser.add_argument(
        "--only",
        choices=["login", "tenant", "environments", "dataverse", "powerbi"],
        action="append",
        help="run only these checks (repeatable); default is all of them",
    )
    parser.add_argument("--json", action="store_true", help="print a JSON report instead of text")
    parser.add_argument("--timeout", type=int, default=30, help="per-request timeout in seconds")
    args = parser.parse_args(argv)

    selected = set(args.only) if args.only else {"login", "tenant", "environments", "dataverse", "powerbi"}
    cfg, missing = load_config()
    report: dict[str, Any] = {"checks": {}, "missing": []}

    if missing:
        report["missing"] = missing
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"{CROSS}  credentials not configured")
            for var in missing:
                print(f"{DOT} {var} is not set")
            print(
                "\nSet all three as environment variables (or network secrets) on this\n"
                "environment, then start a new session. Nothing else can run until then."
            )
        return 2

    failures = 0

    def run(name: str, fn, *fn_args):
        nonlocal failures
        if name not in selected:
            return None
        try:
            result = fn(*fn_args)
            report["checks"][name] = result
            return result
        except CheckError as exc:
            failures += 1
            report["checks"][name] = {
                "ok": False,
                "error": exc.message,
                "detail": exc.detail,
                "hint": exc.hint,
            }
            return None

    login = run("login", check_login, cfg, args.timeout)
    tenant = run("tenant", check_tenant, cfg, args.timeout)
    envs = run("environments", check_environments, cfg, args.timeout)

    if "dataverse" in selected:
        env_list = (envs or {}).get("environments", [])
        targets = [e for e in env_list if e.get("dataverseUrl")]
        if not targets:
            if envs is not None:
                report["checks"]["dataverse"] = {
                    "ok": False,
                    "error": "no environment with a Dataverse database",
                    "hint": "create a developer environment with a database",
                }
                failures += 1
        else:
            results = []
            for env in targets:
                try:
                    results.append(check_dataverse(cfg, env, args.timeout))
                except CheckError as exc:
                    results.append(
                        {"ok": False, "environment": env.get("name"),
                         "error": exc.message, "detail": exc.detail, "hint": exc.hint}
                    )
            report["checks"]["dataverse"] = {"ok": any(r.get("ok") for r in results), "results": results}
            if not any(r.get("ok") for r in results):
                failures += 1

    pbi = run("powerbi", check_powerbi, cfg, args.timeout)

    if args.json:
        report["ok"] = failures == 0
        print(json.dumps(report, indent=2))
        return 0 if failures == 0 else 1

    # --- human-readable output ---
    print("Power Platform access check")
    print("=" * 60)

    def show(name: str, label: str):
        res = report["checks"].get(name)
        if res is None:
            return
        if res.get("ok"):
            print(f"\n{TICK}  {label}")
        else:
            print(f"\n{CROSS}  {label}: {res.get('error')}")
            if res.get("detail"):
                print(f"{DOT} {res['detail']}")
            if res.get("hint"):
                print(f"{DOT} fix: {res['hint']}")

    show("login", "service-principal sign-in")
    if login:
        print(f"{DOT} tenant {login.get('tenantId')}  app {login.get('appId')}")
        print(f"{DOT} service principal object id {login.get('objectId')}")
        if login.get("roles"):
            print(f"{DOT} app roles: {', '.join(login['roles'])}")

    show("tenant", "tenant details")
    if tenant:
        print(f"{DOT} {tenant.get('displayName')}  ({', '.join(tenant.get('domains', [])[:3])})")

    show("environments", "Power Platform environments")
    if envs:
        env_list = envs.get("environments", [])
        print(f"{DOT} {len(env_list)} environment(s) via the {envs.get('source')} API")
        if env_list:
            print()
            print(
                _fmt_table(
                    [
                        [
                            e.get("name") or "",
                            e.get("type") or "",
                            e.get("region") or "",
                            "yes" if e.get("dataverseUrl") else "no",
                            e.get("dataverseUrl") or "",
                        ]
                        for e in env_list
                    ],
                    ["Environment", "Type", "Region", "Dataverse", "URL"],
                )
            )

    dv = report["checks"].get("dataverse")
    if dv:
        if dv.get("ok"):
            print(f"\n{TICK}  Dataverse application user")
            for r in dv.get("results", []):
                if r.get("ok"):
                    print(f"{DOT} {r['environment']}: user {r['userId']}")
                else:
                    print(f"{DOT} {r.get('environment')}: {r.get('error')}")
        else:
            print(f"\n{CROSS}  Dataverse application user: {dv.get('error', 'no environment reachable')}")
            for r in dv.get("results", []):
                print(f"{DOT} {r.get('environment')}: {r.get('error')} — {r.get('hint', '')}")
            if dv.get("hint"):
                print(f"{DOT} fix: {dv['hint']}")

    show("powerbi", "Power BI workspaces")
    if pbi:
        ws = pbi.get("workspaces", [])
        print(f"{DOT} {len(ws)} workspace(s) visible to the service principal")
        if ws:
            print()
            print(
                _fmt_table(
                    [
                        [w.get("name") or "", w.get("id") or "",
                         "premium/fabric" if w.get("isOnDedicatedCapacity") else "shared"]
                        for w in ws
                    ],
                    ["Workspace", "Id", "Capacity"],
                )
            )
        else:
            print(f"{WARN}  none — add the service principal to a workspace as Member or Admin")

    print("\n" + "=" * 60)
    print("all checks passed" if failures == 0 else f"{failures} check(s) failed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
