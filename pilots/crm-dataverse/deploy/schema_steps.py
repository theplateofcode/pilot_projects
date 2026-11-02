"""Deployment steps that create schema: publisher, solution, choices, tables, columns, keys."""

from __future__ import annotations

from .client import Dataverse, DataverseError
from .model import CrmModel, Table

# Solution component types used here (Dataverse componenttype values).
COMPONENT_ENTITY = 1
COMPONENT_ATTRIBUTE = 2
COMPONENT_RELATIONSHIP = 10
COMPONENT_OPTIONSET = 9
COMPONENT_ENTITY_KEY = 14
COMPONENT_ROLE = 20
COMPONENT_SAVED_QUERY = 26
COMPONENT_SYSTEM_FORM = 60
COMPONENT_SITEMAP = 62
COMPONENT_CHART = 59
COMPONENT_APPMODULE = 80


class Deployer:
    """Shared plumbing for every step: logging, state, solution membership."""

    def __init__(self, dv: Dataverse, model: CrmModel, config: dict, state, log, continue_on_error: bool = False):
        self.dv = dv
        self.model = model
        self.config = config
        self.state = state
        self.log = log
        self.continue_on_error = continue_on_error
        self.solution = config["solution"]["uniquename"]
        self.errors: list[str] = []
        self.created: dict[str, int] = {}

    # ----------------------------------------------------------------- helpers
    def count(self, what: str, n: int = 1) -> None:
        self.created[what] = self.created.get(what, 0) + n

    def guard(self, description: str):
        """Context manager that records a failure instead of aborting, when asked to."""
        deployer = self

        class _Guard:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, _tb):
                if exc is None:
                    return False
                if isinstance(exc, DataverseError) and exc.is_duplicate:
                    deployer.log(f"  · {description}: already there")
                    return True
                message = f"{description}: {exc}"
                deployer.errors.append(message)
                deployer.log(f"  ! {message}")
                return deployer.continue_on_error

        return _Guard()

    def add_to_solution(self, component_id: str | None, component_type: int, include_subcomponents: bool = False) -> None:
        if not component_id:
            return
        with self.guard(f"add component {component_type} {component_id} to solution"):
            self.dv.post(
                "AddSolutionComponent",
                {
                    "ComponentId": component_id,
                    "ComponentType": component_type,
                    "SolutionUniqueName": self.solution,
                    "AddRequiredComponents": False,
                    "DoNotIncludeSubcomponents": not include_subcomponents,
                },
                tolerate_duplicate=True,
            )

    # ------------------------------------------------------- publisher/solution
    def ensure_publisher(self) -> str | None:
        spec = self.config["solution"]["publisher"]
        unique = spec["uniquename"]
        cached = self.state.get("publisher_id")
        if cached:
            return cached
        existing = self.dv.first(f"publishers?$select=publisherid,customizationprefix&$filter=uniquename eq '{unique}'")
        if existing:
            if existing.get("customizationprefix") != spec["prefix"]:
                self.log(
                    f"  ! publisher '{unique}' exists with prefix "
                    f"'{existing.get('customizationprefix')}', not '{spec['prefix']}' — using the existing prefix "
                    f"would change every logical name, so fix the prefix in crm-app.yaml or use another publisher"
                )
                raise DataverseError("publisher prefix mismatch")
            self.state.set("publisher_id", existing["publisherid"])
            self.log(f"  · publisher {unique} already there")
            return existing["publisherid"]
        publisher_id = self.dv.create(
            "publishers",
            {
                "uniquename": unique,
                "friendlyname": spec["friendlyname"],
                "customizationprefix": spec["prefix"],
                "customizationoptionvalueprefix": int(spec.get("option_value_prefix", 32100)),
                "description": "Publisher for the pilot-project solutions.",
            },
        )
        self.state.set("publisher_id", publisher_id)
        self.count("publishers")
        self.log(f"  + publisher {unique} ({spec['prefix']})")
        return publisher_id

    def ensure_solution(self, publisher_id: str | None) -> str | None:
        spec = self.config["solution"]
        unique = spec["uniquename"]
        cached = self.state.get("solution_id")
        if cached:
            return cached
        existing = self.dv.first(f"solutions?$select=solutionid,ismanaged&$filter=uniquename eq '{unique}'")
        if existing:
            if existing.get("ismanaged"):
                raise DataverseError(
                    f"solution '{unique}' is installed as managed in this environment; "
                    f"deploy to a clean environment or pick another solution name"
                )
            self.state.set("solution_id", existing["solutionid"])
            self.log(f"  · solution {unique} already there")
            return existing["solutionid"]
        body = {
            "uniquename": unique,
            "friendlyname": spec["friendlyname"],
            "version": spec.get("version", "1.0.0.0"),
            "description": spec.get("description", ""),
        }
        if publisher_id:
            body["publisherid@odata.bind"] = f"/publishers({publisher_id})"
        solution_id = self.dv.create("solutions", body)
        self.state.set("solution_id", solution_id)
        self.count("solutions")
        self.log(f"  + solution {unique}")
        return solution_id

    # ------------------------------------------------------------- choice sets
    def ensure_option_sets(self) -> None:
        for enum_name in sorted(self.model.used_enums):
            name = self.model.option_set_name(enum_name)
            key = f"optionset:{name}"
            if self.state.done(key):
                continue
            payload = self.model.option_set_payload(enum_name)
            existing = None
            if not self.dv.dry_run:
                try:
                    existing = self.dv.get(f"GlobalOptionSetDefinitions(Name='{name}')?$select=MetadataId")
                except DataverseError as exc:
                    if exc.status != 404:
                        raise
            if existing:
                self.state.set(key, existing["MetadataId"])
                self.add_to_solution(existing["MetadataId"], COMPONENT_OPTIONSET)
                continue
            with self.guard(f"choice set {name}"):
                result = self.dv.post(
                    "GlobalOptionSetDefinitions", payload, {"MSCRM.SolutionUniqueName": self.solution}
                )
                metadata_id = (result or {}).get("id")
                self.state.set(key, metadata_id or True)
                self.count("choice sets")
                self.log(f"  + choice set {name} ({len(payload['Options'])} options)")

    def option_set_id(self, enum_name: str) -> str | None:
        name = self.model.option_set_name(enum_name)
        cached = self.state.get(f"optionset:{name}")
        if isinstance(cached, str):
            return cached
        if self.dv.dry_run:
            return "00000000-0000-0000-0000-000000000000"
        found = self.dv.get(f"GlobalOptionSetDefinitions(Name='{name}')?$select=MetadataId")
        metadata_id = found["MetadataId"]
        self.state.set(f"optionset:{name}", metadata_id)
        return metadata_id

    # ------------------------------------------------------------------ tables
    def ensure_tables(self) -> None:
        for table in self.model.custom_tables:
            key = f"table:{table.logical}"
            if self.state.done(key):
                continue
            metadata = None if self.dv.dry_run else self.dv.entity_metadata(table.logical)
            if metadata:
                table.object_type_code = metadata.get("ObjectTypeCode")
                table.entity_set = metadata.get("EntitySetName")
                self.state.set(key, metadata["MetadataId"] if "MetadataId" in metadata else True)
                self.log(f"  · table {table.logical} already there")
                continue
            with self.guard(f"table {table.logical}"):
                result = self.dv.post(
                    "EntityDefinitions",
                    self.model.table_payload(table),
                    {"MSCRM.SolutionUniqueName": self.solution},
                )
                self.state.set(key, (result or {}).get("id", True))
                self.count("tables")
                self.log(f"  + table {table.logical} ({table.display})")
        self.refresh_table_metadata()

    def refresh_table_metadata(self) -> None:
        if self.dv.dry_run:
            for index, table in enumerate(self.model.tables.values()):
                table.object_type_code = table.object_type_code or 10000 + index
                table.entity_set = table.entity_set or f"{table.logical}s"
            return
        for table in self.model.tables.values():
            if table.object_type_code and table.entity_set:
                continue
            metadata = self.dv.entity_metadata(table.logical)
            if metadata:
                table.object_type_code = metadata.get("ObjectTypeCode")
                table.entity_set = metadata.get("EntitySetName")

    def add_tables_to_solution(self) -> None:
        for table in self.model.tables.values():
            if table.is_custom:
                continue  # created straight into the solution
            if not table.columns:
                continue  # nothing of ours on it, so no need to carry it
            metadata = None if self.dv.dry_run else self.dv.entity_metadata(table.logical, ["MetadataId"])
            self.add_to_solution((metadata or {}).get("MetadataId"), COMPONENT_ENTITY)

    # ----------------------------------------------------------------- columns
    def existing_columns(self, table: Table) -> set[str]:
        if self.dv.dry_run:
            return set()
        rows = self.dv.query(
            f"EntityDefinitions(LogicalName='{table.logical}')/Attributes?$select=LogicalName"
        )
        return {row["LogicalName"] for row in rows}

    def ensure_columns(self) -> None:
        for table in self.model.tables.values():
            pending = [c for c in table.columns if c.kind == "attribute" and not c.note.startswith("primary")]
            pending = [c for c in pending if not self.state.done(f"column:{table.logical}:{c.logical}")]
            if not pending:
                continue
            present = self.existing_columns(table)
            for column in pending:
                if column.logical in present:
                    self.state.set(f"column:{table.logical}:{column.logical}", True)
                    continue
                payload = dict(column.payload or {})
                enum_name = column.spec.get("enum")
                if "_globalOptionSet" in payload:
                    payload.pop("_globalOptionSet")
                    payload["GlobalOptionSet@odata.bind"] = (
                        f"/GlobalOptionSetDefinitions({self.option_set_id(enum_name)})"
                    )
                with self.guard(f"column {table.logical}.{column.logical}"):
                    self.dv.post(
                        f"EntityDefinitions(LogicalName='{table.logical}')/Attributes",
                        payload,
                        {"MSCRM.SolutionUniqueName": self.solution},
                    )
                    self.state.set(f"column:{table.logical}:{column.logical}", True)
                    self.count("columns")
            self.log(f"  + {table.logical}: {len(pending)} columns")

    # ----------------------------------------------------------- relationships
    def existing_relationships(self) -> set[str]:
        if self.dv.dry_run:
            return set()
        cached = self.state.get("relationships_seen") or []
        rows = self.dv.query("RelationshipDefinitions?$select=SchemaName")
        return {row["SchemaName"].lower() for row in rows} | {name.lower() for name in cached}

    def ensure_relationships(self) -> None:
        present = self.existing_relationships()
        for table in self.model.tables.values():
            for column in table.columns:
                if column.kind not in ("lookup", "manytomany"):
                    continue
                relationship = column.relationship or {}
                schema_name = relationship.get("SchemaName", "")
                key = f"relationship:{schema_name}"
                if self.state.done(key) or schema_name.lower() in present:
                    self.state.set(key, True)
                    continue
                with self.guard(f"relationship {schema_name}"):
                    self.dv.post(
                        "RelationshipDefinitions", relationship, {"MSCRM.SolutionUniqueName": self.solution}
                    )
                    self.state.set(key, True)
                    self.count("relationships")
                    kind = "N:N" if column.kind == "manytomany" else "lookup"
                    self.log(f"  + {kind} {table.logical}.{column.logical} -> {relationship.get('ReferencedEntity') or relationship.get('Entity2LogicalName')}")

    # ------------------------------------------------------------ alternate keys
    def ensure_keys(self) -> None:
        for table in self.model.custom_tables:
            for payload in self.model.key_payloads(table):
                key = f"key:{payload['SchemaName']}"
                if self.state.done(key):
                    continue
                with self.guard(f"alternate key {payload['SchemaName']}"):
                    self.dv.post(
                        f"EntityDefinitions(LogicalName='{table.logical}')/Keys",
                        payload,
                        {"MSCRM.SolutionUniqueName": self.solution},
                    )
                    self.state.set(key, True)
                    self.count("alternate keys")
                    self.log(f"  + key {payload['SchemaName']}")

    # ----------------------------------------------------------------- publish
    def publish(self, extra_xml: str = "") -> None:
        entities = "".join(f"<entity>{t.logical}</entity>" for t in self.model.tables.values())
        option_sets = "".join(
            f"<optionset>{self.model.option_set_name(e)}</optionset>" for e in sorted(self.model.used_enums)
        )
        xml = (
            "<importexportxml>"
            f"<entities>{entities}</entities>"
            f"<optionsets>{option_sets}</optionsets>"
            f"{extra_xml}"
            "</importexportxml>"
        )
        with self.guard("publish customisations"):
            self.dv.publish_xml(xml)
            self.log("  + published customisations")
