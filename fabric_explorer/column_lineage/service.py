"""The page's column lineage requests, answered from what the session has read.

Nothing extra is fetched to work out what an item does: its program is made when it is read (the
scanners) and kept by the session. Notebooks and pipelines are stages (they read datasets and write
others); semantic models and reports are where Power BI picks columns up. What a view adds is the
datasets' columns, read once from OneLake through Session.columns (the same cache as the Table
content tab), a few at a time. The datasets view doesn't need them for the columns that change, so
it only reads them when all columns are asked for.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from .catalog import CATEGORIES, ROW, catalog_json
from .model import Program
from .resolve import STAR, resolve
from .views import BiLineage, NotebookLineage, datasets_view, notebook_view

STAGE_TYPES = ("Notebook", "DataPipeline")
LINEAGE_TYPES = ("Notebook", "DataPipeline", "SemanticModel", "Report")
SCHEMA_URL = "https://openlineage.io/spec/facets/1-2-0/ColumnLineageDatasetFacet.json"
PRODUCER = "https://github.com/fabric-explorer"


class ColumnLineageService:
    def __init__(self, session):
        self.s = session

    def catalog(self) -> dict:
        return catalog_json()

    # ------------------------------------------------------------------ helpers
    def _item(self, ws_id: str, item: dict) -> dict:
        from ..links import item_url
        return {"id": f"item:{item['id']}", "itemId": item["id"], "name": item["displayName"], "type": item["type"],
                "url": item_url(self.s.portal, ws_id, item["type"], item["id"], self.s.tenant_id)}

    def _schemas(self, refs, refresh: bool = False) -> dict:
        """table id -> (columns [(name, type)] or None, {via, error, hint})"""
        out, todo = {}, []
        for ref in refs:
            if ref.id in out:
                continue
            if not ref.readable:
                why = "It is a folder of files." if ref.kind == "file" else \
                    "It is outside Fabric." if ref.kind == "external" else \
                    "Its lakehouse or warehouse wasn't found." if ref.kind in ("table", "missing") else ""
                out[ref.id] = (None, {"via": "", "error": "", "hint": why})
                continue
            out[ref.id] = None
            todo.append(ref)

        def read(ref):
            try:
                res = self.s.columns(ref.workspace_id, ref.item_id, ref.schema, ref.table, refresh=refresh)
            except Exception as e:  # noqa: BLE001 - one table's columns failing doesn't stop the view
                return ref.id, (None, {"via": "", "error": str(e)[:200], "hint": ""})
            cols = [(c["name"], c.get("type", "")) for c in res.get("columns") or []]
            return ref.id, (cols or None, {"via": res.get("via", ""), "error": res.get("error", ""),
                                           "hint": res.get("hint", "")})

        if todo:
            with ThreadPoolExecutor(max_workers=max(1, min(self.s.concurrency, 8))) as pool:
                for tid, val in pool.map(read, todo):
                    out[tid] = val
        return out

    def _read(self, ws_id: str) -> tuple[list, BiLineage]:
        """(stages [(item, Program)], Power BI) of everything read in the workspace."""
        stages, bi = [], BiLineage()
        for item, prog in self.s.programs(ws_id):
            kind = getattr(prog, "kind", "")
            if isinstance(prog, Program) and item["type"] in STAGE_TYPES:
                stages.append((item, prog))
            elif kind == "model":
                bi.models.append((self._item(ws_id, item), prog))
            elif kind == "report":
                bi.reports.append((self._item(ws_id, item), prog))
        return stages, bi

    def _lineages(self, ws_id: str, programs, schemas: Optional[dict]) -> list:
        out = []
        for item, prog in programs:
            def columns_of(ref, _s=schemas):
                return (_s.get(ref.id) or (None, {}))[0] if _s is not None else None
            info = {tid: (schemas.get(tid) or (None, {}))[1] for tid in prog.tables} if schemas is not None else {}
            out.append(NotebookLineage(self._item(ws_id, item), resolve(prog, columns_of), info))
        return out

    def _unread(self, ws_id: str) -> int:
        w = self.s.workspace_data(ws_id)
        return sum(1 for it in w.items if it["type"] in LINEAGE_TYPES and self.s.cached_scan(ws_id, it["id"]) is None)

    @staticmethod
    def _unfollowed(scan: dict, prog, me: str) -> tuple:
        """(inputs, outputs) the map links to the notebook that its column program doesn't have."""
        from ..scanners import table_ref_of
        from ..graph import Graph
        g = Graph()
        for n in scan.get("nodes") or []:
            g.nodes[n["id"]] = n
        ins, outs = [], []
        for e in scan.get("edges") or []:
            if e["kind"] == "read" and e["target"] == me:
                tid, bucket = e["source"], ins
            elif e["kind"] == "write" and e["source"] == me:
                tid, bucket = e["target"], outs
            else:
                continue
            if tid in prog.tables or any(r.id == tid for r in bucket):
                continue
            ref = table_ref_of(g, tid)
            if ref is not None and ref.kind in ("table", "file"):
                bucket.append(ref)
        return ins, outs

    @staticmethod
    def _connected(focus_prog, programs) -> list:
        """The programs that share datasets with the focus, directly or through others."""
        tables, picked, rest, changed = set(focus_prog.tables), [], list(programs), True
        while changed:
            changed = False
            for pair in list(rest):
                if tables & set(pair[1].tables):
                    tables |= set(pair[1].tables)
                    picked.append(pair)
                    rest.remove(pair)
                    changed = True
        return picked

    # ------------------------------------------------------------------ the two views
    def notebook(self, ws_id: str, notebook_id: str, scope: str = "direct", refresh: bool = False) -> dict:
        ws_id, notebook_id = ws_id.lower(), notebook_id.lower()
        w = self.s.workspace_data(ws_id)
        it = w.by_id.get(notebook_id)
        if it is None or it["type"] not in STAGE_TYPES:
            raise ValueError("That isn't a notebook or pipeline in this workspace.")
        scan = self.s.scan(ws_id, notebook_id)
        base = {"mode": "notebook", "scope": scope, "focus": self._item(ws_id, it), "datasets": [], "ops": [],
                "cards": [], "edges": [], "flows": [], "notebooks": [], "notes": [], "unread": 0}
        if scan.get("status") != "ok":
            return {**base, "error": scan.get("error") or scan.get("summary") or "It couldn't be read.",
                    "hint": scan.get("hint", "")}
        prog = self.s.program(ws_id, notebook_id)
        if prog is None:
            return {**base, "error": "Its columns couldn't be worked out.", "hint": "Refresh it and try again."}
        unfollowed = self._unfollowed(scan, prog, f"item:{notebook_id}")
        stages, bi = self._read(ws_id) if scope == "full" else ([], None)
        others = self._connected(prog, [(i, p) for i, p in stages if i["id"] != notebook_id]) if stages else []
        refs = list(prog.tables.values()) + [r for _, p in others for r in p.tables.values()] + \
            [r for side in unfollowed for r in side]
        schemas = self._schemas(refs, refresh)
        lineages = self._lineages(ws_id, [(it, prog)] + others, schemas)
        side = lambda refs_: [(r, (schemas.get(r.id) or (None, {}))[0], (schemas.get(r.id) or (None, {}))[1]) for r in refs_]
        view = notebook_view(lineages[0], lineages[1:], scope, bi, (side(unfollowed[0]), side(unfollowed[1])))
        if prog.error:
            view["notes"] = [prog.error] + view["notes"]
        return {**base, **view, "unread": self._unread(ws_id) if scope == "full" else 0}

    def datasets(self, ws_id: str, focus: str = "", scope: str = "full", direction: str = "both",
                 all_columns: bool = False, refresh: bool = False) -> dict:
        ws_id = ws_id.lower()
        kind = ""
        if focus.startswith("item:"):              # a semantic model or report: read it (and a report's model)
            iid = focus[5:].lower()
            it = self.s.workspace_data(ws_id).by_id.get(iid)
            if it is None or it["type"] not in ("SemanticModel", "Report"):
                raise ValueError("That isn't a semantic model or report in this workspace.")
            kind = it["type"]
            self.s.scan(ws_id, iid)
            if kind == "SemanticModel":            # which reports are built on it: read the reports not read yet
                todo = [r["id"] for r in self.s.workspace_data(ws_id).items
                        if r["type"] == "Report" and self.s.cached_scan(ws_id, r["id"]) is None][:40]
                if todo:
                    with ThreadPoolExecutor(max_workers=max(1, min(self.s.concurrency, 8))) as pool:
                        list(pool.map(lambda rid: self.s.scan(ws_id, rid), todo))
            prog = self.s.program(ws_id, iid)
            if getattr(prog, "kind", "") == "report" and prog.model and \
                    self.s.workspace_data(ws_id).by_id.get(prog.model.lower()):
                self.s.scan(ws_id, prog.model.lower())
        stages, bi = self._read(ws_id)            # the columns, so every column can be followed (read once)
        lineages = self._lineages(ws_id, stages, self._schemas([r for _, p in stages for r in p.tables.values()],
                                                               refresh))
        view = datasets_view(lineages, focus=focus, scope=scope, direction=direction, all_columns=all_columns,
                             bi=bi if direction != "in" else None)
        return {"mode": "datasets", "focus": focus, "focusKind": kind or ("table" if focus else ""), "scope": scope,
                "direction": "in" if kind else direction, "allColumns": all_columns,
                "read": len(stages), "unread": self._unread(ws_id), **view}

    # ------------------------------------------------------------------ export
    def export(self, ws_id: str, refresh: bool = False) -> dict:
        """Every column path of every notebook and pipeline read, into Power BI: rows for a table, and
        the same as OpenLineage column lineage facets (one job per notebook or pipeline)."""
        ws_id = ws_id.lower()
        w = self.s.workspace_data(ws_id)
        stages, bi = self._read(ws_id)
        lineages = self._lineages(ws_id, stages, self._schemas([r for _, p in stages for r in p.tables.values()],
                                                               refresh))
        ns = f"fabric://{ws_id}"
        rows, jobs = [], []
        for nb in lineages:
            outputs = {}
            for wr in nb.res.writes:
                target = wr.sink.table
                facet = outputs.setdefault(target.id, {"namespace": ns, "name": target.full_name or target.label,
                                                       "facets": {"columnLineage": {"_producer": PRODUCER,
                                                                                    "_schemaURL": SCHEMA_URL,
                                                                                    "fields": {}}}})
                for c in wr.columns:
                    if c.star:
                        continue
                    inputs = facet["facets"]["columnLineage"]["fields"].setdefault(c.name, {"inputFields": []})
                    for p in c.paths or [()]:
                        ops = [nb.res.program.ops[n[1]] for n in p if n[0] == "o" and n[1] in nb.res.program.ops]
                        src = nb.res.program.tables.get(p[0][1]) if p and p[0][0] == "c" else None
                        src_col = next((s.name for s in nb.res.sources.get(src.id, []) if s.name.lower() == p[0][2]),
                                       p[0][2]) if src else ""
                        rows.append(self._row(nb, wr, c, src, src_col, ops))
                        if src is not None and src_col != STAR:
                            inputs["inputFields"].append({"namespace": ns, "name": src.full_name or src.label,
                                                          "field": src_col, "transformations": _transformations(ops)})
            jobs.append({"job": {"namespace": ns, "name": nb.item["name"], "type": nb.item["type"], "url": nb.item["url"]},
                         "inputs": [{"namespace": ns, "name": r.full_name or r.label} for r in nb.res.program.tables.values()
                                    if r.id in nb.res.sources],
                         "outputs": list(outputs.values())})
        rows += self._bi_rows(bi)
        return {"workspace": {"id": ws_id, "name": w.name}, "generated": datetime.now(timezone.utc).isoformat(),
                "columns": ["target", "target_column", "status", "source", "source_column", "via", "via_type",
                            "column_definitions", "row_definitions", "where", "write", "via_url"],
                "rows": rows, "openlineage": jobs, "categories": catalog_json()["categories"]}

    @staticmethod
    def _row(nb, wr, c, src, src_col, ops) -> dict:
        def defs(scope_row):
            return " ▸ ".join(f"{''.join(CATEGORIES[x].letter for x in o.categories[:3])} {o.label}"
                              for o in ops if (o.scope == ROW) == scope_row)
        return {"target": wr.sink.table.full_name or wr.sink.table.label, "target_column": c.name, "status": c.status,
                "source": (src.full_name or src.label) if src else "", "source_column": src_col if src else "",
                "via": nb.item["name"], "via_type": nb.item["type"], "column_definitions": defs(False),
                "row_definitions": defs(True), "where": "; ".join(dict.fromkeys(o.to_json()["where"] for o in ops)),
                "write": f"{wr.sink.verb} ({wr.sink.mode})" if wr.sink.mode else wr.sink.verb, "via_url": nb.item["url"]}

    @staticmethod
    def _bi_rows(bi: BiLineage) -> list:
        rows = []
        for mitem, mp in bi.models:
            for f in mp.fields:
                ref = mp.tables.get(f.source[0]) if f.source else None
                base = {"target": mitem["name"], "target_column": f.label, "status": f.kind, "via": mitem["name"],
                        "via_type": "SemanticModel", "row_definitions": "", "write": "", "via_url": mitem["url"]}
                if f.kind == "column" and ref is not None:
                    rows.append({**base, "source": ref.full_name or ref.label, "source_column": f.source_name,
                                 "column_definitions": "", "where": f"Column of the {f.table} table"})
                elif f.kind != "column":
                    op = f.op_json(mitem["itemId"])
                    for r in f.refs or [("", "")]:
                        used = mp.find(f.table, r)
                        rows.append({**base, "source": mitem["name"] if used else "",
                                     "source_column": used.label if used else "",
                                     "column_definitions": f"{op['letters']} {f.label} = {' '.join(f.expression.split())}",
                                     "where": op["where"]})
            for ritem, rp in bi.reports_of(mitem["itemId"]):
                for v in rp.visuals:
                    for t, n in v.fields:
                        used = mp.find(t, (t, n))
                        rows.append({"target": ritem["name"], "target_column": v.label, "status": "visual",
                                     "source": mitem["name"], "source_column": used.label if used else f"{t}[{n}]",
                                     "via": ritem["name"], "via_type": "Report", "column_definitions": v.type,
                                     "row_definitions": "", "where": f"Page {v.page}", "write": "",
                                     "via_url": ritem["url"]})
        return rows


def _transformations(ops) -> list:
    """OpenLineage transformations of a path: DIRECT for what makes the value, INDIRECT for what picks rows."""
    out = []
    cols = [o for o in ops if o.scope != ROW]
    if not cols:
        out.append({"type": "DIRECT", "subtype": "IDENTITY", "description": "", "masking": False})
    for o in cols:
        sub = "AGGREGATION" if o.category == "aggregate" else "IDENTITY" if o.category == "rename" else "TRANSFORMATION"
        out.append({"type": "DIRECT", "subtype": sub, "description": o.label, "masking": o.category == "hash"})
    for o in ops:
        sub = {"filter": "FILTER", "join": "JOIN", "group": "GROUP_BY", "merge": "JOIN"}.get(o.category)
        if o.scope == ROW and sub:
            out.append({"type": "INDIRECT", "subtype": sub, "description": o.label, "masking": False})
    return out
