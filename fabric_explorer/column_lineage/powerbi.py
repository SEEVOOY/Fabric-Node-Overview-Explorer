"""Power BI: a semantic model's columns and measures, and the report visuals that use them.

    model     every model table reads a dataset (the scanner places it, as on the map). Its columns
              read that dataset's columns (sourceColumn); calculated columns and measures are DAX,
              which names the fields it uses: 'Tags'[Plant], Tags[Plant], [Total tags]
    report    every visual names the fields it shows (and filters on). Both report formats are read:
              PBIR (definition/pages/<page>/visuals/<visual>/visual.json) and the older report.json

The column lineage follows a dataset column into the model column that reads it, the measures built
on that, and the visuals that show either: which reports a column change reaches.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from ..parse.model import text_of
from .catalog import CATEGORIES, dax_categories

_STRING = re.compile(r'"(?:[^"]|"")*"')
_COMMENT = re.compile(r"//[^\n]*|--[^\n]*|/\*.*?\*/", re.S)
_REF = re.compile(r"'(?P<qt>(?:[^']|'')+)'\s*\[(?P<qc>[^\]]+)\]|(?P<t>[A-Za-z_][\w.]*)\s*\[(?P<c>[^\]]+)\]"
                  r"|\[(?P<m>[^\]]+)\]")
_FUNCTION = re.compile(r"\b([A-Za-z][A-Za-z0-9.]*)\s*\(")


def dax(expression: str) -> tuple[list, list]:
    """(the fields a DAX expression names [(table or "", name)], the functions it calls)."""
    text = _COMMENT.sub(" ", _STRING.sub('""', expression or ""))
    refs = []
    for m in _REF.finditer(text):
        if m.group("qt"):
            refs.append((m.group("qt").replace("''", "'"), m.group("qc")))
        elif m.group("t"):
            refs.append((m.group("t"), m.group("c")))
        else:
            refs.append(("", m.group("m")))
    return list(dict.fromkeys(refs)), list(dict.fromkeys(f.lower() for f in _FUNCTION.findall(text)))


@dataclass
class ModelField:
    table: str
    name: str
    kind: str                                      # column | calculated | measure
    source: tuple = ()                             # (dataset id, column lower case) it reads
    source_name: str = ""
    expression: str = ""
    refs: list = field(default_factory=list)       # [(table or "", name)] named in its DAX
    functions: list = field(default_factory=list)
    data_type: str = ""
    hidden: bool = False

    @property
    def key(self) -> str:
        return f"{self.table}[{self.name}]".lower()

    @property
    def label(self) -> str:
        return f"[{self.name}]" if self.kind == "measure" else f"{self.table}[{self.name}]"

    def op_json(self, model_id: str) -> dict:
        """A measure or calculated column as a definition (a balloon with the DAX's letters)."""
        cats = dax_categories(self.functions) or ["custom"]
        letters = "".join(CATEGORIES[c].letter for c in cats if c in CATEGORIES)[:3]
        return {"id": f"dax:{model_id}:{self.key}", "key": self.key, "category": cats[0], "categories": cats[:3],
                "scope": "column", "letters": letters, "label": f"{self.label} = {' '.join(self.expression.split())}"[:120],
                "code": f"{self.label} =\n{self.expression.strip()}", "cell": 0, "line": 0, "tag": "DAX",
                "where": f"{'Measure' if self.kind == 'measure' else 'Calculated column'} in the {self.table} table",
                "functions": self.functions[:12], "detail": "", "notebook": f"item:{model_id}", "lanes": []}


@dataclass
class ModelProgram:
    fields: list
    tables: dict                                   # dataset id -> TableRef the model reads
    notes: list = field(default_factory=list)
    kind: str = "model"
    error: str = ""

    def find(self, home: str, ref: tuple) -> Optional[ModelField]:
        """What a DAX reference names: Table[Name], or [Name] (a measure, or a column of the same table)."""
        table, name = ref[0].lower(), ref[1].lower()
        if table:
            return next((f for f in self.fields if f.table.lower() == table and f.name.lower() == name), None)
        return next((f for f in self.fields if f.kind == "measure" and f.name.lower() == name), None) or \
            next((f for f in self.fields if f.table.lower() == home.lower() and f.name.lower() == name), None)

    def users(self, target: ModelField) -> list:
        """The calculated columns and measures whose DAX names target."""
        return [f for f in self.fields if f.kind != "column" and
                any(self.find(f.table, r) is target for r in f.refs)]


def read_model(bim: dict, sources: dict) -> ModelProgram:
    """sources: model table name -> TableRef of the dataset it reads (placed by the scanner)."""
    model = bim.get("model", bim) if isinstance(bim, dict) else {}
    fields, tables, notes = [], {}, []
    for t in model.get("tables", []) or []:
        name = str(t.get("name") or "")
        if not name or name.startswith(("LocalDateTable_", "DateTableTemplate_")):
            continue
        ref = sources.get(name)
        if ref is not None:
            tables[ref.id] = ref
        for c in t.get("columns") or []:
            kind = str(c.get("type") or "data").lower()
            if kind == "rownumber" or not c.get("name"):
                continue
            if kind in ("calculated", "calculatedtablecolumn"):
                expr = text_of(c.get("expression"))
                refs, fns = dax(expr)
                fields.append(ModelField(name, str(c["name"]), "calculated", expression=expr, refs=refs, functions=fns,
                                         data_type=str(c.get("dataType") or ""), hidden=bool(c.get("isHidden"))))
            else:
                sc = str(c.get("sourceColumn") or c["name"])
                fields.append(ModelField(name, str(c["name"]), "column", source=(ref.id, sc.lower()) if ref else (),
                                         source_name=sc, data_type=str(c.get("dataType") or ""),
                                         hidden=bool(c.get("isHidden"))))
        for ms in t.get("measures") or []:
            if not ms.get("name"):
                continue
            expr = text_of(ms.get("expression"))
            refs, fns = dax(expr)
            fields.append(ModelField(name, str(ms["name"]), "measure", expression=expr, refs=refs, functions=fns,
                                     hidden=bool(ms.get("isHidden"))))
    return ModelProgram(fields, tables, notes)


# ======================================================================= reports
@dataclass
class Visual:
    page: str
    name: str
    type: str
    title: str
    fields: list                                   # [(table, name)]

    @property
    def label(self) -> str:
        what = self.title or self.type or self.name
        return f"{self.page}: {what}" if self.page else what


@dataclass
class ReportProgram:
    model: str                                     # the semantic model's item id
    visuals: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    kind: str = "report"
    error: str = ""


def _fields(obj, alias: Optional[dict] = None) -> list:
    """Every Column / Measure a visual's JSON names: {"Column": {"Expression": {"SourceRef": ...}, "Property": ...}}"""
    out = []

    def walk(o):
        if isinstance(o, dict):
            for kind in ("Column", "Measure"):
                x = o.get(kind)
                if isinstance(x, dict) and "Property" in x:
                    src = ((x.get("Expression") or {}).get("SourceRef") or {})
                    entity = src.get("Entity") or (alias or {}).get(src.get("Source"), "")
                    if entity:
                        out.append((str(entity), str(x["Property"])))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return list(dict.fromkeys(out))


def _title(objects) -> str:
    """A visual's title, if it has one written in: title[0].properties.text.expr.Literal.Value"""
    try:
        v = objects["title"][0]["properties"]["text"]["expr"]["Literal"]["Value"]
        return str(v).strip("'")
    except (KeyError, IndexError, TypeError):
        return ""


def _load(raw) -> dict:
    try:
        return json.loads(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)
    except (ValueError, UnicodeDecodeError):
        return {}


def read_report(parts: dict, model_id: str) -> ReportProgram:
    """parts: path -> bytes, as getDefinition returns them."""
    rep = ReportProgram(model_id)
    paths = {k.replace("\\", "/"): v for k, v in parts.items()}
    pages = {}
    for p, raw in paths.items():
        if p.endswith("/page.json") and "/pages/" in p:
            page_id = p.split("/pages/")[1].split("/")[0]
            pages[page_id] = str(_load(raw).get("displayName") or page_id)
    for p, raw in sorted(paths.items()):
        if p.endswith("/visual.json") and "/visuals/" in p and "/pages/" in p:
            page_id = p.split("/pages/")[1].split("/")[0]
            v = _load(raw)
            vis = v.get("visual") or {}
            rep.visuals.append(Visual(pages.get(page_id, page_id), str(v.get("name") or p.split("/visuals/")[1].split("/")[0]),
                                      str(vis.get("visualType") or ""), _title(vis.get("visualContainerObjects") or {}),
                                      _fields(v)))
        elif p.lower().endswith("report.json") and "/pages/" not in p:
            legacy = _load(raw)
            for sec in legacy.get("sections") or []:
                for vc in sec.get("visualContainers") or []:
                    cfg = vc.get("config")
                    cfg = _load(cfg) if isinstance(cfg, str) else (cfg or {})
                    sv = cfg.get("singleVisual") or {}
                    alias = {f.get("Name"): f.get("Entity") for f in ((sv.get("prototypeQuery") or {}).get("From") or [])}
                    rep.visuals.append(Visual(str(sec.get("displayName") or sec.get("name") or ""),
                                              str(cfg.get("name") or ""), str(sv.get("visualType") or ""),
                                              _title(sv.get("vcObjects") or {}), _fields(sv, alias)))
    rep.visuals = [v for v in rep.visuals if v.fields]
    if not rep.visuals:
        rep.notes.append("No visual in the report names a field of the model.")
    return rep
