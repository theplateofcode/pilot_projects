#!/usr/bin/env python3
"""Canonical schema toolkit for the pilot projects.

The YAML files under schemas/ are the single source of truth for every pilot.
This tool checks them and turns them into the artefacts people read or build from.

Commands
  validate                 check every layer under schemas/ (exit code 1 on errors)
  docs [--check]           regenerate docs/data-dictionary/ (--check: fail if it is stale)
  export --profile NAME    write the resolved model of one deployment profile as JSON

See docs/04-schema-format.md for the file format.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schemas"
DOCS_DIR = ROOT / "docs" / "data-dictionary"
PROFILES_FILE = SCHEMA_DIR / "profiles.yaml"
BUILD_DIR = ROOT / "build"

KINDS = ("foundation", "module", "bridge", "industry", "client")
OWNERSHIP = ("user", "organization")
VOLUMES = ("low", "medium", "high")
SENSITIVITY = ("public", "internal", "confidential", "restricted")
DATAVERSE_MODES = ("custom", "native", "none")  # or "standard:<table>"
SHAREPOINT_MODES = ("list", "library", "pages", "termstore", "native", "none")
SQL_MODES = ("table", "none")
TRAIT_SCOPES = ("all", "userOwned", "optIn")
ON_DELETE = ("restrict", "setNull", "cascade")
MAX_NAME = 32

# type -> kind of argument it takes in the shorthand, e.g. string(100), lookup(Employee)
TYPE_ARGS = {
    "guid": None,
    "string": "length",
    "text": None,
    "richtext": None,
    "email": None,
    "phone": None,
    "url": None,
    "integer": None,
    "decimal": "precision",
    "percent": None,
    "money": None,
    "boolean": None,
    "date": None,
    "datetime": None,
    "duration": None,
    "choice": "enum",
    "multichoice": "enum",
    "lookup": "entity",
    "parent": "entity",
    "multilookup": "entity",
    "polylookup": "entities",
    "term": "termset",
    "multiterm": "termset",
    "address": None,
    "file": None,
    "image": None,
    "autonumber": None,
}
REQUIRED_ARG = {"enum", "entity", "entities", "termset"}
SHORTHAND = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<args>[^()]*)\))?(?P<req>!)?$")

LAYER_KEYS = {"module", "terminology", "traits", "enums", "termSets", "entities", "extend", "seed"}
MODULE_KEYS = {"id", "name", "kind", "version", "dependsOn", "description"}
ENTITY_KEYS = {
    "description", "label", "plural", "ownership", "volume", "sensitivity", "traits",
    "primary", "platforms", "keys", "attributes", "notes",
}
ATTR_KEYS = {
    "type", "description", "label", "unique", "indexed", "default", "format", "sensitivity",
    "pii", "readOnly", "calculated", "currencyField", "onDelete", "deprecated", "min", "max",
    "dataverse", "sharepoint", "sql",
}
TERMSET_KEYS = {"description", "open", "hierarchical", "terms", "extends"}

# Column names SharePoint (and often Dataverse/SQL) already use for something else.
RESERVED_NAMES = {
    "id", "title", "created", "modified", "author", "editor", "attachments", "contenttype",
    "contenttypeid", "order", "path", "fileref", "fileleafref", "guid", "version", "key",
}

PASCAL = re.compile(r"^[A-Z][A-Za-z0-9]*$")
CAMEL = re.compile(r"^[a-z][A-Za-z0-9]*$")
LAYER_ID = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

AUTO_PRIMARY = {
    "type": "string",
    "length": 200,
    "required": True,
    "readOnly": True,
    "description": "Display label generated automatically (flow, plug-in or trigger). "
    "Required by Dataverse (primary name) and SharePoint (Title).",
}

# Targets so common that drawing them would bury the interesting relationships.
DIAGRAM_SKIP = {"Employee", "Currency", "Country", "Language"}


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def words(name: str) -> str:
    """'JobRequisition' -> 'Job Requisition'."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)


def pluralize(label: str) -> str:
    head, _, last = label.rpartition(" ")
    if re.search(r"(s|x|z|ch|sh)$", last):
        last += "es"
    elif re.search(r"[^aeiou]y$", last):
        last = last[:-1] + "ies"
    else:
        last += "s"
    return f"{head} {last}".strip()


@dataclass
class Layer:
    id: str
    name: str
    kind: str
    version: str
    depends: list
    description: str
    file: Path
    raw: dict
    entities: dict = field(default_factory=dict)
    extensions: dict = field(default_factory=dict)
    seeds: dict = field(default_factory=dict)
    terminology: dict = field(default_factory=dict)

    @property
    def where(self) -> str:
        return f"{rel(self.file)} [{self.id}]"


@dataclass
class Entity:
    name: str
    layer: str
    description: str
    label: str
    plural: str
    ownership: str
    volume: str
    sensitivity: str
    traits: list
    primary: str
    platforms: dict
    keys: list
    attributes: dict
    notes: list
    auto_primary: bool = False


class Model:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.layers: dict[str, Layer] = {}
        self.order: list[str] = []
        self.closure: dict[str, set] = {}
        self.entities: dict[str, Entity] = {}
        self.enums: dict[str, dict] = {}
        self.termsets: dict[str, dict] = {}
        self.term_additions: list[tuple] = []  # (layer id, term set, terms)
        self.traits: dict[str, dict] = {}
        self.ext_owner: dict[tuple, str] = {}  # (entity, attribute) -> layer id

    # ------------------------------------------------------------------ loading
    def err(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def load(self) -> "Model":
        files = sorted(p for p in SCHEMA_DIR.rglob("*.yaml") if p != PROFILES_FILE)
        for path in files:
            try:
                docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d is not None]
            except yaml.YAMLError as exc:
                self.err(rel(path), f"invalid YAML: {exc}")
                continue
            for doc in docs:
                self._load_layer(path, doc)
        self._resolve_dependencies()
        for layer_id in self.order:
            self._register(self.layers[layer_id])
        for layer_id in self.order:
            self._register_extensions(self.layers[layer_id])
        for layer_id in self.order:
            self._check_layer(self.layers[layer_id])
        self._check_seeds()
        return self

    def _load_layer(self, path: Path, doc) -> None:
        if not isinstance(doc, dict) or not isinstance(doc.get("module"), dict):
            self.err(rel(path), "every YAML document must start with a 'module' mapping")
            return
        for key in set(doc) - LAYER_KEYS:
            self.err(rel(path), f"unknown top-level key '{key}'")
        mod = doc["module"]
        for key in set(mod) - MODULE_KEYS:
            self.err(rel(path), f"unknown module key '{key}'")
        layer_id = str(mod.get("id", ""))
        if not LAYER_ID.match(layer_id):
            self.err(rel(path), f"module id '{layer_id}' must be lower-case kebab-case")
            return
        if layer_id in self.layers:
            self.err(rel(path), f"duplicate module id '{layer_id}' (also in {rel(self.layers[layer_id].file)})")
            return
        layer = Layer(
            id=layer_id,
            name=str(mod.get("name", layer_id)),
            kind=mod.get("kind", ""),
            version=str(mod.get("version", "")),
            depends=list(mod.get("dependsOn") or []),
            description=str(mod.get("description", "")).strip(),
            file=path,
            raw=doc,
        )
        if layer.kind not in KINDS:
            self.err(layer.where, f"kind must be one of {', '.join(KINDS)}")
        if not SEMVER.match(layer.version):
            self.err(layer.where, "version must be semantic (MAJOR.MINOR.PATCH)")
        if not layer.description:
            self.err(layer.where, "module description is required")
        self.layers[layer_id] = layer

    def _resolve_dependencies(self) -> None:
        state: dict[str, str] = {}

        def visit(layer_id: str, chain: list) -> set:
            if state.get(layer_id) == "done":
                return self.closure[layer_id]
            if state.get(layer_id) == "busy":
                self.err(self.layers[layer_id].where, "dependency cycle: " + " -> ".join(chain + [layer_id]))
                return {layer_id}
            state[layer_id] = "busy"
            layer = self.layers[layer_id]
            closure = {layer_id}
            for dep in layer.depends:
                if dep not in self.layers:
                    self.err(layer.where, f"depends on unknown module '{dep}'")
                    continue
                closure |= visit(dep, chain + [layer_id])
            state[layer_id] = "done"
            self.closure[layer_id] = closure
            self.order.append(layer_id)
            return closure

        for layer_id in sorted(self.layers, key=self._sort_key):
            visit(layer_id, [])
        foundations = [l.id for l in self.layers.values() if l.kind == "foundation"]
        for layer in self.layers.values():
            if layer.kind != "foundation" and not set(foundations) & self.closure[layer.id]:
                self.err(layer.where, "must depend (directly or indirectly) on a foundation layer")

    def _sort_key(self, layer_id: str):
        layer = self.layers[layer_id]
        return (KINDS.index(layer.kind) if layer.kind in KINDS else 99, rel(layer.file), layer_id)

    def sees(self, layer_id: str, defined_in: str) -> bool:
        return defined_in in self.closure.get(layer_id, {layer_id})

    # -------------------------------------------------------------- registering
    def _claim_name(self, where: str, name: str, what: str) -> bool:
        for kind, registry in (("entity", self.entities), ("enum", self.enums), ("term set", self.termsets)):
            if name in registry:
                other = registry[name]
                owner = other.layer if isinstance(other, Entity) else other["layer"]
                self.err(where, f"{what} name '{name}' is already used by {kind} in layer '{owner}'")
                return False
        return True

    def _register(self, layer: Layer) -> None:
        raw = layer.raw
        if raw.get("traits"):
            if layer.kind != "foundation":
                self.err(layer.where, "traits can only be declared in a foundation layer")
            for name, trait in raw["traits"].items():
                self._register_trait(layer, name, trait)
        for name, enum in (raw.get("enums") or {}).items():
            self._register_enum(layer, name, enum)
        for name, termset in (raw.get("termSets") or {}).items():
            self._register_termset(layer, name, termset)
        for name, ent in (raw.get("entities") or {}).items():
            self._register_entity(layer, name, ent)

    def _register_trait(self, layer: Layer, name: str, trait) -> None:
        where = f"{layer.where} trait {name}"
        if not isinstance(trait, dict):
            self.err(where, "must be a mapping")
            return
        scope = trait.get("appliesTo", "optIn")
        if scope not in TRAIT_SCOPES:
            self.err(where, f"appliesTo must be one of {', '.join(TRAIT_SCOPES)}")
        attrs = {}
        for attr_name, spec in (trait.get("attributes") or {}).items():
            attr = self._parse_attr(f"{where}.{attr_name}", attr_name, spec, reserved_ok=True)
            if attr:
                attrs[attr_name] = attr
        self.traits[name] = {
            "layer": layer.id,
            "description": str(trait.get("description", "")).strip(),
            "appliesTo": scope,
            "attributes": attrs,
        }

    def _register_enum(self, layer: Layer, name: str, enum) -> None:
        where = f"{layer.where} enum {name}"
        if not PASCAL.match(name):
            self.err(where, "enum names must be PascalCase")
        if not isinstance(enum, dict) or not enum.get("description"):
            self.err(where, "needs a description")
            enum = enum if isinstance(enum, dict) else {}
        values, codes, keys = [], set(), set()
        for value in enum.get("values") or []:
            if not isinstance(value, dict) or "code" not in value or "key" not in value:
                self.err(where, f"value {value!r} needs 'code' and 'key'")
                continue
            code, key = value["code"], str(value["key"])
            if not isinstance(code, int) or code < 0:
                self.err(where, f"code {code!r} must be a non-negative integer")
            if code in codes:
                self.err(where, f"duplicate code {code}")
            if key in keys:
                self.err(where, f"duplicate key '{key}'")
            if not CAMEL.match(key):
                self.err(where, f"key '{key}' must be camelCase")
            codes.add(code)
            keys.add(key)
            values.append({
                "code": code,
                "key": key,
                "label": value.get("label") or words(key[:1].upper() + key[1:]),
                "deprecated": bool(value.get("deprecated", False)),
            })
        if not values:
            self.err(where, "needs at least one value")
        if self._claim_name(where, name, "enum"):
            self.enums[name] = {"layer": layer.id, "description": str(enum.get("description", "")).strip(), "values": values}

    def _register_termset(self, layer: Layer, name: str, termset) -> None:
        where = f"{layer.where} term set {name}"
        if not isinstance(termset, dict):
            self.err(where, "must be a mapping")
            return
        for key in set(termset) - TERMSET_KEYS:
            self.err(where, f"unknown key '{key}'")
        terms = termset.get("terms") or []
        if termset.get("extends"):
            self.term_additions.append((layer.id, name, terms))
            return
        if not PASCAL.match(name):
            self.err(where, "term set names must be PascalCase")
        if not termset.get("description"):
            self.err(where, "needs a description")
        if self._claim_name(where, name, "term set"):
            self.termsets[name] = {
                "layer": layer.id,
                "description": str(termset.get("description", "")).strip(),
                "open": bool(termset.get("open", False)),
                "hierarchical": bool(termset.get("hierarchical", False)),
                "terms": terms,
            }

    def _register_entity(self, layer: Layer, name: str, raw) -> None:
        where = f"{layer.where} {name}"
        if not PASCAL.match(name) or len(name) > 40:
            self.err(where, "entity names must be PascalCase and at most 40 characters")
        if not isinstance(raw, dict):
            self.err(where, "must be a mapping")
            return
        for key in set(raw) - ENTITY_KEYS:
            self.err(where, f"unknown key '{key}'")
        if not raw.get("description"):
            self.err(where, "needs a description")
        label = raw.get("label") or words(name)
        platforms = {"dataverse": "custom", "sharepoint": "list", "sql": "table"}
        for key, value in (raw.get("platforms") or {}).items():
            if key not in platforms:
                self.err(where, f"unknown platform '{key}'")
                continue
            platforms[key] = str(value)
        if not (platforms["dataverse"] in DATAVERSE_MODES or platforms["dataverse"].startswith("standard:")):
            self.err(where, f"dataverse platform must be one of {', '.join(DATAVERSE_MODES)} or standard:<table>")
        if platforms["sharepoint"] not in SHAREPOINT_MODES:
            self.err(where, f"sharepoint platform must be one of {', '.join(SHAREPOINT_MODES)}")
        if platforms["sql"] not in SQL_MODES:
            self.err(where, f"sql platform must be one of {', '.join(SQL_MODES)}")
        ent = Entity(
            name=name,
            layer=layer.id,
            description=str(raw.get("description", "")).strip(),
            label=label,
            plural=raw.get("plural") or pluralize(label),
            ownership=raw.get("ownership", "user"),
            volume=raw.get("volume", "low"),
            sensitivity=raw.get("sensitivity", "internal"),
            traits=[],
            primary=raw.get("primary", "name"),
            platforms=platforms,
            keys=[list(k) if isinstance(k, list) else [k] for k in raw.get("keys") or []],
            attributes={},
            notes=[str(n).strip() for n in raw.get("notes") or []],
        )
        for value, allowed, what in (
            (ent.ownership, OWNERSHIP, "ownership"),
            (ent.volume, VOLUMES, "volume"),
            (ent.sensitivity, SENSITIVITY, "sensitivity"),
        ):
            if value not in allowed:
                self.err(where, f"{what} must be one of {', '.join(allowed)}")
        if ent.primary == "auto":
            ent.primary, ent.auto_primary = "name", True
            ent.attributes["name"] = dict(AUTO_PRIMARY)
        for attr_name, spec in (raw.get("attributes") or {}).items():
            if attr_name in ent.attributes:
                self.err(f"{where}.{attr_name}", "duplicate attribute")
                continue
            # 'title' is allowed as the primary column: it *is* SharePoint's Title column
            attr = self._parse_attr(f"{where}.{attr_name}", attr_name, spec,
                                    reserved_ok=attr_name == ent.primary == "title")
            if attr:
                ent.attributes[attr_name] = attr
        # traits: implicit ones first, then opt-in ones in declared order
        for trait_name, trait in self.traits.items():
            if trait["appliesTo"] == "all" or (trait["appliesTo"] == "userOwned" and ent.ownership == "user"):
                ent.traits.append(trait_name)
        for trait_name in raw.get("traits") or []:
            trait = self.traits.get(trait_name)
            if not trait or trait["appliesTo"] != "optIn":
                self.err(where, f"unknown opt-in trait '{trait_name}'")
            elif trait_name not in ent.traits:
                ent.traits.append(trait_name)
        for trait_name in ent.traits:
            for attr_name in self.traits[trait_name]["attributes"]:
                if attr_name in ent.attributes:
                    self.err(f"{where}.{attr_name}", f"clashes with trait '{trait_name}'")
        if self._claim_name(where, name, "entity"):
            self.entities[name] = ent
            layer.entities[name] = ent

    def _parse_attr(self, where: str, name: str, spec, reserved_ok: bool = False):
        if not CAMEL.match(name):
            self.err(where, "attribute names must be camelCase")
        if len(name) > MAX_NAME:
            self.err(where, f"attribute names must be at most {MAX_NAME} characters")
        if not reserved_ok and name.lower() in RESERVED_NAMES:
            self.err(where, f"'{name}' is a reserved column name on at least one platform")
        if isinstance(spec, str):
            spec = {"type": spec}
        if not isinstance(spec, dict):
            self.err(where, "must be a type string or a mapping with 'type'")
            return None
        for key in set(spec) - ATTR_KEYS:
            self.err(where, f"unknown attribute key '{key}'")
        match = SHORTHAND.match(str(spec.get("type", "")).strip())
        if not match or match["type"] not in TYPE_ARGS:
            self.err(where, f"invalid type '{spec.get('type')}'")
            return None
        kind = TYPE_ARGS[match["type"]]
        args = match["args"].strip() if match["args"] is not None else None
        attr = {"type": match["type"], "required": bool(match["req"])}
        if args is not None and kind is None:
            self.err(where, f"type '{match['type']}' takes no arguments")
        if kind in REQUIRED_ARG and not args:
            self.err(where, f"type '{match['type']}' needs an argument")
            return None
        try:
            if kind == "length":
                attr["length"] = int(args) if args else 100
            elif kind == "precision":
                precision, scale = (args or "12,2").split(",")
                attr["precision"], attr["scale"] = int(precision), int(scale)
            elif kind == "enum":
                attr["enum"] = args
            elif kind in ("entity",):
                attr["target"] = args
            elif kind == "entities":
                attr["targets"] = [t.strip() for t in args.split("|")]
            elif kind == "termset":
                attr["termSet"] = args
        except ValueError:
            self.err(where, f"bad arguments in '{spec.get('type')}'")
            return None
        for key, value in spec.items():
            if key != "type":
                attr[key] = value
        if attr["type"] == "parent":
            attr["required"] = True
            attr["onDelete"] = "cascade"
        elif attr["type"] == "lookup":
            attr.setdefault("onDelete", "restrict" if attr["required"] else "setNull")
            if attr["onDelete"] not in ON_DELETE:
                self.err(where, f"onDelete must be one of {', '.join(ON_DELETE)}")
        elif "onDelete" in spec:
            self.err(where, "onDelete only applies to lookup attributes")
        if attr["type"] == "autonumber" and not attr.get("format"):
            self.err(where, "autonumber needs a 'format', e.g. \"EMP-{SEQNUM:6}\"")
        if "sensitivity" in attr and attr["sensitivity"] not in SENSITIVITY:
            self.err(where, f"sensitivity must be one of {', '.join(SENSITIVITY)}")
        if "description" in attr:
            attr["description"] = str(attr["description"]).strip()
        return attr

    def _register_extensions(self, layer: Layer) -> None:
        for ent_name, ext in (layer.raw.get("extend") or {}).items():
            where = f"{layer.where} extend {ent_name}"
            if not isinstance(ext, dict) or set(ext) - {"attributes"}:
                self.err(where, "extensions may only contain 'attributes'")
                continue
            target = self.entities.get(ent_name)
            if not target:
                self.err(where, "unknown entity")
                continue
            if target.layer == layer.id:
                self.err(where, "add attributes to the entity itself instead of extending it in its own layer")
                continue
            if not self.sees(layer.id, target.layer):
                self.err(where, f"entity is defined in '{target.layer}', which is not a dependency")
                continue
            attrs = {}
            for attr_name, spec in (ext.get("attributes") or {}).items():
                attr = self._parse_attr(f"{where}.{attr_name}", attr_name, spec)
                if not attr:
                    continue
                owner = self.ext_owner.get((ent_name, attr_name))
                base = self._base_view(target)
                if attr_name in base:
                    self.err(f"{where}.{attr_name}", "already exists on the base entity")
                elif owner:
                    self.err(f"{where}.{attr_name}", f"already added by layer '{owner}'")
                else:
                    self.ext_owner[(ent_name, attr_name)] = layer.id
                    attrs[attr_name] = attr
            layer.extensions[ent_name] = attrs

    # ------------------------------------------------------------------- views
    def _base_view(self, ent: Entity) -> dict:
        view = {}
        for trait_name in ent.traits:
            for attr_name, attr in self.traits[trait_name]["attributes"].items():
                view[attr_name] = {**attr, "trait": trait_name}
        view.update(ent.attributes)
        return view

    def view(self, ent_name: str, layers: set) -> dict:
        """All attributes of an entity as seen by a deployment containing `layers`."""
        ent = self.entities[ent_name]
        view = self._base_view(ent)
        for layer_id in self.order:
            if layer_id in layers:
                for attr_name, attr in self.layers[layer_id].extensions.get(ent_name, {}).items():
                    view[attr_name] = {**attr, "extendedBy": layer_id}
        return view

    def natural_key(self, ent_name: str):
        ent = self.entities[ent_name]
        if "code" in ent.attributes:
            return "code"
        for attr_name, attr in ent.attributes.items():
            if attr.get("unique"):
                return attr_name
        for key in ent.keys:
            if len(key) == 1:
                return key[0]
        return None

    def terms(self, termset: str, layers: set) -> list:
        base = self.termsets.get(termset)
        if not base:
            return []
        out = list(base["terms"])
        for layer_id, name, terms in self.term_additions:
            if name == termset and layer_id in layers:
                out.extend(terms)
        return out

    # ----------------------------------------------------------------- checking
    def _need(self, layer: Layer, where: str, kind: str, name: str) -> None:
        registry = {"enum": self.enums, "entity": self.entities, "term set": self.termsets}[kind]
        item = registry.get(name)
        if item is None:
            self.err(where, f"unknown {kind} '{name}'")
            return
        owner = item.layer if isinstance(item, Entity) else item["layer"]
        if not self.sees(layer.id, owner):
            self.err(where, f"{kind} '{name}' lives in layer '{owner}', which is not a dependency of '{layer.id}'")

    def _check_attr(self, layer: Layer, where: str, attr: dict, view: dict) -> None:
        t = attr["type"]
        if t in ("choice", "multichoice"):
            self._need(layer, where, "enum", attr["enum"])
            if "default" in attr and attr["enum"] in self.enums:
                keys = {v["key"] for v in self.enums[attr["enum"]]["values"]}
                if attr["default"] not in keys:
                    self.err(where, f"default '{attr['default']}' is not a key of {attr['enum']}")
        elif t in ("lookup", "parent", "multilookup"):
            self._need(layer, where, "entity", attr["target"])
        elif t == "polylookup":
            for target in attr["targets"]:
                if target != "*":
                    self._need(layer, where, "entity", target)
        elif t in ("term", "multiterm"):
            self._need(layer, where, "term set", attr["termSet"])
        elif t == "money":
            currency_field = attr.get("currencyField", "currency")
            currency = view.get(currency_field)
            if not currency or currency["type"] != "lookup" or currency.get("target") != "Currency":
                self.err(where, f"money needs a '{currency_field}' lookup(Currency) on the same entity")

    def _check_layer(self, layer: Layer) -> None:
        visible = self.closure[layer.id]
        for trait_name, trait in self.traits.items():
            if trait["layer"] == layer.id:
                for attr_name, attr in trait["attributes"].items():
                    self._check_attr(layer, f"{layer.where} trait {trait_name}.{attr_name}", attr, trait["attributes"])
        for ent in layer.entities.values():
            where = f"{layer.where} {ent.name}"
            view = self.view(ent.name, visible)
            primary = ent.attributes.get(ent.primary)
            if not primary or primary["type"] not in ("string", "autonumber"):
                self.err(where, f"primary attribute '{ent.primary}' must exist and be a string or autonumber")
            for key in ent.keys:
                for attr_name in key:
                    if attr_name not in view:
                        self.err(where, f"key column '{attr_name}' does not exist")
            for attr_name, attr in ent.attributes.items():
                self._check_attr(layer, f"{where}.{attr_name}", attr, view)
        for ent_name, attrs in layer.extensions.items():
            view = self.view(ent_name, visible)
            for attr_name, attr in attrs.items():
                self._check_attr(layer, f"{layer.where} extend {ent_name}.{attr_name}", attr, view)
        for layer_id, name, _ in self.term_additions:
            if layer_id == layer.id:
                self._need(layer, f"{layer.where} term set {name}", "term set", name)
        for key, value in (layer.raw.get("terminology") or {}).items():
            self._check_terminology(layer, key, value)
            layer.terminology[key] = value

    def _check_terminology(self, layer: Layer, key: str, value) -> None:
        where = f"{layer.where} terminology {key}"
        name, _, attr_name = key.partition(".")
        if not isinstance(value, dict):
            self.err(where, "must be a mapping")
            return
        if attr_name:
            if name not in self.entities:
                self.err(where, f"unknown entity '{name}'")
            elif attr_name not in self.view(name, self.closure[layer.id]):
                self.err(where, f"unknown attribute '{attr_name}'")
            if set(value) - {"label"}:
                self.err(where, "attribute terminology only supports 'label'")
            return
        if name in self.entities:
            self._need(layer, where, "entity", name)
        elif name in self.termsets:
            self._need(layer, where, "term set", name)
        else:
            self.err(where, f"unknown entity or term set '{name}'")
        if set(value) - {"singular", "plural"}:
            self.err(where, "entity terminology supports 'singular' and 'plural'")

    def _check_seeds(self) -> None:
        index: dict[str, list] = {}
        for layer_id in self.order:
            layer = self.layers[layer_id]
            for ent_name, rows in (layer.raw.get("seed") or {}).items():
                where = f"{layer.where} seed {ent_name}"
                if ent_name not in self.entities:
                    self.err(where, "unknown entity")
                    continue
                self._need(layer, where, "entity", ent_name)
                if not isinstance(rows, list):
                    self.err(where, "must be a list of rows")
                    continue
                layer.seeds[ent_name] = rows
                index.setdefault(ent_name, []).extend((layer_id, row) for row in rows)
        for ent_name, rows in index.items():
            key = self.natural_key(ent_name)
            if not key:
                self.err(f"seed {ent_name}", "seeded entities need a natural key ('code', a unique column or a single-column key)")
                continue
            seen = {}
            for layer_id, row in rows:
                if isinstance(row, dict) and key in row:
                    value = row[key]
                    if value in seen:
                        self.err(f"{self.layers[layer_id].where} seed {ent_name}",
                                 f"{key} '{value}' already seeded by layer '{seen[value]}'")
                    seen[value] = layer_id
        for layer_id in self.order:
            layer = self.layers[layer_id]
            visible = self.closure[layer_id]
            for ent_name, rows in layer.seeds.items():
                view = self.view(ent_name, visible)
                own = {n: a for n, a in view.items() if "trait" not in a}
                for i, row in enumerate(rows, 1):
                    where = f"{layer.where} seed {ent_name}[{i}]"
                    if not isinstance(row, dict):
                        self.err(where, "rows must be mappings")
                        continue
                    for attr_name, attr in own.items():
                        if (attr["required"] and attr_name not in row and "default" not in attr
                                and not attr.get("readOnly") and attr["type"] != "autonumber"):
                            self.err(where, f"missing required '{attr_name}'")
                    for attr_name, value in row.items():
                        if attr_name not in own:
                            self.err(where, f"unknown attribute '{attr_name}'")
                            continue
                        self._check_seed_value(where, attr_name, own[attr_name], value, index, visible)

    def _check_seed_value(self, where, attr_name, attr, value, index, visible) -> None:
        t = attr["type"]
        where = f"{where}.{attr_name}"
        if value is None:
            return
        if t in ("choice", "multichoice") and attr["enum"] in self.enums:
            keys = {v["key"] for v in self.enums[attr["enum"]]["values"]}
            for item in value if isinstance(value, list) else [value]:
                if item not in keys:
                    self.err(where, f"'{item}' is not a key of {attr['enum']}")
        elif t in ("lookup", "parent"):
            target = attr["target"]
            key = self.natural_key(target) if target in self.entities else None
            found = any(layer_id in visible and isinstance(row, dict) and row.get(key) == value
                        for layer_id, row in index.get(target, []))
            if not found:
                self.err(where, f"no visible {target} seed row with {key} '{value}'")
        elif t in ("term", "multiterm"):
            names = set(flatten_terms(self.terms(attr["termSet"], visible)))
            for item in value if isinstance(value, list) else [value]:
                if item not in names:
                    self.err(where, f"'{item}' is not a term of {attr['termSet']}")
        elif t == "boolean" and not isinstance(value, bool):
            self.err(where, "must be true or false")
        elif t in ("integer", "duration") and (isinstance(value, bool) or not isinstance(value, int)):
            self.err(where, "must be an integer")
        elif t in ("decimal", "percent", "money") and (isinstance(value, bool) or not isinstance(value, (int, float))):
            self.err(where, "must be a number")
        elif t in ("string", "email", "phone", "url", "text", "richtext"):
            if not isinstance(value, str):
                self.err(where, "must be a string")
            elif t == "string" and len(value) > attr["length"]:
                self.err(where, f"longer than {attr['length']} characters")
        elif t == "date" and not isinstance(value, dt.date):
            self.err(where, "must be a date (YYYY-MM-DD)")


def flatten_terms(terms) -> list:
    out = []
    for term in terms or []:
        if isinstance(term, dict):
            out.append(str(term.get("name")))
            out.extend(flatten_terms(term.get("children")))
        else:
            out.append(str(term))
    return out


# ---------------------------------------------------------------- platform map
def type_label(attr: dict) -> str:
    t = attr["type"]
    if t == "string":
        return f"string({attr['length']})"
    if t == "decimal":
        return f"decimal({attr['precision']},{attr['scale']})"
    if t in ("choice", "multichoice"):
        return f"{t}({attr['enum']})"
    if t in ("lookup", "parent", "multilookup"):
        return f"{t}({attr['target']})"
    if t == "polylookup":
        return f"polylookup({'|'.join(attr['targets'])})"
    if t in ("term", "multiterm"):
        return f"{t}({attr['termSet']})"
    return t


def physical(attr: dict) -> dict:
    """Default physical column type per platform for a logical attribute."""
    t = attr["type"]
    target = attr.get("target", "")
    dv = {
        "guid": "Unique identifier",
        "string": f"Text ({attr.get('length')})",
        "text": "Multiline text",
        "richtext": "Multiline text (rich text)",
        "email": "Text (email)",
        "phone": "Text (phone)",
        "url": "Text (URL)",
        "integer": "Whole number",
        "decimal": f"Decimal ({attr.get('precision')},{attr.get('scale')})",
        "percent": "Decimal (5,2)",
        "money": "Currency",
        "boolean": "Yes/No",
        "date": "Date only (behaviour: date only)",
        "datetime": "Date and time (user local)",
        "duration": "Whole number (duration)",
        "choice": f"Choice (global: {attr.get('enum')})",
        "multichoice": f"Choices (global: {attr.get('enum')})",
        "lookup": f"Lookup → {target}",
        "parent": f"Lookup → {target} (parental)",
        "multilookup": f"N:N → {target}",
        "polylookup": ("Text ×2 (table name + id)" if "*" in attr.get("targets", [])
                       else "Multi-table lookup"),
        "term": f"Lookup → Term (set {attr.get('termSet')})",
        "multiterm": f"N:N → Term (set {attr.get('termSet')})",
        "address": "Text ×5 + Lookup → Country",
        "file": "File",
        "image": "Image",
        "autonumber": f"Autonumber `{attr.get('format', '')}`",
    }[t]
    person = target == "Employee"
    sp = {
        "guid": "Single line of text",
        "string": "Single line of text" if attr.get("length", 0) <= 255 else "Multiple lines (plain)",
        "text": "Multiple lines (plain)",
        "richtext": "Multiple lines (enhanced rich text)",
        "email": "Single line of text",
        "phone": "Single line of text",
        "url": "Hyperlink",
        "integer": "Number (0 dp)",
        "decimal": f"Number ({attr.get('scale')} dp)",
        "percent": "Number (%)",
        "money": "Currency",
        "boolean": "Yes/No",
        "date": "Date (date only)",
        "datetime": "Date and time",
        "duration": "Number (minutes)",
        "choice": "Choice",
        "multichoice": "Choice (multiple)",
        "lookup": "Person" if person else f"Lookup → {target}",
        "parent": f"Lookup → {target} (cascade delete)",
        "multilookup": "Person (multiple)" if person else f"Lookup (multiple) → {target}",
        "polylookup": "Text ×2 (list name + id)",
        "term": f"Managed metadata ({attr.get('termSet')})",
        "multiterm": f"Managed metadata, multiple ({attr.get('termSet')})",
        "address": "Text ×5 + Lookup → Country",
        "file": "Attachment / library file",
        "image": "Image",
        "autonumber": "Single line of text (set by flow)",
    }[t]
    sql = {
        "guid": "uniqueidentifier",
        "string": f"nvarchar({attr.get('length')})",
        "text": "nvarchar(max)",
        "richtext": "nvarchar(max)",
        "email": "nvarchar(320)",
        "phone": "nvarchar(50)",
        "url": "nvarchar(2000)",
        "integer": "int",
        "decimal": f"decimal({attr.get('precision')},{attr.get('scale')})",
        "percent": "decimal(5,2)",
        "money": "decimal(19,4)",
        "boolean": "bit",
        "date": "date",
        "datetime": "datetime2 (UTC)",
        "duration": "int (minutes)",
        "choice": "int",
        "multichoice": "junction table",
        "lookup": f"uniqueidentifier FK → {target}",
        "parent": f"uniqueidentifier FK → {target} (cascade)",
        "multilookup": f"junction table → {target}",
        "polylookup": "nvarchar(100) + uniqueidentifier",
        "term": "uniqueidentifier FK → Term",
        "multiterm": "junction table → Term",
        "address": "nvarchar ×5 + FK → Country",
        "file": "nvarchar(2000) (blob URL)",
        "image": "nvarchar(2000) (blob URL)",
        "autonumber": "nvarchar(30) + sequence",
    }[t]
    return {
        "dataverse": attr.get("dataverse", dv),
        "sharepoint": attr.get("sharepoint", sp),
        "sql": attr.get("sql", sql),
    }


# ----------------------------------------------------------------------- docs
def md(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def anchor(name: str) -> str:
    return name.lower()


class DocWriter:
    def __init__(self, model: Model) -> None:
        self.m = model

    def link(self, name: str, here: str) -> str:
        """Markdown link to the section documenting an entity, enum or term set."""
        for registry in (self.m.entities, self.m.enums, self.m.termsets):
            if name in registry:
                item = registry[name]
                owner = item.layer if isinstance(item, Entity) else item["layer"]
                page = "" if owner == here else f"{owner}.md"
                return f"[{name}]({page}#{anchor(name)})"
        return name

    def type_cell(self, attr: dict, here: str) -> str:
        t = attr["type"]
        if t in ("choice", "multichoice"):
            return f"{t}({self.link(attr['enum'], here)})"
        if t in ("lookup", "parent", "multilookup"):
            return f"{t}({self.link(attr['target'], here)})"
        if t == "polylookup":
            return "polylookup(" + " \\| ".join(self.link(x, here) for x in attr["targets"]) + ")"
        if t in ("term", "multiterm"):
            return f"{t}({self.link(attr['termSet'], here)})"
        return type_label(attr)

    def notes_cell(self, attr: dict) -> str:
        bits = []
        if attr.get("description"):
            bits.append(md(attr["description"]))
        flags = []
        if attr.get("unique"):
            flags.append("unique")
        if attr.get("indexed"):
            flags.append("indexed")
        if attr.get("pii"):
            flags.append("PII")
        if attr.get("sensitivity"):
            flags.append(attr["sensitivity"])
        if attr.get("readOnly"):
            flags.append("read-only")
        if "default" in attr:
            flags.append(f"default `{attr['default']}`")
        if attr["type"] == "lookup" and attr.get("onDelete") != ("restrict" if attr["required"] else "setNull"):
            flags.append(f"on delete: {attr['onDelete']}")
        if attr.get("currencyField"):
            flags.append(f"currency from `{attr['currencyField']}`")
        if attr.get("deprecated"):
            flags.append("**deprecated**")
        if attr.get("calculated"):
            bits.append(f"*Calculated:* {md(attr['calculated'])}")
        if flags:
            bits.append(" · ".join(f"_{f}_" for f in flags))
        return "<br>".join(bits)

    def attr_table(self, attrs: dict, here: str, primary: str = "") -> list:
        lines = [
            "| Column | Type | Req | Dataverse | SharePoint | SQL | Notes |",
            "|---|---|:-:|---|---|---|---|",
        ]
        for name, attr in attrs.items():
            phys = physical(attr)
            if name == primary:
                phys["sharepoint"] = "Title (primary column)"
            lines.append(
                f"| `{name}` | {self.type_cell(attr, here)} | {'✔' if attr['required'] else ''} "
                f"| {md(phys['dataverse'])} | {md(phys['sharepoint'])} | {md(phys['sql'])} "
                f"| {self.notes_cell(attr)} |"
            )
        return lines

    def diagram(self, layer: Layer) -> list:
        rels, nodes = [], []
        for ent in layer.entities.values():
            primary = ent.attributes.get(ent.primary, {"type": "string"})
            nodes.append(f"    {ent.name} {{\n        {primary['type']} {ent.primary}\n    }}")
        sources = [(e.name, e.attributes) for e in layer.entities.values()]
        sources += list(layer.extensions.items())
        for src, attrs in sources:
            for name, attr in attrs.items():
                target = attr.get("target")
                if not target or target in DIAGRAM_SKIP:
                    continue
                if attr["type"] == "parent":
                    rels.append(f'    {target} ||--o{{ {src} : "{name}"')
                elif attr["type"] == "multilookup":
                    rels.append(f'    {src} }}o--o{{ {target} : "{name}"')
                elif attr["required"]:
                    rels.append(f'    {src} }}o--|| {target} : "{name}"')
                else:
                    rels.append(f'    {src} }}o--o| {target} : "{name}"')
        if not nodes and not rels:
            return []
        return ["```mermaid", "erDiagram"] + nodes + rels + ["```"]

    def layer_page(self, layer: Layer) -> str:
        m = self.m
        out = [
            f"# {layer.name} (`{layer.id}` v{layer.version})",
            "",
            f"> Generated from [`{rel(layer.file)}`](../../{rel(layer.file)}) by `tools/schematool.py docs`. "
            "Do not edit by hand.",
            "",
            layer.description,
            "",
            "| Kind | Depends on | Entities | Extended entities | Enums | Term sets |",
            "|---|---|---|---|---|---|",
        ]
        enums = {n: e for n, e in m.enums.items() if e["layer"] == layer.id}
        termsets = {n: t for n, t in m.termsets.items() if t["layer"] == layer.id}
        additions = [(n, t) for lid, n, t in m.term_additions if lid == layer.id]
        deps = ", ".join(f"[`{d}`]({d}.md)" for d in layer.depends) or "—"
        out.append(f"| {layer.kind} | {deps} | {len(layer.entities)} | {len(layer.extensions)} "
                   f"| {len(enums)} | {len(termsets) + len(additions)} |")
        out.append("")
        if layer.terminology:
            out += ["## Terminology", "", "Display names this layer applies on top of the generic model.", "",
                    "| Generic name | Shown as |", "|---|---|"]
            for key, value in layer.terminology.items():
                shown = value.get("label") or f"{value.get('singular', '')} / {value.get('plural', '')}"
                out.append(f"| `{key}` | {md(shown)} |")
            out.append("")
        if layer.entities:
            out += ["## Entities", "", "| Entity | Description | Dataverse | SharePoint | Volume | Sensitivity |",
                    "|---|---|---|---|---|---|"]
            for ent in layer.entities.values():
                out.append(f"| [{ent.name}](#{anchor(ent.name)}) | {md(ent.description.split('. ')[0].rstrip('.'))}. "
                           f"| {ent.platforms['dataverse']} | {self.sp_cell(ent)} | {ent.volume} | {ent.sensitivity} |")
            out.append("")
        diagram = self.diagram(layer)
        if diagram:
            out += ["## Relationships", "",
                    "Lookups to " + ", ".join(sorted(DIAGRAM_SKIP)) + " are left out of the diagram to keep it readable.",
                    ""] + diagram + [""]
        for ent in layer.entities.values():
            out += self.entity_section(layer, ent)
        if layer.extensions:
            out += ["## Extensions to entities from other layers", ""]
            for ent_name, attrs in layer.extensions.items():
                owner = m.entities[ent_name].layer
                out += [f"### {ent_name} (+{len(attrs)})", "",
                        f"Adds columns to [{ent_name}]({owner}.md#{anchor(ent_name)}) from `{owner}`.", ""]
                out += self.attr_table(attrs, layer.id) + [""]
        if enums:
            out += ["## Choices (enums)", "",
                    "Codes are stable forever. Dataverse option value = publisher choice prefix + code.", ""]
            for name, enum in enums.items():
                out += [f"### {name}", "", enum["description"], "", "| Code | Key | Label |", "|--:|---|---|"]
                for v in enum["values"]:
                    label = v["label"] + (" *(deprecated)*" if v["deprecated"] else "")
                    out.append(f"| {v['code']} | `{v['key']}` | {md(label)} |")
                out.append("")
        if termsets or additions:
            out += ["## Term sets", "",
                    "Stored in the SharePoint term store; mirrored to the Term table on Dataverse/SQL.", ""]
            for name, ts in termsets.items():
                flags = ", ".join(f for f, on in (("open", ts["open"]), ("hierarchical", ts["hierarchical"])) if on)
                out += [f"### {name}", "", ts["description"] + (f" _({flags})_" if flags else ""), ""]
                out += self.term_lines(ts["terms"]) + [""]
            for name, terms in additions:
                out += [f"### {name} (adds to [{name}]({m.termsets[name]['layer']}.md#{anchor(name)}))"
                        if name in m.termsets else f"### {name}", ""]
                out += self.term_lines(terms) + [""]
        if layer.seeds:
            out += ["## Reference data (seed)", "",
                    "Loaded at deployment. Deactivate rows you do not need instead of deleting them.", ""]
            for ent_name, rows in layer.seeds.items():
                cols = []
                for row in rows:
                    for key in row:
                        if key not in cols:
                            cols.append(key)
                out += [f"### {ent_name} ({len(rows)} rows)", "", "| " + " | ".join(cols) + " |",
                        "|" + "---|" * len(cols)]
                for row in rows:
                    out.append("| " + " | ".join(md(self.seed_value(row.get(c))) for c in cols) + " |")
                out.append("")
        return "\n".join(out).rstrip() + "\n"

    @staticmethod
    def seed_value(value) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        return str(value)

    def term_lines(self, terms, depth: int = 0) -> list:
        lines = []
        for term in terms or []:
            if isinstance(term, dict):
                lines.append("  " * depth + f"- {term.get('name')}")
                lines += self.term_lines(term.get("children"), depth + 1)
            else:
                lines.append("  " * depth + f"- {term}")
        return lines

    @staticmethod
    def sp_cell(ent: Entity) -> str:
        sp = ent.platforms["sharepoint"]
        if sp == "list" and ent.volume == "high":
            return "list ⚠ high volume"
        return sp

    def entity_section(self, layer: Layer, ent: Entity) -> list:
        m = self.m
        dv = ent.platforms["dataverse"]
        dv_text = {"custom": f"custom table `pfx_{ent.name.lower()}`", "native": "provided by the platform",
                   "none": "not deployed"}.get(dv, f"standard table `{dv.split(':', 1)[-1]}`")
        traits = ", ".join(
            f"`{t}` ({', '.join(m.traits[t]['attributes'])})" for t in ent.traits
        )
        out = [
            f"### {ent.name}",
            "",
            ent.description,
            "",
            f"- **Display name:** {ent.label} / {ent.plural}",
            f"- **Ownership:** {ent.ownership} · **Volume:** {ent.volume} · **Sensitivity:** {ent.sensitivity}",
            f"- **Dataverse:** {dv_text} · **SharePoint:** {self.sp_cell(ent)} · **SQL:** {ent.platforms['sql']}",
            f"- **Primary column:** `{ent.primary}`" + (" (auto-generated)" if ent.auto_primary else ""),
        ]
        if ent.keys:
            out.append("- **Alternate keys:** " + "; ".join("(" + ", ".join(f"`{k}`" for k in key) + ")"
                                                          for key in ent.keys))
        out.append(f"- **Standard columns from traits:** {traits}")
        added = [lid for lid in m.order if ent.name in m.layers[lid].extensions and lid != layer.id]
        if added:
            out.append("- **Extended by:** " + ", ".join(f"[`{lid}`]({lid}.md#{anchor(ent.name)}-{len(m.layers[lid].extensions[ent.name])})"
                                                        for lid in added))
        out.append("")
        out += self.attr_table(ent.attributes, layer.id, ent.primary)
        if ent.notes:
            out += [""] + [f"> {md(note)}" + ("  " if i < len(ent.notes) - 1 else "")
                           for i, note in enumerate(ent.notes)]
        out.append("")
        return out

    def index_page(self, profiles: dict) -> str:
        m = self.m
        out = [
            "# Data dictionary",
            "",
            "> Generated by `tools/schematool.py docs` from the YAML under [`schemas/`](../../schemas). "
            "Do not edit by hand.",
            "",
            "## Layers",
            "",
            "| Layer | Kind | Version | Depends on | Entities | Extended | Enums | Columns | Seed rows |",
            "|---|---|---|---|--:|--:|--:|--:|--:|",
        ]
        totals = [0, 0, 0, 0, 0]
        for layer_id in sorted(m.layers, key=m._sort_key):
            layer = m.layers[layer_id]
            enums = sum(1 for e in m.enums.values() if e["layer"] == layer_id)
            cols = sum(len(e.attributes) for e in layer.entities.values())
            cols += sum(len(a) for a in layer.extensions.values())
            seeds = sum(len(rows) for rows in layer.seeds.values())
            totals = [totals[0] + len(layer.entities), totals[1] + len(layer.extensions), totals[2] + enums,
                      totals[3] + cols, totals[4] + seeds]
            out.append(f"| [{layer.name}]({layer_id}.md) `{layer_id}` | {layer.kind} | {layer.version} "
                       f"| {', '.join(layer.depends) or '—'} | {len(layer.entities)} | {len(layer.extensions)} "
                       f"| {enums} | {cols} | {seeds} |")
        out.append("| **Total** | | | | " + " | ".join(f"**{n}**" for n in totals) + " |")
        out += ["", "## Dependency graph", "", "Arrows point from a layer to the layers it depends on.", "",
                "```mermaid", "flowchart BT"]
        groups: dict[str, list] = {}
        for layer_id in sorted(m.layers, key=m._sort_key):
            layer = m.layers[layer_id]
            if layer.kind in ("industry", "client"):
                group = f"{layer.kind.title()}: {layer.name.split(' – ')[0]}"
            else:
                group = {"foundation": "Foundation", "module": "Modules", "bridge": "Bridges"}.get(layer.kind, layer.kind)
            groups.setdefault(group, []).append(layer_id)
        for i, (group, members) in enumerate(groups.items()):
            out.append(f'    subgraph g{i}["{group}"]')
            for layer_id in members:
                out.append(f'        {layer_id.replace("-", "_")}["{layer_id}"]')
            out.append("    end")
        for layer_id in sorted(m.layers, key=m._sort_key):
            for dep in m.layers[layer_id].depends:
                out.append(f"    {layer_id.replace('-', '_')} --> {dep.replace('-', '_')}")
        out += ["```", ""]
        if profiles:
            out += ["## Deployment profiles", "",
                    "A profile is the set of layers deployed for one kind of client. "
                    "`python tools/schematool.py export --profile <name>` resolves it into one model.", "",
                    "| Profile | Description | Layers |", "|---|---|---|"]
            for name, profile in profiles.items():
                out.append(f"| `{name}` | {md(profile.get('description', ''))} "
                           f"| {', '.join(f'`{l}`' for l in profile.get('layers', []))} |")
            out.append("")
        out += ["## Standard columns (traits)", "",
                "Every entity gets these columns without declaring them; Dataverse and SharePoint "
                "provide most of them natively.", ""]
        for name, trait in m.traits.items():
            scope = {"all": "every entity", "userOwned": "user-owned entities",
                     "optIn": "entities that list it under `traits`"}[trait["appliesTo"]]
            out += [f"### `{name}` — {scope}", "", trait["description"], ""]
            out += self.attr_table(trait["attributes"], "core") + [""]
        out += ["## Logical type → platform mapping", "",
                "Defaults used throughout this dictionary. A column can override them with "
                "`dataverse:`, `sharepoint:` or `sql:` in its definition.", "",
                "| Logical type | Dataverse | SharePoint | SQL (Azure SQL / SQL Server) |", "|---|---|---|---|"]
        samples = {
            "string(n)": {"type": "string", "length": 0},
            "decimal(p,s)": {"type": "decimal", "precision": "p", "scale": "s"},
            "lookup(Employee)": {"type": "lookup", "target": "Employee"},
            "lookup(X)": {"type": "lookup", "target": "X"},
            "parent(X)": {"type": "parent", "target": "X"},
            "multilookup(X)": {"type": "multilookup", "target": "X"},
            "polylookup(A|B)": {"type": "polylookup", "targets": ["A", "B"]},
            "polylookup(*)": {"type": "polylookup", "targets": ["*"]},
            "choice(E)": {"type": "choice", "enum": "E"},
            "multichoice(E)": {"type": "multichoice", "enum": "E"},
            "term(S)": {"type": "term", "termSet": "S"},
            "multiterm(S)": {"type": "multiterm", "termSet": "S"},
            "autonumber": {"type": "autonumber", "format": "PRE-{SEQNUM:6}"},
        }
        for t in TYPE_ARGS:
            if t in ("string", "decimal", "lookup", "parent", "multilookup", "polylookup", "choice",
                     "multichoice", "term", "multiterm", "autonumber"):
                continue
            samples[t] = {"type": t}
        for label, attr in samples.items():
            phys = physical({"required": False, **attr})
            if label == "string(n)":
                phys = {"dataverse": "Text (n)", "sql": "nvarchar(n)",
                        "sharepoint": "Single line of text (n ≤ 255) / Multiple lines (n > 255)"}
            out.append(f"| `{md(label)}` | {md(phys['dataverse'])} | {md(phys['sharepoint'])} | {md(phys['sql'])} |")
        return "\n".join(out).rstrip() + "\n"


def load_profiles(model: Model) -> dict:
    if not PROFILES_FILE.exists():
        return {}
    data = yaml.safe_load(PROFILES_FILE.read_text(encoding="utf-8")) or {}
    profiles = data.get("profiles") or {}
    for name, profile in profiles.items():
        where = f"{rel(PROFILES_FILE)} {name}"
        layers = profile.get("layers") or []
        for layer_id in layers:
            if layer_id not in model.layers:
                model.err(where, f"unknown layer '{layer_id}'")
            else:
                missing = model.closure[layer_id] - set(layers)
                if missing:
                    model.err(where, f"'{layer_id}' also needs: {', '.join(sorted(missing))}")
    return profiles


def render_docs(model: Model, profiles: dict) -> dict:
    writer = DocWriter(model)
    pages = {DOCS_DIR / "README.md": writer.index_page(profiles)}
    for layer_id, layer in model.layers.items():
        pages[DOCS_DIR / f"{layer_id}.md"] = writer.layer_page(layer)
    return pages


# --------------------------------------------------------------------- export
def export_profile(model: Model, profile_name: str, profile: dict) -> dict:
    layers = set(profile.get("layers") or [])
    ordered = [l for l in model.order if l in layers]
    terminology = {}
    for layer_id in ordered:
        terminology.update(model.layers[layer_id].terminology)
    entities = {}
    for layer_id in ordered:
        for ent in model.layers[layer_id].entities.values():
            attrs = {}
            for name, attr in model.view(ent.name, layers).items():
                attrs[name] = {**attr, "physical": physical(attr)}
            label = terminology.get(ent.name, {})
            entities[ent.name] = {
                "layer": ent.layer,
                "description": ent.description,
                "label": label.get("singular", ent.label),
                "plural": label.get("plural", ent.plural),
                "ownership": ent.ownership,
                "volume": ent.volume,
                "sensitivity": ent.sensitivity,
                "platforms": ent.platforms,
                "primary": ent.primary,
                "keys": ent.keys,
                "attributes": attrs,
                "notes": ent.notes,
            }
    for key, value in terminology.items():
        ent_name, _, attr_name = key.partition(".")
        if attr_name and ent_name in entities and attr_name in entities[ent_name]["attributes"]:
            entities[ent_name]["attributes"][attr_name]["label"] = value["label"]
    seeds: dict = {}
    for layer_id in ordered:
        for ent_name, rows in model.layers[layer_id].seeds.items():
            seeds.setdefault(ent_name, []).extend(rows)
    return {
        "profile": profile_name,
        "description": profile.get("description", ""),
        "layers": {l: model.layers[l].version for l in ordered},
        "terminology": terminology,
        "entities": entities,
        "enums": {n: e for n, e in model.enums.items() if e["layer"] in layers},
        "termSets": {n: {**t, "terms": model.terms(n, layers)} for n, t in model.termsets.items()
                     if t["layer"] in layers},
        "seed": seeds,
    }


# ----------------------------------------------------------------------- main
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate", help="check the schemas")
    docs = sub.add_parser("docs", help="generate the data dictionary")
    docs.add_argument("--check", action="store_true", help="fail if the generated docs are out of date")
    export = sub.add_parser("export", help="resolve one deployment profile into a JSON model")
    export.add_argument("--profile", required=True)
    export.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    model = Model().load()
    profiles = load_profiles(model)
    if model.errors:
        for error in model.errors:
            print(f"ERROR {error}", file=sys.stderr)
        print(f"\n{len(model.errors)} error(s)", file=sys.stderr)
        return 1

    if args.command == "validate":
        attrs = sum(len(e.attributes) for e in model.entities.values())
        ext = sum(len(a) for l in model.layers.values() for a in l.extensions.values())
        print(f"OK: {len(model.layers)} layers, {len(model.entities)} entities, {attrs + ext} columns, "
              f"{len(model.enums)} enums, {len(model.termsets)} term sets, {len(profiles)} profiles")
        return 0

    if args.command == "docs":
        pages = render_docs(model, profiles)
        if args.check:
            stale = [rel(p) for p, text in pages.items()
                     if not p.exists() or p.read_text(encoding="utf-8") != text]
            extra = [rel(p) for p in DOCS_DIR.glob("*.md") if p not in pages]
            for path in stale:
                print(f"STALE {path}", file=sys.stderr)
            for path in extra:
                print(f"ORPHAN {path}", file=sys.stderr)
            if stale or extra:
                print("Run: python tools/schematool.py docs", file=sys.stderr)
                return 1
            print(f"OK: {len(pages)} pages up to date")
            return 0
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        for path in DOCS_DIR.glob("*.md"):
            if path not in pages:
                path.unlink()
        for path, text in pages.items():
            path.write_text(text, encoding="utf-8")
        print(f"Wrote {len(pages)} pages to {rel(DOCS_DIR)}")
        return 0

    if args.command == "export":
        if args.profile not in profiles:
            print(f"Unknown profile '{args.profile}'. Known: {', '.join(profiles)}", file=sys.stderr)
            return 1
        result = export_profile(model, args.profile, profiles[args.profile])
        out = args.out or BUILD_DIR / f"{args.profile}.model.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"Wrote {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out} "
              f"({len(result['entities'])} entities)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
