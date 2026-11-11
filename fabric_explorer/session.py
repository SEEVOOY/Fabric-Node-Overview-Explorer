"""Everything the app knows, kept in memory for as long as it runs.

Opening a workspace fetches only the high-level map (one list of items, one list of lakehouses,
the folders). An item's content is fetched when you select it, once: the derived result (nodes,
edges, details) is cached, and only Rescan fetches it again. Two requests for the same item at
the same time share one fetch.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Optional

from .column_lineage import ColumnLineageService
from .fabric_api import FABRIC_API, ApiError
from .links import item_url, parse_link, workspace_url
from .parse.onelake import is_guid
from .scanners import SCANNERS, ScanContext, explain, explain_columns, read_columns, scan_generic, scannable


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _norm(it: dict, ws_id: str) -> dict:
    return {"id": str(it.get("id", "")).lower(), "displayName": str(it.get("displayName") or ""),
            "type": str(it.get("type") or ""), "description": str(it.get("description") or ""),
            "folderId": str(it.get("folderId") or "").lower(), "workspaceId": ws_id}


class WorkspaceData:
    def __init__(self, info: dict, items: list, folders: list, lakehouses: dict, notes: list):
        self.id = str(info.get("id", "")).lower()
        self.name = str(info.get("displayName") or self.id)
        self.info = info
        self.items = items
        self.by_id = {it["id"]: it for it in items}
        self.by_type_name: dict = {}
        for it in items:
            self.by_type_name.setdefault((it["type"], it["displayName"].lower()), it)
        self.folders = folders
        self.lakehouses = lakehouses
        self.notes = notes
        self.loaded_at = _now()


class _Flight:
    def __init__(self):
        self.event = threading.Event()
        self.result = None


class Session:
    def __init__(self, client, portal: str = "https://app.fabric.microsoft.com", tenant_id: Optional[str] = None,
                 default_workspace: str = "", account: str = "", demo: bool = False, concurrency: int = 4,
                 open_groups=("Lakehouse", "Notebook"), start_item: str = ""):
        self.client = client
        self.portal = portal.rstrip("/")
        self.tenant_id = tenant_id if tenant_id and is_guid(tenant_id) else None
        self.default_workspace = default_workspace
        self.start_item = start_item
        self.account = account
        self.demo = demo
        self.concurrency = concurrency
        self.open_groups = list(open_groups)
        self._lock = threading.RLock()
        self._ws: dict = {}
        self._scans: dict = {}
        self._flights: dict = {}
        self._ws_list: Optional[list] = None
        self._ws_names: dict = {}
        self._ext_items: dict = {}
        self._ext_lists: dict = {}
        self._ext_lakes: dict = {}
        self._columns: dict = {}
        self._programs: dict = {}               # what each read notebook does to columns (not sent to the page)
        self.column_lineage = ColumnLineageService(self)

    # ------------------------------------------------------------------ workspaces
    def workspaces(self, refresh: bool = False) -> list:
        with self._lock:
            if self._ws_list is not None and not refresh:
                return self._ws_list
        raw = self.client.get_paged(f"{FABRIC_API}/workspaces")
        out = sorted(({"id": str(w.get("id", "")).lower(), "name": str(w.get("displayName") or ""),
                       "type": str(w.get("type") or "")} for w in raw), key=lambda w: w["name"].lower())
        with self._lock:
            self._ws_list = out
            for w in out:
                self._ws_names[w["id"]] = w["name"]
        return out

    def resolve_link(self, text: str) -> dict:
        """A pasted ID or address -> {workspaceId, itemId, itemType} or {error}."""
        ref = parse_link(text)
        if not ref.ok:
            return {"error": ref.error}
        ws = ref.workspace_id
        if ws == "me":
            mine = next((w for w in self.workspaces() if w["type"].lower() == "personal"), None)
            if mine is None:
                return {"error": "Your My workspace wasn't found in the list of workspaces you can open."}
            ws = mine["id"]
        return {"workspaceId": ws, "itemId": ref.item_id, "itemType": ref.item_type, "tenantId": ref.tenant_id}

    def ws_name(self, ws_id: str) -> str:
        with self._lock:
            if ws_id in self._ws_names:
                return self._ws_names[ws_id]
            if ws_id in self._ws:
                return self._ws[ws_id].name
        try:
            name = str(self.client.get_json(f"{FABRIC_API}/workspaces/{ws_id}").get("displayName") or "")
        except ApiError:
            name = ""
        with self._lock:
            self._ws_names[ws_id] = name or f"workspace {ws_id[:8]}"
            return self._ws_names[ws_id]

    def ws_id_by_name(self, name: str) -> Optional[str]:
        try:
            for w in self.workspaces():
                if w["name"].lower() == name.lower():
                    return w["id"]
        except ApiError:
            pass
        return None

    def ext_item(self, ws_id: str, item_id: str) -> Optional[dict]:
        key = (ws_id, item_id)
        with self._lock:
            if key in self._ext_items:
                return self._ext_items[key]
            if ws_id in self._ws:
                return self._ws[ws_id].by_id.get(item_id)
        try:
            it = _norm(self.client.get_json(f"{FABRIC_API}/workspaces/{ws_id}/items/{item_id}"), ws_id)
        except ApiError:
            it = None
        with self._lock:
            self._ext_items[key] = it
        return it

    def items_of(self, ws_id: str) -> list:
        with self._lock:
            if ws_id in self._ws:
                return self._ws[ws_id].items
            if ws_id in self._ext_lists:
                return self._ext_lists[ws_id]
        try:
            items = [_norm(i, ws_id) for i in self.client.get_paged(f"{FABRIC_API}/workspaces/{ws_id}/items")]
        except ApiError:
            items = []
        with self._lock:
            self._ext_lists[ws_id] = items
        return items

    def ext_lakehouses(self, ws_id: str) -> dict:
        with self._lock:
            if ws_id in self._ws:
                return self._ws[ws_id].lakehouses
            if ws_id in self._ext_lakes:
                return self._ext_lakes[ws_id]
        try:
            lakes = {str(x.get("id", "")).lower(): x.get("properties") or {}
                     for x in self.client.get_paged(f"{FABRIC_API}/workspaces/{ws_id}/lakehouses")}
        except ApiError:
            lakes = {}
        with self._lock:
            self._ext_lakes[ws_id] = lakes
        return lakes

    def workspace_data(self, ws_id: str, refresh: bool = False) -> WorkspaceData:
        ws_id = ws_id.lower()
        with self._lock:
            if ws_id in self._ws and not refresh:
                return self._ws[ws_id]
        info = self.client.get_json(f"{FABRIC_API}/workspaces/{ws_id}")
        items = [_norm(i, ws_id) for i in self.client.get_paged(f"{FABRIC_API}/workspaces/{ws_id}/items")]
        notes = []
        try:
            lakes = {str(x.get("id", "")).lower(): x.get("properties") or {}
                     for x in self.client.get_paged(f"{FABRIC_API}/workspaces/{ws_id}/lakehouses")}
        except ApiError as e:
            lakes = {}
            notes.append(f"Lakehouse details weren't available: {e.message[:120]}")
        try:
            folders = [{"id": str(f.get("id", "")).lower(), "name": str(f.get("displayName") or ""),
                        "parentId": str(f.get("parentFolderId") or "").lower()}
                       for f in self.client.get_paged(f"{FABRIC_API}/workspaces/{ws_id}/folders")]
        except ApiError:
            folders = []
        data = WorkspaceData(info, items, folders, lakes, notes)
        with self._lock:
            self._ws[ws_id] = data
            self._ws_names[ws_id] = data.name
        return data

    def open_workspace(self, ws_id: str, refresh: bool = False) -> dict:
        """The high-level map of a workspace (no item content), plus any scans already cached."""
        w = self.workspace_data(ws_id, refresh)
        with self._lock:
            scans = [s for (ws, _), s in self._scans.items() if ws == w.id]
        return {
            "workspace": {"id": w.id, "name": w.name, "type": w.info.get("type", ""),
                          "url": workspace_url(self.portal, w.id, self.tenant_id)},
            "items": [{"id": it["id"], "name": it["displayName"], "type": it["type"],
                       "description": it["description"], "folderId": it["folderId"],
                       "scannable": scannable(it["type"]),
                       "url": item_url(self.portal, w.id, it["type"], it["id"], self.tenant_id)}
                      for it in w.items],
            "folders": w.folders,
            "lakehouses": {k: {"schemas": bool(v.get("defaultSchema")),
                               "sqlEndpointId": str((v.get("sqlEndpointProperties") or {}).get("id", "")).lower()}
                           for k, v in w.lakehouses.items()},
            "scans": scans,
            "notes": w.notes,
            "loadedAt": w.loaded_at,
        }

    # ------------------------------------------------------------------ scans
    def unload(self, ws_id: str, item_ids) -> int:
        """Forget what was read for these items: they are 'not read' again, also after a page reload,
        and selecting one reads it fresh from Fabric. Returns how many were forgotten."""
        n = 0
        with self._lock:
            for item_id in item_ids or []:
                key = (ws_id.lower(), str(item_id).lower())
                self._programs.pop(key, None)
                if self._scans.pop(key, None) is not None:
                    n += 1
        return n

    def cached_scan(self, ws_id: str, item_id: str) -> Optional[dict]:
        with self._lock:
            return self._scans.get((ws_id.lower(), item_id.lower()))

    def scan(self, ws_id: str, item_id: str, refresh: bool = False, _chain: tuple = ()) -> dict:
        """The derived content of one item; fetched once, then served from memory until refresh."""
        key = (ws_id.lower(), item_id.lower())
        with self._lock:
            if not refresh and key in self._scans:
                return self._scans[key]
            flight = self._flights.get(key)
            owner = flight is None
            if owner:
                flight = self._flights[key] = _Flight()
        if not owner:
            flight.event.wait(timeout=600)
            return flight.result or self._scans.get(key) or {"itemId": key[1], "status": "error",
                                                             "error": "The scan didn't finish."}
        try:
            result = self._run_scan(key[0], key[1], _chain + (key[1],))
            program = result.pop("_columnProgram", None)
            with self._lock:
                self._scans[key] = result
                if program is not None:
                    self._programs[key] = program
                else:
                    self._programs.pop(key, None)
            flight.result = result
            return result
        finally:
            with self._lock:
                self._flights.pop(key, None)
            flight.event.set()

    def _run_scan(self, ws_id: str, item_id: str, chain: tuple) -> dict:
        t0 = time.perf_counter()
        w = self.workspace_data(ws_id)
        it = w.by_id.get(item_id)
        base = {"workspaceId": ws_id, "itemId": item_id, "scannedAt": _now(), "nodes": [], "edges": [],
                "unresolved": [], "details": {}, "summary": "", "error": "", "hint": ""}
        if it is None:
            return {**base, "status": "error", "type": "", "name": item_id,
                    "error": "This item isn't in the workspace any more.", "hint": "Reload the workspace."}
        base.update(type=it["type"], name=it["displayName"])
        if not scannable(it["type"]):
            return {**base, "status": "unsupported", "summary": "Fabric doesn't provide a definition for this kind "
                                                                  "of item, so there's nothing more to read."}
        side: list = []

        def exports_of(nb: dict) -> Optional[dict]:
            """Constants of a notebook pulled in with %run: cached, being read right now, or read here."""
            key = (ws_id, nb["id"])
            if nb["id"] in chain or len(chain) >= 4:
                return None
            with self._lock:
                cached, flight = self._scans.get(key), self._flights.get(key)
            if cached is None and flight is not None:
                flight.event.wait(timeout=30)      # read by another request right now; a %run loop just times out
                cached = flight.result or self._scans.get(key)
            elif cached is None:
                cached = self.scan(ws_id, nb["id"], _chain=chain)
                side.append(cached)
            return (cached or {}).get("details", {}).get("exports") if cached else None

        def helpers_of(nb: dict) -> dict:
            """Functions of a notebook pulled in with %run (it is read, as for its constants)."""
            exports_of(nb)
            with self._lock:
                prog = self._programs.get((ws_id, nb["id"]))
            return dict(getattr(prog, "helpers", {}) or {})

        ctx = ScanContext(self, w, it, exports_of, helpers_of)
        try:
            out = SCANNERS.get(it["type"], scan_generic)(ctx)
            result = {**base, **out, "status": "ok"}
        except ApiError as e:
            hint = explain(e, it["type"])
            unsupported = hint.startswith("Fabric doesn't provide")
            result = {**base, "status": "unsupported" if unsupported else "error",
                      "summary": hint if unsupported else "",
                      "error": "" if unsupported else (e.message or str(e))[:400],
                      "hint": "" if unsupported else hint}
        except Exception as e:  # noqa: BLE001 - a parser problem shouldn't take the app down
            result = {**base, "status": "error", "error": f"{type(e).__name__}: {e}"[:400],
                      "hint": explain(e, it["type"])}
        result["ms"] = round((time.perf_counter() - t0) * 1000)
        if side:
            result = {**result, "alsoScanned": [s["itemId"] for s in side if s]}
        return result

    def program(self, ws_id: str, item_id: str):
        """What a read notebook does to columns (column_lineage.Program), or None."""
        with self._lock:
            return self._programs.get((ws_id.lower(), item_id.lower()))

    def programs(self, ws_id: str) -> list:
        """[(item, program)] for every notebook of the workspace that has been read."""
        w = self.workspace_data(ws_id)
        with self._lock:
            return [(w.by_id[i], p) for (ws, i), p in self._programs.items() if ws == w.id and i in w.by_id]

    def columns(self, ws_id: str, lakehouse_id: str, schema: str, table: str, refresh: bool = False) -> dict:
        """A table's column names (read once, then from memory until refresh)."""
        if not (ws_id and lakehouse_id and table):
            raise ValueError("Missing workspace, lakehouse or table.")
        key = (ws_id.lower(), lakehouse_id.lower(), (schema or "").lower(), table.lower())
        with self._lock:
            if not refresh and key in self._columns:
                return self._columns[key]
        t0 = time.perf_counter()
        base = {"table": table, "schema": schema or "", "lakehouseId": lakehouse_id, "readAt": _now()}
        try:
            res = {**base, **read_columns(self.client, ws_id, lakehouse_id, schema or "", table), "error": "", "hint": ""}
        except ApiError as e:
            res = {**base, "columns": [], "via": "", "error": (e.message or str(e))[:300], "hint": explain_columns(e)}
        res["ms"] = round((time.perf_counter() - t0) * 1000)
        with self._lock:
            self._columns[key] = res
        return res

    def hello(self) -> dict:
        from . import __version__
        return {"version": __version__, "account": self.account or getattr(self.client.signin, "account",
                                                                            lambda: "")(),
                "workspaceId": self.default_workspace, "startItem": self.start_item, "portal": self.portal,
                "demo": self.demo, "concurrency": self.concurrency, "openGroups": self.open_groups}
