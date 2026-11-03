"""Graph fragments, and the resolver that places references on items, tables and folders.

Node ids (the same everywhere, so fragments from different scans merge in the app):
    item:<item id>                      any Fabric item, in this or another workspace
    table:<lakehouse id>:<table>        a table (schema-enabled lakehouses: <schema>/<table>)
    file:<lakehouse id>:Files/<folder>  a folder under Files
    missing:<type>:<name>               something the code names that doesn't exist (or isn't visible)
    ext:<label>                         storage outside OneLake (ADLS, S3, JDBC, ...)

Edges run in the direction data flows:
    read   table/file -> notebook     write  notebook -> table/file     maintain notebook -> table
    run    caller -> notebook/pipeline        refresh  notebook/pipeline -> semantic model
    model_source  table -> semantic model     report_model  semantic model -> report
    shortcut  source table/folder -> the shortcut in the lakehouse
    default_lakehouse / attached / environment  notebook -> lakehouse / environment (settings)
    references  item -> item (an ID found in a definition, meaning unknown)
"""
from __future__ import annotations

from typing import Optional

from .links import item_url, workspace_url
from .parse.onelake import external_label, files_folder, is_guid, table_from_rel

LAKE_TYPES = ("Lakehouse",)


class Graph:
    def __init__(self):
        self.nodes: dict = {}
        self.edges: dict = {}

    def node(self, n: dict) -> str:
        cur = self.nodes.get(n["id"])
        if cur is None:
            self.nodes[n["id"]] = dict(n)
        else:
            for k, v in n.items():
                if v not in (None, "", [], {}) and cur.get(k) in (None, "", [], {}):
                    cur[k] = v
        return n["id"]

    def edge(self, kind: str, source: str, target: str, evidence: Optional[dict] = None) -> str:
        eid = f"{kind}|{source}|{target}"
        e = self.edges.setdefault(eid, {"id": eid, "kind": kind, "source": source, "target": target,
                                        "evidence": []})
        if evidence and evidence not in e["evidence"] and len(e["evidence"]) < 60:
            e["evidence"].append(evidence)
        return eid

    def to_json(self) -> tuple[list, list]:
        return list(self.nodes.values()), list(self.edges.values())


class Resolver:
    """Look things up for one scan: items by ID or name (any workspace), lakehouse layout, nodes."""

    def __init__(self, session, ws):
        self.s = session
        self.ws = ws                       # WorkspaceData of the workspace being explored

    # ------------------------------------------------------------------ items
    def workspace_id_of(self, ref: str) -> Optional[str]:
        if not ref:
            return self.ws.id
        if is_guid(ref):
            return ref.lower()
        if ref.lower() == self.ws.name.lower():
            return self.ws.id
        return self.s.ws_id_by_name(ref)

    def item(self, item_id: str, ws_id: Optional[str] = None) -> Optional[dict]:
        if not item_id:
            return None
        item_id = item_id.lower()
        ws_id = (ws_id or self.ws.id).lower()
        if ws_id == self.ws.id:
            return self.ws.by_id.get(item_id)
        return self.s.ext_item(ws_id, item_id)

    def find(self, name: str, types, ws_ref: str = "") -> Optional[dict]:
        if not name:
            return None
        if is_guid(name):
            it = self.item(name, self.workspace_id_of(ws_ref))
            return it if it and (not types or it["type"] in types) else None
        ws_id = self.workspace_id_of(ws_ref)
        if ws_id is None:
            return None
        if ws_id == self.ws.id:
            for t in types:
                it = self.ws.by_type_name.get((t, name.lower()))
                if it:
                    return it
            return None
        for it in self.s.items_of(ws_id):
            if it["type"] in types and it["displayName"].lower() == name.lower():
                return it
        return None

    def lakehouse_props(self, it: dict) -> Optional[dict]:
        if it is None or it.get("type") != "Lakehouse":
            return None
        if it["workspaceId"] == self.ws.id:
            return self.ws.lakehouses.get(it["id"])
        return self.s.ext_lakehouses(it["workspaceId"]).get(it["id"])

    def schemas(self, it: Optional[dict]) -> Optional[bool]:
        """True / False for a lakehouse with / without schemas; None when unknown. A warehouse always
        has schemas (dbo by default)."""
        if it and it.get("type") == "Warehouse":
            return True
        props = self.lakehouse_props(it)
        if props is None:
            return None
        return bool(props.get("defaultSchema"))

    def lakehouse_of_sql_endpoint(self, endpoint_id: str, ws_id: str) -> Optional[dict]:
        ws_id = (ws_id or self.ws.id).lower()
        lakes = self.ws.lakehouses if ws_id == self.ws.id else self.s.ext_lakehouses(ws_id)
        for lh_id, props in lakes.items():
            if str((props.get("sqlEndpointProperties") or {}).get("id", "")).lower() == endpoint_id.lower():
                return self.item(lh_id, ws_id)
        return None

    # ------------------------------------------------------------------ nodes
    def item_node(self, g: Graph, it: dict) -> str:
        ext = it["workspaceId"] != self.ws.id
        return g.node({"id": f"item:{it['id']}", "kind": "item", "type": it["type"], "label": it["displayName"],
                       "itemId": it["id"], "workspaceId": it["workspaceId"], "external": ext,
                       "workspaceName": self.s.ws_name(it["workspaceId"]) if ext else "",
                       "url": item_url(self.s.portal, it["workspaceId"], it["type"], it["id"], self.s.tenant_id)})

    def missing_node(self, g: Graph, type_: str, name: str, note: str) -> str:
        return g.node({"id": f"missing:{type_}:{name.lower()}", "kind": "missing", "type": type_,
                       "label": name, "note": note})

    def no_default_node(self, g: Graph) -> str:
        return self.missing_node(g, "Lakehouse", "(no default lakehouse)",
                                 "The notebook has no default lakehouse, so a table name without a lakehouse "
                                 "in front of it can't be placed.")

    def external_node(self, g: Graph, url: str, label: str = "", note: str = "Storage outside OneLake") -> str:
        label = label or external_label(url)
        return g.node({"id": f"ext:{label.lower()}", "kind": "external", "type": "External", "label": label,
                       "note": note, "detail": url})

    def lakehouse_parent(self, g: Graph, it: Optional[dict], missing_name: str = "") -> str:
        if it is not None:
            return self.item_node(g, it)
        if missing_name:
            return self.missing_node(g, "Lakehouse", missing_name,
                                     f"No lakehouse called {missing_name} was found.")
        return self.no_default_node(g)

    def table_node(self, g: Graph, parent: str, it: Optional[dict], schema: Optional[str], table: str,
                   listed: bool = False, extra: Optional[dict] = None) -> str:
        enabled = self.schemas(it)
        props = self.lakehouse_props(it) or {}
        default_schema = str(props.get("defaultSchema") or "dbo")
        if schema and not enabled and schema.lower() == "dbo":
            schema = None
        if enabled and not schema:
            schema = default_schema
        key = f"{schema.lower()}/{table.lower()}" if schema else table.lower()
        owner = it["id"] if it else parent.split(":", 1)[1]
        show_schema = bool(schema) and not (enabled and schema.lower() == default_schema.lower())
        node = {"id": f"table:{owner}:{key}", "kind": "table", "type": "Table",
                "label": f"{schema}.{table}" if show_schema else table, "schema": schema or "", "table": table,
                "parent": parent,
                "fullName": ".".join(x for x in ((it or {}).get("displayName", ""), schema or "", table) if x),
                "url": item_url(self.s.portal, it["workspaceId"], it["type"], it["id"], self.s.tenant_id)
                if it else ""}
        if listed:
            node["listed"] = True
        node.update(extra or {})
        return g.node(node)

    def file_node(self, g: Graph, parent: str, it: Optional[dict], folder: str, listed: bool = False) -> str:
        owner = it["id"] if it else parent.split(":", 1)[1]
        node = {"id": f"file:{owner}:{folder.lower()}", "kind": "file", "type": "Folder", "label": folder,
                "parent": parent,
                "fullName": "/".join(x for x in ((it or {}).get("displayName", ""), folder) if x),
                "url": item_url(self.s.portal, it["workspaceId"], it["type"], it["id"], self.s.tenant_id)
                if it else ""}
        if listed:
            node["listed"] = True
        return g.node(node)

    def place_table(self, g: Graph, parts: list, default_lh: Optional[dict]) -> str:
        """lakehouse / schema / table from a name as written in Spark: t, lh.t, lh.schema.t, ws.lh.t, ws.lh.s.t"""
        parts = [p for p in parts if p]
        it, missing, schema, table = None, "", None, parts[-1]
        if len(parts) == 1:
            it = default_lh
        elif len(parts) == 2:
            a = parts[0]
            it = self.find(a, LAKE_TYPES)
            if it is None:
                if default_lh is not None and self.schemas(default_lh):
                    it, schema = default_lh, a
                else:
                    missing = a
        elif len(parts) == 3:
            a, b = parts[0], parts[1]
            it = self.find(a, LAKE_TYPES)
            if it is not None:
                schema = b
            else:
                it = self.find(b, LAKE_TYPES, ws_ref=a)
                if it is None:
                    missing, schema = a, b
        else:
            ws_ref, lh, schema = parts[-4], parts[-3], parts[-2]
            it = self.find(lh, LAKE_TYPES, ws_ref=ws_ref)
            if it is None:
                missing = f"{ws_ref}.{lh}"
        if it is None and not missing and len(parts) == 1 and default_lh is None:
            parent = self.no_default_node(g)
        else:
            parent = self.lakehouse_parent(g, it, missing)
        return self.table_node(g, parent, it, schema, table)

    def place_location(self, g: Graph, loc, default_lh: Optional[dict]) -> Optional[str]:
        """A node for a parsed path: table, Files folder, an item, or external storage."""
        if loc is None:
            return None
        if loc.external:
            return self.external_node(g, loc.external)
        it, missing = None, ""
        if loc.default:
            it = default_lh
        else:
            ws_id = self.workspace_id_of(loc.workspace or "")
            types = (loc.item_type,) if loc.item_type else ("Lakehouse", "Warehouse", "KQLDatabase",
                                                            "MirroredDatabase", "SQLDatabase", "SparkJobDefinition")
            if loc.item and is_guid(loc.item):
                it = self.item(loc.item, ws_id) if ws_id else None
            elif loc.item:
                it = self.find(loc.item, types, ws_ref=ws_id or "")
            if it is None:
                missing = loc.item or (loc.workspace or "")
                if loc.item and is_guid(loc.item):
                    return g.node({"id": f"missing:Item:{loc.item}", "kind": "missing", "type": "Item",
                                   "label": loc.item[:8] + "…", "note": "An item this account can't see "
                                   f"(workspace {loc.workspace or 'unknown'}).", "detail": loc.raw})
        if loc.area == "" and not loc.rel:
            return self.lakehouse_parent(g, it, missing) if (it or missing) else None
        parent = self.lakehouse_parent(g, it, missing) if (it or missing) else self.no_default_node(g)
        if loc.area == "Tables":
            schema, table = table_from_rel(loc.rel, self.schemas(it))
            if table is None:
                return parent
            return self.table_node(g, parent, it, schema, table)
        if loc.area == "Files":
            return self.file_node(g, parent, it, files_folder(loc.rel))
        return parent

    def workspace_link(self) -> str:
        return workspace_url(self.s.portal, self.ws.id, self.s.tenant_id)
