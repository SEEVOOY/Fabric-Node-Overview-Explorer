"""A data pipeline's Copy activities -> what they do to columns, as the same program a notebook gives.

A Copy activity reads a source and writes a sink. Its translator (the column mapping in the activity)
says which source column goes to which sink column, and the types on each side:

    "translator": {"type": "TabularTranslator", "mappings": [
        {"source": {"name": "TagNo", "type": "String"}, "sink": {"name": "tag_no", "type": "String"}}]}

Every column goes through the copy (a row definition, P); a column given another name gets a rename
(R), another type a type change (T). Without a mapping the columns are copied by name. A source
query (sqlReaderQuery) is read like any SQL. Stores outside Fabric take part as external datasets.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

from ..parse.pipeline import _store, _val, walk_activities
from .catalog import COLUMN, ROW
from .model import Builder, Dep, Entry, Program, Sink, TableRef
from .sql import SqlReader

Place = Callable[[dict, str], Optional[TableRef]]       # (store settings, "source" | "sink") -> dataset


def _field(side) -> str:
    if not isinstance(side, dict):
        return ""
    v = side.get("name") or side.get("path") or ""
    v = str(v).strip()
    m = re.fullmatch(r"\$?\[?['\"]?(.+?)['\"]?\]?", v)             # $['TagNo'] (JSON paths)
    return (m.group(1) if m else v).strip()


def read_pipeline_columns(content: dict, place: Place) -> Program:
    b = Builder(lambda *a: None)
    acts = ((content or {}).get("properties") or content or {}).get("activities") or []
    copies = [a for a in walk_activities(acts) if str(a.get("type") or "").lower() == "copy"]
    for i, a in enumerate(copies, 1):
        b.cell = i
        name = str(a.get("name") or f"Copy {i}")
        tp = a.get("typeProperties") or {}
        src_node, sink_node = tp.get("source") or {}, tp.get("sink") or {}
        src, sink = place(_store(src_node), "source"), place(_store(sink_node), "sink")
        if sink is None:
            b.note(f"Activity {name}: where it copies to is a pipeline expression or isn't known.")
            continue
        for ref in (src, sink):
            if ref is not None:
                b.p.tables.setdefault(ref.id, ref)
        query, dynamic = _val(src_node.get("sqlReaderQuery") or src_node.get("query") or "")
        fid = SqlReader(b).run(query, line=0, key="q:") if query and not dynamic else None
        if fid is None:
            fid = b.source(src, 0) if src is not None else b.opaque(f"the source of {name}", 0)
        translator = tp.get("translator") or {}
        code = json.dumps({"source": src_node.get("type", ""), "sink": sink_node.get("type", ""),
                           "translator": translator}, indent=2)
        copy = b.op("copy", ROW, ["copy"], f"copy: {name}", code, 0, category="copy", detail=f"Copy activity {name}")
        entries = []
        for k, m in enumerate(translator.get("mappings") or []):
            s, t = _field(m.get("source")), _field(m.get("sink"))
            if not s or not t:
                continue
            st, tt = str((m.get("source") or {}).get("type") or ""), str((m.get("sink") or {}).get("type") or "")
            ops = []
            if s.lower() != t.lower():
                ops.append(b.op(f"map{k}", COLUMN, [], f"{s} → {t}", json.dumps(m), 0, category="rename"))
            if st and tt and st.lower() != tt.lower():
                ops.append(b.op(f"type{k}", COLUMN, ["cast"], f"{t}: {st} → {tt}", json.dumps(m), 0, category="cast"))
            entries.append(Entry(name=t, deps=[Dep(fid, s)], ops=ops))
        out = b.frame(entries=entries, row_ops=[copy]) if entries else b.star(fid, row_ops=[copy])
        mode = str(sink_node.get("tableActionOption") or sink_node.get("writeBehavior") or "copy").lower()
        b.sink(Sink(sink, out, "Copy activity", mode, i, 0, code=code))
        for key in [k for k in b.p.ops if k.startswith(f"{i}:")]:
            b.p.ops[key].tag, b.p.ops[key].where = f"A{i}", f"Activity {name}"
        if not translator.get("mappings"):
            b.note(f"Activity {name} has no column mapping: the columns are copied by name.")
    return b.p
