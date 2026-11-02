"""Deploy the CRM pilot into a Dataverse environment.

    python -m deploy.cli plan                        # offline: show and write every payload
    python -m deploy.cli deploy --org https://org.crm.dynamics.com
    python -m deploy.cli deploy --org ... --steps data
    python -m deploy.cli status --org ...

Steps, in order: ``solution`` ``choices`` ``tables`` ``columns`` ``relationships`` ``keys``
``publish`` ``views`` ``forms`` ``charts`` ``sitemap`` ``app`` ``security`` ``reference`` ``demo``.
Every step is idempotent: re-running skips what is already there, so a failed deployment can be
resumed with the same command.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

import yaml

from .client import Dataverse, DataverseError
from .data_steps import DataDeployer
from .model import CrmModel, ScopeError, load_model
from .schema_steps import Deployer
from .security_steps import SecurityDeployer
from .state import State
from .ux_steps import UxDeployer

HERE = pathlib.Path(__file__).resolve().parent
PILOT = HERE.parent
REPO = PILOT.parent.parent

ALL_STEPS = [
    "solution", "choices", "tables", "columns", "relationships", "keys", "publish",
    "views", "forms", "charts", "sitemap", "app", "security", "reference", "demo",
]


def log(message: str) -> None:
    print(message, flush=True)


def build_model_json(profile: str, path: pathlib.Path) -> pathlib.Path:
    """Resolve the canonical schema for a profile if the export is missing or stale."""
    if path.exists():
        newest_schema = max(
            (p.stat().st_mtime for p in (REPO / "schemas").rglob("*.yaml")), default=0
        )
        if path.stat().st_mtime >= newest_schema:
            return path
    tool = REPO / "tools" / "schematool.py"
    if not tool.exists():
        if path.exists():
            return path
        raise SystemExit(f"cannot find {tool} to export profile '{profile}', and {path} does not exist")
    log(f"· exporting profile '{profile}' from the canonical schema")
    subprocess.run([sys.executable, str(tool), "export", "--profile", profile], cwd=REPO, check=True)
    return path


def load_config(path: pathlib.Path, prefix: str | None) -> dict:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if prefix:
        config["solution"]["publisher"]["prefix"] = prefix
    return config


class Runner:
    def __init__(self, args):
        self.args = args
        model_path = pathlib.Path(args.model) if args.model else REPO / "build" / f"{args.profile}.model.json"
        if not args.model:
            build_model_json(args.profile, model_path)
        elif not model_path.exists():
            raise SystemExit(f"model file {model_path} not found")
        self.config = load_config(pathlib.Path(args.config), args.prefix)
        prefix = self.config["solution"]["publisher"]["prefix"]
        try:
            self.model = CrmModel(
                load_model(str(model_path)),
                prefix,
                self.config["tables"]["include"],
                self.config["tables"].get("exclude", []),
            )
        except ScopeError as exc:
            raise SystemExit(f"cannot scope the model: {exc}")
        self.model_path = model_path
        dry = args.command == "plan"
        self.dv = Dataverse(
            org_url=args.org or "https://example.crm.dynamics.com",
            auth=args.auth,
            tenant=args.tenant,
            client_id=args.client_id,
            client_secret=args.client_secret or os.environ.get("DATAVERSE_CLIENT_SECRET"),
            dry_run=dry,
            verbose=args.verbose,
            log=log,
        )
        self.state = State.for_env(
            args.state_dir or (PILOT / ".deploy-state"),
            self.dv.org_url,
            self.config["solution"]["uniquename"],
            read_only=dry,
        )
        common = (self.dv, self.model, self.config, self.state, log, args.continue_on_error)
        self.schema = Deployer(*common)
        self.ux = UxDeployer(*common)
        self.security = SecurityDeployer(*common)
        self.data = DataDeployer(*common)
        self.deployers = [self.schema, self.ux, self.security, self.data]

    # ------------------------------------------------------------------ report
    def print_scope(self) -> None:
        log(f"\nProfile      {self.args.profile}  ({self.model_path})")
        log(f"Prefix       {self.model.prefix}")
        log(f"Solution     {self.config['solution']['uniquename']} / app {self.config['app']['uniquename']}")
        log(f"Scope        {self.model.summary()}")
        log("\nTables")
        for table in self.model.tables.values():
            kind = "new" if table.is_custom else "standard"
            columns = len([c for c in table.columns if c.kind == "attribute"])
            lookups = len([c for c in table.columns if c.kind == "lookup"])
            many = len([c for c in table.columns if c.kind == "manytomany"])
            log(
                f"  {table.logical:34} {kind:9} {columns:3} columns  {lookups:2} lookups  "
                f"{many} N:N  ({table.display})"
            )
        if self.model.warnings:
            log("\nNotes")
            for warning in self.model.warnings:
                log(f"  · {warning}")

    def write_payloads(self, directory: pathlib.Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "scope.json").write_text(
            json.dumps(
                {
                    "profile": self.args.profile,
                    "prefix": self.model.prefix,
                    "tables": {
                        t.logical: {
                            "display": t.display,
                            "custom": t.is_custom,
                            "columns": [
                                {"logical": c.logical, "type": c.type, "kind": c.kind} for c in t.columns
                            ],
                        }
                        for t in self.model.tables.values()
                    },
                    "warnings": self.model.warnings,
                },
                indent=1,
            )
        )
        (directory / "optionsets.json").write_text(
            json.dumps([self.model.option_set_payload(e) for e in sorted(self.model.used_enums)], indent=1)
        )
        (directory / "tables.json").write_text(
            json.dumps([self.model.table_payload(t) for t in self.model.custom_tables], indent=1)
        )
        (directory / "columns.json").write_text(
            json.dumps(
                [
                    {"table": t.logical, "payload": c.payload}
                    for t in self.model.tables.values()
                    for c in t.columns
                    if c.kind == "attribute"
                ],
                indent=1,
            )
        )
        (directory / "relationships.json").write_text(
            json.dumps(
                [c.relationship for t in self.model.tables.values() for c in t.columns if c.relationship],
                indent=1,
            )
        )
        (directory / "sitemap.xml").write_text(self.ux.site_map_xml())
        forms = directory / "forms"
        views = directory / "views"
        forms.mkdir(exist_ok=True)
        views.mkdir(exist_ok=True)
        self.schema.refresh_table_metadata()
        for table in self.model.tables.values():
            spec = (self.config.get("ux", {}) or {}).get(table.name, {})
            tabs = spec.get("form") or self.ux.generated_form(table)
            (forms / f"{table.logical}.xml").write_text(self.ux.form_xml(table, tabs))
            view_specs = spec.get("views") or [
                {"name": f"Active {table.plural.lower()}", "columns": self.ux.generated_columns(table, 7),
                 "_generated": True}
            ]
            for view in view_specs:
                columns = [
                    self.ux.resolve(table, c) if not view.get("_generated") else c for c in view["columns"]
                ]
                columns = [c for c in columns if c]
                safe = view["name"].lower().replace(" ", "_").replace("/", "-")
                (views / f"{table.logical}.{safe}.fetch.xml").write_text(
                    self.ux.fetch_xml(table, columns, view.get("order", []), view.get("filter", []))
                )
        log(f"\n· payloads written to {directory}")

    # ------------------------------------------------------------------- steps
    def run_steps(self, steps: list[str]) -> None:
        started = time.time()
        publisher_id = solution_id = None
        site_map_id = app_id = None

        if "solution" in steps:
            log("\n[solution] publisher and solution")
            publisher_id = self.schema.ensure_publisher()
            solution_id = self.schema.ensure_solution(publisher_id)
        if "choices" in steps:
            log("\n[choices] global choice sets")
            self.schema.ensure_option_sets()
        if "tables" in steps:
            log("\n[tables] tables")
            self.schema.ensure_tables()
            self.schema.add_tables_to_solution()
        else:
            self.schema.refresh_table_metadata()
        if "columns" in steps:
            log("\n[columns] columns")
            self.schema.ensure_columns()
        if "relationships" in steps:
            log("\n[relationships] lookups and many-to-many")
            self.schema.ensure_relationships()
        if "keys" in steps:
            log("\n[keys] alternate keys")
            self.schema.ensure_keys()
        if "publish" in steps:
            log("\n[publish] publish schema")
            self.schema.publish()
        if "views" in steps:
            log("\n[views] views")
            self.ux.ensure_views()
        if "forms" in steps:
            log("\n[forms] main forms")
            self.ux.ensure_forms()
        if "charts" in steps:
            log("\n[charts] charts")
            self.ux.ensure_charts()
        if "sitemap" in steps:
            log("\n[sitemap] navigation")
            site_map_id = self.ux.ensure_site_map()
        else:
            site_map_id = self.state.get("sitemap_id")
        if "app" in steps:
            log("\n[app] model-driven app")
            app_id = self.ux.ensure_app(site_map_id if isinstance(site_map_id, str) else None)
            self.ux.validate_and_publish(app_id)
        else:
            app_id = self.state.get("app_id")
        if "security" in steps:
            log("\n[security] security roles")
            self.security.ensure_roles()
            self.security.attach_roles_to_app(app_id if isinstance(app_id, str) else None)
            if self.args.assign_me:
                self.security.assign_role_to_me()
        if "reference" in steps:
            log("\n[reference] reference data from the schema seeds")
            self.data.load_reference_data()
        if "demo" in steps:
            log("\n[demo] demo data")
            self.data.load_demo_data(self.args.demo_data)

        self.summary(time.time() - started)

    def summary(self, seconds: float) -> None:
        created: dict[str, int] = {}
        errors: list[str] = []
        for deployer in self.deployers:
            for what, count in deployer.created.items():
                created[what] = created.get(what, 0) + count
            errors.extend(deployer.errors)
        log("\n" + "─" * 64)
        log(f"Created: " + (", ".join(f"{v} {k}" for k, v in sorted(created.items())) or "nothing new"))
        log(f"API calls: {self.dv.calls}   elapsed: {seconds:.0f}s")
        if errors:
            log(f"\n{len(errors)} problem(s):")
            for error in errors:
                log(f"  ! {error}")
            log("\nFix the cause and run the same command again — finished work is skipped.")
        else:
            log("No errors.")
        if not self.dv.dry_run:
            app = self.config["app"]
            log(
                f"\nOpen the app:\n  {self.dv.org_url}/main.aspx?appname={app['uniquename']}"
                f"\n  (or Power Apps > Apps > {app['name']})"
            )

    def status(self) -> None:
        me = self.dv.whoami()
        log(f"Signed in. UserId {me.get('UserId')}  OrganizationId {me.get('OrganizationId')}")
        solution = self.dv.first(
            f"solutions?$select=uniquename,version,ismanaged&$filter=uniquename eq "
            f"'{self.config['solution']['uniquename']}'"
        )
        log(f"Solution: {solution or 'not installed'}")
        present = missing = 0
        for table in self.model.custom_tables:
            if self.dv.entity_metadata(table.logical):
                present += 1
            else:
                missing += 1
        log(f"Tables: {present} present, {missing} missing (of {len(self.model.custom_tables)} custom)")
        app = self.dv.first(
            f"appmodules?$select=name,uniquename,ispublished&$filter=uniquename eq "
            f"'{self.config['app']['uniquename']}'"
        )
        log(f"App: {app or 'not created'}")
        for role in (self.config.get("security", {}) or {}).get("roles", []):
            escaped = role["name"].replace("'", "''")
            found = self.dv.first(f"roles?$select=roleid&$filter=name eq '{escaped}'")
            log(f"Role '{role['name']}': {'present' if found else 'missing'}")


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Deploy the CRM pilot to Dataverse.")
    parser.add_argument("command", choices=["plan", "deploy", "status"])
    parser.add_argument("--org", default=os.environ.get("DATAVERSE_URL"),
                        help="environment URL, e.g. https://contoso.crm.dynamics.com")
    parser.add_argument("--profile", default="law-crm-starter", help="schema profile to deploy")
    parser.add_argument("--model", help="path to an already-exported model JSON")
    parser.add_argument("--config", default=str(PILOT / "app" / "crm-app.yaml"))
    parser.add_argument("--demo-data", default=str(PILOT / "data" / "demo-fabrikam.yaml"))
    parser.add_argument("--prefix", help="override the publisher prefix in the config")
    parser.add_argument("--steps", help=f"comma-separated subset of: {', '.join(ALL_STEPS)}")
    parser.add_argument("--skip", help="comma-separated steps to leave out")
    parser.add_argument("--auth", choices=["device", "secret"], default="device")
    parser.add_argument("--tenant", default=os.environ.get("DATAVERSE_TENANT", "common"))
    parser.add_argument("--client-id", default=os.environ.get("DATAVERSE_CLIENT_ID"))
    parser.add_argument("--client-secret")
    parser.add_argument("--state-dir")
    parser.add_argument("--payload-dir", default=str(PILOT / "build" / "payloads"))
    parser.add_argument("--assign-me", action="store_true",
                        help="give the signed-in user the Partner role after deployment")
    parser.add_argument("--continue-on-error", action="store_true", default=True)
    parser.add_argument("--stop-on-error", dest="continue_on_error", action="store_false")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command != "plan" and not args.org:
        raise SystemExit("--org (or DATAVERSE_URL) is required for deploy and status")
    runner = Runner(args)

    if args.command == "plan":
        runner.print_scope()
        runner.write_payloads(pathlib.Path(args.payload_dir))
        return 0

    if args.command == "status":
        runner.status()
        return 0

    steps = [s.strip() for s in (args.steps.split(",") if args.steps else ALL_STEPS) if s.strip()]
    unknown = [s for s in steps if s not in ALL_STEPS]
    if unknown:
        raise SystemExit(f"unknown step(s): {', '.join(unknown)}")
    if args.skip:
        skip = {s.strip() for s in args.skip.split(",")}
        steps = [s for s in steps if s not in skip]
    runner.print_scope()
    log(f"\nDeploying to {runner.dv.org_url}: {', '.join(steps)}")
    try:
        runner.run_steps(steps)
    except DataverseError as exc:
        log(f"\nSTOPPED: {exc}")
        return 2
    return 1 if any(d.errors for d in runner.deployers) else 0


if __name__ == "__main__":
    sys.exit(main())
