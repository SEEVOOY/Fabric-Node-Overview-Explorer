"""Where a semantic model's tables come from (the same reading as pims-explorer's table sources).

The model definition (TMSL, model.bim) is fetched with getDefinition?format=TMSL. For every table,
its first partition says how it is loaded:
    entity partitions (Direct Lake)  entityName / schemaName + a shared expression whose M text
                                     holds the OneLake URL (workspace + lakehouse IDs) or the SQL
                                     endpoint (Sql.Database(server, <SQL endpoint ID>))
    M partitions (Import / DirectQuery) Lakehouse.Contents navigation (workspaceId, lakehouseId, Id)
                                     or Sql.Database(server, database) + Schema / Item
"""
from __future__ import annotations

import re

from .onelake import is_guid

_MODES = {"directlake": "Direct Lake", "import": "Import", "directquery": "DirectQuery", "dual": "Dual",
          "push": "Push", "default": "Default"}
_ONELAKE_URL = re.compile(r"https?://[^\"'\s/]*onelake[^\"'\s/]*\.fabric\.microsoft\.com/"
                          r"(?P<ws>[^/\"'\s]+)/(?P<item>[^/\"'\s]+)", re.I)
_SQL_DB = re.compile(r"Sql\.Databases?\s*\(\s*\"(?P<server>[^\"]+)\"(?:\s*,\s*\"(?P<db>[^\"]+)\")?", re.I)
_NAV = re.compile(r"\b(?P<key>workspaceId|lakehouseId|warehouseId|Id|Schema|Item|ItemKind)\s*=\s*\"(?P<val>[^\"]*)\"")


def text_of(expr) -> str:
    """TMSL stores expressions either as one string or as a list of lines."""
    return "\n".join(str(x) for x in expr) if isinstance(expr, list) else str(expr or "")


def mode_name(value, default: str = "import") -> str:
    key = str(value or default).replace(" ", "").lower()
    if key == "default":
        key = str(default).replace(" ", "").lower()
    return _MODES.get(key, str(value or default))


def parse_m(text: str) -> dict:
    """{connector, workspace_id, item_id, server, database, entity, schema} found in M text."""
    info: dict = {}
    if not text:
        return info
    m = _ONELAKE_URL.search(text)
    if m:
        info.update(connector="onelake", workspace_id=m.group("ws").lower(), item_id=m.group("item").lower())
    m = _SQL_DB.search(text)
    if m:
        info.update(connector="sql", server=m.group("server"), database=m.group("db") or "")
        if is_guid(info["database"]):
            info["item_id"] = info["database"].lower()
    nav: dict = {}
    for n in _NAV.finditer(text):
        nav.setdefault(n.group("key").lower(), n.group("val"))
    if re.search(r"\bLakehouse\.Contents\b", text):
        info["connector"] = "lakehouse"
    if re.search(r"\bFabric\.Warehouse\b|\bWarehouse\.Contents\b", text):
        info["connector"] = "warehouse"
    if nav.get("workspaceid"):
        info["workspace_id"] = nav["workspaceid"].lower()
    for key in ("lakehouseid", "warehouseid"):
        if nav.get(key):
            info["item_id"] = nav[key].lower()
    if nav.get("id") and (nav.get("itemkind", "").lower() in ("table", "") or info.get("connector") == "lakehouse"):
        info["entity"] = nav["id"]
    if nav.get("item"):
        info["entity"] = nav["item"]
    if nav.get("schema"):
        info["schema"] = nav["schema"]
    return info


def model_columns(table: dict) -> list:
    """A model table's columns as the model has them: its name, the source column it reads (empty for
    calculated columns), the data type, and whether it is hidden. Internal row-number columns are left out."""
    out = []
    for c in table.get("columns") or []:
        kind = str(c.get("type") or "data").lower()
        if kind == "rownumber" or not c.get("name"):
            continue
        calculated = kind in ("calculated", "calculatedtablecolumn")
        out.append({"name": str(c["name"]), "source": "" if calculated else str(c.get("sourceColumn") or ""),
                    "dataType": str(c.get("dataType") or ""), "hidden": bool(c.get("isHidden")), "calculated": calculated})
    return out


def model_tables(bim: dict) -> list:
    """One record per model table: name, storage mode, kind, and the source found for it."""
    model = bim.get("model", bim)
    default_mode = model.get("defaultMode", "import")
    exprs = {str(e.get("name")): text_of(e.get("expression")) for e in model.get("expressions", []) if e.get("name")}
    out = []
    for t in model.get("tables", []):
        name = str(t.get("name") or "")
        parts = t.get("partitions") or []
        if not name or name.startswith(("LocalDateTable_", "DateTableTemplate_")):
            continue
        if not parts:
            out.append({"table": name, "mode": "", "kind": "No partitions", "source": {}, "entity": "",
                        "schema": "", "expression": "", "columns": model_columns(t)})
            continue
        p = parts[0]
        src = p.get("source") or {}
        typ = str(src.get("type") or "").lower()
        rec = {"table": name, "mode": mode_name(p.get("mode"), default_mode), "kind": "", "source": {},
               "entity": "", "schema": "", "expression": "", "columns": model_columns(t)}
        if typ == "entity":
            rec["expression"] = str(src.get("expressionSource") or "")
            rec["entity"] = str(src.get("entityName") or "")
            rec["schema"] = str(src.get("schemaName") or "")
            rec["source"] = parse_m(exprs.get(rec["expression"], ""))
            rec["kind"] = "Direct Lake" if rec["mode"] == "Direct Lake" else f"{rec['mode']} (entity)"
        elif typ == "m":
            text = text_of(src.get("expression"))
            for ename, body in exprs.items():          # may start from a shared query
                if re.search(rf'(?<![\w#"]){re.escape(ename)}(?![\w"])|#"{re.escape(ename)}"', text):
                    text = body + "\n" + text
                    rec["expression"] = ename
                    break
            rec["source"] = parse_m(text)
            rec["entity"] = rec["source"].get("entity", "")
            rec["schema"] = rec["source"].get("schema", "")
            rec["kind"] = rec["mode"]
        elif typ == "calculated":
            rec["kind"] = "Calculated table"
        elif typ == "calculationgroup":
            rec["kind"] = "Calculation group"
        else:
            rec["kind"] = typ or "Unknown"
        out.append(rec)
    return out
