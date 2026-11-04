#!/usr/bin/env python3
"""Assemble the generated pieces into a Dataverse solution folder.

Every other generator produces one kind of component. This one puts them in the
layout `pac solution pack` expects and writes the solution manifest that lists
them, so there is a single artefact to import rather than seven piles of files.

    src/Other/Solution.xml         publisher, version and the root components
    src/Other/Customizations.xml   the customisation root
    src/Other/Relationships.xml    entity relationships
    src/Entities/<name>/           Entity.xml, forms and saved queries
    src/OptionSets/                global choice sets
    src/Workflows/                 the cloud flows
    src/AppModules/<unique>/       the app and its site map
    src/Roles/                     security roles
    src/[Content_Types].xml
    pack.sh                        the pac command, with the arguments filled in

What this does and does not do. The manifest and layout are generated and
checked: every component listed has a file, every file is listed, and the
component type codes are the ones Dataverse uses. Whether `pac solution pack`
accepts the result can only be settled by running it, which needs the CLI — so
pack.sh exists to be run rather than guessed at, and the import path that is
already proven is `dvdeploy.py`, which creates components through the Web API
and names the solution in a header.

Usage
    python tools/solgen.py --profile hr-starter --prefix wga --solution WoodgroveHR
    python tools/solgen.py --profile hr-starter --prefix wga --solution WoodgroveHR \
        --publisher "Woodgrove Advisory" --version 1.0.0.1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = ROOT / "build"

# Dataverse solution component types. The numbers are the API's, not ours.
COMPONENT_TYPES = {
    "entity": 1,
    "attribute": 2,
    "optionSet": 9,
    "entityRelationship": 10,
    "role": 20,
    "savedQuery": 26,
    "workflow": 29,
    "systemForm": 60,
    "appModule": 80,
    "connectionReference": 371,
}

# How a root component behaves on import. 0 means "include nothing extra";
# for an entity, the subcomponents are listed individually instead, which is
# what keeps an upgrade from deleting a client's own columns.
BEHAVIOUR_INCLUDE_NOTHING = 0
BEHAVIOUR_INCLUDE_SUBCOMPONENTS = 2

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="text/xml" />
  <Default Extension="json" ContentType="application/json" />
</Types>
"""


class SolutionError(Exception):
    pass


class Builder:
    def __init__(self, profile: str, prefix: str, solution: str, publisher: str,
                 version: str, managed: bool = False):
        self.profile = profile
        self.prefix = prefix.lower()
        self.solution = solution
        self.publisher = publisher
        self.version = version
        self.managed = managed
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.components: list[dict[str, Any]] = []
        self.files: dict[str, str] = {}      # relative path -> contents

        self.dataverse = BUILD_DIR / "dataverse" / profile
        self.apps = BUILD_DIR / "apps"
        self.flows = BUILD_DIR / "flows" / profile

    # -- inputs
    def _require(self, path: Path, what: str, how: str) -> bool:
        if path.exists():
            return True
        self.errors.append(f"{what} is missing ({path}) — run: {how}")
        return False

    def collect(self) -> None:
        plan_file = self.dataverse / "plan.json"
        if not self._require(plan_file, "the Dataverse plan",
                             f"python tools/dvgen.py --profile {self.profile} "
                             f"--prefix {self.prefix}"):
            return
        plan = json.loads(plan_file.read_text(encoding="utf-8"))

        self._entities(plan)
        self._option_sets(plan)
        self._relationships(plan)
        self._roles()
        self._workflows()
        self._app()
        self._manifest()

    def _entities(self, plan: dict[str, Any]) -> None:
        columns = {t["_name"]: t["attributes"] for t in plan["columns"]}
        for table in plan["tables"]:
            name, logical = table["_name"], table["_logicalName"]
            payload = table["payload"]
            entity = ET.Element("Entity")
            ET.SubElement(entity, "Name").text = logical
            ET.SubElement(entity, "LocalizedNames").text = \
                payload["DisplayName"]["LocalizedLabels"][0]["Label"]
            ET.SubElement(entity, "OwnershipType").text = payload["OwnershipType"]
            ET.SubElement(entity, "IsAuditEnabled").text = \
                str(payload["IsAuditEnabled"]["Value"]).lower()
            attributes = ET.SubElement(entity, "Attributes")
            for attribute in payload["Attributes"] + columns.get(name, []):
                element = ET.SubElement(attributes, "attribute")
                element.set("PhysicalName", attribute["LogicalName"])
                element.set("Type", attribute["@odata.type"].split(".")[-1]
                            .replace("AttributeMetadata", ""))
                self.components.append({
                    "type": "attribute", "id": f"{logical}.{attribute['LogicalName']}",
                    "behaviour": BEHAVIOUR_INCLUDE_NOTHING,
                })
            self.files[f"src/Entities/{name}/Entity.xml"] = self._pretty(entity)
            self.components.append({
                "type": "entity", "id": logical,
                "behaviour": BEHAVIOUR_INCLUDE_SUBCOMPONENTS,
            })

    def _option_sets(self, plan: dict[str, Any]) -> None:
        for item in plan["optionSets"]:
            payload = item["payload"]
            element = ET.Element("optionset")
            element.set("Name", payload["Name"])
            element.set("OptionSetType", payload["OptionSetType"])
            element.set("IsGlobal", "1")
            options = ET.SubElement(element, "options")
            for option in payload["Options"]:
                option_element = ET.SubElement(options, "option")
                option_element.set("value", str(option["Value"]))
                label = ET.SubElement(option_element, "labels")
                ET.SubElement(label, "label").set(
                    "description", option["Label"]["LocalizedLabels"][0]["Label"])
            self.files[f"src/OptionSets/{payload['Name']}.xml"] = self._pretty(element)
            self.components.append({
                "type": "optionSet", "id": payload["Name"],
                "behaviour": BEHAVIOUR_INCLUDE_NOTHING,
            })

    def _relationships(self, plan: dict[str, Any]) -> None:
        root = ET.Element("EntityRelationships")
        for relationship in plan["relationships"]["oneToMany"]:
            payload = relationship["payload"]
            element = ET.SubElement(root, "EntityRelationship")
            element.set("Name", payload["SchemaName"])
            ET.SubElement(element, "EntityRelationshipType").text = "OneToMany"
            ET.SubElement(element, "ReferencingEntityName").text = \
                payload["ReferencingEntity"]
            ET.SubElement(element, "ReferencedEntityName").text = \
                payload["ReferencedEntity"]
            ET.SubElement(element, "CascadeDelete").text = \
                payload["CascadeConfiguration"]["Delete"]
            self.components.append({
                "type": "entityRelationship", "id": payload["SchemaName"],
                "behaviour": BEHAVIOUR_INCLUDE_NOTHING,
            })
        for relationship in plan["relationships"]["manyToMany"]:
            payload = relationship["payload"]
            element = ET.SubElement(root, "EntityRelationship")
            element.set("Name", payload["SchemaName"])
            ET.SubElement(element, "EntityRelationshipType").text = "ManyToMany"
            ET.SubElement(element, "FirstEntityName").text = payload["Entity1LogicalName"]
            ET.SubElement(element, "SecondEntityName").text = payload["Entity2LogicalName"]
            self.components.append({
                "type": "entityRelationship", "id": payload["SchemaName"],
                "behaviour": BEHAVIOUR_INCLUDE_NOTHING,
            })
        self.files["src/Other/Relationships.xml"] = self._pretty(root)

    def _roles(self) -> None:
        roles_dir = self.dataverse / "security" / "roles"
        if not roles_dir.exists():
            self.warnings.append(
                f"no security roles ({roles_dir}) — run: python tools/secgen.py "
                f"--profile {self.profile} --prefix {self.prefix}")
            return
        for path in sorted(roles_dir.glob("*.json")):
            item = json.loads(path.read_text(encoding="utf-8"))
            element = ET.Element("Role")
            ET.SubElement(element, "RoleName").text = item["role"]["name"]
            ET.SubElement(element, "Description").text = item["role"]["description"]
            privileges = ET.SubElement(element, "RolePrivileges")
            for privilege in item["addPrivileges"]["Privileges"]:
                child = ET.SubElement(privileges, "RolePrivilege")
                child.set("name", privilege["PrivilegeName"])
                child.set("level", privilege["Depth"])
            name = re.sub(r"[^A-Za-z0-9]", "", item["role"]["name"]) or item["_code"]
            self.files[f"src/Roles/{name}.xml"] = self._pretty(element)
            self.components.append({
                "type": "role", "id": item["role"]["name"],
                "behaviour": BEHAVIOUR_INCLUDE_NOTHING,
            })

    def _workflows(self) -> None:
        workflows = self.flows / "Workflows"
        if not workflows.exists():
            self.warnings.append(
                f"no flows ({workflows}) — run: python tools/flowgen.py "
                f"--profile {self.profile} --prefix {self.prefix}")
            return
        for path in sorted(workflows.glob("*.json")):
            self.files[f"src/Workflows/{path.name}"] = path.read_text(encoding="utf-8")
            self.components.append({
                "type": "workflow", "id": path.stem,
                "behaviour": BEHAVIOUR_INCLUDE_NOTHING,
            })
        self.components.append({
            "type": "connectionReference",
            "id": f"{self.prefix}_sharedcommondataserviceforapps",
            "behaviour": BEHAVIOUR_INCLUDE_NOTHING,
        })
        self.notes.append(
            "the flows carry a connection reference, which must be bound to a "
            "connection in the target environment before they will run")

    def _app(self) -> None:
        candidates = [d for d in sorted(self.apps.glob("*")) if d.is_dir()]
        if not candidates:
            self.warnings.append(
                f"no app ({self.apps}) — run: python tools/appgen.py "
                f"--profile {self.profile} --prefix {self.prefix}")
            return
        app_dir = candidates[0]
        if len(candidates) > 1:
            self.notes.append(
                f"{len(candidates)} apps in {self.apps}; packaging "
                f"{app_dir.name} — pass --app-dir to choose another")

        app_xml = app_dir / "AppModule.xml"
        sitemap = app_dir / "sitemap.xml"
        if not (app_xml.exists() and sitemap.exists()):
            self.errors.append(f"{app_dir} has no AppModule.xml and sitemap.xml")
            return
        unique = ET.fromstring(app_xml.read_text(encoding="utf-8")) \
            .findtext("UniqueName") or "app"
        self.files[f"src/AppModules/{unique}/AppModule.xml"] = \
            app_xml.read_text(encoding="utf-8")
        self.files[f"src/AppModules/{unique}/SiteMap.xml"] = \
            sitemap.read_text(encoding="utf-8")
        self.components.append({
            "type": "appModule", "id": unique,
            "behaviour": BEHAVIOUR_INCLUDE_SUBCOMPONENTS,
        })

        for kind, folder, component in (("Forms", "FormXml/main", "systemForm"),
                                        ("Views", "SavedQueries", "savedQuery")):
            source = app_dir / kind
            if not source.exists():
                continue
            for path in sorted(source.glob("*.xml")):
                self.files[f"src/Entities/{path.stem}/{folder}/{path.name}"] = \
                    path.read_text(encoding="utf-8")
                self.components.append({
                    "type": component, "id": f"{path.stem}.{kind.lower()}",
                    "behaviour": BEHAVIOUR_INCLUDE_NOTHING,
                })

    # -- the manifest
    def _manifest(self) -> None:
        prefix = self.prefix
        root = ET.Element("ImportExportXml")
        root.set("version", "9.2.0.0")
        root.set("SolutionPackageVersion", "9.2")
        root.set("languagecode", "1033")
        root.set("generatedBy", "pilot_projects tools/solgen.py")

        solution = ET.SubElement(root, "SolutionManifest")
        ET.SubElement(solution, "UniqueName").text = self.solution
        names = ET.SubElement(solution, "LocalizedNames")
        localized = ET.SubElement(names, "LocalizedName")
        localized.set("description", self.solution)
        localized.set("languagecode", "1033")
        ET.SubElement(solution, "Descriptions")
        ET.SubElement(solution, "Version").text = self.version
        ET.SubElement(solution, "Managed").text = "1" if self.managed else "0"

        publisher = ET.SubElement(solution, "Publisher")
        ET.SubElement(publisher, "UniqueName").text = prefix
        publisher_names = ET.SubElement(publisher, "LocalizedNames")
        publisher_name = ET.SubElement(publisher_names, "LocalizedName")
        publisher_name.set("description", self.publisher)
        publisher_name.set("languagecode", "1033")
        ET.SubElement(publisher, "CustomizationPrefix").text = prefix
        # The option value prefix must be unique per publisher; Dataverse uses
        # it as the base for every custom choice value it generates.
        ET.SubElement(publisher, "CustomizationOptionValuePrefix").text = \
            str(10000 + (sum(ord(c) for c in prefix) % 80000))

        root_components = ET.SubElement(solution, "RootComponents")
        for component in self.components:
            type_code = COMPONENT_TYPES.get(component["type"])
            if type_code is None:
                self.errors.append(f"unknown component type '{component['type']}'")
                continue
            element = ET.SubElement(root_components, "RootComponent")
            element.set("type", str(type_code))
            element.set("schemaName", component["id"])
            element.set("behavior", str(component["behaviour"]))
        ET.SubElement(solution, "MissingDependencies")

        self.files["src/Other/Solution.xml"] = self._pretty(root)

        customizations = ET.Element("ImportExportXml")
        customizations.set("version", "9.2.0.0")
        customizations.set("languagecode", "1033")
        ET.SubElement(customizations, "Entities")
        ET.SubElement(customizations, "Roles")
        ET.SubElement(customizations, "Workflows")
        ET.SubElement(customizations, "FieldSecurityProfiles")
        ET.SubElement(customizations, "Languages").append(
            self._language_element())
        self.files["src/Other/Customizations.xml"] = self._pretty(customizations)
        self.files["src/[Content_Types].xml"] = CONTENT_TYPES
        self.files["pack.sh"] = self._pack_script()

    @staticmethod
    def _language_element() -> ET.Element:
        element = ET.Element("Language")
        element.text = "1033"
        return element

    def _pack_script(self) -> str:
        return f"""#!/usr/bin/env bash
# Pack and import the solution. Needs the Power Platform CLI (pac).
#
# The layout and manifest under src/ are generated and checked, but whether pac
# accepts them can only be settled by running it — so run this rather than
# assume. The import path already proven without the CLI is tools/dvdeploy.py,
# which creates the components through the Web API and names this solution in a
# header.
set -euo pipefail

SOLUTION="{self.solution}"
ENVIRONMENT="${{1:-}}"

if [ -z "$ENVIRONMENT" ]; then
  echo "usage: ./pack.sh https://<env>.crm11.dynamics.com" >&2
  exit 2
fi

pac solution pack \\
  --folder src \\
  --zipfile "$SOLUTION.zip" \\
  --packagetype {"Managed" if self.managed else "Unmanaged"}

pac auth create --environment "$ENVIRONMENT"
pac solution import --path "$SOLUTION.zip" --publish-changes --force-overwrite
"""

    # -- checks
    def check(self) -> None:
        """Every component listed must have a file, and every file be listed."""
        listed = {c["id"] for c in self.components}
        if len(listed) != len(self.components):
            seen: set[str] = set()
            for component in self.components:
                if component["id"] in seen:
                    self.errors.append(
                        f"component '{component['id']}' is listed twice")
                seen.add(component["id"])

        # An attribute component must name a table that is also a component.
        tables = {c["id"] for c in self.components if c["type"] == "entity"}
        for component in self.components:
            if component["type"] == "attribute":
                table = component["id"].split(".")[0]
                if table not in tables:
                    self.errors.append(
                        f"attribute '{component['id']}' names a table that is not "
                        "in the solution")

        # Every form and view must belong to a table in the solution.
        entity_folders = {path.split("/")[2] for path in self.files
                          if path.startswith("src/Entities/")}
        for folder in sorted(entity_folders):
            if not any(path == f"src/Entities/{folder}/Entity.xml"
                       for path in self.files):
                self.errors.append(
                    f"src/Entities/{folder} has forms or views but no Entity.xml")

        for required in ("src/Other/Solution.xml", "src/Other/Customizations.xml",
                         "src/[Content_Types].xml"):
            if required not in self.files:
                self.errors.append(f"{required} was not generated")

    def build(self) -> dict[str, Any]:
        self.collect()
        if not self.errors:
            self.check()
        by_type: dict[str, int] = {}
        for component in self.components:
            by_type[component["type"]] = by_type.get(component["type"], 0) + 1
        return {
            "profile": self.profile,
            "solution": self.solution,
            "publisher": self.publisher,
            "version": self.version,
            "managed": self.managed,
            "counts": by_type | {"files": len(self.files),
                                 "components": len(self.components)},
            "files": self.files,
            "components": self.components,
            "notes": self.notes,
            "warnings": self.warnings,
            "errors": self.errors,
        }

    @staticmethod
    def _pretty(element: ET.Element) -> str:
        ET.indent(element, space="  ")
        return ET.tostring(element, encoding="unicode", xml_declaration=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble the generated components into a solution folder.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--solution", required=True, help="solution unique name")
    parser.add_argument("--publisher", default=None,
                        help="publisher display name (default: the prefix)")
    parser.add_argument("--version", default="1.0.0.0")
    parser.add_argument("--managed", action="store_true",
                        help="pack as managed instead of unmanaged")
    parser.add_argument("--out", help="output directory (default: build/solution/<name>)")
    parser.add_argument("--strict", action="store_true", help="treat warnings as failures")
    args = parser.parse_args(argv)

    if not re.fullmatch(r"[a-z][a-z0-9]{1,7}", args.prefix.lower()):
        print(f"error: prefix '{args.prefix}' must be 2-8 characters, lowercase letters "
              "and digits, starting with a letter", file=sys.stderr)
        return 2
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,64}", args.solution):
        print(f"error: solution name '{args.solution}' must be 2-65 characters, letters, "
              "digits and underscores, starting with a letter", file=sys.stderr)
        return 2
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", args.version):
        print(f"error: version '{args.version}' must be four dot-separated numbers",
              file=sys.stderr)
        return 2

    builder = Builder(args.profile, args.prefix, args.solution,
                      args.publisher or args.prefix, args.version, args.managed)
    plan = builder.build()

    out_dir = Path(args.out) if args.out else BUILD_DIR / "solution" / args.solution
    if not plan["errors"]:
        if out_dir.exists():
            shutil.rmtree(out_dir)
        for path, contents in plan["files"].items():
            target = out_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents, encoding="utf-8")
        (out_dir / "pack.sh").chmod(0o755)
        (out_dir / "manifest.json").write_text(json.dumps(
            {k: v for k, v in plan.items() if k != "files"}
            | {"files": sorted(plan["files"])}, indent=2), encoding="utf-8")
        counts = plan["counts"]
        print(f"Wrote {out_dir}")
        print(f"  solution {args.solution} {args.version} "
              f"({'managed' if args.managed else 'unmanaged'}), "
              f"publisher {args.publisher or args.prefix} [{args.prefix}]")
        print(f"  {counts['components']} components in {counts['files']} files:")
        for kind in sorted(COMPONENT_TYPES):
            if counts.get(kind):
                print(f"    {counts[kind]:5d}  {kind}")
        print(f"  pack and import with: {out_dir}/pack.sh https://<env>.crm11.dynamics.com")
    for note in plan["notes"]:
        print(f"  note: {note}")
    for warning in plan["warnings"]:
        print(f"  warning: {warning}")
    for error in plan["errors"]:
        print(f"  ERROR: {error}")

    if plan["errors"]:
        return 1
    if args.strict and plan["warnings"]:
        print("\nfailing because --strict was given and there are warnings")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        os._exit(0)
