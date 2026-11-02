"""Security roles for the three CRM personas."""

from __future__ import annotations

from .client import DataverseError
from .schema_steps import COMPONENT_ROLE, Deployer

DEPTH_ORDER = ["basic", "local", "deep", "global"]
DEPTH_API = {"basic": "Basic", "local": "Local", "deep": "Deep", "global": "Global"}
CAN_BE = {"basic": "CanBeBasic", "local": "CanBeLocal", "deep": "CanBeDeep", "global": "CanBeGlobal"}
PRIVILEGE_TYPES = {
    "create": "Create", "read": "Read", "write": "Write", "delete": "Delete",
    "append": "Append", "appendto": "AppendTo", "assign": "Assign", "share": "Share",
}
READONLY = {"read": "global"}

# Every model-driven app user needs these, whatever their persona.
PLATFORM_PRIVILEGES: dict[str, dict[str, str]] = {
    "appmodule": {"read": "global"},
    "sitemap": {"read": "global"},
    "systemform": {"read": "global"},
    "webresource": {"read": "global"},
    "savedquery": {"read": "global"},
    "userquery": {"create": "basic", "read": "basic", "write": "basic", "delete": "basic",
                  "append": "basic", "appendto": "basic", "assign": "basic", "share": "basic"},
    "savedqueryvisualization": {"read": "global"},
    "userqueryvisualization": {"create": "basic", "read": "basic", "write": "basic", "delete": "basic",
                               "append": "basic", "appendto": "basic", "assign": "basic", "share": "basic"},
    "systemuser": {"read": "global", "append": "global", "appendto": "global"},
    "team": {"read": "global", "appendto": "global"},
    "businessunit": {"read": "global"},
    "transactioncurrency": {"read": "global"},
    "annotation": {"create": "basic", "read": "local", "write": "basic", "delete": "basic",
                   "append": "basic", "appendto": "local"},
    "activitypointer": {"read": "local", "appendto": "local"},
    "processsession": {"read": "basic"},
    "asyncoperation": {"read": "basic"},
}


class SecurityDeployer(Deployer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.role_ids: dict[str, str] = {}
        self._privilege_cache: dict[str, dict[str, dict]] = {}

    # ------------------------------------------------------------------ lookup
    def root_business_unit(self) -> str | None:
        cached = self.state.get("root_bu")
        if cached:
            return cached
        if self.dv.dry_run:
            return "00000000-0000-0000-0000-000000000000"
        row = self.dv.first("businessunits?$select=businessunitid&$filter=parentbusinessunitid eq null")
        if row:
            self.state.set("root_bu", row["businessunitid"])
            return row["businessunitid"]
        return None

    def privileges_for(self, logical: str) -> dict[str, dict]:
        """PrivilegeType -> privilege metadata, read from the table's own metadata."""
        if logical in self._privilege_cache:
            return self._privilege_cache[logical]
        privileges: dict[str, dict] = {}
        if not self.dv.dry_run:
            try:
                metadata = self.dv.get(f"EntityDefinitions(LogicalName='{logical}')?$select=LogicalName,Privileges")
                for privilege in metadata.get("Privileges", []) or []:
                    privileges[privilege["PrivilegeType"]] = privilege
            except DataverseError as exc:
                self.log(f"  ! could not read privileges for {logical}: {exc}")
        self._privilege_cache[logical] = privileges
        return privileges

    @staticmethod
    def clamp(privilege: dict, wanted: str) -> str | None:
        """Deepest depth the privilege allows, no deeper than asked for."""
        if wanted in ("none", None):
            return None
        index = DEPTH_ORDER.index(wanted)
        for depth in reversed(DEPTH_ORDER[: index + 1]):
            if privilege.get(CAN_BE[depth], False):
                return DEPTH_API[depth]
        return None

    # ------------------------------------------------------------------- roles
    def ensure_roles(self) -> None:
        business_unit = self.root_business_unit()
        for spec in (self.config.get("security", {}) or {}).get("roles", []) or []:
            role_id = self.ensure_role(spec, business_unit)
            if not role_id:
                continue
            self.role_ids[spec["name"]] = role_id
            self.grant_privileges(spec, role_id)
            self.add_to_solution(role_id, COMPONENT_ROLE)

    def ensure_role(self, spec: dict, business_unit: str | None) -> str | None:
        name = spec["name"]
        key = f"role:{name}"
        cached = self.state.get(key)
        if cached:
            return cached
        if not self.dv.dry_run:
            escaped = name.replace("'", "''")
            existing = self.dv.first(f"roles?$select=roleid&$filter=name eq '{escaped}' and parentroleid eq null")
            if existing:
                self.state.set(key, existing["roleid"])
                self.log(f"  · role {name} already there")
                return existing["roleid"]
        body = {"name": name, "description": spec.get("description", "")}
        if business_unit:
            body["businessunitid@odata.bind"] = f"/businessunits({business_unit})"
        role_id = None
        with self.guard(f"role {name}"):
            result = self.dv.post("roles", body, {"MSCRM.SolutionUniqueName": self.solution})
            role_id = (result or {}).get("id")
            self.count("roles")
            self.log(f"  + role {name}")
        if role_id:
            self.state.set(key, role_id)
        return role_id

    def wanted_privileges(self, spec: dict) -> dict[str, dict[str, str]]:
        """Table logical name -> {privilege: depth} for one role."""
        defaults = spec.get("defaults", {}) or {}
        overrides = spec.get("overrides", {}) or {}
        readonly = set(spec.get("readonly", []) or [])
        wanted: dict[str, dict[str, str]] = {}
        for name, table in self.model.tables.items():
            if name in readonly:
                depths = dict(READONLY)
            else:
                depths = dict(defaults)
                depths.update(overrides.get(name, {}) or {})
            if depths:
                wanted[table.logical] = depths
        for logical, depths in (spec.get("native", {}) or {}).items():
            wanted[logical] = {**wanted.get(logical, {}), **depths}
        for logical, depths in PLATFORM_PRIVILEGES.items():
            merged = dict(depths)
            merged.update(wanted.get(logical, {}))
            wanted[logical] = merged
        return wanted

    def grant_privileges(self, spec: dict, role_id: str) -> None:
        key = f"roleprivileges:{spec['name']}"
        if self.state.done(key):
            return
        payload: list[dict] = []
        for logical, depths in self.wanted_privileges(spec).items():
            available = self.privileges_for(logical)
            for action, depth in depths.items():
                privilege_type = PRIVILEGE_TYPES.get(action)
                if not privilege_type:
                    self.log(f"  ! unknown privilege '{action}' on {logical}")
                    continue
                privilege = available.get(privilege_type)
                if not privilege:
                    if not self.dv.dry_run:
                        continue
                    privilege = {"PrivilegeId": "00000000-0000-0000-0000-000000000000", CAN_BE[depth]: True}
                resolved = self.clamp(privilege, depth)
                if not resolved:
                    continue
                payload.append({"Depth": resolved, "PrivilegeId": privilege["PrivilegeId"]})
        if not payload:
            return
        granted = 0
        for index in range(0, len(payload), 100):
            batch = payload[index : index + 100]
            with self.guard(f"privileges for {spec['name']} ({index + 1}-{index + len(batch)})"):
                self.dv.post(
                    f"roles({role_id})/Microsoft.Dynamics.CRM.AddPrivilegesRole", {"Privileges": batch}
                )
                granted += len(batch)
        self.state.set(key, True)
        self.count("privilege grants", granted)
        self.log(f"  + {granted} privileges on {spec['name']}")

    # --------------------------------------------------------------- app roles
    def attach_roles_to_app(self, app_id: str | None) -> None:
        if not app_id:
            return
        for name, role_id in self.role_ids.items():
            key = f"approle:{name}"
            if self.state.done(key):
                continue
            with self.guard(f"give role '{name}' access to the app"):
                self.dv.post(
                    f"appmodules({app_id})/appmoduleroles_association/$ref",
                    {"@odata.id": f"{self.dv.base}/roles({role_id})"},
                    tolerate_duplicate=True,
                )
                self.state.set(key, True)
                self.log(f"  + app access for {name}")

    def assign_role_to_me(self, role_name: str | None = None) -> None:
        """Give the signed-in user the Partner role, so the app is usable immediately."""
        if self.dv.dry_run:
            return
        role_name = role_name or next(
            (r["name"] for r in (self.config.get("security", {}) or {}).get("roles", []) if "Partner" in r["name"]),
            None,
        )
        role_id = self.role_ids.get(role_name) if role_name else None
        if not role_id:
            return
        with self.guard(f"assign '{role_name}' to the signed-in user"):
            me = self.dv.whoami()
            user_id = me.get("UserId")
            self.dv.post(
                f"systemusers({user_id})/systemuserroles_association/$ref",
                {"@odata.id": f"{self.dv.base}/roles({role_id})"},
                tolerate_duplicate=True,
            )
            self.log(f"  + assigned '{role_name}' to you ({user_id})")
