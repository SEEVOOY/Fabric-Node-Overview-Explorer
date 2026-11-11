"""What selecting an item fetches, per item type, and how the answer becomes nodes and edges.

    Notebook        POST notebooks/{id}/getDefinition?format=ipynb  -> code -> reads / writes / runs
    Lakehouse       GET lakehouses/{id}, .../tables (or OneLake folder listing), items/{id}/shortcuts
    SemanticModel   POST semanticModels/{id}/getDefinition?format=TMSL -> source of every table
    Report          GET (Power BI) groups/{ws}/reports/{id}             -> its semantic model
    DataPipeline    POST items/{id}/getDefinition                       -> activities
    anything else   POST items/{id}/getDefinition                       -> IDs of other items in it
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

from urllib.parse import quote

from .auth import POWERBI_SCOPE, STORAGE_SCOPE
from .column_lineage import TableRef, read_notebook_columns
from .column_lineage.pipelines import read_pipeline_columns
from .column_lineage.powerbi import ReportProgram, read_model, read_report
from .fabric_api import FABRIC_API, ONELAKE_DFS, ONELAKE_TABLES, POWERBI_API, ApiError
from .graph import LAKE_TYPES, Graph, Resolver
from .parse.model import model_tables, parse_m
from .parse.notebook import analyze_notebook, read_notebook
from .parse.onelake import GUID_RE, is_guid, parse_location
from .parse.pipeline import _ref_from_store, analyze_pipeline

NOT_DEFINABLE = {"Dashboard", "Datamart", "MirroredWarehouse", "PaginatedReport"}
_ONELAKE_IN_TEXT = re.compile(r"(?:abfss?://[^\s\"'@]+@[^\s\"'/]*onelake[^\s\"'/]*|https?://[^\s\"'/]*onelake"
                              r"[^\s\"'/]*)\.fabric\.microsoft\.com/[^\s\"'<>)]*", re.I)


class ScanContext:
    def __init__(self, session, ws, item: dict, exports_of: Callable[[dict], Optional[dict]],
                 helpers_of: Optional[Callable[[dict], dict]] = None):
        self.s = session
        self.client = session.client
        self.ws = ws
        self.item = item
        self.r = Resolver(session, ws)
        self.exports_of = exports_of
        self.helpers_of = helpers_of or (lambda nb: {})


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _io_summary(g: Graph, me: str, unresolved: int = 0) -> str:
    kinds = {nid: n.get("kind") for nid, n in g.nodes.items()}
    types = {nid: n.get("type") for nid, n in g.nodes.items()}

    def group(ids):
        t = sum(1 for i in ids if kinds.get(i) == "table")
        f = sum(1 for i in ids if kinds.get(i) == "file")
        m = sum(1 for i in ids if types.get(i) == "SemanticModel")
        o = len(ids) - t - f - m
        bits = [_plural(t, "table") if t else "", _plural(f, "folder") if f else "",
                _plural(m, "semantic model") if m else "", _plural(o, "other source") if o else ""]
        bits = [b for b in bits if b]
        return " and ".join([", ".join(bits[:-1]), bits[-1]]) if len(bits) > 1 else (bits[0] if bits else "")

    edges = g.edges.values()
    reads = {e["source"] for e in edges if e["kind"] == "read" and e["target"] == me}
    writes = {e["target"] for e in edges if e["kind"] == "write" and e["source"] == me}
    maint = {e["target"] for e in edges if e["kind"] == "maintain" and e["source"] == me}
    runs = {e["target"] for e in edges if e["kind"] == "run" and e["source"] == me}
    refresh = {e["target"] for e in edges if e["kind"] == "refresh" and e["source"] == me}
    parts = []
    if reads:
        parts.append(f"reads {group(reads)}")
    if writes:
        parts.append(f"writes {group(writes)}")
    if maint:
        parts.append(f"maintains {_plural(len(maint), 'table')}")
    if runs:
        run_types = {types.get(i) for i in runs}
        word = "notebook" if run_types <= {"Notebook"} else "item"
        parts.append(f"runs {_plural(len(runs), word)}")
    if refresh:
        parts.append(f"refreshes {_plural(len(refresh), 'semantic model')}")
    text = ", ".join(parts)
    text = (text[0].upper() + text[1:] + ".") if text else "Found no reads or writes."
    if unresolved:
        text += f" {_plural(unresolved, 'reference')} use names built at run time."
    return text


def _connected(g: Graph, me: str) -> set:
    """Node ids already linked to me, plus the lakehouses of linked tables and folders."""
    out = set()
    for e in g.edges.values():
        for a, b in ((e["source"], e["target"]), (e["target"], e["source"])):
            if a == me:
                out.add(b)
                parent = g.nodes.get(b, {}).get("parent")
                if parent:
                    out.add(parent)
    return out


def _evidence(ref) -> dict:
    return {"where": f"Cell {ref.cell}, line {ref.line}", "cell": ref.cell, "line": ref.line, "code": ref.code,
            "verb": ref.verb, "note": ref.note}


def _leaf(name: str) -> str:
    return name.replace("\\", "/").rstrip("/").split("/")[-1].removesuffix(".ipynb")


# ---------------------------------------------------------------------- notebook
def scan_notebook(ctx: ScanContext) -> dict:
    it, r = ctx.item, ctx.r
    parts = ctx.client.get_definition(
        f"{FABRIC_API}/workspaces/{it['workspaceId']}/notebooks/{it['id']}/getDefinition?format=ipynb")
    info = read_notebook(parts)
    refs, facts = analyze_notebook(info)
    seeded, seed = [], {}
    if facts["runTargets"] and any(x.dynamic for x in refs):
        for target in facts["runTargets"]:
            nb = r.find(_leaf(target), ("Notebook",))
            if nb and nb["id"] != it["id"]:
                exported = ctx.exports_of(nb) or {}
                if exported:
                    seeded.append(nb["displayName"])
                for k, vals in exported.items():
                    cur = seed.setdefault(k, [])
                    cur.extend(v for v in vals if v not in cur)
        if seed:
            refs, facts = analyze_notebook(info, seed=seed)

    g = Graph()
    me = r.item_node(g, it)
    dl = info.default_lakehouse
    default_lh = None
    if dl.get("id"):
        default_lh = r.item(dl["id"], dl.get("workspace_id") or None)
        lh_node = r.item_node(g, default_lh) if default_lh else g.node(
            {"id": f"missing:Lakehouse:{dl['id']}", "kind": "missing", "type": "Lakehouse",
             "label": dl.get("name") or dl["id"][:8], "note": "The default lakehouse isn't visible to this account "
             "(deleted, or in a workspace you can't open)."})
        g.edge("default_lakehouse", me, lh_node, {"where": "Notebook settings", "verb": "Default lakehouse"})
    attached = []
    for kid in info.known_lakehouses:
        if kid == dl.get("id"):
            continue
        k = r.item(kid)
        if k:
            attached.append(r.item_node(g, k))
            g.edge("attached", me, attached[-1], {"where": "Notebook settings", "verb": "Attached lakehouse"})
    env_node = None
    if info.environment.get("id"):
        env = r.item(info.environment["id"], info.environment.get("workspace_id") or None)
        if env:
            env_node = r.item_node(g, env)
            g.edge("environment", me, env_node, {"where": "Notebook settings", "verb": "Environment"})
    if info.default_warehouse.get("id"):
        wh = r.item(info.default_warehouse["id"])
        if wh:
            g.edge("attached", me, r.item_node(g, wh), {"where": "Notebook settings", "verb": "Default warehouse"})

    current, unresolved = default_lh, []
    for ref in refs:
        ev = _evidence(ref)
        if ref.dynamic:
            unresolved.append({**ev, "kind": ref.kind, "op": ref.op, "shown": ref.shown or "?"})
            continue
        if ref.kind == "use":
            target = r.find(ref.parts[0], LAKE_TYPES)
            if target:
                current = target
            continue
        node = None
        if ref.kind == "table":
            node = r.place_table(g, ref.parts, current)
        elif ref.kind == "path":
            node = r.place_location(g, parse_location(ref.path), current)
        elif ref.kind == "warehouse_table":
            p = ref.parts
            wh = r.find(p[0], ("Warehouse", "Lakehouse", "SQLEndpoint")) if len(p) >= 2 else None
            parent = r.item_node(g, wh) if wh else r.missing_node(g, "Warehouse", p[0], "No warehouse with this name.")
            node = r.table_node(g, parent, wh if wh and wh["type"] in ("Lakehouse", "Warehouse") else None,
                                p[-2] if len(p) >= 3 else None, p[-1])
        elif ref.kind == "notebook":
            name = _leaf(ref.target)
            nb = r.find(name, ("Notebook",), ws_ref=ref.workspace) if not is_guid(name) else r.item(
                name, r.workspace_id_of(ref.workspace))
            node = r.item_node(g, nb) if nb else r.missing_node(g, "Notebook", name, "No notebook with this name in "
                                                                 + ("that workspace." if ref.workspace else
                                                                    "the workspace."))
        elif ref.kind == "model":
            m = r.find(ref.target, ("SemanticModel",), ws_ref=ref.workspace)
            node = r.item_node(g, m) if m else r.missing_node(g, "SemanticModel", ref.target,
                                                              "No semantic model with this name.")
        elif ref.kind == "external":
            node = r.external_node(g, ref.path, label=" ".join(x for x in ("JDBC", ref.target) if x),
                                   note=ref.path)
        if node is None or node == me:
            continue
        if ref.op == "read":
            g.edge("read", node, me, ev)
        elif ref.op in ("write", "maintain", "run", "refresh"):
            g.edge(ref.op, me, node, ev)

    imported = {}                               # functions of the notebooks it %runs, for its column lineage
    for target in facts["runTargets"]:
        nb = r.find(_leaf(target), ("Notebook",))
        if nb and nb["id"] != it["id"]:
            for name, h in (ctx.helpers_of(nb) or {}).items():
                imported.setdefault(name, (*h[:4], nb["displayName"]))
    program = column_program(r, info, seed, default_lh, imported)
    nodes, edges = g.to_json()
    details = {"language": ", ".join(facts["languages"]) or info.language, "kernel": info.kernel,
               "cells": facts["cells"], "codeCells": facts["codeCells"], "format": info.format,
               "defaultLakehouse": (f"item:{default_lh['id']}" if default_lh else
                                    (f"missing:Lakehouse:{dl['id']}" if dl.get("id") else "")),
               "attached": attached, "environment": env_node or "", "exports": facts["exports"],
               "includedFrom": seeded, "regexCells": facts["regexCells"]}
    return {"summary": _io_summary(g, me, len(unresolved)), "nodes": nodes, "edges": edges,
            "unresolved": unresolved, "details": details, "_columnProgram": program}


def table_ref_of(g: Graph, node: Optional[str], as_written: str = "") -> Optional[TableRef]:
    """A table, folder or outside store placed on the map, as a dataset of the column lineage."""
    n = g.nodes.get(node or "")
    if not n or n.get("kind") not in ("table", "file", "external"):
        return None
    parent = g.nodes.get(n.get("parent", ""), {})
    item = parent.get("kind") == "item"
    return TableRef(id=node, label=n.get("label", ""), full_name=n.get("fullName", "") or n.get("label", ""),
                    kind=n["kind"], lakehouse=n.get("parent", ""), lakehouse_name=parent.get("label", ""),
                    workspace_id=parent.get("workspaceId", "") if item else "",
                    item_id=parent.get("itemId", "") if item else "", schema=n.get("schema", ""),
                    table=n.get("table", ""), as_written=as_written)


def column_program(r: Resolver, info, seed: dict, default_lh: Optional[dict], imported: Optional[dict] = None):
    """What the notebook does to columns (column_lineage), its datasets placed like the map places them."""
    scratch = Graph()

    def resolve_table(kind: str, parts: list, path: str, database: str) -> Optional[TableRef]:
        current = default_lh
        if database:
            current = r.find(database.split(".")[-1], LAKE_TYPES) or current
        if kind == "path":
            node = r.place_location(scratch, parse_location(path), current)
        elif kind == "warehouse_table" and len(parts) >= 2:
            wh = r.find(parts[0], ("Warehouse", "Lakehouse", "SQLEndpoint"))
            parent = r.item_node(scratch, wh) if wh else r.missing_node(scratch, "Warehouse", parts[0],
                                                                         "No warehouse with this name.")
            node = r.table_node(scratch, parent, wh if wh and wh["type"] in ("Lakehouse", "Warehouse") else None,
                                parts[-2] if len(parts) >= 3 else None, parts[-1])
        elif parts:
            node = r.place_table(scratch, parts, current)
        else:
            return None
        ref = table_ref_of(scratch, node, ".".join(parts) if parts else path)
        return ref if ref is None or ref.kind in ("table", "file") else None

    return read_notebook_columns(info, resolve_table, seed or None, imported)


# ---------------------------------------------------------------------- lakehouse
def _list_tables(ctx: ScanContext, schemas: bool) -> tuple[list, str, list]:
    """[(schema, table, format, type)], how they were listed, notes."""
    it, notes = ctx.item, []
    base = f"{FABRIC_API}/workspaces/{it['workspaceId']}/lakehouses/{it['id']}/tables"
    if not schemas:
        try:
            out, url, pages = [], f"{base}?maxResults=100", 0
            while url and pages < 100:
                data = ctx.client.get_json(url)
                pages += 1
                for t in data.get("data") or data.get("value") or []:
                    out.append((None, str(t.get("name") or ""), str(t.get("format") or ""), str(t.get("type") or "")))
                url = data.get("continuationUri") or (
                    f"{base}?maxResults=100&continuationToken={data['continuationToken']}"
                    if data.get("continuationToken") else "")
            return [t for t in out if t[1]], "Lakehouse API", notes
        except ApiError as e:
            notes.append(f"The tables API answered: {e.message[:160]}. Listed the Tables folder instead.")
    try:
        out = []
        top = ctx.client.onelake_paths(it["workspaceId"], f"{it['id']}/Tables")
        for p in top:
            if str(p.get("isDirectory", "")).lower() != "true":
                continue
            name = str(p.get("name", "")).rstrip("/").split("/")[-1]
            if not schemas:
                out.append((None, name, "", ""))
                continue
            for q in ctx.client.onelake_paths(it["workspaceId"], f"{it['id']}/Tables/{name}"):
                if str(q.get("isDirectory", "")).lower() == "true":
                    out.append((name, str(q.get("name", "")).rstrip("/").split("/")[-1], "", ""))
        return out, "OneLake", notes
    except ApiError as e:
        notes.append(f"Couldn't list the tables: {e.message[:160]}")
        return [], "", notes


def scan_lakehouse(ctx: ScanContext) -> dict:
    it, r, g = ctx.item, ctx.r, Graph()
    me = r.item_node(g, it)
    props = ctx.client.get_json(f"{FABRIC_API}/workspaces/{it['workspaceId']}/lakehouses/{it['id']}").get(
        "properties") or {}
    if it["workspaceId"] == ctx.ws.id:
        ctx.ws.lakehouses[it["id"]] = props
    schemas = bool(props.get("defaultSchema"))
    tables, via, notes = _list_tables(ctx, schemas)
    table_ids = []
    for schema, name, fmt, ttype in tables:
        table_ids.append(r.table_node(g, me, it, schema, name, listed=True,
                                      extra={"format": fmt, "tableType": ttype}))
    folders = []
    try:                                              # the top-level folders under Files
        for p in ctx.client.onelake_paths(it["workspaceId"], f"{it['id']}/Files"):
            if str(p.get("isDirectory", "")).lower() == "true":
                name = str(p.get("name", "")).rstrip("/").split("/")[-1]
                folders.append(r.file_node(g, me, it, f"Files/{name}", listed=True))
    except ApiError as e:
        notes.append(f"Couldn't list the Files folders: {e.message[:160]}")
    shortcuts = []
    try:
        for sc in ctx.client.get_paged(f"{FABRIC_API}/workspaces/{it['workspaceId']}/items/{it['id']}/shortcuts"):
            path, name = str(sc.get("path") or ""), str(sc.get("name") or "")
            target = sc.get("target") or {}
            segs = [s for s in path.split("/") if s]
            if segs and segs[0].lower() == "tables":
                here = r.table_node(g, me, it, segs[1] if len(segs) > 1 else None, name, listed=True,
                                    extra={"shortcut": True})
            else:
                here = r.file_node(g, me, it, "/".join([*segs, name]) or name)
            src, label = None, ""
            ol = target.get("oneLake") or {}
            if ol.get("itemId"):
                src_item = r.item(str(ol["itemId"]), str(ol.get("workspaceId") or "") or None)
                tpath = str(ol.get("path") or "")
                if src_item:
                    loc = parse_location(f"https://onelake.dfs.fabric.microsoft.com/{src_item['workspaceId']}/"
                                         f"{src_item['id']}/{tpath}")
                    src = r.place_location(g, loc, None)
                    label = f"{src_item['displayName']}/{tpath}"
                else:
                    src = g.node({"id": f"missing:Item:{str(ol['itemId']).lower()}", "kind": "missing",
                                  "type": "Item", "label": str(ol["itemId"])[:8] + "…",
                                  "note": "A shortcut source this account can't see."})
                    label = tpath
            else:
                kind = next((k for k in target if k != "type" and isinstance(target.get(k), dict)), "")
                spec = target.get(kind) or {}
                url = (str(spec.get("location") or "").rstrip("/") + str(spec.get("subpath") or "")) or kind
                host = re.sub(r"^[a-z]+://", "", str(spec.get("location") or ""), flags=re.I).split(".")[0]
                nice = {"adlsGen2": "ADLS", "amazonS3": "S3", "googleCloudStorage": "GCS", "s3Compatible": "S3",
                        "dataverse": "Dataverse", "oneDriveSharePoint": "SharePoint"}.get(kind, kind or "External")
                label = f"{nice} {host}{spec.get('subpath') or ''}".strip()
                src = r.external_node(g, url or label, label=label,
                                      note=f"Shortcut target outside OneLake ({nice})")
            if src:
                g.edge("shortcut", src, here, {"where": f"Shortcut {path}/{name}", "verb": "Shortcut",
                                               "note": label})
            shortcuts.append({"name": name, "path": path, "node": here, "source": src or "", "target": label})
    except ApiError as e:
        notes.append(f"Couldn't list shortcuts: {e.message[:160]}")
    sql = props.get("sqlEndpointProperties") or {}
    nodes, edges = g.to_json()
    summary = ", ".join(x for x in (_plural(len(table_ids), "table"), _plural(len(folders), "folder") if folders else "",
                                    _plural(len(shortcuts), "shortcut") if shortcuts else "") if x) + "."
    return {"summary": summary, "nodes": nodes, "edges": edges, "unresolved": [],
            "details": {"schemas": schemas, "defaultSchema": props.get("defaultSchema") or "",
                        "tables": table_ids, "tablesVia": via, "shortcuts": shortcuts, "notes": notes,
                        "folders": folders,
                        "sqlEndpoint": {"id": sql.get("id", ""), "connectionString": sql.get("connectionString", ""),
                                        "status": sql.get("provisioningStatus", "")},
                        "oneLakeTablesPath": props.get("oneLakeTablesPath", ""),
                        "oneLakeFilesPath": props.get("oneLakeFilesPath", "")}}


# ---------------------------------------------------------------------- warehouse
def scan_warehouse(ctx: ScanContext) -> dict:
    """A warehouse's tables, by schema. Warehouses keep their tables in OneLake as Delta tables
    (Tables/<schema>/<table>), so they are listed there, and their columns read like a lakehouse's."""
    it, r, g = ctx.item, ctx.r, Graph()
    me = r.item_node(g, it)
    tables, via, notes = _list_tables(ctx, True)
    table_ids = [r.table_node(g, me, it, schema, name, listed=True, extra={"format": "Delta"}) for schema, name, _, _ in tables]
    schemas = sorted({t[0] for t in tables if t[0]})
    if not tables and not notes:
        notes.append("No tables found in its Tables folder in OneLake.")
    nodes, edges = g.to_json()
    summary = f"{_plural(len(table_ids), 'table')}" + (f" in {_plural(len(schemas), 'schema')}" if schemas else "") + "."
    return {"summary": summary, "nodes": nodes, "edges": edges, "unresolved": [],
            "details": {"schemas": True, "defaultSchema": "dbo", "tables": table_ids, "tablesVia": via, "notes": notes,
                        "folders": [], "shortcuts": []}}


# ---------------------------------------------------------------------- semantic model
def scan_semantic_model(ctx: ScanContext) -> dict:
    it, r, g = ctx.item, ctx.r, Graph()
    me = r.item_node(g, it)
    parts = ctx.client.get_definition(
        f"{FABRIC_API}/workspaces/{it['workspaceId']}/semanticModels/{it['id']}/getDefinition?format=TMSL")
    raw = next((v for k, v in parts.items() if k.lower().endswith("model.bim")), None)
    if raw is None:
        raise ApiError(0, "The model definition had no model.bim part.", "NoModelBim")
    tables, sources, placed = [], set(), {}
    bim = json.loads(raw.decode("utf-8-sig"))
    for t in model_tables(bim):
        src, node = t["source"], None
        ev = {"where": f"Model table {t['table']}", "verb": t["kind"] or t["mode"]}
        if src.get("item_id"):
            ws_id = src.get("workspace_id") or it["workspaceId"]
            store = r.lakehouse_of_sql_endpoint(src["item_id"], ws_id) or r.item(src["item_id"], ws_id)
            if store is not None:
                parent = r.item_node(g, store)
                sources.add(parent)
                if t["entity"]:
                    node = r.table_node(g, parent, store if store["type"] == "Lakehouse" else None,
                                        t["schema"] or None, t["entity"])
                else:
                    node = parent
            else:
                node = g.node({"id": f"missing:Item:{src['item_id']}", "kind": "missing", "type": "Item",
                               "label": src["item_id"][:8] + "…", "note": "A source this account can't see."})
        elif src.get("server"):
            store = r.find(src.get("database") or "", ("Lakehouse", "Warehouse"))
            if store:
                parent = r.item_node(g, store)
                sources.add(parent)
                node = r.table_node(g, parent, store if store["type"] == "Lakehouse" else None,
                                    t["schema"] or None, t["entity"]) if t["entity"] else parent
            else:
                label = f"{src['server'].split('.')[0]}/{src.get('database') or ''}".rstrip("/")
                node = r.external_node(g, f"sql://{src['server']}/{src.get('database', '')}", label=f"SQL {label}",
                                       note=src["server"])
        if node:
            g.edge("model_source", node, me, ev)
            ref = table_ref_of(g, node, f"{t['schema']}.{t['entity']}".strip("."))
            if ref is not None:
                placed[t["table"]] = ref
        tables.append({"table": t["table"], "mode": t["mode"], "kind": t["kind"], "entity": t["entity"],
                       "schema": t["schema"], "source": node or "", "columns": t.get("columns", [])})
    nodes, edges = g.to_json()
    used = sum(1 for t in tables if t["source"])
    names = sorted(g.nodes[s]["label"] for s in sources)
    where = " and ".join([", ".join(names[:-1]), names[-1]]) if len(names) > 1 else (names[0] if names else "")
    summary = f"{_plural(len(tables), 'table')}" + (f"; {used} read from {where}." if where else
                                                     "; none come from a lakehouse or warehouse.")
    return {"summary": summary, "nodes": nodes, "edges": edges, "unresolved": [], "details": {"tables": tables},
            "_columnProgram": read_model(bim, placed)}


# ---------------------------------------------------------------------- report
def scan_report(ctx: ScanContext) -> dict:
    it, r, g = ctx.item, ctx.r, Graph()
    me = r.item_node(g, it)
    ds_id, ds_ws, via, parts = "", it["workspaceId"], "Power BI API", None
    try:
        rep = ctx.client.get_json(f"{POWERBI_API}/groups/{it['workspaceId']}/reports/{it['id']}", POWERBI_SCOPE)
        ds_id = str(rep.get("datasetId") or "").lower()
        ds_ws = str(rep.get("datasetWorkspaceId") or ds_ws).lower()
    except ApiError:
        via = "report definition"
        parts = ctx.client.get_definition(f"{FABRIC_API}/workspaces/{it['workspaceId']}/items/{it['id']}/getDefinition")
        pbir = next((v for k, v in parts.items() if k.lower().endswith("definition.pbir")), b"")
        text = pbir.decode("utf-8-sig", "replace")
        m = re.search(r"semanticmodelid=(" + GUID_RE.pattern + ")", text, re.I) or GUID_RE.search(text)
        ds_id = (m.group(1) if m and m.groups() else m.group(0)).lower() if m else ""
    model = r.item(ds_id, ds_ws) if ds_id else None
    if model:
        g.edge("report_model", r.item_node(g, model), me, {"where": f"Report settings ({via})", "verb": "Built on"})
    elif ds_id:
        g.edge("report_model", g.node({"id": f"missing:Item:{ds_id}", "kind": "missing", "type": "SemanticModel",
                                       "label": ds_id[:8] + "…", "note": "A semantic model this account can't see."}),
               me, {"where": f"Report settings ({via})", "verb": "Built on"})
    try:                                         # its visuals and the fields they show, for the column lineage
        if parts is None:
            parts = ctx.client.get_definition(f"{FABRIC_API}/workspaces/{it['workspaceId']}/items/{it['id']}/getDefinition")
        program = read_report(parts, ds_id)
    except ApiError as e:
        program = ReportProgram(ds_id, notes=[f"Its visuals couldn't be read: {e}"])
    nodes, edges = g.to_json()
    return {"summary": f"Built on {model['displayName']}." if model else "Its semantic model wasn't found.",
            "nodes": nodes, "edges": edges, "unresolved": [], "details": {"semanticModel": f"item:{ds_id}" if ds_id else ""},
            "_columnProgram": program}


# ---------------------------------------------------------------------- pipeline
def scan_pipeline(ctx: ScanContext) -> dict:
    it, r, g = ctx.item, ctx.r, Graph()
    me = r.item_node(g, it)
    parts = ctx.client.get_definition(f"{FABRIC_API}/workspaces/{it['workspaceId']}/items/{it['id']}/getDefinition")
    raw = next((v for k, v in parts.items() if k.lower().endswith("pipeline-content.json")), b"{}")
    content = json.loads(raw.decode("utf-8-sig") or "{}")
    unresolved, activities = [], []
    for pr in analyze_pipeline(content):
        ev = {"where": f"Activity {pr.activity} ({pr.activity_type}{', ' + pr.side if pr.side else ''})",
              "verb": pr.activity_type}
        activities.append({"name": pr.activity, "type": pr.activity_type, "op": pr.op})
        target = r.item(pr.item_id, pr.workspace_id or None) if pr.item_id else (
            r.find(pr.item_name, (pr.type_hint,)) if pr.item_name else None)
        if pr.op in ("run", "refresh"):
            if target:
                g.edge(pr.op, me, r.item_node(g, target), ev)
            elif pr.item_id:
                g.edge(pr.op, me, g.node({"id": f"missing:Item:{pr.item_id}", "kind": "missing",
                                          "type": pr.type_hint or "Item", "label": pr.item_id[:8] + "…",
                                          "note": "An item this account can't see."}), ev)
            else:
                unresolved.append({**ev, "kind": pr.type_hint, "op": pr.op, "shown": pr.item_name or "an expression"})
            continue
        node = None
        if target is not None:
            parent = r.item_node(g, target)
            if pr.table:
                node = r.table_node(g, parent, target if target["type"] == "Lakehouse" else None,
                                    pr.schema or None, pr.table)
            elif pr.path:
                node = r.file_node(g, parent, target, pr.path)
            else:
                node = parent
        elif pr.external:
            node = r.external_node(g, pr.external, label=pr.external, note=f"{pr.side or 'data store'} of "
                                                                             f"activity {pr.activity}")
        if node is None:
            if pr.dynamic:
                unresolved.append({**ev, "kind": "table", "op": pr.op, "shown": "a pipeline expression"})
            continue
        if pr.dynamic and pr.table == "":
            ev["note"] = "parts of this location are pipeline expressions"
        if pr.op == "read":
            g.edge("read", node, me, ev)
        else:
            g.edge("write", me, node, ev)
    covered = _connected(g, me)
    for gid in sorted({m.lower() for m in GUID_RE.findall(raw.decode("utf-8-sig", "replace"))}):
        ref = ctx.ws.by_id.get(gid)
        if ref and ref["id"] != it["id"] and f"item:{gid}" not in covered:
            g.edge("references", me, r.item_node(g, ref), {"where": "Pipeline definition", "verb": "ID in definition"})
    def place(st: dict, side: str) -> Optional[TableRef]:
        pr = _ref_from_store(st, "read" if side == "source" else "write", "", "", side)
        scratch, node = Graph(), None
        target = r.item(pr.item_id, pr.workspace_id or None) if pr.item_id else None
        if target is not None:
            parent = r.item_node(scratch, target)
            if pr.table:
                node = r.table_node(scratch, parent, target if target["type"] == "Lakehouse" else None,
                                    pr.schema or None, pr.table)
            elif pr.path:
                node = r.file_node(scratch, parent, target, pr.path)
        elif pr.external:
            node = r.external_node(scratch, pr.external, label=pr.external, note="")
        return table_ref_of(scratch, node)

    nodes, edges = g.to_json()
    return {"summary": _io_summary(g, me, len(unresolved)), "nodes": nodes, "edges": edges,
            "unresolved": unresolved, "details": {"activities": activities},
            "_columnProgram": read_pipeline_columns(content, place)}


# ---------------------------------------------------------------------- everything else
def scan_sql_endpoint(ctx: ScanContext) -> dict:
    it, r, g = ctx.item, ctx.r, Graph()
    me = r.item_node(g, it)
    lh = r.lakehouse_of_sql_endpoint(it["id"], it["workspaceId"])
    if lh:
        g.edge("references", me, r.item_node(g, lh), {"where": "Lakehouse properties", "verb": "SQL endpoint of"})
    nodes, edges = g.to_json()
    return {"summary": f"The SQL analytics endpoint of {lh['displayName']}." if lh else
            "A SQL analytics endpoint; its lakehouse wasn't found.", "nodes": nodes, "edges": edges,
            "unresolved": [], "details": {}}


def scan_generic(ctx: ScanContext) -> dict:
    it, r, g = ctx.item, ctx.r, Graph()
    me = r.item_node(g, it)
    parts = ctx.client.get_definition(f"{FABRIC_API}/workspaces/{it['workspaceId']}/items/{it['id']}/getDefinition")
    files, texts = [], []
    for path, data in parts.items():
        if path == ".platform" or not data:
            continue
        files.append(path)
        text = data.decode("utf-8-sig", "replace")
        texts.append((path, text))
        if path.endswith(".json"):
            try:
                doc = json.loads(text)
            except ValueError:
                doc = {}
            if isinstance(doc, dict) and doc.get("defaultLakehouseArtifactId"):
                lh = r.item(str(doc["defaultLakehouseArtifactId"]))
                if lh:
                    g.edge("default_lakehouse", me, r.item_node(g, lh), {"where": path, "verb": "Default lakehouse"})
                for extra in doc.get("additionalLakehouseIds") or []:
                    x = r.item(str(extra))
                    if x:
                        g.edge("attached", me, r.item_node(g, x), {"where": path, "verb": "Attached lakehouse"})
        if path.endswith(".pq") or "mashup" in path.lower():
            info = parse_m(text)
            if info.get("item_id"):
                src = r.item(info["item_id"], info.get("workspace_id") or None)
                if src:
                    node = r.item_node(g, src)
                    if info.get("entity") and src["type"] == "Lakehouse":
                        node = r.table_node(g, node, src, info.get("schema") or None, info["entity"])
                    g.edge("read", node, me, {"where": path, "verb": "Power Query source"})
    for path, text in texts:                          # anything else the definition points at
        for m in _ONELAKE_IN_TEXT.finditer(text):
            node = r.place_location(g, parse_location(m.group(0)), None)
            if node and node != me and node not in _connected(g, me):
                g.edge("references", me, node, {"where": path, "verb": "OneLake path"})
        for gid in sorted({x.lower() for x in GUID_RE.findall(text)}):
            ref = ctx.ws.by_id.get(gid)
            if ref and ref["id"] != it["id"] and f"item:{gid}" not in _connected(g, me):
                g.edge("references", me, r.item_node(g, ref), {"where": path, "verb": "ID in definition"})
    nodes, edges = g.to_json()
    others = {e["target"] for e in edges if e["source"] == me} | {e["source"] for e in edges if e["target"] == me}
    return {"summary": f"Its definition mentions {_plural(len(others), 'other item')}." if others else
            "Its definition doesn't mention other items.", "nodes": nodes, "edges": edges, "unresolved": [],
            "details": {"parts": files}}


# ---------------------------------------------------------------------- table columns
def _delta_type(t) -> str:
    if isinstance(t, dict):
        kind = t.get("type")
        if kind == "array":
            return f"array<{_delta_type(t.get('elementType'))}>"
        if kind == "map":
            return f"map<{_delta_type(t.get('keyType'))},{_delta_type(t.get('valueType'))}>"
        return str(kind or "struct")
    return str(t or "")


def read_columns(client, ws_id: str, lh_id: str, schema: str, table: str) -> dict:
    """A table's columns in their order.

    First the OneLake table API (GET .../unity-catalog/tables/<item>.<schema>.<table>; non-schema
    lakehouses answer under the fixed schema dbo), then the table's Delta log: the newest commit
    that records the table's schema (metaData.schemaString).
    """
    sch = schema or "dbo"
    base = f"{ONELAKE_TABLES}/{ws_id}/{lh_id}/api/2.1/unity-catalog/tables"
    last = None
    for url in (f"{base}/{quote(f'{lh_id}.{sch}.{table}', safe='')}",
                f"{base}/{quote(table, safe='')}?catalog_name={lh_id}&schema_name={quote(sch, safe='')}"):
        try:
            data = client.get_json(url, STORAGE_SCOPE)
        except ApiError as e:
            last = e
            if e.status in (401, 403):
                break
            continue
        cols = data.get("columns") or []
        if cols:
            order = sorted(range(len(cols)), key=lambda i: (cols[i].get("position") if isinstance(
                cols[i].get("position"), int) else i))
            return {"via": "OneLake table API", "columns": [
                {"name": str(cols[i].get("name") or ""), "type": str(cols[i].get("type_text") or
                                                                       cols[i].get("type_name") or ""),
                 "nullable": bool(cols[i].get("nullable", True))} for i in order]}
    if last is not None and last.status in (401, 403):
        raise last
    folder = f"{lh_id}/Tables/{schema + '/' if schema else ''}{table}/_delta_log"
    try:
        paths = client.onelake_paths(ws_id, folder)
    except ApiError as e:
        raise last if last is not None and e.status == 404 else e
    commits = sorted(str(p.get("name", "")) for p in paths if re.search(r"/\d{20}\.json$", str(p.get("name", ""))))
    for name in reversed(commits[-30:]):
        for line in client.get_text(f"{ONELAKE_DFS}/{ws_id}/{quote(name, safe='/')}").splitlines():
            if '"metaData"' not in line:
                continue
            try:
                meta = json.loads(line).get("metaData") or {}
                fields = json.loads(meta.get("schemaString") or "{}").get("fields") or []
            except ValueError:
                continue
            return {"via": "Delta log", "columns": [{"name": str(f.get("name") or ""), "type": _delta_type(f.get("type")),
                                                     "nullable": bool(f.get("nullable", True))} for f in fields]}
    raise ApiError(0, "The table API didn't return the columns, and the recent Delta log entries don't record the "
                      "table's schema.", "NoSchema")


def explain_columns(e: Exception) -> str:
    if isinstance(e, ApiError):
        if e.status in (401, 403):
            return "Your account can't read this table in OneLake (workspace role or OneLake security)."
        if e.status == 404 or "notfound" in (e.code or "").lower():
            return ("The table wasn't found in its lakehouse or warehouse. The code may create it at run time, or the name in "
                    "the code may differ from the table's.")
        if e.code == "NoSchema":
            return "Open the table in Fabric to see its columns."
    return explain(e)


SCANNERS = {"Notebook": scan_notebook, "Lakehouse": scan_lakehouse, "Warehouse": scan_warehouse, "SemanticModel": scan_semantic_model,
            "Report": scan_report, "DataPipeline": scan_pipeline, "SQLEndpoint": scan_sql_endpoint}


def scannable(item_type: str) -> bool:
    return item_type not in NOT_DEFINABLE


def explain(e: Exception, item_type: str = "") -> str:
    """What an error means for the person using the app, and what to do about it."""
    if not isinstance(e, ApiError):
        return "Something unexpected went wrong while reading this item. Rescan to try again."
    code, status, msg = (e.code or "").lower(), e.status, (e.message or "").lower()
    if code in ("operationnotsupportedforitem", "itemdefinitionnotsupported", "unsupporteditemtype") or \
            "not supported" in msg:
        return "Fabric doesn't provide a readable definition for this kind of item."
    if "encrypted" in msg or "sensitivity" in msg:
        return "This item has an encrypted sensitivity label, which blocks reading its content."
    if status == 401:
        return "The sign-in was rejected. Restart the app; if it keeps happening, run it with --sign-out once."
    if status == 403 or "insufficient" in code or "unauthorized" in code or "forbidden" in code:
        if item_type in ("Notebook", "SemanticModel", "DataPipeline", "Report", "SparkJobDefinition", "Dataflow"):
            return ("Reading this item's content needs write access to it: the Contributor role (or higher) on "
                    "the workspace, or edit permission on the item.")
        return "Your account doesn't have access to this."
    if status == 404 or "notfound" in code:
        return "It wasn't found; it may have been deleted or moved. Reload the workspace."
    if status == 429:
        return "Fabric is limiting the number of requests. Wait a minute, then rescan."
    if code == "networkerror":
        return "Fabric couldn't be reached. Check the network connection or VPN."
    if code == "timeout":
        return "Fabric took too long to prepare the definition. Rescan to try again."
    return ""
