"""A Fabric notebook definition -> its attached lakehouses and every reference in its code.

The definition comes from getDefinition?format=ipynb (notebook-content.ipynb). The Fabric source
format (notebook-content.py / .sql / .scala / .r, with "# META" blocks) is read as well, in case
a tenant returns that instead.

Attached lakehouse and environment come from the notebook metadata:
    metadata.dependencies.lakehouse   default_lakehouse, default_lakehouse_name,
                                      default_lakehouse_workspace_id, known_lakehouses
    metadata.dependencies.environment environmentId, workspaceId
(older notebooks keep the same under metadata.trident).
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from .python_code import CodeRef, PythonAnalyzer, regex_refs
from .sql import sql_refs

MAGIC_LANG = {"sql": "sql", "sparksql": "sql", "spark": "scala", "scala": "scala", "pyspark": "python",
              "python": "python", "pyspark3": "python", "sparkr": "r", "r": "r", "configure": "config",
              "html": "skip", "markdown": "skip", "md": "skip", "csharp": "skip", "tsql": "tsql", "kql": "skip",
              "time": "python", "capture": "python", "writefile": "skip", "bash": "skip", "sh": "skip"}
KERNEL_LANG = {"synapse_pyspark": "python", "jupyter": "python", "python3": "python", "synapse_spark": "scala",
               "synapse_sparkr": "r", "sparksql": "sql", "sqldatawarehouse": "tsql", "spark": "scala"}
_RUN = re.compile(r"""^\s*%run\s+(?P<flags>(?:-{1,2}[\w-]+\s+)*)(?P<target>"[^"]+"|'[^']+'|[^\s{]+)""")
_MARKER = re.compile(r"^(?:#|--|//)\s*(?P<kind>CELL|MARKDOWN|PARAMETERS CELL|METADATA)\s+\*{5,}\s*$")
_META = re.compile(r"^(?:#|--|//)\s*META\b ?(?P<json>.*)$")


@dataclass
class Cell:
    index: int                 # 1-based, counting every cell (as Fabric shows them)
    kind: str                  # code | markdown | raw
    language: str
    source: str
    parameters: bool = False


@dataclass
class NotebookInfo:
    format: str = "ipynb"
    language: str = "python"
    kernel: str = ""
    default_lakehouse: dict = field(default_factory=dict)   # id, name, workspace_id
    known_lakehouses: list = field(default_factory=list)    # ids
    environment: dict = field(default_factory=dict)         # id, workspace_id
    default_warehouse: dict = field(default_factory=dict)   # id, type
    cells: list = field(default_factory=list)


@dataclass
class NotebookRef(CodeRef):
    cell: int = 0
    code: str = ""


def _source(value) -> str:
    return "".join(value) if isinstance(value, list) else str(value or "")


def _find_key(obj, key):
    """First dict under obj (depth-first) that contains key."""
    if isinstance(obj, dict):
        if key in obj:
            return obj
        for v in obj.values():
            hit = _find_key(v, key)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _find_key(v, key)
            if hit is not None:
                return hit
    return None


def _dependencies(meta: dict, info: NotebookInfo) -> None:
    lake = _find_key(meta, "default_lakehouse") or _find_key(meta, "known_lakehouses") or {}
    if lake.get("default_lakehouse"):
        info.default_lakehouse = {"id": str(lake.get("default_lakehouse") or "").lower(),
                                  "name": str(lake.get("default_lakehouse_name") or ""),
                                  "workspace_id": str(lake.get("default_lakehouse_workspace_id") or "").lower()}
    known = []
    for k in lake.get("known_lakehouses") or []:
        kid = str(k.get("id") if isinstance(k, dict) else k or "").lower()
        if kid and kid not in known:
            known.append(kid)
    info.known_lakehouses = known
    env = _find_key(meta, "environmentId")
    if env and env.get("environmentId"):
        info.environment = {"id": str(env["environmentId"]).lower(),
                            "workspace_id": str(env.get("workspaceId") or "").lower()}
    wh = _find_key(meta, "default_warehouse")
    if wh and wh.get("default_warehouse"):
        info.default_warehouse = {"id": str(wh["default_warehouse"]).lower(),
                                  "type": str((next((w for w in wh.get("known_warehouses") or []
                                                     if isinstance(w, dict) and str(w.get("id", "")).lower()
                                                     == str(wh["default_warehouse"]).lower()), {}) or {})
                                              .get("type", ""))}
    kernel = (meta.get("kernel_info") or {}).get("name") or (meta.get("kernelspec") or {}).get("name") or ""
    info.kernel = str(kernel)
    lang = (meta.get("language_info") or {}).get("name") or KERNEL_LANG.get(info.kernel, "python")
    info.language = {"sparksql": "sql", "spark": "scala"}.get(str(lang).lower(), str(lang).lower())


def _cell_language(meta: dict, source: str, default: str) -> str:
    first = next((ln.strip() for ln in source.splitlines() if ln.strip()), "")
    if first.startswith("%%"):
        magic = first[2:].split()[0].lower() if first[2:].split() else ""
        return MAGIC_LANG.get(magic, "skip")
    ms = (meta or {}).get("microsoft") or {}
    lang = str(ms.get("language") or "").lower()
    if lang:
        return {"sparksql": "sql", "spark": "scala", "sparkr": "r", "csharp": "skip"}.get(lang, lang)
    return default


def read_ipynb(data: bytes) -> NotebookInfo:
    nb = json.loads(data.decode("utf-8-sig"))
    meta = nb.get("metadata") or {}
    info = NotebookInfo(format="ipynb")
    _dependencies(meta, info)
    for i, c in enumerate(nb.get("cells") or [], start=1):
        src = _source(c.get("source"))
        cmeta = c.get("metadata") or {}
        kind = c.get("cell_type", "code")
        tags = cmeta.get("tags") or []
        info.cells.append(Cell(i, kind, _cell_language(cmeta, src, info.language) if kind == "code" else kind,
                               src, parameters="parameters" in tags))
    return info


def read_fabric_source(text: str, ext: str) -> NotebookInfo:
    """notebook-content.py / .sql / .scala / .r (Fabric's Git source format).

    A METADATA block before the first cell describes the notebook; one after a cell describes
    that cell (its language).
    """
    info = NotebookInfo(format="fabricGitSource")
    default = {"py": "python", "sql": "sql", "scala": "scala", "r": "r"}.get(ext, "python")
    info.language = default
    blocks: list = []
    cur, in_meta = None, False
    for line in text.splitlines():
        m = _MARKER.match(line)
        if m:
            if m.group("kind") == "METADATA":
                if cur is None:
                    cur = {"kind": "NOTEBOOK", "lines": [], "meta": []}
                    blocks.append(cur)
                in_meta = True
            else:
                cur = {"kind": m.group("kind"), "lines": [], "meta": []}
                blocks.append(cur)
                in_meta = False
            continue
        if cur is None:
            continue
        if in_meta:
            mm = _META.match(line)
            if mm:
                cur["meta"].append(mm.group("json"))
        else:
            cur["lines"].append(line)
    index = 0
    for b in blocks:
        try:
            meta = json.loads("\n".join(b["meta"])) if b["meta"] else {}
        except ValueError:
            meta = {}
        if b["kind"] == "NOTEBOOK":
            _dependencies(meta, info)
            info.language = info.language or default
            continue
        index += 1
        src = "\n".join(b["lines"]).strip("\n")
        if b["kind"] == "MARKDOWN":
            info.cells.append(Cell(index, "markdown", "markdown", src))
            continue
        lang = str(meta.get("language") or "").lower()
        lang = {"sparksql": "sql", "spark": "scala", "sparkr": "r"}.get(lang, lang) or default
        info.cells.append(Cell(index, "code", _cell_language({}, src, lang), src,
                               parameters=b["kind"] == "PARAMETERS CELL"))
    return info


def read_notebook(parts: dict) -> NotebookInfo:
    """parts: {path: bytes} from getDefinition."""
    for path, data in parts.items():
        if path.lower().endswith(".ipynb"):
            return read_ipynb(data)
    for path, data in parts.items():
        low = path.lower()
        for ext in ("py", "sql", "scala", "r"):
            if low.endswith(f"notebook-content.{ext}"):
                return read_fabric_source(data.decode("utf-8-sig"), ext)
    raise ValueError("The notebook definition had no notebook-content part.")


def _prepare_python(source: str) -> tuple[str, list]:
    """Blank out IPython magics (keeping line numbers); return the code and the %run targets."""
    lines = source.splitlines()
    runs = []
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("%%"):
            lines[i] = ""
        elif stripped.startswith("%"):
            m = _RUN.match(line)
            if m and "-b" not in (m.group("flags") or "") and "builtin" not in (m.group("flags") or ""):
                target = m.group("target").strip("\"'")
                if not target.lower().startswith(("builtin/", "/builtin")):
                    runs.append((i + 1, target))
            lines[i] = ""
        elif stripped.startswith("!"):
            lines[i] = ""
    return "\n".join(lines), runs


def analyze_notebook(info: NotebookInfo, seed: Optional[dict] = None) -> tuple[list, dict]:
    """(references, facts) for a notebook.

    seed: constants from notebooks this one pulls in with %run (name -> list of values).
    facts: languages, cell counts, %run targets and the constants this notebook defines.
    """
    analyzer = PythonAnalyzer(seed)
    python_cells, other_cells, runs = [], [], []
    for cell in info.cells:
        if cell.kind != "code" or not cell.source.strip():
            continue
        if cell.language == "python":
            code, cell_runs = _prepare_python(cell.source)
            runs.extend((cell.index, ln, t) for ln, t in cell_runs)
            try:
                tree = ast.parse(code)
            except SyntaxError:
                other_cells.append((cell, "python-regex"))
                continue
            python_cells.append((cell, tree))
        elif cell.language in ("sql", "tsql", "scala", "r"):
            other_cells.append((cell, cell.language))

    for _, tree in python_cells:
        analyzer.learn_functions(tree)
    for _ in range(2):                       # twice, so values that depend on other values settle
        for _, tree in python_cells:
            analyzer.learn(tree)

    refs: list = []

    def add(cell: Cell, r: CodeRef):
        lines = cell.source.splitlines()
        code = lines[r.line - 1].strip() if 0 < r.line <= len(lines) else ""
        refs.append(NotebookRef(**{**r.__dict__, "cell": cell.index, "code": code[:240]}))

    for cell_no, line, target in runs:
        cell = next(c for c in info.cells if c.index == cell_no)
        add(cell, CodeRef("notebook", "run", "%run", target=target.removesuffix(".ipynb"), line=line))
    for cell, tree in python_cells:
        for r in analyzer.analyze(tree):
            add(cell, r)
    for cell, lang in other_cells:
        if lang in ("sql", "tsql"):
            text = "\n".join("" if ln.lstrip().startswith("%%") else ln for ln in cell.source.splitlines())
            for r in sql_refs(text, local_names=analyzer.temp_views):
                add(cell, PythonAnalyzer._from_sql(r, 1))
        else:
            for r in regex_refs(cell.source):
                add(cell, r)

    refs.sort(key=lambda r: (r.cell, r.line))
    seen, unique = set(), []
    for r in refs:
        key = (r.kind, r.op, tuple(p.lower() for p in r.parts), r.path, r.target.lower(), r.cell, r.line,
               r.dynamic)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    languages = sorted({c.language for c in info.cells if c.kind == "code" and c.source.strip()})
    facts = {
        "cells": len(info.cells),
        "codeCells": sum(1 for c in info.cells if c.kind == "code"),
        "languages": languages,
        "runTargets": [t for _, _, t in runs],
        "exports": analyzer.exports(),
        "regexCells": [c.index for c, lang in other_cells if lang == "python-regex"],
    }
    return unique, facts
