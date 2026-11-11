"""The two column lineage views, as graphs for the page.

Notebook view: the datasets a notebook (or a pipeline) reads and writes, with all their columns, and
between them its definitions. Every column has its own line. A row operation (filter, join, group
by, merge, a pipeline's copy) is a shared node with one lane per column passing through it. A column
definition has a lane per column too: the page draws one used by many columns (a loop, a transform
of every column) as a block they pass straight through, or gathers them into one point. Full lineage adds the notebooks
and pipelines before and after it, as far as their columns connect, and the Power BI semantic models
that read its datasets: their columns, the measures built on them, and the report visuals that show
them.

Datasets view: datasets and the notebooks and pipelines between them (their columns read, so every
column can be followed through copies). Black lines: one reads a
dataset and writes another. Green lines: only the columns that change (new, altered, renamed), from
the columns they come from, through the definition, to where they are written. Thin lines carry a
changed column on through copies, and yellow lines take it into Power BI: which reports it reaches.

Ports (where lines attach) are "<node>|<column>" for dataset columns, "<op>|<lane>" for the lanes of
a row operation, "<op>|cond" where a row operation looks at a column, "<node>|" for a header, a
card's row id for its rows, and the op id itself for a definition. A flow is one column's path, as a
list of edges, so the page can follow one column without lighting up everything sharing a node.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from .catalog import CATEGORIES, ROW
from .resolve import STAR, Resolution

CHANGED = ("new", "altered", "renamed")
MAX_DEPTH = 8
_RANKS = {"": 0, "kept": 1, "unknown": 2, "bi": 2, "carried": 3, "feeds": 3, "unchanged": 3, "renamed": 4,
          "altered": 5, "new": 6}


def _rank(s: str) -> int:
    return _RANKS.get(s, 0)


@dataclass
class NotebookLineage:
    """A notebook's or a pipeline's resolution, and who it is."""
    item: dict                                         # id (item:<id>), itemId, name, url, type
    res: Resolution
    schemas: dict = field(default_factory=dict)        # table id -> {via, error, hint}

    @property
    def id(self) -> str:
        return self.item["id"]

    def data_flows(self):
        """(path, written table id, written column) for every column a write makes."""
        for w in self.res.writes:
            for c in w.columns:
                for p in c.paths:
                    yield p, w.sink.table.id, c.name


@dataclass
class BiLineage:
    """The semantic models and reports that have been read: [(item, ModelProgram)], [(item, ReportProgram)]."""
    models: list = field(default_factory=list)
    reports: list = field(default_factory=list)

    def reports_of(self, model_item_id: str) -> list:
        return [(i, r) for i, r in self.reports if r.model and r.model.lower() == model_item_id.lower()]


def _dataset(ref, inst: str, split: str, schema: dict) -> dict:
    return {"id": inst, "table": ref.id, "label": ref.label, "fullName": ref.full_name or ref.label,
            "kind": ref.kind, "lakehouse": ref.lakehouse, "lakehouseName": ref.lakehouse_name, "split": split,
            "role": "", "columns": [], "schema": schema or {}, "writes": [], "hidden": 0}


class _Graph:
    def __init__(self):
        self.datasets: dict = {}
        self.cols: dict = defaultdict(dict)            # dataset id -> {lower: column}
        self.ops: dict = {}
        self.lanes: dict = defaultdict(dict)           # op id -> {prefix: port}
        self.cards: dict = {}                          # notebook / pipeline / model / report cards
        self.rows: dict = defaultdict(dict)            # card id -> {row id: row}
        self.edges: dict = {}
        self.flows: list = []

    # ------------------------------------------------------------------ things
    def dataset(self, ref, inst: str, split: str = "", schema=None) -> dict:
        d = self.datasets.get(inst)
        if d is None:
            d = self.datasets[inst] = _dataset(ref, inst, split, schema or {})
        return d

    def column(self, inst: str, name: str, **extra) -> str:
        low = name.lower()
        port = f"{inst}|{low}"
        col = self.cols[inst].get(low)
        if col is None:
            self.cols[inst][low] = {"id": port, "name": name, "type": "", "status": "", "inferred": False,
                                    "star": name == STAR, "inTable": True, "used": False, "changedBy": [], **extra}
        else:
            for k, v in extra.items():
                if k == "status":
                    if _rank(v) > _rank(col.get("status", "")):
                        col["status"] = v
                elif v not in (None, "", False) and not col.get(k):
                    col[k] = v
        return port

    def op(self, nb: str, op) -> str:
        oid = f"op:{nb}:{op.key}"
        if oid not in self.ops:
            j = op.to_json()
            j.update(id=oid, notebook=nb, lanes=[])
            self.ops[oid] = j
        return oid

    def raw_op(self, j: dict) -> str:
        self.ops.setdefault(j["id"], j)
        return j["id"]

    def card(self, cid: str, j: dict) -> dict:
        if cid not in self.cards:
            self.cards[cid] = {**j, "rows": [], "rowOps": list(j.get("rowOps", []))}
        return self.cards[cid]

    def row(self, cid: str, rid: str, j: dict) -> str:
        if rid not in self.rows[cid]:
            self.rows[cid][rid] = {"id": rid, **j}
        return rid

    # ------------------------------------------------------------------ lines
    def edge(self, a: str, an: str, b: str, bn: str, kind: str, flow) -> int:
        e = self.edges.get((a, b))
        if e is None:
            e = self.edges[(a, b)] = {"i": len(self.edges), "from": a, "to": b, "fromNode": an, "toNode": bn,
                                      "kind": kind, "flows": []}
        elif kind == "data" and e["kind"] == "condition":
            e["kind"] = "data"
        if flow is not None and flow not in e["flows"]:
            e["flows"].append(flow)
        return e["i"]

    def flow(self, ports: list, kind: str) -> None:
        """One column's path through [(port, node)]."""
        if len(ports) < 2:
            return
        flow = len(self.flows)
        edges = [self.edge(a, an, b, bn, kind, flow) for (a, an), (b, bn) in zip(ports, ports[1:]) if a != b]
        self.flows.append({"edges": edges, "from": ports[0][0], "to": ports[-1][0], "kind": kind})

    def add_path(self, nb: str, res: Resolution, path, src_inst, end, kind: str) -> None:
        """A path from a resolution; end: (dataset id, column) where it is written, or None (a condition)."""
        ports, label = [], ""
        for k, node in enumerate(path):
            if node[0] == "c":
                inst = src_inst(node[1])
                ref = res.program.tables.get(node[1])
                if ref is None:
                    continue
                self.dataset(ref, inst, "before" if inst.endswith("#before") else "")
                name = next((c.name for c in res.sources.get(node[1], []) if c.name.lower() == node[2]), node[2])
                label = label or name
                ports.append((self.column(inst, name, inferred=node[2] != STAR and _inferred(res, node)), inst))
                continue
            op = res.program.ops.get(node[1])
            if op is None:
                continue
            oid = self.op(nb, op)
            prefix = tuple(p for p, _ in ports)        # a lane per column passing through, for every definition
            lanes = self.lanes[oid]
            if op.scope == ROW and kind == "condition" and k == len(path) - 1:
                port = lanes.get(prefix) or f"{oid}|cond"
                if port in lanes.values():
                    return                              # the column also passes through: no extra line
            else:
                port = lanes.get(prefix)
                if port is None:
                    port = lanes[prefix] = f"{oid}|{len(lanes)}"
                    self.ops[oid]["lanes"].append({"id": port, "label": label or "(made here)"})
            ports.append((port, oid))
        if end is not None:
            ports.append((self.column(end[0], end[1]), end[0]))
        self.flow(ports, kind)

    def finish(self) -> dict:
        used = {e["from"] for e in self.edges.values()} | {e["to"] for e in self.edges.values()}
        incoming = {e["toNode"] for e in self.edges.values() if e["kind"] in ("data", "change", "copy", "bi")}
        outgoing = {e["fromNode"] for e in self.edges.values() if e["kind"] != "relation"}
        for inst, d in self.datasets.items():
            cols = list(self.cols[inst].values())
            for c in cols:
                c["used"] = c["id"] in used
            d["columns"] = [c for c in cols if not c["star"]] + [c for c in cols if c["star"]]
            d["role"] = "between" if inst in incoming and inst in outgoing else \
                ("target" if inst in incoming else "source")
        cards = []
        for cid, c in self.cards.items():
            c["rows"] = list(self.rows[cid].values())
            cards.append(c)
        return {"datasets": list(self.datasets.values()), "ops": list(self.ops.values()), "cards": cards,
                "edges": sorted(self.edges.values(), key=lambda e: e["i"]), "flows": self.flows}


def _inferred(res: Resolution, node) -> bool:
    return any(c.inferred and c.name.lower() == node[2] for c in res.sources.get(node[1], []))


def _src_inst(nb: NotebookLineage):
    return lambda tid: f"{tid}#before" if tid in nb.res.read_first else tid


def _stage_json(nb: NotebookLineage, focus: bool = False) -> dict:
    kind = "pipeline" if nb.item.get("type") == "DataPipeline" else "notebook"
    return {**nb.item, "kind": kind, "focus": focus, "notes": nb.res.notes[:20], "error": nb.res.program.error}


# ======================================================================= Power BI
def _add_bi(g: _Graph, bi: BiLineage, take, relation: bool = False) -> None:
    """Semantic models that read datasets in the view: the model columns reading the columns take()
    allows, the measures and calculated columns built on them, and the report visuals that show any."""
    for mitem, mp in bi.models:
        mid = mitem["id"]
        cols = [f for f in mp.fields if f.kind == "column" and f.source and f.source[0] in g.datasets and
                take(f.source[0], f.source[1])]
        if not cols:
            continue
        g.card(mid, {**mitem, "kind": "model", "notes": mp.notes[:10], "error": mp.error})
        if relation:
            for tid in sorted({f.source[0] for f in cols}):
                g.edge(f"{tid}|", tid, f"{mid}|", mid, "relation", None)
        reached = {}
        for f in cols:
            tid = f.source[0]
            name = next((c["name"] for c in g.cols[tid].values() if c["name"].lower() == f.source[1]), f.source_name)
            port = g.column(tid, name, status="bi")
            row = g.row(mid, f"{mid}|{f.key}", {"label": f.label, "kind": "column", "type": f.data_type,
                                                "hidden": f.hidden, "letters": "", "category": ""})
            g.flow([(port, tid), (row, mid)], "bi")
            reached[f.key] = (row, mid)
        frontier = list(cols)
        while frontier:                                # DAX built on what is reached, as far as it goes
            f = frontier.pop()
            for u in mp.users(f):
                oid = g.raw_op(u.op_json(mitem["itemId"]))
                g.flow([reached[f.key], (oid, oid)], "bi")
                if u.key not in reached:
                    reached[u.key] = (oid, oid)
                    frontier.append(u)
        for ritem, rp in bi.reports_of(mitem["itemId"]):
            rid = ritem["id"]
            for v in rp.visuals:
                hits = []
                for t, n in v.fields:
                    target = mp.find(t, (t, n))
                    if target is not None and target.key in reached:
                        hits.append(reached[target.key])
                if not hits:
                    continue
                g.card(rid, {**ritem, "kind": "report", "notes": rp.notes[:10], "error": rp.error})
                row = g.row(rid, f"{rid}|{v.page}/{v.name}", {"label": v.label, "kind": "visual", "type": v.type,
                                                              "letters": "", "category": ""})
                for hit in dict.fromkeys(hits):
                    g.flow([hit, (row, rid)], "bi")
            if relation and rid in g.cards:
                g.edge(f"{mid}|", mid, f"{rid}|", rid, "relation", None)


# ======================================================================= notebook view
def _register(g: _Graph, nb: NotebookLineage, flows, whole: bool) -> None:
    """Datasets and columns of one notebook or pipeline (all of them when whole), then its paths."""
    res, to_src = nb.res, _src_inst(nb)
    if whole:
        for tid, cols in res.sources.items():
            ref = res.program.tables[tid]
            inst = to_src(tid)
            g.dataset(ref, inst, "before" if inst != tid else "", nb.schemas.get(tid))
            for c in cols:
                g.column(inst, c.name, type=c.type, inferred=c.inferred)
    for w in res.writes:
        tid = w.sink.table.id
        if not whole and not any(f[1] == tid for f in flows):
            continue
        d = g.dataset(w.sink.table, tid, "", nb.schemas.get(tid))
        d["writes"].append({"notebook": nb.id, "verb": w.sink.verb, "mode": w.sink.mode, "cell": w.sink.cell,
                            "line": w.sink.line})
        for c in w.columns:
            if whole or any(f[1] == tid and f[2].lower() == c.name.lower() for f in flows):
                g.column(tid, c.name, type=c.type, status=c.status, inTable=c.in_table, inferred=c.inferred)
    for path, tid, name in flows:
        g.add_path(nb.id, res, path, to_src, (tid, name), "data")


def _conditions(g: _Graph, nb: NotebookLineage) -> None:
    for p in nb.res.conditions:
        if f"op:{nb.id}:{p[-1][1]}" in g.ops:
            g.add_path(nb.id, nb.res, p, _src_inst(nb), None, "condition")


def _unfollowed(g: _Graph, focus: NotebookLineage, inputs: list, outputs: list) -> None:
    """Datasets the map links to the notebook (it reads or writes them) that its code couldn't be followed
    into column by column (a class, a function never called, a library): drawn on their side, joined
    through a card for the notebook, so both sides are always there."""
    hub = f"hub:{focus.id}"
    g.card(hub, {**focus.item, "id": hub, "kind": "notebook", "hub": True, "notes": [], "error": ""})
    g.row(hub, f"{hub}|note", {"label": "read or written in a way not followed column by column", "kind": "copy",
                               "letters": "", "category": "copy"})
    for side, items in (("in", inputs), ("out", outputs)):
        for ref, cols, schema in items:
            g.dataset(ref, ref.id, "", schema)["notFollowed"] = True
            for name, typ in cols or []:
                g.column(ref.id, name, type=typ)
            if side == "in":
                g.edge(f"{ref.id}|", ref.id, f"{hub}|", hub, "relation", None)
            else:
                g.edge(f"{hub}|", hub, f"{ref.id}|", ref.id, "relation", None)


def notebook_view(focus: NotebookLineage, others: list, scope: str = "direct", bi: BiLineage = None,
                  unfollowed: tuple = ((), ())) -> dict:
    g = _Graph()
    chosen = {focus.id: (focus, list(focus.data_flows()))}
    if scope == "full":
        _upstream(focus, others, chosen)
        _downstream(focus, others, chosen)
    for nid, (nb, flows) in chosen.items():
        _register(g, nb, flows, whole=nid == focus.id)
    for nid, (nb, _) in chosen.items():
        _conditions(g, nb)
    if scope == "full" and bi is not None:
        _add_bi(g, bi, lambda tid, col: True)
    if unfollowed[0] or unfollowed[1]:
        _unfollowed(g, focus, list(unfollowed[0]), list(unfollowed[1]))
    out = g.finish()
    outs = {ref.id for ref, _, _ in unfollowed[1]}
    for d in out["datasets"]:
        if d["id"] in outs:
            d["role"] = "between" if d["role"] == "between" else "target"
    out["notebooks"] = [_stage_json(nb, nid == focus.id) for nid, (nb, _) in chosen.items()]
    out["notes"] = focus.res.notes[:20]
    return out


def _upstream(focus: NotebookLineage, others: list, chosen: dict) -> None:
    """What writes what the focus reads, column by column, and so on upstream."""
    want = {(p[0][1], p[0][2]) for p, _, _ in focus.data_flows() if p and p[0][0] == "c"} | \
           {(p[0][1], p[0][2]) for p in focus.res.conditions if p and p[0][0] == "c"}
    seen = set(want)
    for _ in range(MAX_DEPTH):
        nxt = set()
        for nb in others:
            if nb.id == focus.id:
                continue
            picked = [f for f in nb.data_flows() if (f[1], f[2].lower()) in want or (f[1], STAR) in want]
            if not picked:
                continue
            cur = chosen.setdefault(nb.id, (nb, []))[1]
            for f in picked:
                if f not in cur:
                    cur.append(f)
                if f[0] and f[0][0][0] == "c":
                    nxt.add((f[0][0][1], f[0][0][2]))
        want = nxt - seen
        seen |= nxt
        if not want:
            break


def _downstream(focus: NotebookLineage, others: list, chosen: dict) -> None:
    """What reads what the focus writes, column by column, and so on downstream."""
    have = {(tid, name.lower()) for _, tid, name in focus.data_flows()}
    seen = set(have)
    for _ in range(MAX_DEPTH):
        nxt = set()
        for nb in others:
            if nb.id == focus.id:
                continue
            picked = [f for f in nb.data_flows() if f[0] and f[0][0][0] == "c" and
                      ((f[0][0][1], f[0][0][2]) in have or (f[0][0][2] == STAR and
                                                            any(t == f[0][0][1] for t, _ in have)))]
            if not picked:
                continue
            cur = chosen.setdefault(nb.id, (nb, []))[1]
            for f in picked:
                if f not in cur:
                    cur.append(f)
                nxt.add((f[1], f[2].lower()))
        have = nxt - seen
        seen |= nxt
        if not have:
            break


# ======================================================================= datasets view
def _thread(lineages: list, focus: str, scope: str, direction: str) -> set:
    """The notebooks and pipelines on a dataset's thread (Direct links: right next to it)."""
    reads, writes = defaultdict(set), defaultdict(set)
    for nb in lineages:
        for tid in nb.res.sources:
            reads[tid].add(nb.id)
        for w in nb.res.writes:
            writes[w.sink.table.id].add(nb.id)
    by_id = {nb.id: nb for nb in lineages}
    picked = set()
    for dir_ in (("in", "out") if direction == "both" else (direction,)):
        queue, seen = deque([(focus, 0)]), {focus}
        while queue:
            tid, depth = queue.popleft()
            for nid in writes[tid] if dir_ == "in" else reads[tid]:
                picked.add(nid)
                if scope != "full" or depth >= MAX_DEPTH:
                    continue
                nb = by_id[nid]
                for t in (list(nb.res.sources) if dir_ == "in" else [w.sink.table.id for w in nb.res.writes]):
                    if t not in seen:
                        seen.add(t)
                        queue.append((t, depth + 1))
    return picked


def _chain_row(nb: NotebookLineage, path) -> tuple:
    """The card row of a path: the chain of its column definitions (or a plain copy)."""
    ops = [nb.res.program.ops[n[1]] for n in path if n[0] == "o" and n[1] in nb.res.program.ops and
           nb.res.program.ops[n[1]].scope != ROW]
    rid = f"{nb.id}|" + (">".join(o.key for o in ops) if ops else "copy")
    letters = "".join(dict.fromkeys(CATEGORIES[o.category].letter for o in ops))
    first = ops[0].to_json() if ops else {}
    return rid, {"label": " ▸ ".join(o.label for o in ops) or "copied as it is", "letters": letters[:3],
                 "category": ops[-1].category if ops else "copy", "kind": "definition" if ops else "copy",
                 "ops": [f"op:{nb.id}:{o.key}" for o in ops], "code": "\n".join(dict.fromkeys(o.code for o in ops)),
                 "cell": ops[0].cell if ops else 0, "line": ops[0].line if ops else 0,
                 "tag": first.get("tag", ""), "where": first.get("where", "")}


def _bi_focus(bi: BiLineage, focus: str):
    """A semantic model or report to focus on: (the model, the reports to show), or None."""
    if bi is None or not focus.startswith("item:"):
        return None
    iid = focus[5:].lower()
    model = next(((i, m) for i, m in bi.models if i["itemId"].lower() == iid), None)
    report = next(((i, r) for i, r in bi.reports if i["itemId"].lower() == iid), None)
    if report is not None and model is None:
        model = next(((i, m) for i, m in bi.models if i["itemId"].lower() == (report[1].model or "").lower()), None)
    if model is None:
        return None
    return model, [report] if report is not None else bi.reports_of(model[0]["itemId"])


def datasets_view(lineages: list, focus: str = "", scope: str = "full", direction: str = "both",
                  all_columns: bool = False, bi: BiLineage = None) -> dict:
    focused_bi = _bi_focus(bi, focus)
    if focused_bi is not None:                         # a model or report: everything upstream of what it reads
        (mitem, mp), reports = focused_bi
        picked = set()
        for tid in mp.tables:
            picked |= _thread(lineages, tid, scope, "in")
        chosen = [nb for nb in lineages if nb.id in picked]
        bi = BiLineage([(mitem, mp)], reports)
    else:
        chosen = [nb for nb in lineages if nb.id in _thread(lineages, focus, scope, direction)] if focus else \
            list(lineages)
    g = _Graph()
    if focused_bi is not None:
        for tid, ref in focused_bi[0][1].tables.items():
            g.dataset(ref, tid)

    def source_name(nb, tid, low):
        return next((s.name for s in nb.res.sources.get(tid, []) if s.name.lower() == low), low)

    for nb in chosen:                                  # datasets, relations, and what each does to rows
        res = nb.res
        card = g.card(nb.id, _stage_json(nb))
        header = f"{nb.id}|"
        for tid in res.sources:
            g.dataset(res.program.tables[tid], tid, "", nb.schemas.get(tid))
            g.edge(f"{tid}|", tid, header, nb.id, "relation", None)
            if all_columns:
                for c in res.sources[tid]:
                    if not c.star:
                        g.column(tid, c.name, status="unchanged", type=c.type, inferred=c.inferred)
        for w in res.writes:
            tid = w.sink.table.id
            d = g.dataset(w.sink.table, tid, "", nb.schemas.get(tid))
            d["writes"].append({"notebook": nb.id, "verb": w.sink.verb, "mode": w.sink.mode, "cell": w.sink.cell,
                                "line": w.sink.line})
            g.edge(header, nb.id, f"{tid}|", tid, "relation", None)
            if all_columns:
                for c in w.columns:
                    if not c.star and c.status in ("unchanged", "kept", "unknown"):
                        g.column(tid, c.name, status="unchanged" if c.status != "kept" else "kept", type=c.type)
        flows = list(nb.data_flows())
        for op in res.program.ops.values():
            if op.scope == ROW and any(("o", op.key) in p for p, _, _ in flows):
                card["rowOps"].append({"letters": CATEGORIES[op.category].letter, "category": op.category,
                                       "label": op.label, "key": f"op:{nb.id}:{op.key}"})
    changed = set()
    for nb in chosen:                                  # green: the columns that change
        for w in nb.res.writes:
            tid = w.sink.table.id
            for c in w.columns:
                if c.status not in CHANGED:
                    continue
                target = g.column(tid, c.name, status=c.status, type=c.type)
                changed.add((tid, c.name.lower()))
                col = g.cols[tid][c.name.lower()]
                if nb.id not in col["changedBy"]:
                    col["changedBy"].append(nb.id)
                for p in c.paths:
                    rid, row = _chain_row(nb, p)
                    g.row(nb.id, rid, row)
                    ports = []
                    if p[0][0] == "c" and p[0][1] in nb.res.program.tables:
                        g.dataset(nb.res.program.tables[p[0][1]], p[0][1])
                        ports.append((g.column(p[0][1], source_name(nb, p[0][1], p[0][2]), status="feeds"), p[0][1]))
                    g.flow(ports + [(rid, nb.id), (target, tid)], "change")
    carried, reach, grew = [], set(changed), True      # thin: a changed column carried on by copies
    while grew:
        grew = False
        for nb in chosen:
            for p, tid, name in nb.data_flows():
                key = (tid, name.lower())
                if not p or p[0][0] != "c" or (p[0][1], p[0][2]) not in reach or key in changed or key in reach:
                    continue
                carried.append((nb, p, tid, name))
                reach.add(key)
                grew = True
    if all_columns:                                    # All columns: every column's whole path, copies too
        done = {(nb.id, p, tid) for nb, p, tid, _ in carried}
        for nb in chosen:
            for w in nb.res.writes:
                tid = w.sink.table.id
                for c in w.columns:
                    if c.status in CHANGED or c.star:
                        continue
                    for p in c.paths:
                        if p and p[0][0] == "c" and p[0][1] in nb.res.program.tables and (nb.id, p, tid) not in done:
                            carried.append((nb, p, tid, c.name))
                            done.add((nb.id, p, tid))
    for nb, p, tid, name in carried:
        rid, row = _chain_row(nb, p)
        g.row(nb.id, rid, row)
        g.dataset(nb.res.program.tables[p[0][1]], p[0][1])
        src = g.column(p[0][1], source_name(nb, p[0][1], p[0][2]))
        g.flow([(src, p[0][1]), (rid, nb.id), (g.column(tid, name, status="carried" if (tid, name.lower()) in reach else "unchanged"), tid)], "copy")
    if bi is not None:                                 # yellow: into Power BI (all of a model focused on)
        everything = all_columns or focused_bi is not None
        _add_bi(g, bi, (lambda tid, col: True) if everything else (lambda tid, col: (tid, col) in reach),
                relation=True)
    out = g.finish()
    for d in out["datasets"]:
        if not all_columns:
            known = max((len(nb.res.sources.get(d["id"], [])) for nb in chosen), default=0)
            d["hidden"] = max(0, known - len(d["columns"])) if known else 0
    out["notebooks"] = [c for c in out["cards"] if c["kind"] in ("notebook", "pipeline")]
    return out
