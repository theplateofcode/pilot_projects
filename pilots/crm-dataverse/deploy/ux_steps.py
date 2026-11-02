"""Deployment steps that create the user experience: views, forms, charts, site map, app."""

from __future__ import annotations

import uuid
from xml.sax.saxutils import escape, quoteattr

from .client import DataverseError
from .model import Table, title_label
from .schema_steps import (
    COMPONENT_APPMODULE,
    COMPONENT_CHART,
    COMPONENT_ENTITY,
    COMPONENT_SAVED_QUERY,
    COMPONENT_SITEMAP,
    COMPONENT_SYSTEM_FORM,
    Deployer,
)

LANG = 1033

# Classic form control class ids, by canonical column type.
CLASSIDS = {
    "string": "{4273EDBD-AC1D-40d3-9FB2-095C621B552D}",
    "autonumber": "{4273EDBD-AC1D-40d3-9FB2-095C621B552D}",
    "email": "{4273EDBD-AC1D-40d3-9FB2-095C621B552D}",
    "phone": "{4273EDBD-AC1D-40d3-9FB2-095C621B552D}",
    "url": "{4273EDBD-AC1D-40d3-9FB2-095C621B552D}",
    "text": "{E0DECE4B-6FC8-4a8f-A065-082708572369}",
    "richtext": "{E0DECE4B-6FC8-4a8f-A065-082708572369}",
    "integer": "{C3EFE0C3-0EC6-42be-8349-CBD9079DFD8E}",
    "duration": "{C3EFE0C3-0EC6-42be-8349-CBD9079DFD8E}",
    "decimal": "{C3EFE0C3-0EC6-42be-8349-CBD9079DFD8E}",
    "percent": "{C3EFE0C3-0EC6-42be-8349-CBD9079DFD8E}",
    "money": "{533B9E00-756B-4312-95A0-DC888637AC78}",
    "boolean": "{67FAC785-CD58-4f9f-ABB3-4B7DDC6ED5ED}",
    "date": "{5B773807-9FB2-42db-97C3-7A91EFF8ADFF}",
    "datetime": "{5B773807-9FB2-42db-97C3-7A91EFF8ADFF}",
    "choice": "{3EF39988-22BB-4f0b-BBBE-64B5A3748AEE}",
    "multichoice": "{3EF39988-22BB-4f0b-BBBE-64B5A3748AEE}",
    "lookup": "{270BD3DB-D9AF-4782-9025-509E298DEC0A}",
    "parent": "{270BD3DB-D9AF-4782-9025-509E298DEC0A}",
    "term": "{270BD3DB-D9AF-4782-9025-509E298DEC0A}",
    "native": "{4273EDBD-AC1D-40d3-9FB2-095C621B552D}",
}
SUBGRID_CLASSID = "{E7A81278-8635-4d9e-8D4D-59480B391C5B}"

NATIVE_CLASSIDS = {
    "ownerid": "{270BD3DB-D9AF-4782-9025-509E298DEC0A}",
    "primarycontactid": "{270BD3DB-D9AF-4782-9025-509E298DEC0A}",
    "parentaccountid": "{270BD3DB-D9AF-4782-9025-509E298DEC0A}",
    "parentcustomerid": "{270BD3DB-D9AF-4782-9025-509E298DEC0A}",
    "transactioncurrencyid": "{270BD3DB-D9AF-4782-9025-509E298DEC0A}",
    "createdon": "{5B773807-9FB2-42db-97C3-7A91EFF8ADFF}",
    "modifiedon": "{5B773807-9FB2-42db-97C3-7A91EFF8ADFF}",
    "statecode": "{3EF39988-22BB-4f0b-BBBE-64B5A3748AEE}",
    "statuscode": "{3EF39988-22BB-4f0b-BBBE-64B5A3748AEE}",
    "description": "{E0DECE4B-6FC8-4a8f-A065-082708572369}",
}

# Columns never worth putting on a generated form or view.
HIDDEN = {"externalId", "sourceSystem", "lastSyncedOn", "retentionLabel", "rowVersion"}


def guid() -> str:
    return "{" + str(uuid.uuid4()) + "}"


def attr(value) -> str:
    return quoteattr(str(value))


class UxDeployer(Deployer):
    """Views, forms, charts, site map and the app module itself."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ux = self.config.get("ux", {}) or {}
        self.view_ids: dict[str, str] = {}        # "<table>:<view name>" -> savedqueryid
        self.default_view: dict[str, str] = {}    # table logical name -> savedqueryid
        self.form_ids: dict[str, str] = {}
        self.chart_ids: list[str] = []

    # ------------------------------------------------------------- resolution
    def primary_id(self, table: Table) -> str:
        return f"{table.logical}id"

    def resolve(self, table: Table, canonical: str) -> str | None:
        """Canonical column name -> Dataverse logical name, tolerating logical names."""
        logical = table.logical_of(canonical)
        if logical:
            return logical
        if canonical in ("owner", "ownerid"):
            return "ownerid" if table.spec.get("ownership") != "organization" else None
        if canonical in ("createdon", "modifiedon", "statecode", "statuscode"):
            return canonical
        known = {c.logical for c in table.columns + table.natives}
        if canonical in known:
            return canonical
        self.log(f"  ! {table.name}: no column '{canonical}' — skipped")
        return None

    def column_type(self, table: Table, logical: str) -> str:
        for column in table.columns:
            if column.logical == logical:
                return "lookup" if column.kind == "lookup" else column.type
        for column in table.natives:
            if column.logical == logical:
                return "native"
        return "native"

    def classid(self, table: Table, logical: str) -> str:
        kind = self.column_type(table, logical)
        if kind == "native":
            return NATIVE_CLASSIDS.get(logical, CLASSIDS["native"])
        return CLASSIDS.get(kind, CLASSIDS["string"])

    def display_of(self, table: Table, logical: str) -> str:
        for column in table.columns + table.natives:
            if column.logical == logical:
                return column.display
        return title_label(logical.split("_", 1)[-1])

    def generated_columns(self, table: Table, limit: int | None = None) -> list[str]:
        """Sensible column order when the spec does not say: primary first, then the rest."""
        order: list[str] = []
        primary = table.primary_logical if table.is_custom else table.logical_of(table.spec.get("primary") or "name")
        if primary:
            order.append(primary)
        for column in table.columns:
            if column.kind not in ("attribute", "lookup"):
                continue
            if column.name in HIDDEN or column.logical in order:
                continue
            if column.type in ("image", "file"):
                continue
            order.append(column.logical)
        if table.spec.get("ownership") != "organization":
            order.append("ownerid")
        return order[:limit] if limit else order

    # -------------------------------------------------------------- fetch xml
    def condition_xml(self, table: Table, condition: dict) -> str:
        if "owner" in condition:
            return '<condition attribute="ownerid" operator="eq-userid" />'
        logical = self.resolve(table, condition["column"])
        if not logical:
            return ""
        operator = condition.get("operator")
        if "eq" in condition or "in" in condition:
            raw = condition.get("eq", condition.get("in"))
            values = raw if isinstance(raw, list) else [raw]
            resolved = [self.condition_value(table, logical, v) for v in values]
            resolved = [v for v in resolved if v is not None]
            if not resolved:
                return ""
            if len(resolved) == 1:
                return f'<condition attribute={attr(logical)} operator="eq" value={attr(resolved[0])} />'
            options = "".join(f"<value>{escape(str(v))}</value>" for v in resolved)
            return f'<condition attribute={attr(logical)} operator="in">{options}</condition>'
        if operator in ("null", "not-null", "this-quarter", "this-month", "next-x-days", "on-or-after-today"):
            mapping = {
                "null": '<condition attribute=%s operator="null" />',
                "not-null": '<condition attribute=%s operator="not-null" />',
                "this-quarter": '<condition attribute=%s operator="this-fiscal-quarter" />',
                "this-month": '<condition attribute=%s operator="this-month" />',
                "on-or-after-today": '<condition attribute=%s operator="on-or-after" value="@today" />',
            }
            if operator == "next-x-days":
                return (
                    f'<condition attribute={attr(logical)} operator="next-x-days" '
                    f'value={attr(condition.get("value", 30))} />'
                )
            return mapping[operator] % attr(logical)
        return ""

    def condition_value(self, table: Table, logical: str, value):
        kind = self.column_type(table, logical)
        if kind in ("choice", "multichoice"):
            for column in table.columns:
                if column.logical == logical:
                    code = self.model.enum_value(column.spec["enum"], value)
                    if code is None:
                        self.log(f"  ! {table.name}.{logical}: unknown choice '{value}'")
                    return code
        if isinstance(value, bool):
            return 1 if value else 0
        return value

    def fetch_xml(self, table: Table, columns: list[str], order: list, filters: list[dict]) -> str:
        attributes = "".join(f"<attribute name={attr(c)} />" for c in columns)
        orders = ""
        for entry in order or []:
            column, direction = (entry + ["asc"])[:2] if isinstance(entry, list) else (entry, "asc")
            logical = self.resolve(table, column)
            if logical:
                descending = "true" if str(direction).lower().startswith("desc") else "false"
                orders += f'<order attribute={attr(logical)} descending="{descending}" />'
        conditions = "".join(self.condition_xml(table, c) for c in (filters or []))
        filter_xml = f'<filter type="and">{conditions}</filter>' if conditions else ""
        return (
            '<fetch version="1.0" mapping="logical" no-lock="false" distinct="false">'
            f"<entity name={attr(table.logical)}>{attributes}{orders}{filter_xml}</entity>"
            "</fetch>"
        )

    def layout_xml(self, table: Table, columns: list[str]) -> str:
        widths = [300] + [150] * (len(columns) - 1)
        cells = "".join(
            f"<cell name={attr(column)} width=\"{width}\" />" for column, width in zip(columns, widths)
        )
        return (
            f'<grid name="resultset" object="{table.object_type_code or 1}" '
            f"jump={attr(columns[0] if columns else table.primary_logical)} select=\"1\" icon=\"1\" preview=\"1\">"
            f"<row name=\"result\" id={attr(self.primary_id(table))}>{cells}</row></grid>"
        )

    # ------------------------------------------------------------------ views
    def ensure_views(self) -> None:
        for table in self.model.tables.values():
            specs = (self.ux.get(table.name, {}) or {}).get("views")
            if not specs:
                columns = self.generated_columns(table, limit=7)
                specs = [{"name": f"Active {table.plural.lower()}", "default": True, "columns": columns}]
                generated = True
            else:
                generated = False
            existing = self.existing_views(table)
            for spec in specs:
                name = spec["name"]
                key = f"view:{table.logical}:{name}"
                saved_id = self.state.get(key) or existing.get(name.lower())
                if saved_id:
                    self.view_ids[f"{table.name}:{name}"] = saved_id
                    if spec.get("default"):
                        self.default_view[table.logical] = saved_id
                    self.state.set(key, saved_id)
                    continue
                columns = []
                for canonical in spec["columns"]:
                    logical = self.resolve(table, canonical) if not generated else canonical
                    if logical and logical not in columns:
                        columns.append(logical)
                if not columns:
                    continue
                fetch = self.fetch_xml(table, columns, spec.get("order", []), spec.get("filter", []))
                payload = {
                    "name": name,
                    "description": spec.get("description", f"{table.display} view from the CRM pilot."),
                    "returnedtypecode": table.logical,
                    "querytype": 0,
                    "fetchxml": fetch,
                    "layoutxml": self.layout_xml(table, columns),
                }
                with self.guard(f"view {table.logical} / {name}"):
                    result = self.dv.post("savedqueries", payload, {"MSCRM.SolutionUniqueName": self.solution})
                    saved_id = (result or {}).get("id") or str(uuid.uuid4())
                    self.state.set(key, saved_id)
                    self.view_ids[f"{table.name}:{name}"] = saved_id
                    if spec.get("default"):
                        self.default_view[table.logical] = saved_id
                    self.count("views")
                    self.log(f"  + view {table.logical}: {name}")
        self.set_default_views()

    def existing_views(self, table: Table) -> dict[str, str]:
        if self.dv.dry_run:
            return {}
        rows = self.dv.query(
            f"savedqueries?$select=savedqueryid,name&$filter=returnedtypecode eq '{table.logical}' and querytype eq 0"
        )
        return {row["name"].lower(): row["savedqueryid"] for row in rows}

    def set_default_views(self) -> None:
        for logical, saved_id in self.default_view.items():
            key = f"viewdefault:{logical}"
            if self.state.done(key):
                continue
            with self.guard(f"default view for {logical}"):
                self.dv.patch(f"savedqueries({saved_id})", {"isdefault": True})
                self.state.set(key, True)

    # ------------------------------------------------------------------ forms
    def ensure_forms(self) -> None:
        for table in self.model.tables.values():
            spec = (self.ux.get(table.name, {}) or {}).get("form")
            key = f"form:{table.logical}"
            if self.state.done(key):
                continue
            tabs = spec or self.generated_form(table)
            form_xml = self.form_xml(table, tabs)
            existing = self.main_form(table)
            with self.guard(f"form {table.logical}"):
                if existing:
                    self.dv.patch(
                        f"systemforms({existing['formid']})",
                        {"formxml": form_xml},
                        {"MSCRM.SolutionUniqueName": self.solution},
                    )
                    self.form_ids[table.logical] = existing["formid"]
                    self.state.set(key, existing["formid"])
                    self.log(f"  ~ form {table.logical} updated")
                else:
                    result = self.dv.post(
                        "systemforms",
                        {
                            "name": "Information",
                            "description": f"{table.display} main form (CRM pilot).",
                            "objecttypecode": table.logical,
                            "type": 2,
                            "formxml": form_xml,
                            "formactivationstate": 1,
                        },
                        {"MSCRM.SolutionUniqueName": self.solution},
                    )
                    form_id = (result or {}).get("id") or str(uuid.uuid4())
                    self.form_ids[table.logical] = form_id
                    self.state.set(key, form_id)
                    self.log(f"  + form {table.logical} created")
                self.count("forms")

    def main_form(self, table: Table) -> dict | None:
        if self.dv.dry_run:
            return None
        rows = self.dv.query(
            f"systemforms?$select=formid,name,type&$filter=objecttypecode eq '{table.logical}' and type eq 2"
        )
        if not rows:
            return None
        for row in rows:
            if (row.get("name") or "").lower() in ("information", table.display.lower()):
                return row
        return rows[0]

    def generated_form(self, table: Table) -> list[dict]:
        columns = [c for c in self.generated_columns(table) if c != "ownerid"]
        chunk = 12
        sections = []
        for index in range(0, len(columns), chunk):
            sections.append(
                {
                    "name": "Details" if index == 0 else f"More details {index // chunk}",
                    "columns": columns[index : index + chunk],
                    "_logical": True,
                }
            )
        return [{"tab": table.display, "sections": sections}]

    def form_xml(self, table: Table, tabs: list[dict]) -> str:
        placed: set[str] = set()
        tab_xml = ""
        for tab in tabs:
            sections_xml = ""
            for section in tab.get("sections", []) or []:
                cells = ""
                for canonical in section["columns"]:
                    logical = canonical if section.get("_logical") else self.resolve(table, canonical)
                    if not logical or logical in placed:
                        continue
                    placed.add(logical)
                    cells += self.cell_xml(table, logical)
                if not cells:
                    continue
                sections_xml += self.section_xml(section["name"], cells)
            for grid in tab.get("grids", []) or []:
                grid_xml = self.subgrid_xml(table, grid)
                if grid_xml:
                    sections_xml += self.section_xml(grid.get("label", grid["table"]), grid_xml, columns="1")
            if not sections_xml:
                continue
            tab_xml += (
                f'<tab name={attr("tab_" + str(tab["tab"]).lower().replace(" ", "_"))} id={attr(guid())} '
                'IsUserDefined="0" locklevel="0" showlabel="true" expanded="true" verticallayout="true">'
                f'<labels><label description={attr(tab["tab"])} languagecode="{LANG}" /></labels>'
                f'<columns><column width="100%"><sections>{sections_xml}</sections></column></columns></tab>'
            )

        # Anything the spec did not place still belongs somewhere.
        leftovers = [c for c in self.generated_columns(table) if c not in placed and c != "ownerid"]
        if leftovers:
            cells = "".join(self.cell_xml(table, logical) for logical in leftovers)
            tab_xml += (
                f'<tab name="tab_more" id={attr(guid())} IsUserDefined="0" locklevel="0" showlabel="true" '
                'expanded="true" verticallayout="true">'
                f'<labels><label description="More details" languagecode="{LANG}" /></labels>'
                f'<columns><column width="100%"><sections>'
                f'{self.section_xml("Other columns", cells)}'
                "</sections></column></columns></tab>"
            )
        return f"<form><tabs>{tab_xml}</tabs></form>"

    def section_xml(self, name: str, cells: str, columns: str = "11") -> str:
        identifier = "sec_" + str(name).lower().replace(" ", "_").replace("/", "_")[:40]
        return (
            f"<section name={attr(identifier)} id={attr(guid())} IsUserDefined=\"0\" locklevel=\"0\" "
            f"showlabel=\"true\" showbar=\"false\" layout=\"varwidth\" columns={attr(columns)} "
            f"celllabelalignment=\"Left\" celllabelposition=\"Left\">"
            f"<labels><label description={attr(name)} languagecode=\"{LANG}\" /></labels>"
            f"<rows>{cells}</rows></section>"
        )

    def cell_xml(self, table: Table, logical: str) -> str:
        display = self.display_of(table, logical)
        disabled = "false"
        for column in table.columns:
            if column.logical == logical and (column.read_only or column.spec.get("calculated")):
                disabled = "true"
        return (
            f"<row><cell id={attr(guid())} showlabel=\"true\" locklevel=\"0\">"
            f"<labels><label description={attr(display)} languagecode=\"{LANG}\" /></labels>"
            f"<control id={attr(logical)} classid={attr(self.classid(table, logical))} "
            f"datafieldname={attr(logical)} disabled=\"{disabled}\" /></cell></row>"
        )

    def subgrid_xml(self, table: Table, grid: dict) -> str:
        child = self.model.tables.get(grid["table"])
        if not child:
            self.log(f"  ! subgrid on {table.name}: table '{grid['table']}' is not deployed — skipped")
            return ""
        column = child.column(grid["column"])
        if not column or column.kind != "lookup":
            self.log(f"  ! subgrid on {table.name}: {grid['table']}.{grid['column']} is not a lookup — skipped")
            return ""
        relationship = (column.relationship or {}).get("SchemaName", "")
        view_id = self.default_view.get(child.logical) or "{00000000-0000-0000-0000-000000000000}"
        if not view_id.startswith("{"):
            view_id = "{" + view_id + "}"
        label = grid.get("label", child.plural)
        return (
            f"<row><cell id={attr(guid())} showlabel=\"true\" locklevel=\"0\" colspan=\"2\" rowspan=\"6\">"
            f"<labels><label description={attr(label)} languagecode=\"{LANG}\" /></labels>"
            f"<control id={attr('grid_' + child.logical)} classid={attr(SUBGRID_CLASSID)} "
            f"indicationOfSubgrid=\"true\" uniqueid={attr(guid())}><parameters>"
            f"<TargetEntityType>{escape(child.logical)}</TargetEntityType>"
            f"<ViewId>{escape(view_id)}</ViewId>"
            f"<RelationshipName>{escape(relationship)}</RelationshipName>"
            "<AutoExpand>Fixed</AutoExpand><EnableQuickFind>false</EnableQuickFind>"
            "<EnableViewPicker>true</EnableViewPicker><ViewIds /><EnableChartPicker>false</EnableChartPicker>"
            "<RecordsPerPage>10</RecordsPerPage><TargetEntityName />"
            "</parameters></control></cell></row>"
        )

    # ----------------------------------------------------------------- charts
    def ensure_charts(self) -> None:
        for spec in self.config.get("charts", []) or []:
            table = self.model.tables.get(spec["table"])
            if not table:
                continue
            key = f"chart:{table.logical}:{spec['name']}"
            if self.state.done(key):
                continue
            group_by = self.resolve(table, spec["group_by"])
            measure_column = spec["measure"]["column"]
            measure = self.resolve(table, measure_column) or self.primary_id(table)
            if not group_by:
                continue
            aggregate = spec["measure"].get("aggregate", "count")
            date_grouping = ""
            if self.column_type(table, group_by) in ("date", "datetime"):
                date_grouping = ' dategrouping="month"'
            conditions = "".join(self.condition_xml(table, c) for c in (spec.get("filter") or []))
            filter_xml = f'<filter type="and">{conditions}</filter>' if conditions else ""
            fetch = (
                '<fetch mapping="logical" aggregate="true">'
                f"<entity name={attr(table.logical)}>"
                f"<attribute name={attr(measure)} aggregate={attr(aggregate)} alias=\"measure\" />"
                f"<attribute name={attr(group_by)} groupby=\"true\" alias=\"category\"{date_grouping} />"
                f"{filter_xml}"
                '<order alias="category" descending="false" />'
                "</entity></fetch>"
            )
            data_description = (
                "<datadefinition><fetchcollection>"
                f"{fetch}"
                "</fetchcollection><categorycollection><categories>"
                '<category alias="category"><measurecollection>'
                '<measure alias="measure" /></measurecollection></category>'
                "</categories></categorycollection></datadefinition>"
            )
            chart_type = {"bar": "Bar", "column": "Column", "pie": "Pie", "line": "Line"}.get(
                spec.get("type", "column").lower(), "Column"
            )
            presentation = (
                '<Chart Palette="BrightPastel" PaletteCustomColors="" BorderColor="26, 59, 105" '
                'BorderWidth="0" BorderSkinStyle="None"><Series>'
                f'<Series ChartType={attr(chart_type)} IsValueShownAsLabel="true" Color="59, 130, 246" '
                'Font="{0}, 9.5px" LabelForeColor="59, 59, 59" CustomProperties="PointWidth=0.75, '
                'MaxPixelPointWidth=40" /></Series>'
                '<ChartAreas><ChartArea BorderColor="Transparent"><AxisY LabelAutoFitMinFontSize="8">'
                '<MajorGrid LineColor="239, 242, 246" /><LabelStyle Font="{0}, 10px" /></AxisY>'
                '<AxisX LabelAutoFitMinFontSize="8"><MajorGrid LineColor="Transparent" />'
                '<LabelStyle Font="{0}, 10px" /></AxisX></ChartArea></ChartAreas>'
                '<Legends><Legend Alignment="Center" LegendStyle="Table" Docking="Bottom" '
                'IsEquallySpacedItems="true" Font="{0}, 11px" /></Legends>'
                '<Titles><Title Alignment="TopLeft" DockedToChartArea="ChartArea" '
                f'Font="{{0}}, 12px" Text={attr(spec["name"])} /></Titles></Chart>'
            )
            with self.guard(f"chart {spec['name']}"):
                result = self.dv.post(
                    "savedqueryvisualizations",
                    {
                        "name": spec["name"],
                        "description": f"{spec['name']} — CRM pilot.",
                        "primaryentitytypecode": table.logical,
                        "datadescription": data_description,
                        "presentationdescription": presentation,
                        "isdefault": False,
                    },
                    {"MSCRM.SolutionUniqueName": self.solution},
                )
                chart_id = (result or {}).get("id")
                if chart_id:
                    self.chart_ids.append(chart_id)
                self.state.set(key, chart_id or True)
                self.count("charts")
                self.log(f"  + chart {spec['name']}")

    # --------------------------------------------------------------- site map
    def site_map_xml(self) -> str:
        app = self.config["app"]
        areas = ""
        for area in app["areas"]:
            groups = ""
            for group in area["groups"]:
                subareas = ""
                for subarea in group["subareas"]:
                    target = subarea["table"]
                    if target.startswith("native:"):
                        logical = target.split(":", 1)[1]
                    else:
                        table = self.model.tables.get(target)
                        if not table:
                            self.log(f"  ! site map: table '{target}' is not deployed — skipped")
                            continue
                        logical = table.logical
                    title = subarea.get("title") or title_label(target)
                    subareas += (
                        f"<SubArea Id={attr('sa_' + logical)} Entity={attr(logical)} Client=\"All\" "
                        'AvailableOffline="true" PassParams="false">'
                        f'<Titles><Title LCID="{LANG}" Title={attr(title)} /></Titles></SubArea>'
                    )
                if not subareas:
                    continue
                groups += (
                    f"<Group Id={attr(group['id'])} IsProfile=\"false\">"
                    f'<Titles><Title LCID="{LANG}" Title={attr(group["title"])} /></Titles>'
                    f"{subareas}</Group>"
                )
            if not groups:
                continue
            areas += (
                f"<Area Id={attr(area['id'])} ShowGroups=\"true\">"
                f'<Titles><Title LCID="{LANG}" Title={attr(area["title"])} /></Titles>'
                f"{groups}</Area>"
            )
        return f'<SiteMap IntroducedVersion="7.0.0.0">{areas}</SiteMap>'

    def ensure_site_map(self) -> str | None:
        app = self.config["app"]
        unique = f"{app['uniquename']}_sitemap"
        cached = self.state.get("sitemap_id")
        if cached:
            return cached
        existing = None
        if not self.dv.dry_run:
            existing = self.dv.first(f"sitemaps?$select=sitemapid&$filter=sitemapnameunique eq '{unique}'")
        xml = self.site_map_xml()
        if existing:
            with self.guard("site map update"):
                self.dv.patch(f"sitemaps({existing['sitemapid']})", {"sitemapxml": xml})
            self.state.set("sitemap_id", existing["sitemapid"])
            return existing["sitemapid"]
        site_map_id = None
        with self.guard("site map"):
            result = self.dv.post(
                "sitemaps",
                {"sitemapname": app["name"], "sitemapnameunique": unique, "sitemapxml": xml},
                {"MSCRM.SolutionUniqueName": self.solution},
            )
            site_map_id = (result or {}).get("id")
            self.state.set("sitemap_id", site_map_id or True)
            self.count("site maps")
            self.log("  + site map")
        self.add_to_solution(site_map_id, COMPONENT_SITEMAP)
        return site_map_id

    # --------------------------------------------------------------- app module
    def ensure_app(self, site_map_id: str | None) -> str | None:
        app = self.config["app"]
        cached = self.state.get("app_id")
        app_id = cached
        if not app_id:
            existing = None
            if not self.dv.dry_run:
                existing = self.dv.first(
                    f"appmodules?$select=appmoduleid&$filter=uniquename eq '{app['uniquename']}'"
                )
            if existing:
                app_id = existing["appmoduleid"]
                self.log("  · app already there")
            else:
                body = {
                    "name": app["name"],
                    "uniquename": app["uniquename"],
                    "description": app.get("description", ""),
                    "clienttype": 4,
                    "navigationtype": 0,
                }
                with self.guard("app module"):
                    try:
                        result = self.dv.post("appmodules", body, {"MSCRM.SolutionUniqueName": self.solution})
                    except DataverseError:
                        # Some environments reject the optional presentation fields.
                        body.pop("clienttype", None)
                        body.pop("navigationtype", None)
                        result = self.dv.post("appmodules", body, {"MSCRM.SolutionUniqueName": self.solution})
                    app_id = (result or {}).get("id")
                    self.count("apps")
                    self.log(f"  + app {app['name']}")
            if app_id:
                self.state.set("app_id", app_id)
        if not app_id:
            return None

        self.add_app_components(app_id, site_map_id)
        self.add_to_solution(app_id, COMPONENT_APPMODULE)
        return app_id

    def add_app_components(self, app_id: str, site_map_id: str | None) -> None:
        components: list[dict] = []
        if site_map_id:
            components.append({"@odata.type": "Microsoft.Dynamics.CRM.sitemap", "sitemapid": site_map_id})
        for table in self.model.tables.values():
            metadata_id = None
            if not self.dv.dry_run:
                metadata = self.dv.entity_metadata(table.logical, ["MetadataId"])
                metadata_id = (metadata or {}).get("MetadataId")
            if metadata_id or self.dv.dry_run:
                components.append(
                    {
                        "@odata.type": "Microsoft.Dynamics.CRM.entity",
                        "MetadataId": metadata_id or "00000000-0000-0000-0000-000000000000",
                    }
                )
        for logical in self.native_app_tables():
            metadata_id = None
            if not self.dv.dry_run:
                metadata = self.dv.entity_metadata(logical, ["MetadataId"])
                metadata_id = (metadata or {}).get("MetadataId")
            if metadata_id or self.dv.dry_run:
                components.append(
                    {
                        "@odata.type": "Microsoft.Dynamics.CRM.entity",
                        "MetadataId": metadata_id or "00000000-0000-0000-0000-000000000000",
                    }
                )
        for view_id in self.view_ids.values():
            components.append({"@odata.type": "Microsoft.Dynamics.CRM.savedquery", "savedqueryid": view_id})
        for form_id in self.form_ids.values():
            components.append({"@odata.type": "Microsoft.Dynamics.CRM.systemform", "formid": form_id})
        for chart_id in self.chart_ids:
            components.append(
                {"@odata.type": "Microsoft.Dynamics.CRM.savedqueryvisualization", "savedqueryvisualizationid": chart_id}
            )

        # Send in batches: one bad component should not cost the whole app.
        for index in range(0, len(components), 25):
            batch = components[index : index + 25]
            with self.guard(f"app components {index + 1}-{index + len(batch)}"):
                self.dv.post("AddAppComponents", {"AppId": app_id, "Components": batch})
        self.log(f"  + {len(components)} app components")

    def native_app_tables(self) -> list[str]:
        natives = []
        for area in self.config["app"]["areas"]:
            for group in area["groups"]:
                for subarea in group["subareas"]:
                    if subarea["table"].startswith("native:"):
                        natives.append(subarea["table"].split(":", 1)[1])
        extras = ["task", "phonecall", "appointment", "email", "annotation"]
        return list(dict.fromkeys(natives + extras))

    def validate_and_publish(self, app_id: str | None) -> None:
        if not app_id:
            return
        if not self.dv.dry_run:
            with self.guard("validate app"):
                result = self.dv.get(f"ValidateApp(AppModuleId={app_id})")
                for message in (result or {}).get("AppValidationResponse", {}).get("ValidationIssueList", []) or []:
                    self.log(f"  ! app validation: {message.get('Message') or message}")
        with self.guard("publish app"):
            self.dv.publish_xml(f"<importexportxml><appmodules><appmodule>{app_id}</appmodule></appmodules></importexportxml>")
            self.log("  + app published")
