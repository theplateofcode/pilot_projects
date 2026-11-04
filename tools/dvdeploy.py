#!/usr/bin/env python3
"""Apply a generated Dataverse plan to an environment.

Reads the plan `dvgen.py` wrote and creates the components through the Dataverse
Web API, in the plan's order. Every step is idempotent: a component that already
exists is left alone, so a failed run can simply be run again.

Credentials come from the environment, as in ppcheck.py:

    PP_TENANT_ID  PP_CLIENT_ID  PP_CLIENT_SECRET

Usage
    python tools/dvdeploy.py --plan build/dataverse/hr-starter --dry-run
    python tools/dvdeploy.py --plan build/dataverse/hr-starter \
        --url https://woodgrove-dev.crm11.dynamics.com --solution WoodgroveHR
    python tools/dvdeploy.py --plan build/dataverse/hr-starter --url ... --only tables,columns

--dry-run needs no credentials and no network: it prints what would be created.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ppcheck  # noqa: E402  — reuses the token and HTTP plumbing

API = "/api/data/v9.2"
STEPS = ("optionsets", "tables", "columns", "relationships", "keys")

# Dataverse reports a duplicate component with these codes.
ALREADY_EXISTS = (
    "0x80040237",   # duplicate record
    "0x8004502b",   # name already in use
    "DuplicateAttributeSchemaName",
    "already exists",
    "duplicate",
)


class Deployer:
    def __init__(self, url: str, cfg: dict[str, str], solution: str | None,
                 timeout: int = 120, dry_run: bool = False):
        self.url = url.rstrip("/")
        self.cfg = cfg
        self.solution = solution
        self.timeout = timeout
        self.dry_run = dry_run
        self._token: str | None = None
        self.created = 0
        self.skipped = 0
        self.failed: list[dict[str, str]] = []

    # -- plumbing
    @property
    def token(self) -> str:
        if self._token is None:
            self._token = ppcheck.get_token(
                self.cfg["tenant"], self.cfg["client_id"], self.cfg["client_secret"],
                f"{self.url}/.default", self.timeout,
            )
        return self._token

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
        }
        if self.solution:
            # Adds every component created by the request to this solution.
            headers["MSCRM.SolutionUniqueName"] = self.solution
        return headers

    def post(self, path: str, payload: dict[str, Any] | list[Any], what: str) -> bool:
        """POST one component. Returns True when it was created."""
        if self.dry_run:
            print(f"  would create {what}")
            self.created += 1
            return True

        status, body = ppcheck._request(
            f"{self.url}{API}/{path}",
            method="POST",
            token=self.token,
            data=json.dumps(payload).encode(),
            headers=self._headers(),
            timeout=self.timeout,
        )
        if status in (200, 201, 204):
            print(f"  created {what}")
            self.created += 1
            return True

        detail = self._error(body)
        if any(marker.lower() in detail.lower() for marker in ALREADY_EXISTS):
            print(f"  exists  {what}")
            self.skipped += 1
            return False

        print(f"  FAILED  {what}: {detail[:200]}")
        self.failed.append({"component": what, "error": detail})
        return False

    @staticmethod
    def _error(body: dict[str, Any] | str) -> str:
        if isinstance(body, dict):
            err = body.get("error") or {}
            if isinstance(err, dict):
                return str(err.get("message") or err.get("code") or body)
            return str(err)
        return str(body)[:500]

    # -- steps
    def optionsets(self, plan: dict[str, Any]) -> None:
        print(f"\noption sets ({len(plan['optionSets'])})")
        for item in plan["optionSets"]:
            self.post("GlobalOptionSetDefinitions", item["payload"], f"choice set {item['_name']}")

    def tables(self, plan: dict[str, Any]) -> None:
        print(f"\ntables ({len(plan['tables'])})")
        for item in plan["tables"]:
            self.post("EntityDefinitions", item["payload"], f"table {item['_logicalName']}")

    def columns(self, plan: dict[str, Any]) -> None:
        total = sum(len(t["attributes"]) for t in plan["columns"])
        print(f"\ncolumns ({total})")
        for table in plan["columns"]:
            path = f"EntityDefinitions(LogicalName='{table['_logicalName']}')/Attributes"
            for attr in table["attributes"]:
                self.post(path, attr, f"{table['_logicalName']}.{attr['LogicalName']}")

    def relationships(self, plan: dict[str, Any]) -> None:
        one = plan["relationships"]["oneToMany"]
        many = plan["relationships"]["manyToMany"]
        print(f"\nrelationships ({len(one)} one-to-many, {len(many)} many-to-many)")
        for rel in one:
            payload = rel["payload"]
            self.post("RelationshipDefinitions", payload,
                      f"{payload['ReferencingEntity']}.{payload['Lookup']['LogicalName']} "
                      f"-> {payload['ReferencedEntity']}")
        for rel in many:
            payload = rel["payload"]
            self.post("RelationshipDefinitions", payload,
                      f"{payload['Entity1LogicalName']} <-> {payload['Entity2LogicalName']}")

    def keys(self, plan: dict[str, Any]) -> None:
        total = sum(len(k["keys"]) for k in plan["alternateKeys"])
        print(f"\nalternate keys ({total})")
        for table in plan["alternateKeys"]:
            path = f"EntityDefinitions(LogicalName='{table['_logicalName']}')/Keys"
            for key in table["keys"]:
                self.post(path, key, f"{table['_logicalName']} key {key['SchemaName']}")

    def publish(self) -> None:
        """Publish customisations so the new metadata is live for apps."""
        if self.dry_run:
            print("\nwould publish all customisations")
            return
        print("\npublishing customisations")
        status, body = ppcheck._request(
            f"{self.url}{API}/PublishAllXml",
            method="POST", token=self.token, data=b"{}",
            headers=self._headers(), timeout=max(self.timeout, 300),
        )
        print("  published" if status in (200, 204) else f"  publish returned HTTP {status}: "
                                                         f"{self._error(body)[:200]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply a generated Dataverse plan.")
    parser.add_argument("--plan", required=True, help="plan directory written by dvgen.py")
    parser.add_argument("--url", help="Dataverse environment URL (not needed with --dry-run)")
    parser.add_argument("--solution", help="solution unique name to add components to")
    parser.add_argument("--only", help=f"comma-separated steps to run ({', '.join(STEPS)})")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--timeout", type=int, default=120, help="per-request timeout in seconds")
    parser.add_argument("--no-publish", action="store_true", help="skip PublishAllXml at the end")
    args = parser.parse_args(argv)

    plan_dir = Path(args.plan)
    plan_file = plan_dir / "plan.json" if plan_dir.is_dir() else plan_dir
    if not plan_file.exists():
        print(f"error: {plan_file} not found — run tools/dvgen.py first", file=sys.stderr)
        return 2
    plan = json.loads(plan_file.read_text(encoding="utf-8"))

    steps = [s.strip() for s in args.only.split(",")] if args.only else list(STEPS)
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        print(f"error: unknown step(s) {', '.join(unknown)}; choose from {', '.join(STEPS)}",
              file=sys.stderr)
        return 2

    cfg, missing = ppcheck.load_config()
    if not args.dry_run:
        if not args.url:
            print("error: --url is required unless --dry-run is given", file=sys.stderr)
            return 2
        if missing:
            print(f"error: {', '.join(missing)} not set — see tools/ppcheck.py", file=sys.stderr)
            return 2

    print(f"plan    {plan_file}")
    print(f"profile {plan.get('profile')}  prefix {plan.get('prefix')}  "
          f"layers {', '.join(plan.get('layers') or [])}")
    print(f"target  {args.url or '(dry run — nothing will be changed)'}")
    if args.solution:
        print(f"solution {args.solution}")

    deployer = Deployer(args.url or "https://dry.run", cfg,
                        args.solution or plan.get("solution"),
                        args.timeout, args.dry_run)

    started = time.time()
    for step in steps:
        getattr(deployer, step)(plan)
    if not args.no_publish and not deployer.failed:
        deployer.publish()

    print("\n" + "=" * 60)
    print(f"created {deployer.created}, already present {deployer.skipped}, "
          f"failed {len(deployer.failed)}  ({time.time() - started:.0f}s)")
    if plan.get("manualSteps"):
        print(f"\n{len(plan['manualSteps'])} step(s) must still be done by hand:")
        for item in plan["manualSteps"]:
            print(f"  · {item['entity']}.{item['column']}: {item['detail']}")
    if deployer.failed:
        print(f"\n{len(deployer.failed)} component(s) failed:")
        for item in deployer.failed[:20]:
            print(f"  · {item['component']}: {item['error'][:160]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
