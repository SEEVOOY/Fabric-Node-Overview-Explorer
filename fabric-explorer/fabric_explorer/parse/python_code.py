"""What PySpark / Python notebook code reads, writes and runs, found with the Python AST.

Strings are resolved where the code makes it possible: literals, f-strings, + and .format() of
known values, constants assigned anywhere in the notebook (and in notebooks pulled in with %run),
loop variables over literal lists or dict.items(), and helper-function parameters that are called
with literal arguments. Anything else is reported as unresolved, with the expression as written.

Cells that aren't valid Python (and Scala / R cells) fall back to regular expressions.
"""
from __future__ import annotations

import ast
import itertools
import posixpath
import re
from dataclasses import dataclass, field
from typing import Optional

from .onelake import parse_location
from .sql import DYN, split_name, sql_refs

MAX_VALUES = 40

SPARK_ROOTS = {"spark", "sqlContext", "sqlcontext", "session", "ss"}
UTIL_ROOTS = {"mssparkutils", "notebookutils", "dbutils"}
FILE_FORMATS = {"parquet", "csv", "json", "text", "orc", "avro", "delta", "xml", "excel"}
DELTA_WRITE = {"merge", "delete", "update", "upgradeTableProtocol", "restoreToVersion", "restoreToTimestamp"}
DELTA_MAINTAIN = {"optimize", "vacuum"}
TEMP_VIEW_METHODS = {"createOrReplaceTempView", "createTempView", "createOrReplaceGlobalTempView",
                     "createGlobalTempView", "registerTempTable"}
PANDAS_ROOTS = {"pd", "pandas", "pl", "polars"}
PANDAS_WRITERS = {"to_csv", "to_parquet", "to_excel", "to_json", "to_feather", "write_delta", "write_parquet",
                  "write_csv", "write_json"}
SEMPY_READS = {"read_table", "evaluate_measure", "evaluate_dax", "list_tables", "list_measures",
               "list_columns", "list_relationships", "read_parquet"}


@dataclass
class CodeRef:
    kind: str                 # table | path | notebook | model | warehouse_table | external | use
    op: str                   # read | write | maintain | run | refresh | use
    verb: str                 # what the code calls, e.g. saveAsTable, spark.read.table, MERGE INTO
    parts: list = field(default_factory=list)   # table name parts, e.g. ["silver", "tags"]
    path: str = ""            # file / OneLake path, or an external URL
    target: str = ""          # notebook or semantic model (name or id)
    workspace: str = ""       # explicit workspace argument (name or id), if any
    line: int = 0             # 1-based line within the cell
    dynamic: bool = False     # the name couldn't be worked out from the code
    shown: str = ""           # the expression as written, for unresolved references
    note: str = ""            # e.g. "one of 3 values"


def _product(pieces: list) -> Optional[list]:
    out = []
    for combo in itertools.product(*pieces):
        s = "".join(combo)
        if s not in out:
            out.append(s)
        if len(out) >= MAX_VALUES:
            break
    return out


def _unparse(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return "?"


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _chain(call: ast.Call) -> list:
    """[(name, call-or-None), ...] from the root object to the called method.

    df.write.mode("x").saveAsTable("t") -> [(df, None), (write, None), (mode, <call>), (saveAsTable, <call>)]
    """
    out = []
    node, pending = call, None
    while True:
        if isinstance(node, ast.Call):
            pending = node
            node = node.func
        elif isinstance(node, ast.Attribute):
            out.append((node.attr, pending))
            pending = None
            node = node.value
        elif isinstance(node, ast.Name):
            out.append((node.id, pending))
            break
        else:
            out.append(("?", pending))
            break
    out.reverse()
    return out


def _arg(call, index: int = 0, names=()) -> Optional[ast.AST]:
    if call is None:
        return None
    if len(call.args) > index and not isinstance(call.args[index], ast.Starred):
        return call.args[index]
    for k in call.keywords:
        if k.arg in names:
            return k.value
    return None


def render(text: str, holes: list) -> str:
    """Put the unresolved expressions back for display: __dyn0__ -> {expr}."""
    return re.sub(rf"{DYN}(\d+)__", lambda m: "{" + holes[int(m.group(1))] + "}"
                  if int(m.group(1)) < len(holes) else "{?}", text)


def split_dotted(value: str) -> list:
    parts = split_name(value)
    return parts if parts else [value]


class PythonAnalyzer:
    """One analyzer per notebook: learn() every cell first, then analyze() each cell."""

    def __init__(self, seed: Optional[dict] = None):
        self.strs: dict[str, list] = {}
        self.lists: dict[str, list] = {}
        self.tuples: dict[str, list] = {}
        self.dicts: dict[str, list] = {}
        self.dict_nodes: dict[str, ast.Dict] = {}
        self.functions: dict[str, ast.FunctionDef] = {}
        self.temp_views: set = set()
        self.reader_vars: set = set()
        self.writer_vars: set = set()
        self.delta_vars: dict[str, list] = {}
        self.top_names: set = set()
        for k, v in (seed or {}).items():
            self._add(k, v if isinstance(v, list) else [v])

    # ------------------------------------------------------------------ values
    def _add(self, name: str, values) -> None:
        cur = self.strs.setdefault(name, [])
        for v in values:
            if isinstance(v, str) and v not in cur and len(cur) < MAX_VALUES:
                cur.append(v)

    def exports(self) -> dict:
        """Constants other notebooks can see after %run this notebook: top-level assignments only."""
        return {k: list(v) for k, v in self.strs.items()
                if k in self.top_names and v and len(v) <= 5 and not k.startswith("_")}

    def resolve(self, node, depth: int = 0) -> Optional[list]:
        """Every string value the expression can have, or None if that can't be worked out."""
        if node is None or depth > 10:
            return None
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return [node.value]
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return [str(node.value)]
            return None
        if isinstance(node, ast.JoinedStr):
            pieces = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    pieces.append([str(v.value)])
                elif isinstance(v, ast.FormattedValue):
                    r = self.resolve(v.value, depth + 1)
                    if r is None:
                        return None
                    pieces.append(r)
                else:
                    return None
            return _product(pieces)
        if isinstance(node, ast.Name):
            vals = self.strs.get(node.id)
            return list(vals) if vals else None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.resolve(node.left, depth + 1), self.resolve(node.right, depth + 1)
            return _product([left, right]) if left is not None and right is not None else None
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "str" and len(node.args) == 1:
                return self.resolve(node.args[0], depth + 1)
            if not isinstance(func, ast.Attribute):
                return None
            attr = func.attr
            if attr == "format":
                base = self.resolve(func.value, depth + 1)
                args = [self.resolve(a, depth + 1) for a in node.args]
                kws = {k.arg: self.resolve(k.value, depth + 1) for k in node.keywords if k.arg}
                if base is None or any(a is None or len(a) != 1 for a in args) or \
                        any(v is None or len(v) != 1 for v in kws.values()):
                    return None
                try:
                    return [b.format(*[a[0] for a in args], **{k: v[0] for k, v in kws.items()}) for b in base]
                except (IndexError, KeyError, ValueError):
                    return None
            if attr in ("lower", "upper", "strip", "lstrip", "rstrip") and not node.args:
                base = self.resolve(func.value, depth + 1)
                return [getattr(b, attr)() for b in base] if base is not None else None
            if attr == "join" and _dotted(func.value) in ("os.path", "posixpath", "path"):
                parts = [self.resolve(a, depth + 1) for a in node.args]
                if any(p is None or len(p) != 1 for p in parts) or not parts:
                    return None
                return [posixpath.join(*[p[0] for p in parts])]
            if attr == "join" and isinstance(func.value, ast.Constant) and isinstance(func.value.value, str) \
                    and len(node.args) == 1:
                items = self._iter_items(node.args[0])
                if items and all(isinstance(i, str) for i in items):
                    return [func.value.value.join(items)]
        return None

    def template(self, node) -> tuple[str, list]:
        """The expression as text with unresolved parts replaced by __dynN__ placeholders."""
        holes: list = []

        def t(n):
            r = self.resolve(n)
            if r is not None and len(r) == 1:
                return r[0]
            if isinstance(n, ast.JoinedStr):
                out = []
                for v in n.values:
                    if isinstance(v, ast.Constant):
                        out.append(str(v.value))
                    elif isinstance(v, ast.FormattedValue):
                        out.append(t(v.value))
                return "".join(out)
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
                return t(n.left) + t(n.right)
            holes.append(_unparse(n))
            return f"{DYN}{len(holes) - 1}__"

        return t(node), holes

    def _iter_items(self, node) -> Optional[list]:
        """Items of something iterated over: strings, or tuples of strings."""
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            out = []
            for e in node.elts:
                if isinstance(e, ast.Tuple):
                    comp = [self.resolve(x) for x in e.elts]
                    if any(c is None or len(c) != 1 for c in comp):
                        return None
                    out.append(tuple(c[0] for c in comp))
                else:
                    r = self.resolve(e)
                    if r is None:
                        return None
                    out.extend(r)
            return out
        if isinstance(node, ast.Name):
            if node.id in self.lists:
                return list(self.lists[node.id])
            if node.id in self.tuples:
                return list(self.tuples[node.id])
            if node.id in self.dicts:
                return [k for k, _ in self.dicts[node.id]]
            return None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and not node.args:
            pairs = None
            if isinstance(node.func.value, ast.Name):
                pairs = self.dicts.get(node.func.value.id)
            elif isinstance(node.func.value, ast.Dict):
                pairs = self._dict_pairs(node.func.value)
            if pairs is not None:
                if node.func.attr == "items":
                    return [tuple(p) for p in pairs]
                if node.func.attr == "keys":
                    return [k for k, _ in pairs]
                if node.func.attr == "values":
                    return [v for _, v in pairs]
        if isinstance(node, ast.Dict):
            pairs = self._dict_pairs(node)
            return [k for k, _ in pairs] if pairs is not None else None
        return None

    def _dict_pairs(self, node: ast.Dict) -> Optional[list]:
        pairs = []
        for k, v in zip(node.keys, node.values):
            kr, vr = self.resolve(k), self.resolve(v)
            if kr is None or len(kr) != 1:
                return None
            if vr is not None and len(vr) == 1:
                pairs.append((kr[0], vr[0]))
        return pairs

    # ------------------------------------------------------------------ pass 1: learn
    def learn_functions(self, tree) -> None:
        for node in getattr(tree, "body", []):
            targets = node.targets if isinstance(node, ast.Assign) else \
                [node.target] if isinstance(node, ast.AnnAssign) else []
            for t in targets:
                for n in ([t] if isinstance(t, ast.Name) else getattr(t, "elts", [])):
                    if isinstance(n, ast.Name):
                        self.top_names.add(n.id)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node

    def learn(self, tree) -> None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args.args + node.args.kwonlyargs
                defaults = [None] * (len(node.args.args) - len(node.args.defaults)) + list(node.args.defaults)
                defaults += list(node.args.kw_defaults)
                for a, d in zip(args, defaults):
                    if d is not None:
                        vals = self.resolve(d)
                        if vals:
                            self._add(a.arg, vals)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    self._bind(target, node.value)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                self._bind(node.target, node.value)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                self._bind_loop(node.target, node.iter)
            elif isinstance(node, ast.Call):
                self._learn_call(node)

    def _bind(self, target, value) -> None:
        if isinstance(target, ast.Name):
            name = target.id
            if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                items = self._iter_items(value)
                if items:
                    if all(isinstance(i, str) for i in items):
                        self.lists[name] = items
                    elif all(isinstance(i, tuple) for i in items):
                        self.tuples[name] = items
                return
            if isinstance(value, ast.Dict):
                self.dict_nodes[name] = value
                pairs = self._dict_pairs(value)
                if pairs:
                    self.dicts[name] = pairs
                return
            if isinstance(value, ast.Call):
                names = [n for n, _ in _chain(value)]
                if any(n in ("read", "readStream") for n in names):
                    self.reader_vars.add(name)
                if any(n in ("write", "writeStream") for n in names):
                    self.writer_vars.add(name)
            vals = self.resolve(value)
            if vals:
                self._add(name, vals)
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) \
                and len(target.elts) == len(value.elts):
            for t, v in zip(target.elts, value.elts):
                self._bind(t, v)

    def _bind_loop(self, target, iterable) -> None:
        items = self._iter_items(iterable)
        if not items:
            return
        if isinstance(target, ast.Name):
            self._add(target.id, [i for i in items if isinstance(i, str)])
        elif isinstance(target, (ast.Tuple, ast.List)):
            for i, elt in enumerate(target.elts):
                if isinstance(elt, ast.Name):
                    self._add(elt.id, [t[i] for t in items if isinstance(t, tuple) and len(t) > i])

    def _learn_call(self, call: ast.Call) -> None:
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr in TEMP_VIEW_METHODS:
            for v in self.resolve(_arg(call, 0, ("viewName", "name"))) or []:
                self.temp_views.add(v.lower())
        if isinstance(func, ast.Name) and func.id in self.functions:
            fn = self.functions[func.id]
            params = [a.arg for a in fn.args.args]
            for i, a in enumerate(call.args):
                if i < len(params) and not isinstance(a, ast.Starred):
                    vals = self.resolve(a)
                    if vals:
                        self._add(params[i], vals)
            for k in call.keywords:
                if k.arg:
                    vals = self.resolve(k.value)
                    if vals:
                        self._add(k.arg, vals)

    # ------------------------------------------------------------------ pass 2: analyze
    def analyze(self, tree) -> list:
        parents = {}
        for p in ast.walk(tree):
            for c in ast.iter_child_nodes(p):
                parents[c] = p
        assigned = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                assigned[id(node.value)] = node.targets[0].id
        calls = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Call)),
                       key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0)))
        refs: list = []
        for call in calls:
            refs.extend(self._call(call, parents, assigned))
        return refs

    def _outer_methods(self, call, parents) -> list:
        out, node = [], call
        while True:
            p = parents.get(node)
            if isinstance(p, ast.Attribute) and p.value is node:
                out.append(p.attr)
                node = p
            elif isinstance(p, ast.Call) and p.func is node:
                node = p
            else:
                return out

    def _tables(self, node, op, verb, line, kind="table", note="") -> list:
        if node is None:
            return []
        vals = self.resolve(node)
        if vals is not None:
            many = f"one of {len(vals)} values" if len(vals) > 1 else ""
            return [CodeRef(kind, op, verb, parts=split_dotted(v.strip()), line=line, note=note or many)
                    for v in vals if v.strip()]
        text, holes = self.template(node)
        return [CodeRef(kind, op, verb, parts=split_dotted(text), line=line, dynamic=True,
                        shown=render(text, holes))]

    def _paths(self, node, op, verb, line, strict=False) -> list:
        """Paths: lakehouse / OneLake / external storage. strict: skip anything not recognisably one."""
        if node is None:
            return []
        vals = self.resolve(node)
        if vals is not None:
            out = []
            for v in vals:
                loc = parse_location(v)
                if loc is None:
                    continue
                out.append(CodeRef("path", op, verb, path=v, line=line,
                                   note=f"one of {len(vals)} values" if len(vals) > 1 else ""))
            return out
        if strict:
            return []
        text, holes = self.template(node)
        return [CodeRef("path", op, verb, path=text, line=line, dynamic=True, shown=render(text, holes))]

    def _sql(self, node, line, paths_only=False) -> list:
        vals = self.resolve(node)
        out = []
        if vals is not None:
            for v in vals:
                for r in sql_refs(v, paths_only=paths_only, local_names=self.temp_views):
                    out.append(self._from_sql(r, line, note=f"one of {len(vals)} values" if len(vals) > 1 else ""))
        else:
            text, holes = self.template(node)
            for r in sql_refs(text, paths_only=paths_only, local_names=self.temp_views):
                ref = self._from_sql(r, line)
                if r.dynamic or DYN in r.path:
                    ref.dynamic = True
                    ref.shown = render(".".join(r.parts) if r.parts else r.path, holes)
                out.append(ref)
        seen, unique = set(), []
        for r in out:
            key = (r.kind, r.op, r.verb, tuple(p.lower() for p in r.parts), r.path, r.line)
            if key not in seen:
                seen.add(key)
                unique.append(r)
        return unique

    @staticmethod
    def _from_sql(r, line, note="") -> CodeRef:
        if r.op == "use":
            return CodeRef("use", "use", "USE", parts=r.parts, line=line + r.line)
        if r.path:
            return CodeRef("path", r.op, r.verb, path=r.path, line=line + r.line, note=note)
        return CodeRef("table", r.op, r.verb, parts=r.parts, line=line + r.line, dynamic=r.dynamic, note=note)

    def _dag_items(self, node) -> list:
        """runMultiple(...) argument -> [(notebook, workspace)]"""
        if isinstance(node, ast.Name) and node.id in self.dict_nodes:
            node = self.dict_nodes[node.id]
        if isinstance(node, ast.Name) and node.id in self.lists:
            return [(v, "") for v in self.lists[node.id]]
        if isinstance(node, (ast.List, ast.Tuple)):
            out = []
            for e in node.elts:
                if isinstance(e, ast.Dict):
                    out.extend(self._dag_items(ast.Dict(keys=[ast.Constant("activities")],
                                                         values=[ast.List(elts=[e], ctx=ast.Load())])))
                else:
                    out.extend((v, "") for v in self.resolve(e) or [])
            return out
        if isinstance(node, ast.Dict):
            acts = None
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "activities":
                    acts = v
            if isinstance(acts, ast.Name) and acts.id in self.dict_nodes:
                acts = self.dict_nodes[acts.id]
            out = []
            for a in getattr(acts, "elts", []) or []:
                if not isinstance(a, ast.Dict):
                    continue
                fields = {k.value: v for k, v in zip(a.keys, a.values) if isinstance(k, ast.Constant)}
                ws = (self.resolve(fields.get("workspace")) or [""])[0] if fields.get("workspace") is not None else ""
                for p in self.resolve(fields.get("path")) or []:
                    out.append((p, ws))
            return out
        return []

    def _call(self, call: ast.Call, parents, assigned) -> list:
        ch = _chain(call)
        names = [n for n, _ in ch]
        if not names:
            return []
        method, root = names[-1], names[0]
        line = getattr(call, "lineno", 0)
        ctx_write = any(n in ("write", "writeStream") for n in names[:-1]) or root in self.writer_vars
        ctx_read = any(n in ("read", "readStream") for n in names[:-1]) or root in self.reader_vars
        fmt, options = "", {}
        for n, c in ch:
            if c is None:
                continue
            if n == "format":
                v = self.resolve(_arg(c, 0, ("source",)))
                fmt = v[0].lower() if v and len(v) == 1 else fmt
            elif n == "option" and len(c.args) >= 2:
                k = self.resolve(c.args[0])
                if k and len(k) == 1:
                    options[k[0].lower()] = c.args[1]
            elif n == "options":
                for kw in c.keywords:
                    if kw.arg:
                        options[kw.arg.lower()] = kw.value

        # spark.sql("..."), duckdb.sql("...") (paths only)
        if method == "sql" and len(names) >= 2:
            if root in SPARK_ROOTS or "sparkSession" in names:
                return self._sql(_arg(call, 0, ("sqlQuery", "query")), line)
            return self._sql(_arg(call, 0, ("query", "sql_query")), line, paths_only=True)
        if method in ("execute", "query") and root in ("duckdb", "con", "conn", "connection"):
            return self._sql(_arg(call, 0, ("query",)), line, paths_only=True)
        if method == "setCurrentDatabase" and "catalog" in names:
            return [CodeRef("use", "use", "setCurrentDatabase", parts=p.parts, line=line)
                    for p in self._tables(_arg(call, 0, ("dbName",)), "use", "use", line) if not p.dynamic]

        # tables
        if method == "table" and ((root in SPARK_ROOTS and len(names) == 2) or ctx_read):
            return self._tables(_arg(call, 0, ("tableName",)), "read", "spark.read.table", line)
        for n, c in ch:
            if n == "writeTo" and c is not None and n != method:
                return self._tables(_arg(c, 0, ("table",)), "write", "writeTo", line)
        if method == "writeTo":
            return []                       # handled when the chain's last call is analyzed
        if method in ("saveAsTable", "insertInto") and (ctx_write or len(names) > 1):
            return self._tables(_arg(call, 0, ("name", "tableName")), "write", method, line)
        if method == "toTable" and ctx_write:
            return self._tables(_arg(call, 0, ("tableName",)), "write", "writeStream.toTable", line)
        if method == "to_lakehouse_table":
            return self._tables(_arg(call, 0, ("name",)), "write", "to_lakehouse_table", line)

        # Delta Lake (pyspark): DeltaTable.forName / forPath, builders
        if root == "DeltaTable" and method in ("forName", "forPath"):
            outer = self._outer_methods(call, parents)
            op = "write" if DELTA_WRITE & set(outer) else "maintain" if DELTA_MAINTAIN & set(outer) else "read"
            verb = f"DeltaTable.{method}" + (f".{next(m for m in outer if m in DELTA_WRITE | DELTA_MAINTAIN)}"
                                             if op != "read" else "")
            if method == "forName":
                refs = self._tables(_arg(call, 1, ("tableOrViewName",)), op, verb, line)
            else:
                refs = self._paths(_arg(call, 1, ("path",)), op, verb, line)
            var = assigned.get(id(call))
            if var:
                self.delta_vars[var] = refs
            return refs
        if root == "DeltaTable" and method == "tableName":
            return self._tables(_arg(call, 0, ("identifier",)), "write", "DeltaTable.create", line)
        if root == "DeltaTable" and method == "location":
            return self._paths(_arg(call, 0, ("location",)), "write", "DeltaTable.create", line)
        if root in self.delta_vars and len(names) > 1:
            used = set(names[1:])
            for r in self.delta_vars[root]:
                base = ".".join(r.verb.split(".")[:2])
                if used & DELTA_WRITE and r.op != "write":
                    r.op, r.verb = "write", f"{base}.{sorted(used & DELTA_WRITE)[0]}"
                elif used & DELTA_MAINTAIN and r.op == "read":
                    r.op, r.verb = "maintain", f"{base}.{sorted(used & DELTA_MAINTAIN)[0]}"
            return []

        # deltalake (delta-rs) and pandas / polars in Python notebooks
        if method == "write_deltalake" or names == ["write_deltalake"]:
            return self._paths(_arg(call, 0, ("table_or_uri",)), "write", "write_deltalake", line, strict=True)
        if names == ["DeltaTable"]:
            return self._paths(_arg(call, 0, ("table_uri",)), "read", "DeltaTable", line, strict=True)
        if root in PANDAS_ROOTS and (method.startswith("read_") or method.startswith("scan_")):
            return self._paths(_arg(call, 0, ("path", "filepath_or_buffer", "source")), "read",
                               f"{root}.{method}", line, strict=True)
        if method in PANDAS_WRITERS and len(names) > 1 and not ctx_write:
            return self._paths(_arg(call, 0, ("path", "path_or_buf", "file", "target")), "write", method, line,
                               strict=True)

        # files and other formats through spark.read / df.write
        if method in ("save", "start") and ctx_write:
            node = _arg(call, 0, ("path",)) or options.get("path")
            return self._paths(node, "write", "write.save", line) if node is not None else []
        if method == "load" and ctx_read:
            if fmt == "jdbc" or "url" in options or "dbtable" in options:
                url = (self.resolve(options.get("url")) or [""])[0] if "url" in options else ""
                table = (self.resolve(options.get("dbtable")) or [""])[0] if "dbtable" in options else ""
                return [CodeRef("external", "read", "jdbc", path=url or "jdbc", target=table, line=line)]
            node = _arg(call, 0, ("path",)) or options.get("path")
            return self._paths(node, "read", "spark.read.load", line) if node is not None else []
        if method in FILE_FORMATS and (ctx_write or ctx_read):
            op = "write" if ctx_write else "read"
            return self._paths(_arg(call, 0, ("path",)), op, f"{'write' if ctx_write else 'read'}.{method}", line)
        if method == "jdbc" and (ctx_write or ctx_read):
            url = (self.resolve(_arg(call, 0, ("url",))) or ["jdbc"])[0]
            table = (self.resolve(_arg(call, 1, ("table",))) or [""])[0]
            return [CodeRef("external", "write" if ctx_write else "read", "jdbc", path=url, target=table, line=line)]
        if method == "synapsesql":
            return self._tables(_arg(call, 0, ("tableName",)), "write" if ctx_write else "read", "synapsesql",
                                line, kind="warehouse_table")

        # notebookutils / mssparkutils
        if root in UTIL_ROOTS or (root == "notebook" and method in ("run", "runMultiple")):
            if "notebook" in names and method == "run":
                target = _arg(call, 0, ("path", "name"))
                ws_node = _arg(call, 3, ("workspace", "workspaceId", "workspace_id"))
                ws = (self.resolve(ws_node) or [""])[0] if ws_node is not None else ""
                vals = self.resolve(target)
                if vals:
                    return [CodeRef("notebook", "run", "notebook.run", target=v, workspace=ws, line=line) for v in vals]
                text, holes = self.template(target) if target is not None else ("", [])
                return [CodeRef("notebook", "run", "notebook.run", target=text, line=line, dynamic=True,
                                shown=render(text, holes))]
            if "notebook" in names and method == "runMultiple":
                items = self._dag_items(_arg(call, 0, ("dag",)))
                if items:
                    return [CodeRef("notebook", "run", "notebook.runMultiple", target=p, workspace=w, line=line)
                            for p, w in items]
                return [CodeRef("notebook", "run", "notebook.runMultiple", line=line, dynamic=True,
                                shown=_unparse(_arg(call, 0, ("dag",))) if _arg(call, 0, ("dag",)) else "?")]
            if "fs" in names:
                if method in ("cp", "fastcp", "mv"):
                    return (self._paths(_arg(call, 0, ("src", "from")), "read", f"fs.{method}", line, strict=True)
                            + self._paths(_arg(call, 1, ("dest", "to")), "write", f"fs.{method}", line, strict=True))
                if method in ("rm", "put", "append"):
                    return self._paths(_arg(call, 0, ("file", "dir", "path")), "write", f"fs.{method}", line,
                                       strict=True)
                if method in ("head", "ls"):
                    return self._paths(_arg(call, 0, ("file", "dir", "path")), "read", f"fs.{method}", line,
                                       strict=True)
            return []

        # semantic link (sempy)
        if root == "fabric" or names[:2] == ["sempy", "fabric"]:
            ws_node = None
            for k in call.keywords:
                if k.arg == "workspace":
                    ws_node = k.value
            ws = (self.resolve(ws_node) or [""])[0] if ws_node is not None else ""
            if method in SEMPY_READS or method == "refresh_dataset":
                ds = _arg(call, 0, ("dataset",))
                vals = self.resolve(ds)
                op = "refresh" if method == "refresh_dataset" else "read"
                note = ""
                if method == "read_table":
                    t = self.resolve(_arg(call, 1, ("table",)))
                    note = f"table {t[0]}" if t and len(t) == 1 else ""
                if vals:
                    return [CodeRef("model", op, f"fabric.{method}", target=v, workspace=ws, line=line, note=note)
                            for v in vals]
                if ds is not None:
                    text, holes = self.template(ds)
                    return [CodeRef("model", op, f"fabric.{method}", target=text, line=line, dynamic=True,
                                    shown=render(text, holes))]
        return []


# ---------------------------------------------------------------------- regex fallback
_RX_STR = r"(?P<q>\"\"\"|'''|\"|')(?P<s>.*?)(?P=q)"
_RX_TABLE_WRITE = re.compile(r"\.\s*(?P<m>saveAsTable|insertInto|toTable|writeTo)\s*\(\s*[fFrRs]{0,2}" + _RX_STR, re.S)
_RX_TABLE_READ = re.compile(r"(?:\bspark\s*\.\s*(?:read(?:Stream)?\s*\.\s*)?|\.\s*read(?:Stream)?\s*\.\s*)"
                            r"table\s*\(\s*[fFrRs]{0,2}" + _RX_STR, re.S)
_RX_PATH = re.compile(r"\.\s*(?P<m>load|save|parquet|csv|json|orc|text|delta|start)\s*\(\s*[fFrRs]{0,2}" + _RX_STR,
                      re.S)
_RX_RUN = re.compile(r"\b(?:mssparkutils|notebookutils)\s*\.\s*notebook\s*\.\s*run\s*\(\s*[fFrRs]{0,2}" + _RX_STR,
                     re.S)
_RX_SQL = re.compile(r"\.\s*sql\s*\(\s*[fFrRs]{0,2}" + _RX_STR, re.S)


def regex_refs(code: str) -> list:
    """Best effort for Scala / R cells and Python that doesn't parse."""
    refs: list = []

    def line_of(pos):
        return code.count("\n", 0, pos) + 1

    def dyn(s):
        return "{" in s or "$" in s

    for m in _RX_TABLE_WRITE.finditer(code):
        s = m.group("s")
        refs.append(CodeRef("table", "write", m.group("m"), parts=split_dotted(s), line=line_of(m.start()),
                            dynamic=dyn(s), shown=s if dyn(s) else ""))
    for m in _RX_TABLE_READ.finditer(code):
        s = m.group("s")
        refs.append(CodeRef("table", "read", "spark.read.table", parts=split_dotted(s), line=line_of(m.start()),
                            dynamic=dyn(s), shown=s if dyn(s) else ""))
    for m in _RX_PATH.finditer(code):
        s, meth = m.group("s"), m.group("m")
        before = code[max(0, m.start() - 400):m.start()]
        stmt = re.split(r"\n\s*\n|;", before)[-1]
        w, r = max(stmt.rfind(".write"), stmt.rfind("writeStream")), max(stmt.rfind(".read"), stmt.rfind("readStream"))
        if w > r:
            op = "write"
        elif r > w:
            op = "read"
        else:
            op = {"save": "write", "start": "write", "load": "read"}.get(meth, "")
        if not op or (not dyn(s) and parse_location(s) is None):
            continue
        refs.append(CodeRef("path", op, f"{op}.{meth}", path=s, line=line_of(m.start()), dynamic=dyn(s),
                            shown=s if dyn(s) else ""))
    for m in _RX_RUN.finditer(code):
        s = m.group("s")
        refs.append(CodeRef("notebook", "run", "notebook.run", target=s, line=line_of(m.start()), dynamic=dyn(s),
                            shown=s if dyn(s) else ""))
    for m in _RX_SQL.finditer(code):
        s = m.group("s")
        if dyn(s):
            s = re.sub(r"\$\{[^}]*\}|\$\w+|\{[^}]*\}", lambda x: f"{DYN}0__", s)
        for r in sql_refs(s):
            ref = PythonAnalyzer._from_sql(r, line_of(m.start()))
            if r.dynamic:
                ref.shown = ".".join(r.parts).replace(f"{DYN}0__", "{…}")
            refs.append(ref)
    refs.sort(key=lambda r: r.line)
    return refs
