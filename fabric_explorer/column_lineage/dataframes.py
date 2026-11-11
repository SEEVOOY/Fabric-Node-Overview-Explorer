"""A notebook's cells, in the order they run -> what the notebook does to columns (model.Program).

Python cells are followed DataFrame by DataFrame:

    sources   spark.read.table / spark.table / spark.read.<format>(path) / spark.sql / DeltaTable.toDF,
              pd.read_* and pl.read_* on lakehouse paths, duckdb.sql(... delta_scan('...') ...)
    steps     select, selectExpr, withColumn(s), withColumnRenamed, drop, toDF, alias, filter / where,
              distinct / dropDuplicates, limit, fillna / na.fill, replace, join, union(ByName),
              groupBy / agg / count / sum ..., transform, and the pandas equivalents
    writes    saveAsTable, insertInto, save / parquet / ... on a lakehouse path, writeTo(...).append(),
              toTable, synapsesql, DeltaTable merges and updates, write_deltalake, to_parquet / to_csv
    SQL       spark.sql(...), %%sql cells and temporary views go through sql.SqlReader

Names are worked out with the same reader the map uses (parse.python_code.PythonAnalyzer). Values
are followed in the order the code runs: a later assignment replaces an earlier one. Loops over a
known list run once per item; a loop over something unknown (for c in df.columns) is read once,
as applying to every column. The notebook's own helper functions are read through, so their
definitions point at the helper's code.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field, replace
from typing import Optional

from ..parse.notebook import NotebookInfo, _prepare_python
from ..parse.python_code import SPARK_ROOTS, PythonAnalyzer, split_dotted
from .catalog import COLUMN, FUNCTIONS, ROW
from .expressions import (DATAFRAME_ATTRS, F_MODULES, Expr, ExprContext, PyExprReader, Ref,
                          sql_expression_text)
from .model import Assignment, Builder, Dep, Entry, Program, Sink, TableResolver
from .sql import SqlReader

MAX_LOOP = 40
MAX_INLINE = 4
MAX_COMPREHENSION = 300

# ---------------------------------------------------------------- what a Python value can be
@dataclass(frozen=True)
class FrameVal:
    fid: int


@dataclass
class ReaderVal:                                   # spark.read / spark.readStream, with its format and options
    fmt: str = ""
    path: Optional[ast.AST] = None


@dataclass
class WriterVal:                                   # df.write / df.writeStream / df.writeTo(name)
    fid: int
    mode: str = ""
    fmt: str = ""
    path: Optional[ast.AST] = None
    table: list = field(default_factory=list)      # writeTo(name)
    v2: bool = False


@dataclass(frozen=True)
class NaVal:                                       # df.na
    fid: int


@dataclass
class GroupVal:                                    # df.groupBy(...)
    fid: int
    keys: list                                     # [(Expr, node)]
    node: ast.AST
    pivot: Optional[tuple] = None                  # (Expr, node)


@dataclass
class DeltaVal:                                    # DeltaTable.forName / forPath (pyspark) or DeltaTable(path)
    ref: object
    alias: str = ""


@dataclass
class MergeVal:                                    # DeltaTable.merge(...) and its when... clauses
    target: DeltaVal
    src: int
    cond: Optional[ast.AST]
    node: ast.AST
    assignments: list = field(default_factory=list)
    every: bool = False


@dataclass
class ExprVal:                                     # a column expression held in a variable
    expr: Expr


@dataclass
class ListVal:                                     # a list of column expressions: select(*cols)
    items: list                                    # [(Expr, node)]


@dataclass(frozen=True)
class DuckVal:                                     # duckdb.connect()
    pass


@dataclass(frozen=True)
class CatalogVal:                                  # spark.catalog
    pass


PASSTHROUGH = {"cache", "persist", "unpersist", "repartition", "repartitionByRange", "coalesce", "hint", "checkpoint",
               "localCheckpoint", "orderBy", "sort", "sortWithinPartitions", "withWatermark", "toPandas", "to_pandas",
               "pandas_api", "to_pandas_on_spark", "to_spark", "observe", "sort_values", "sort_index", "reset_index",
               "copy", "df", "pl", "arrow", "fetchdf", "fetch_df", "to_df", "to_arrow_table", "set_index",
               "infer_objects", "convert_dtypes", "lazy", "collect", "withMetadata", "to_polars", "fetch_arrow_table",
               "persist_pandas", "toLocalIterator_df"}
ACTIONS = {"show", "count", "take", "first", "describe", "summary", "printSchema", "explain", "foreach",
           "foreachPartition", "toLocalIterator", "isEmpty", "isLocal", "inputFiles", "toJSON", "display", "info",
           "createOrReplaceTempView", "createTempView", "createGlobalTempView", "createOrReplaceGlobalTempView",
           "registerTempTable", "to_dict", "to_list", "tolist", "to_numpy", "plot", "printschema", "sameSemantics"}
TEMP_VIEWS = {"createOrReplaceTempView", "createTempView", "createOrReplaceGlobalTempView", "createGlobalTempView",
              "registerTempTable"}
FILE_FORMATS = {"parquet", "csv", "json", "orc", "text", "delta", "avro", "xml", "excel"}
PANDAS_WRITES = {"to_parquet", "to_csv", "to_json", "to_excel", "to_feather", "write_delta", "write_parquet",
                 "write_csv", "write_json"}
SKIP_CALLS = {"display", "print", "len", "str", "int", "float", "list", "set", "dict", "range", "isinstance", "type",
              "getattr", "setattr", "hasattr", "repr", "sorted", "enumerate", "zip", "max", "min", "sum", "any",
              "all", "open", "round", "abs", "tuple", "next", "iter", "format", "bool"}


def pretty(text: str) -> str:
    """F.upper(F.trim(F.col("TagNo"))).alias("Tag") -> upper(trim(TagNo)) AS Tag"""
    t = re.sub(r"\b(?:F|f|fn|fns|func|funcs|functions|sf|psf|SF|fx|pyspark\.sql\.functions)\.", "", text)
    t = re.sub(r"\bcol(?:umn)?\(\s*(['\"])([^'\"]*)\1\s*\)", r"\2", t)
    t = re.sub(r"\bcol(?:umn)?\(\s*([A-Za-z_]\w*)\s*\)", r"\1", t)           # col(c): a loop's column
    t = re.sub(r"\bexpr\(\s*(['\"])(.*?)\1\s*\)", r"\2", t)                    # expr("upper(x)"): the SQL
    t = re.sub(r"\blit\(\s*([^()]*?)\s*\)", r"\1", t)
    t = re.sub(r"\.(?:alias|name)\(\s*(['\"])([^'\"]*)\1\s*\)", r" AS \2", t)
    return " ".join(t.split())


def _slice(src: str, l1: int, c1: int, l2: int, c2: int) -> str:
    """Source text between two AST positions (columns are UTF-8 byte offsets, as the ast module gives them)."""
    lines = src.splitlines(True)
    if not (0 < l1 <= len(lines) and 0 < l2 <= len(lines)):
        return ""
    if l1 == l2:
        return lines[l1 - 1].encode("utf-8")[c1:c2].decode("utf-8", "replace")
    out = [lines[l1 - 1].encode("utf-8")[c1:].decode("utf-8", "replace")]
    out += lines[l1:l2 - 1]
    out.append(lines[l2 - 1].encode("utf-8")[:c2].decode("utf-8", "replace"))
    return "".join(out)


class NotebookReader(ExprContext):
    def __init__(self, resolve_table: TableResolver, seed: Optional[dict] = None, imported: Optional[dict] = None):
        self.b = Builder(resolve_table)
        self.pa = PythonAnalyzer(seed)
        self.sqlr = SqlReader(self.b, self._sql_frame)
        self.px = PyExprReader(self)
        self.scopes: list = [{}]
        self.fn_alias: dict = {}
        self.star_import = False
        self.modules = set(F_MODULES)
        self.udfs: set = set()
        self.helpers: dict = {}                     # name -> (FunctionDef, cell, cell source, node ids, %run notebook)
        self.imported = dict(imported or {})        # functions of the notebooks it %runs
        self.wild: set = set()
        self.src = ""
        self.node_ids: set = set()
        self.stmt: Optional[ast.AST] = None
        self.depth = 0
        self.returned = None

    # ================================================================ the notebook
    def read(self, info: NotebookInfo) -> Program:
        cells = []
        for cell in info.cells:
            if cell.kind != "code" or not cell.source.strip():
                continue
            if cell.language == "python":
                code, _ = _prepare_python(cell.source)
                try:
                    tree = ast.parse(code)
                except SyntaxError:
                    self.b.cell = cell.index
                    self.b.note(f"Cell {cell.index} isn't valid Python, so its columns can't be followed.")
                    continue
                cells.append((cell.index, code, tree))
            elif cell.language == "sql":
                text = "\n".join("" if ln.lstrip().startswith("%%") else ln for ln in cell.source.splitlines())
                cells.append((cell.index, text, None))
            elif cell.language in ("scala", "r"):
                self.b.cell = cell.index
                self.b.note(f"Cell {cell.index} is {cell.language.capitalize()}; only Python and SQL cells are "
                            "followed column by column.")
        trees = [(i, code, t) for i, code, t in cells if t is not None]
        for i, code, tree in trees:
            self.pa.learn_functions(tree)
            self._learn(i, code, tree)
        for _ in range(2):
            for _, _, tree in trees:
                self.pa.learn(tree)
        for name, h in self.imported.items():       # its own functions win over those of a %run notebook
            self.helpers.setdefault(name, h)
        self.b.p.helpers = {n: h for n, h in self.helpers.items() if not h[4]}
        for index, code, tree in cells:
            self.b.cell = index
            if tree is None:
                self.sqlr.run(code, line=1, key="sql:")
            else:
                self.src, self.node_ids = code, {id(n) for n in ast.walk(tree)}
                self.block(tree.body)
        return self.b.p

    def _learn(self, cell: int, code: str, tree) -> None:
        """Imports, the notebook's own functions and its UDFs, before anything runs."""
        ids = {id(n) for n in ast.walk(tree)}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in ("pyspark.sql.functions", "pyspark.pandas"):
                for a in node.names:
                    if a.name == "*":
                        self.star_import = True
                    else:
                        self.fn_alias[a.asname or a.name] = a.name
            elif isinstance(node, ast.ImportFrom) and node.module == "pyspark.sql":
                for a in node.names:
                    if a.name == "functions":
                        self.modules.add(a.asname or a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "pyspark.sql.functions" and a.asname:
                        self.modules.add(a.asname)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                deco = [ast.unparse(d) for d in node.decorator_list]
                if any(re.search(r"\b(?:pandas_)?udf\b", d) for d in deco):
                    self.udfs.add(node.name)
                else:
                    self.helpers[node.name] = (node, cell, code, ids, "")
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                fn = ast.unparse(node.value.func)
                if re.search(r"(?:^|\.)(?:pandas_)?udf$", fn) or fn.endswith("udf.register"):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            self.udfs.add(t.id)

    # ================================================================ ExprContext
    def strings(self, node) -> Optional[list]:
        if isinstance(node, ast.Name) and node.id in self.wild:
            return None
        return self.pa.resolve(node)

    def items(self, node) -> Optional[list]:
        if isinstance(node, ast.Name) and node.id in self.wild:
            return None
        vals = self.pa._iter_items(node)
        return [v for v in vals if isinstance(v, str)] if vals else None

    def lookup(self, name: str):
        top = self.scopes[-1]
        if name in top:
            return top[name]
        return self.scopes[0].get(name)

    def set(self, name: str, value) -> None:
        self.scopes[-1][name] = value

    def frame_var(self, name: str) -> Optional[int]:
        v = self.lookup(name)
        return v.fid if isinstance(v, FrameVal) else None

    def expr_var(self, name: str) -> Optional[Expr]:
        v = self.lookup(name)
        return v.expr if isinstance(v, ExprVal) else None

    def function_alias(self, name: str) -> Optional[str]:
        if name in self.fn_alias:
            return self.fn_alias[name]
        if self.star_import and (name.lower() in FUNCTIONS or name in ("col", "column", "lit", "expr", "when",
                                                                         "broadcast")):
            return name
        return None

    def is_functions_module(self, name: str) -> bool:
        return name in self.modules and self.lookup(name) is None

    def helper(self, name: str):
        h = self.helpers.get(name)
        return h[0] if h else None

    def is_udf(self, name: str) -> bool:
        return name in self.udfs

    def is_wildcard(self, name: str) -> bool:
        return name in self.wild

    def text(self, node) -> str:
        if id(node) in self.node_ids:
            seg = ast.get_source_segment(self.src, node)
            if seg is not None:
                return seg
        try:
            return ast.unparse(node)
        except Exception:  # noqa: BLE001
            return "?"

    def _sql_frame(self, name: str) -> Optional[int]:
        return self.frame_var(name)

    # ================================================================ statements
    def block(self, body) -> None:
        for st in body:
            prev, self.stmt = self.stmt, st
            try:
                self.statement(st)
            except RecursionError:
                raise
            except Exception as e:  # noqa: BLE001 - odd code: say so and carry on with the rest
                self.b.note(f"Cell {self.b.cell}, line {getattr(st, 'lineno', 0)} couldn't be followed "
                            f"({type(e).__name__}).")
            finally:
                self.stmt = prev

    def statement(self, st) -> None:
        if isinstance(st, ast.Assign):
            val = self.value(st.value)
            for target in st.targets:
                self.bind(target, val, st.value)
        elif isinstance(st, ast.AnnAssign) and st.value is not None:
            self.bind(st.target, self.value(st.value), st.value)
        elif isinstance(st, ast.Expr):
            self.value(st.value)
        elif isinstance(st, (ast.For, ast.AsyncFor)):
            self.loop(st)
        elif isinstance(st, ast.If):
            self.block(st.body)
            self.block(st.orelse)
        elif isinstance(st, (ast.With, ast.AsyncWith, ast.While)):
            self.block(st.body)
        elif isinstance(st, ast.Try) or type(st).__name__ == "TryStar":
            self.block(st.body)
            for h in st.handlers:
                self.block(h.body)
            self.block(st.orelse)
            self.block(st.finalbody)
        elif isinstance(st, ast.Return):
            self.returned = self.value(st.value) if st.value is not None else None

    def bind(self, target, val, node) -> None:
        if isinstance(target, ast.Name):
            name = target.id
            self._constant(name, node)
            if val is None:
                e = self.px.read(node) if isinstance(node, (ast.Call, ast.BinOp, ast.Compare, ast.UnaryOp)) else None
                val = ExprVal(e) if e is not None and (e.refs or e.functions) and not e.unknown else None
            self.set(name, val)
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(node, (ast.Tuple, ast.List)) and \
                len(target.elts) == len(node.elts):
            for t, v in zip(target.elts, node.elts):
                self.bind(t, self.value(v), v)
        elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            fv = self.lookup(target.value.id)                     # pdf["new"] = expression (pandas)
            names = self.strings(target.slice)
            if isinstance(fv, FrameVal) and names:
                e = self.px.read(node)
                fid = self.b.frame(entries=[Entry(star=fv.fid, exclude=frozenset([names[0].lower()])),
                                            self.entry(fv.fid, e, node, name=names[0], code=self.text(self.stmt))],
                                   position=(self.b.cell, node.lineno))
                self.set(target.value.id, FrameVal(fid))

    def _constant(self, name: str, node) -> None:
        """Follow constants in the order the code runs: the newest assignment is the one that counts."""
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            items = self.pa._iter_items(node)
            if items is not None:
                if all(isinstance(i, str) for i in items):
                    self.pa.lists[name] = items
                elif all(isinstance(i, tuple) for i in items):
                    self.pa.tuples[name] = items
            return
        if isinstance(node, ast.Dict):
            self.pa.dict_nodes[name] = node
            pairs = self.pa._dict_pairs(node)
            if pairs:
                self.pa.dicts[name] = pairs
            return
        vals = self.pa.resolve(node)
        if vals:
            self.pa.strs[name] = list(vals)

    def _bind_consts(self, target, item) -> list:
        saved = []

        def put(name, value):
            saved.append((name, self.pa.strs.get(name), self.pa.lists.get(name)))
            if isinstance(value, str):
                self.pa.strs[name] = [value]
            self.wild.discard(name)

        if isinstance(target, ast.Name):
            put(target.id, item)
        elif isinstance(target, (ast.Tuple, ast.List)) and isinstance(item, tuple):
            for t, v in zip(target.elts, item):
                if isinstance(t, ast.Name):
                    put(t.id, v)
        return saved

    def _restore(self, saved) -> None:
        for name, strs, lists in reversed(saved):
            if strs is None:
                self.pa.strs.pop(name, None)
            else:
                self.pa.strs[name] = strs
            if lists is not None:
                self.pa.lists[name] = lists

    def loop(self, st) -> None:
        items = self.pa._iter_items(st.iter)
        if not items:
            names = [st.target.id] if isinstance(st.target, ast.Name) else []
            self.wild.update(names)                 # for c in df.columns: every column
            try:
                self.block(st.body)
            finally:
                self.wild.difference_update(names)
            return
        if len(items) > MAX_LOOP:
            self.b.note(f"Cell {self.b.cell}, line {st.lineno}: the loop runs over {len(items)} values; the first "
                        f"{MAX_LOOP} are followed.")
        for item in items[:MAX_LOOP]:
            saved = self._bind_consts(st.target, item)
            try:
                self.block(st.body)
            finally:
                self._restore(saved)

    # ================================================================ values
    def value(self, node):
        if node is None:
            return None
        if isinstance(node, ast.Name):
            return self.lookup(node.id)
        if isinstance(node, ast.Call):
            return self.call(node)
        if isinstance(node, ast.Attribute):
            return self.attribute(node)
        if isinstance(node, ast.Subscript):
            return self.subscript(node)
        if isinstance(node, (ast.ListComp, ast.GeneratorExp)):
            return ListVal(self.comprehension(node))
        if isinstance(node, (ast.List, ast.Tuple)):
            vals = [self.value(e) for e in node.elts]
            if vals and all(isinstance(v, FrameVal) for v in vals):
                return vals
            return None
        if isinstance(node, ast.IfExp):
            return self.value(node.body) or self.value(node.orelse)
        if isinstance(node, ast.Await):
            return self.value(node.value)
        return None

    def attribute(self, node: ast.Attribute):
        base_node = node.value
        if isinstance(base_node, ast.Name) and base_node.id in SPARK_ROOTS and self.lookup(base_node.id) is None:
            if node.attr in ("read", "readStream"):
                return ReaderVal()
            if node.attr == "catalog":
                return CatalogVal()
            return None
        base = self.value(base_node)
        if isinstance(base, FrameVal):
            if node.attr in ("write", "writeStream"):
                return WriterVal(base.fid)
            if node.attr == "na":
                return NaVal(base.fid)
            if node.attr in DATAFRAME_ATTRS:
                return None
            return ExprVal(Expr(refs=[Ref(node.attr, "", base.fid)], text=self.text(node), column=True))
        return None

    def subscript(self, node: ast.Subscript):
        base = self.value(node.value)
        if not isinstance(base, FrameVal):
            return None
        key = node.slice
        if isinstance(key, (ast.List, ast.Tuple)) or (isinstance(key, ast.Name) and key.id in self.pa.lists):
            names = self.items(key)
            if names:                                  # pdf[["a", "b"]]
                return FrameVal(self.b.frame(entries=[Entry(name=n, deps=[Dep(base.fid, n)]) for n in names],
                                             position=(self.b.cell, node.lineno), label="select"))
        names = self.strings(key)
        if names:
            return ExprVal(Expr(refs=[Ref(names[0], "", base.fid)], text=self.text(node), column=True))
        e = self.px.read(key, column_ctx=False)        # df[condition]: a filter
        return FrameVal(self.filter_expr(base.fid, e, node, self.text(node)))

    def call(self, node: ast.Call):
        f = node.func
        if isinstance(f, ast.Name):
            return self.call_name(f.id, node)
        if not isinstance(f, ast.Attribute):
            return None
        m, recv_node = f.attr, f.value
        if isinstance(recv_node, ast.Name) and self.lookup(recv_node.id) is None:
            r = recv_node.id
            if r in SPARK_ROOTS:
                return self.spark_call(m, node)
            if self.is_functions_module(r) or r == "Window":
                if m == "broadcast" and node.args:
                    return self.value(node.args[0])
                return ExprVal(self.px.read(node))
            if r == "DeltaTable":
                return self.delta_static(m, node)
            if r == "duckdb":
                return self.duck_call(m, node)
            if r in ("pd", "pandas", "pl", "polars"):
                return self.pandas_call(m, node)
            if r in ("notebookutils", "mssparkutils", "dbutils", "fabric", "sempy", "logging", "logger", "os",
                     "json", "time", "datetime"):
                return None
        recv = self.value(recv_node)
        if isinstance(recv, FrameVal):
            return self.frame_call(recv.fid, m, node)
        if isinstance(recv, ReaderVal):
            return self.reader_call(recv, m, node)
        if isinstance(recv, WriterVal):
            return self.writer_call(recv, m, node)
        if isinstance(recv, NaVal):
            return self.na_call(recv.fid, m, node)
        if isinstance(recv, GroupVal):
            return self.group_call(recv, m, node)
        if isinstance(recv, DeltaVal):
            return self.delta_call(recv, m, node)
        if isinstance(recv, MergeVal):
            return self.merge_call(recv, m, node)
        if isinstance(recv, DuckVal) and m in ("sql", "query", "execute"):
            return self.sql_call(node, duck=True)
        if isinstance(recv, CatalogVal) and m == "setCurrentDatabase":
            vals = self.strings(node.args[0]) if node.args else None
            if vals:
                self.b.database = vals[0]
            return None
        if isinstance(recv, ExprVal):
            return ExprVal(self.px.read(node))
        return None

    def call_name(self, name: str, node: ast.Call):
        if name in SKIP_CALLS or self.lookup(name) is not None and not isinstance(self.lookup(name), ExprVal):
            return None
        if name == "write_deltalake":
            self.write_deltalake(node)
            return None
        if name == "DeltaTable":                       # deltalake (delta-rs): DeltaTable(path)
            path = self.strings(node.args[0]) if node.args else None
            ref = self.b.table([], path[0]) if path else None
            return DeltaVal(ref) if ref is not None else None
        if name in self.helpers and name not in self.udfs:
            if self.depth < MAX_INLINE:
                out = self.inline(name, node)
                if out is not None:
                    return out
            e = self.px.read(node)
            return ExprVal(e) if e.refs or e.functions else None
        frame = next((v for v in (self.value(a) for a in node.args[:2]) if isinstance(v, FrameVal)), None)
        if frame is not None and name not in self.udfs and not self.function_alias(name):
            return FrameVal(self._opaque_step(frame.fid, name, node))     # mystery(df): the thread goes on
        alias = self.function_alias(name)
        if alias == "broadcast":
            return self.value(node.args[0]) if node.args else None
        if alias or name in self.udfs:
            return ExprVal(self.px.read(node))
        if name == "reduce" and len(node.args) >= 2:   # reduce(DataFrame.unionByName, dfs)
            frames = self.value(node.args[1])
            if isinstance(frames, list) and frames:
                how = "union_by_name" if "ByName" in self.text(node.args[0]) else "union"
                op = self.b.op(f"{node.lineno}:{node.col_offset}:union", ROW, ["union"], "union", self.text(node),
                               node.lineno, category="join")
                return FrameVal(self.b.frame(combine=how, parents=[v.fid for v in frames], row_ops=[op]))
        return None

    def inline(self, name: str, node: ast.Call):
        """A helper function (of the notebook, or of one it %runs): run its body with the call's arguments.
        What it makes is told where it comes from: the function, its docstring, the notebook."""
        fn, cell, code, ids, origin = self.helpers[name]
        before = set(self.b.p.ops)
        params = [a.arg for a in fn.args.args + fn.args.kwonlyargs]
        scope, saved = {}, []
        args = list(zip(params, node.args)) + [(k.arg, k.value) for k in node.keywords if k.arg in params]
        defaults = fn.args.defaults
        given = {p for p, _ in args}
        for p, d in zip([a.arg for a in fn.args.args][len(fn.args.args) - len(defaults):], defaults):
            if p not in given:
                args.append((p, d))
        for p, a in args:
            if isinstance(a, ast.Starred):
                continue
            v = self.value(a)
            if v is not None:
                scope[p] = v
            vals = self.pa.resolve(a)
            items = self.pa._iter_items(a)
            saved.append((p, self.pa.strs.get(p), self.pa.lists.get(p)))
            if vals:
                self.pa.strs[p] = list(vals)
            if items and all(isinstance(i, str) for i in items):
                self.pa.lists[p] = items
        here = (self.b.cell, self.src, self.node_ids, self.returned, self.b.origin)
        self.b.cell, self.src, self.node_ids, self.returned = cell, code, ids, None
        if origin:
            self.b.origin = origin
        self.scopes.append(scope)
        self.depth += 1
        try:
            self.block(fn.body)
            out = self.returned
        finally:
            self.depth -= 1
            self.scopes.pop()
            self.b.cell, self.src, self.node_ids, self.returned, self.b.origin = here
            self._restore(saved)
        doc = (ast.get_docstring(fn) or "").strip().splitlines()
        about = f"In {name}()" + (f": {doc[0]}" if doc else "") + (f" (from {origin}, %run)" if origin else "")
        for key in set(self.b.p.ops) - before:
            op = self.b.p.ops[key]
            op.detail = about + (f". {op.detail}" if op.detail else "")
        return out

    # ================================================================ helpers for frames
    def line_of(self, call) -> int:
        f = call.func
        return getattr(f, "end_lineno", None) or getattr(call, "lineno", 0)

    def key_of(self, node, tag: str) -> str:
        f = getattr(node, "func", None)
        if isinstance(f, ast.Attribute) and getattr(f, "end_lineno", None):
            return f"{f.end_lineno}:{f.end_col_offset}:{tag}"
        return f"{getattr(node, 'lineno', 0)}:{getattr(node, 'col_offset', 0)}:{tag}"

    def segment(self, call) -> str:
        """The part of a method chain that is this call: .withColumn("x", ...)"""
        f = call.func
        if isinstance(f, ast.Attribute) and id(call) in self.node_ids and getattr(f.value, "end_lineno", None):
            text = _slice(self.src, f.value.end_lineno, f.value.end_col_offset, call.end_lineno, call.end_col_offset)
            if text.strip():
                return text.strip()
        return self.text(call)

    def arg(self, call: ast.Call, index: int, *names):
        if len(call.args) > index and not isinstance(call.args[index], ast.Starred):
            return call.args[index]
        for k in call.keywords:
            if k.arg in names:
                return k.value
        return None

    def names_of(self, node) -> list:
        """Column names given as a string, a list of strings, or Column objects."""
        if node is None:
            return []
        if isinstance(node, (ast.List, ast.Tuple)):
            out = []
            for e in node.elts:
                out += self.names_of(e)
            return out
        items = self.items(node) if isinstance(node, ast.Name) and node.id in self.pa.lists else None
        if items:
            return items
        vals = self.strings(node)
        if vals:
            return vals[:1]
        e = self.px.read(node)
        return [e.name] if e.column and e.refs else []

    def entry(self, fid: int, e: Expr, node, name: str = "", code: str = "") -> Entry:
        """One column made by an expression over frame fid."""
        if e.star is not None:
            return Entry(star=fid, qualifier=e.star)
        if e.wildcard:
            if e.functions or e.alias:
                return Entry(star=fid, ops=[self.col_op(e, node, code, category="" if e.functions else "rename")],
                             wildcard=True)
            return Entry(star=fid)
        name = name or e.name
        deps = [Dep(fid, r.name, r.qualifier, r.hint) for r in e.refs]
        if e.column and not e.helper and deps and name.lower() == e.refs[0].name.split(".")[-1].lower():
            return Entry(name=name, deps=deps)
        category = "rename" if e.column and not e.helper else ("custom" if e.unknown and not e.functions else "")
        return Entry(name=name, deps=deps, ops=[self.col_op(e, node, code, category=category)])

    def col_op(self, e: Expr, node, code: str = "", category: str = "", tag: str = "col") -> str:
        line = getattr(node, "lineno", 0)
        label = pretty(e.text)
        if e.alias and not re.search(r"\bAS\b|alias|\bname\(", label):
            label = f"{label} AS {e.alias}"
        functions = list(e.functions) or (["lit"] if e.literal else [])
        return self.b.op(f"{line}:{getattr(node, 'col_offset', 0)}:{tag}", COLUMN, functions, label,
                         code or self.text(node), line, category=category,
                         detail=f"via the notebook's function {e.helper}()" if e.helper else "")

    def row_op(self, call, fid: int, kind: str, label: str, functions=(), refs=(), detail: str = "",
               tag: str = "") -> str:
        reads = [Dep(fid, r.name, r.qualifier, r.hint) for r in refs]
        return self.b.op(self.key_of(call, tag or kind), ROW, [kind, *functions], label, self.segment(call),
                         self.line_of(call), category=kind, reads=reads, detail=detail)

    def filter_expr(self, fid: int, e: Expr, call, label_text: str) -> int:
        op = self.row_op(call, fid, "filter", f"filter {pretty(e.text)}", e.functions, e.refs)
        return self.b.star(fid, row_ops=[op])

    def condition(self, node) -> Expr:
        """A condition: a SQL string ("TagNo IS NOT NULL") or a Column expression."""
        vals = self.strings(node)
        if vals:
            return sql_expression_text(vals[0])
        return self.px.read(node, column_ctx=False)

    def select_exprs(self, args) -> list:
        """The items of select(...) / groupBy(...) / agg(...): [(Expr, node)]."""
        out = []
        for a in args:
            if isinstance(a, ast.Starred):
                a = a.value
            if isinstance(a, (ast.ListComp, ast.GeneratorExp)):
                out += self.comprehension(a)
            elif isinstance(a, (ast.List, ast.Tuple)):
                out += self.select_exprs(a.elts)
            elif isinstance(a, ast.Name) and isinstance(self.lookup(a.id), ListVal):
                out += self.lookup(a.id).items
            elif isinstance(a, ast.Name) and a.id in self.pa.lists and a.id not in self.wild:
                out += [(Expr(refs=[Ref(n)], text=n, column=True), a) for n in self.pa.lists[a.id]]
            else:
                out.append((self.px.read(a), a))
        return out

    def comprehension(self, node) -> list:
        gen = node.generators[0] if len(node.generators) == 1 else None
        if gen is None:
            return [(Expr(text=self.text(node), unknown=True), node)]
        items = self.pa._iter_items(gen.iter)
        names = [gen.target.id] if isinstance(gen.target, ast.Name) else \
            [t.id for t in getattr(gen.target, "elts", []) if isinstance(t, ast.Name)]
        if items is None:                               # [F.col(c).alias(c.lower()) for c in df.columns]
            self.wild.update(names)
            try:
                return [(self.px.read(node.elt), node.elt)]
            finally:
                self.wild.difference_update(names)
        out = []
        for item in items[:MAX_COMPREHENSION]:
            saved = self._bind_consts(gen.target, item)
            try:
                if all(self._true(t) is not False for t in gen.ifs):
                    out.append((self.px.read(node.elt), node.elt))
            finally:
                self._restore(saved)
        return out

    def _true(self, test) -> Optional[bool]:
        """A comprehension's condition on known values (c != "id", c in [...]); None when it can't be told."""
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            v = self._true(test.operand)
            return None if v is None else not v
        if isinstance(test, ast.Compare) and len(test.ops) == 1:
            left = self.pa.resolve(test.left)
            right_node = test.comparators[0]
            op = test.ops[0]
            if left is None or len(left) != 1:
                return None
            if isinstance(op, (ast.In, ast.NotIn)):
                items = self.pa._iter_items(right_node)
                if items is None:
                    return None
                return (left[0] in items) == isinstance(op, ast.In)
            right = self.pa.resolve(right_node)
            if right is None or len(right) != 1:
                return None
            if isinstance(op, ast.Eq):
                return left[0] == right[0]
            if isinstance(op, ast.NotEq):
                return left[0] != right[0]
        if isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute) and \
                test.func.attr in ("startswith", "endswith") and test.args:
            base, arg = self.pa.resolve(test.func.value), self.pa.resolve(test.args[0])
            if base and arg and len(base) == 1 and len(arg) == 1:
                return getattr(base[0], test.func.attr)(arg[0])
        return None

    # ================================================================ sources
    def spark_call(self, m: str, node: ast.Call):
        if m == "table":
            return self.source_table(self.arg(node, 0, "tableName"), node)
        if m == "sql":
            return self.sql_call(node)
        if m == "range":
            op = self.b.op(self.key_of(node, "range"), COLUMN, ["sequence"], pretty(self.text(node)),
                           self.text(node), node.lineno, category="number")
            return FrameVal(self.b.frame(entries=[Entry(name="id", ops=[op])], label="range"))
        if m == "createDataFrame":
            return self.created_frame(node)
        return None

    def source_table(self, arg, node, kind: str = "table"):
        line = getattr(node, "lineno", 0)
        names = self.strings(arg) if arg is not None else None
        if not names:
            self.b.note(f"Cell {self.b.cell}, line {line}: a table name is built while the notebook runs, so its "
                        "columns can't be followed.")
            return FrameVal(self.b.opaque("a table named at run time", line))
        frames = []
        for n in names[:8]:
            ref = self.b.table(split_dotted(n.strip()), kind=kind)
            if ref is not None:
                frames.append(self.b.source(ref, line))
        if not frames:
            return FrameVal(self.b.opaque(f"the table {names[0]}", line))
        if len(frames) == 1:
            return FrameVal(frames[0])
        self.b.note(f"Cell {self.b.cell}, line {line}: the table name can be one of {len(names)} values; all of "
                    "them are followed.")
        return FrameVal(self.b.frame(combine="union_by_name", parents=frames, label="one of several tables"))

    def source_path(self, arg, node):
        line = getattr(node, "lineno", 0)
        vals = self.strings(arg) if arg is not None else None
        if not vals:
            return FrameVal(self.b.opaque("a path named at run time", line))
        ref = self.b.table([], vals[0])
        if ref is None:
            return FrameVal(self.b.opaque(f"files at {vals[0]}", line, vals[0]))
        return FrameVal(self.b.source(ref, line))

    def sql_call(self, node: ast.Call, duck: bool = False):
        arg = self.arg(node, 0, "sqlQuery", "query", "sql_query")
        if arg is None:
            return None
        line = getattr(arg, "lineno", node.lineno)
        key = f"{node.lineno}:{node.col_offset}:"
        vals = self.strings(arg)
        last = None
        if vals:
            for v in vals[:4]:
                r = self.sqlr.run(v, line=line, key=key, duck=duck)
                last = r if r is not None else last
        else:
            text, holes = self.pa.template(arg)
            last = self.sqlr.run(text, line=line, key=key, duck=duck)
            if holes:
                self.b.note(f"Cell {self.b.cell}, line {line}: parts of a SQL statement are built while the notebook "
                            "runs; the rest is followed.")
        return FrameVal(last) if last is not None else None

    def created_frame(self, node: ast.Call):
        schema = self.arg(node, 1, "schema")
        names = []
        if schema is not None:
            vals = self.strings(schema)
            if vals and ("," in vals[0] or " " in vals[0].strip()):
                names = [p.strip().split()[0].strip("`") for p in vals[0].split(",") if p.strip()]
            elif self.items(schema):
                names = self.items(schema)
            else:
                names = [n.args[0].value for n in ast.walk(schema) if isinstance(n, ast.Call) and
                         ast.unparse(n.func).endswith("StructField") and n.args and
                         isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)]
        if not names:
            return FrameVal(self.b.opaque("data made in the notebook", node.lineno, "createDataFrame"))
        op = self.b.op(self.key_of(node, "data"), COLUMN, ["lit"], "data made in the notebook", self.text(node),
                       node.lineno, category="constant")
        return FrameVal(self.b.frame(entries=[Entry(name=n, ops=[op]) for n in names], label="createDataFrame"))

    def reader_call(self, rv: ReaderVal, m: str, node: ast.Call):
        if m == "format":
            v = self.strings(node.args[0]) if node.args else None
            return replace(rv, fmt=(v[0].lower() if v else rv.fmt))
        if m == "option" and len(node.args) >= 2:
            k = self.strings(node.args[0])
            return replace(rv, path=node.args[1]) if k and k[0].lower() == "path" else rv
        if m in ("options", "schema", "option"):
            return rv
        if m == "table":
            return self.source_table(self.arg(node, 0, "tableName"), node)
        if m == "synapsesql":
            return self.source_table(self.arg(node, 0, "tableName"), node, kind="warehouse_table")
        if m == "load":
            if rv.fmt == "jdbc":
                self.b.note(f"Cell {self.b.cell}, line {node.lineno}: a JDBC source is outside Fabric; its columns "
                            "can't be read.")
                return FrameVal(self.b.opaque("a JDBC source", node.lineno, "JDBC"))
            return self.source_path(self.arg(node, 0, "path") or rv.path, node)
        if m in FILE_FORMATS:
            return self.source_path(self.arg(node, 0, "path", "paths"), node)
        return None

    def delta_static(self, m: str, node: ast.Call):
        if m == "forName":
            vals = self.strings(self.arg(node, 1, "tableOrViewName"))
            ref = self.b.table(split_dotted(vals[0])) if vals else None
        elif m == "forPath":
            vals = self.strings(self.arg(node, 1, "path"))
            ref = self.b.table([], vals[0]) if vals else None
        else:
            return None
        return DeltaVal(ref) if ref is not None else None

    def pandas_call(self, m: str, node: ast.Call):
        if m.startswith(("read_", "scan_")):
            return self.source_path(self.arg(node, 0, "path", "filepath_or_buffer", "source", "table_uri"), node)
        if m == "concat" and node.args:
            frames = self.value(node.args[0])
            if isinstance(frames, list) and frames:
                op = self.b.op(self.key_of(node, "concat"), ROW, ["union"], "concat", self.text(node), node.lineno,
                               category="join")
                return FrameVal(self.b.frame(combine="union_by_name", parents=[v.fid for v in frames], row_ops=[op]))
        if m == "merge" and len(node.args) >= 2:
            left, right = self.value(node.args[0]), self.value(node.args[1])
            if isinstance(left, FrameVal) and isinstance(right, FrameVal):
                return self.join(left.fid, node, right.fid, pandas=True)
        if m == "DataFrame":
            return FrameVal(self.b.opaque("data made in the notebook", node.lineno, "DataFrame"))
        return None

    def duck_call(self, m: str, node: ast.Call):
        if m in ("sql", "query", "execute", "query_df"):
            return self.sql_call(node, duck=True)
        if m == "connect":
            return DuckVal()
        if m.startswith("read_"):
            return self.source_path(self.arg(node, 0, "path_or_buffer", "path"), node)
        return None

    # ================================================================ DataFrame steps
    def frame_call(self, fid: int, m: str, node: ast.Call):
        if m in PASSTHROUGH:
            return FrameVal(fid)
        if m in TEMP_VIEWS:
            vals = self.strings(self.arg(node, 0, "viewName", "name"))
            if vals:
                self.b.views[vals[0].lower().split(".")[-1]] = fid
            return None
        if m in ACTIONS:
            return None
        if m == "alias":
            vals = self.strings(self.arg(node, 0, "alias"))
            return FrameVal(self.b.star(fid, alias=vals[0] if vals else "")) if vals else FrameVal(fid)
        if m == "select":
            items = self.select_exprs(node.args)
            entries = [self.entry(fid, e, n, code=self.text(n)) if not (e.unknown and not e.refs) else
                       self._unknown_item(fid, e, n) for e, n in items]
            return FrameVal(self.b.frame(entries=entries, position=(self.b.cell, self.line_of(node)), label="select"))
        if m == "selectExpr":
            entries = []
            for a in node.args:
                for v in (self.strings(a) or [])[:1]:
                    e = sql_expression_text(v)
                    entries.append(self.entry(fid, e, a, code=v))
            return FrameVal(self.b.frame(entries=entries, position=(self.b.cell, self.line_of(node))))
        if m == "withColumn":
            return FrameVal(self.with_columns(fid, node, [(self.arg(node, 0, "colName"), self.arg(node, 1, "col"))]))
        if m in ("withColumns",):
            d = self.arg(node, 0, "colsMap", "cols_map")
            pairs = list(zip(d.keys, d.values)) if isinstance(d, ast.Dict) else []
            return FrameVal(self.with_columns(fid, node, pairs))
        if m == "assign":
            pairs = [(ast.Constant(k.arg), k.value) for k in node.keywords if k.arg]
            return FrameVal(self.with_columns(fid, node, pairs))
        if m in ("withColumnRenamed", "withColumnsRenamed", "rename"):
            return FrameVal(self.rename(fid, m, node))
        if m == "drop":
            names = []
            for a in node.args:
                names += self.names_of(a)
            for k in node.keywords:
                if k.arg in ("columns", "labels"):
                    names += self.names_of(k.value)
            if any(isinstance(a, ast.Name) and a.id in self.wild for a in node.args):
                return FrameVal(fid)
            return FrameVal(self.b.star(fid, exclude=names))
        if m == "toDF":
            names = []
            for a in node.args:
                names += self.names_of(a)
            if not names:
                return FrameVal(fid)
            return FrameVal(self.b.frame(entries=[Entry(star=fid)], rename_to=names, label="toDF"))
        if m in ("filter", "where", "query"):
            cond = self.arg(node, 0, "condition", "expr")
            if cond is None:
                return FrameVal(fid)
            return FrameVal(self.filter_expr(fid, self.condition(cond), node, ""))
        if m in ("distinct", "dropDuplicates", "drop_duplicates", "dropDuplicatesWithinWatermark", "unique"):
            subset = self.names_of(self.arg(node, 0, "subset"))
            label = f"{m}({', '.join(subset)})" if subset else m + "()"
            op = self.row_op(node, fid, "filter", label, ["distinct"], [Ref(s) for s in subset])
            return FrameVal(self.b.star(fid, row_ops=[op]))
        if m in ("limit", "head", "tail", "sample", "sampleBy", "offset", "nlargest", "nsmallest"):
            op = self.row_op(node, fid, "filter", pretty(self.segment(node)).lstrip("."), ["limit"])
            return FrameVal(self.b.star(fid, row_ops=[op]))
        if m in ("dropna",):
            return FrameVal(self.na_call(fid, "drop", node).fid)
        if m in ("fillna",):
            return self.na_call(fid, "fill", node)
        if m == "replace":
            return self.na_call(fid, "replace", node)
        if m in ("join", "crossJoin", "merge", "join_asof"):
            other = self.value(self.arg(node, 0, "other", "right"))
            if isinstance(other, FrameVal):
                return self.join(fid, node, other.fid, pandas=m in ("merge", "join_asof"), cross=m == "crossJoin")
            return FrameVal(fid)
        if m in ("union", "unionAll", "unionByName"):
            other = self.value(self.arg(node, 0, "other"))
            if isinstance(other, FrameVal):
                op = self.row_op(node, fid, "join", m, ["union"])
                how = "union_by_name" if m == "unionByName" else "union"
                return FrameVal(self.b.frame(combine=how, parents=[fid, other.fid], row_ops=[op], label=m))
            return FrameVal(fid)
        if m in ("intersect", "intersectAll", "subtract", "exceptAll"):
            op = self.row_op(node, fid, "join", f"{m}({self.text(node.args[0]) if node.args else ''})", [m.lower()])
            return FrameVal(self.b.star(fid, row_ops=[op]))
        if m in ("groupBy", "groupby", "rollup", "cube", "group_by"):
            keys = self.select_exprs(node.args) + [(self.px.read(k.value), k.value) for k in node.keywords
                                                   if k.arg == "by"]
            return GroupVal(fid, keys, node)
        if m == "agg":
            return self.group_call(GroupVal(fid, [], node), "agg", node)
        if m == "transform":
            return self.transform(fid, node)
        if m == "writeTo":
            vals = self.strings(self.arg(node, 0, "table"))
            return WriterVal(fid, table=split_dotted(vals[0]) if vals else [], v2=True)
        if m in PANDAS_WRITES:
            self.write_path(fid, self.arg(node, 0, "path", "path_or_buf", "file", "target"), node, m, "overwrite")
            return None
        if m == "astype":
            return FrameVal(self.cast_all(fid, node))
        if m in ("explode",):
            names = self.names_of(self.arg(node, 0, "column"))
            op = self.b.op(self.key_of(node, "explode"), COLUMN, ["explode"], pretty(self.segment(node)).lstrip("."),
                           self.segment(node), self.line_of(node))
            return FrameVal(self.b.frame(entries=[Entry(star=fid, exclude=frozenset(n.lower() for n in names))] +
                                         [Entry(name=n, deps=[Dep(fid, n)], ops=[op]) for n in names]))
        return FrameVal(self._opaque_step(fid, m, node))

    def _unknown_item(self, fid: int, e: Expr, node) -> Entry:
        self.b.note(f"Cell {self.b.cell}, line {getattr(node, 'lineno', 0)}: {pretty(e.text)[:60]} couldn't be "
                    "followed, so every column is taken to pass through.")
        return Entry(star=fid)

    NAMES_OUT = re.compile(r"^(?:out|output|target|new|result|dest|to)_?(?:col|column|name)s?$|^alias$", re.I)
    NAMES_IN = re.compile(r"(?:^|_)(?:col|cols|column|columns|field|fields|key|keys|subset)$", re.I)

    def _named_columns(self, node) -> tuple[list, list]:
        """(columns a call names as inputs, as outputs): keyword arguments such as domain_col="Domain",
        columns=["a", "b"], out_col="SiteCode" (inside a lambda too)."""
        ins, outs = [], []
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            for k in call.keywords:
                if not k.arg:
                    continue
                vals = self.items(k.value) if isinstance(k.value, (ast.List, ast.Tuple, ast.Name)) else None
                vals = vals or self.strings(k.value) or []
                if self.NAMES_OUT.search(k.arg):
                    outs += [v for v in vals if v not in outs]
                elif self.NAMES_IN.search(k.arg):
                    ins += [v for v in vals if v not in ins]
        return ins, [o for o in outs if o not in ins]

    def _opaque_step(self, fid: int, m: str, node) -> int:
        """Code the app can't see into (a method it doesn't know, a function of a library). The columns the
        call names are what it works on, the others pass it by; when it names none, every column goes
        through it."""
        ins, outs = self._named_columns(node) if isinstance(node, ast.Call) else ([], [])
        what = (f"It names the columns {', '.join(ins + outs)}" if ins or outs else "It names no column") + \
            "; the code itself can't be seen, so " + ("the other columns are taken to pass it by" if ins or outs else
                                                      "every column is taken to go through it")
        op = self.b.op(self.key_of(node, "own"), COLUMN, [m + "()"], pretty(self.segment(node)).lstrip("."),
                       self.segment(node), self.line_of(node), category="custom", detail=what + ".")
        if outs:                                 # it makes these, from the columns it names
            return self.b.frame(entries=[Entry(star=fid, exclude=frozenset(o.lower() for o in outs))] +
                                [Entry(name=o, deps=[Dep(fid, c) for c in ins], ops=[op]) for o in outs])
        if ins:                                  # it works on these
            return self.b.frame(entries=[Entry(star=fid, exclude=frozenset(c.lower() for c in ins))] +
                                [Entry(name=c, deps=[Dep(fid, c)], ops=[op]) for c in ins])
        return self.b.frame(entries=[Entry(star=fid, ops=[op], wildcard=True)])

    def with_columns(self, fid: int, node, pairs) -> int:
        entries, exclude = [], set()
        code = self.segment(node) if isinstance(node, ast.Call) else self.text(node)
        for name_node, expr_node in pairs:
            if name_node is None or expr_node is None:
                continue
            e = self.px.read(expr_node)
            if isinstance(name_node, ast.Name) and name_node.id in self.wild:   # withColumn(c, ...) over every column
                op = self.col_op(e, expr_node, code, category="" if e.functions else "custom")
                entries.append(Entry(star=fid, ops=[op], wildcard=True))
                exclude = None
                continue
            names = self.strings(name_node)
            if not names:
                continue
            name = names[0]
            if exclude is not None:
                exclude.add(name.lower())
            if e.unknown and not e.refs:
                op = self.col_op(e, expr_node, code, category="custom")
                entries.append(Entry(name=name, ops=[op]))
            else:
                entries.append(self.entry(fid, e, expr_node, name=name, code=code))
        if exclude is None:
            return self.b.frame(entries=[x for x in entries if x.is_star][-1:] or [Entry(star=fid)],
                                position=(self.b.cell, self.line_of(node)))
        return self.b.frame(entries=[Entry(star=fid, exclude=frozenset(exclude))] + entries,
                            position=(self.b.cell, self.line_of(node)))

    def rename(self, fid: int, m: str, node: ast.Call) -> int:
        pairs = []
        if m == "withColumnRenamed":
            pairs = [(self.arg(node, 0, "existing"), self.arg(node, 1, "new"))]
        else:
            d = self.arg(node, 0, "colsMap", "columns", "mapper")
            if m == "rename" and not isinstance(d, ast.Dict):
                d = next((k.value for k in node.keywords if k.arg == "columns"), None)
            if isinstance(d, ast.Dict):
                pairs = list(zip(d.keys, d.values))
        code = self.segment(node)
        entries, exclude = [], set()
        for old_node, new_node in pairs:
            if isinstance(old_node, ast.Name) and old_node.id in self.wild:
                e = self.px.read(new_node, column_ctx=False)
                op = self.b.op(self.key_of(node, "rename"), COLUMN, [], f"rename every column: {pretty(e.text)}", code,
                               self.line_of(node), category="rename")
                return self.b.frame(entries=[Entry(star=fid, ops=[op], wildcard=True)])
            old, new = self.strings(old_node), self.strings(new_node)
            if not old or not new:
                continue
            exclude.add(old[0].lower())
            op = self.b.op(self.key_of(node, "rename") + f":{old[0]}", COLUMN, [], f"{old[0]} → {new[0]}", code,
                           self.line_of(node), category="rename")
            entries.append(Entry(name=new[0], deps=[Dep(fid, old[0])], ops=[op]))
        return self.b.frame(entries=[Entry(star=fid, exclude=frozenset(exclude))] + entries,
                            position=(self.b.cell, self.line_of(node)))

    def cast_all(self, fid: int, node: ast.Call) -> int:
        d = self.arg(node, 0, "dtype")
        code = self.segment(node)
        if isinstance(d, ast.Dict):
            pairs = [(self.strings(k), v) for k, v in zip(d.keys, d.values)]
            names = [p[0][0] for p in pairs if p[0]]
            op = self.b.op(self.key_of(node, "cast"), COLUMN, ["astype"], pretty(code).lstrip("."), code,
                           self.line_of(node))
            return self.b.frame(entries=[Entry(star=fid, exclude=frozenset(n.lower() for n in names))] +
                                [Entry(name=n, deps=[Dep(fid, n)], ops=[op]) for n in names])
        op = self.b.op(self.key_of(node, "cast"), COLUMN, ["astype"], pretty(code).lstrip("."), code,
                       self.line_of(node))
        return self.b.frame(entries=[Entry(star=fid, ops=[op], wildcard=True)])

    def join(self, fid: int, node: ast.Call, other: int, pandas: bool = False, cross: bool = False):
        on = None if cross else self.arg(node, 1, "on")
        how_node = None if cross else self.arg(node, 2, "how")
        how = (self.strings(how_node) or ["inner"])[0].lower().replace("_", " ") if how_node is not None else \
            ("cross" if cross else "inner")
        using, refs = [], []
        if on is not None:
            names = self.items(on) if isinstance(on, (ast.List, ast.Tuple, ast.Name)) else None
            single = self.strings(on)
            if names:
                using = names
            elif single and isinstance(on, (ast.Constant, ast.JoinedStr, ast.Name)):
                using = single[:1]
            else:
                refs = self.px.read(on, column_ctx=False).refs
        if pandas:
            for side in ("left_on", "right_on"):
                v = next((k.value for k in node.keywords if k.arg == side), None)
                if v is not None:
                    refs += [Ref(n) for n in self.names_of(v)]
        pre = self.b.frame(entries=[Entry(star=fid), Entry(star=other)], label="join input")
        reads = [Dep(pre, r.name, r.qualifier, r.hint) for r in refs]
        for u in using:
            reads += [Dep(fid, u), Dep(other, u)]
        other_label = self.text(self.arg(node, 0, "other", "right"))
        cond = (" on " + ", ".join(using)) if using else (" on " + pretty(self.text(on)) if on is not None else "")
        op = self.b.op(self.key_of(node, "join"), ROW, ["join"], f"{how} join {other_label}{cond}",
                       self.segment(node), self.line_of(node), category="join", reads=reads, detail=f"{how} join")
        semi = "semi" in how or "anti" in how
        entries = [Entry(star=fid)]
        if not semi:
            entries.append(Entry(star=other, exclude=frozenset(u.lower() for u in using)))
        return FrameVal(self.b.frame(entries=entries, row_ops=[op], label=f"{how} join"))

    def transform(self, fid: int, node: ast.Call):
        func = self.arg(node, 0, "func")
        if isinstance(func, ast.Name) and func.id in self.helpers and self.depth < MAX_INLINE:
            call = ast.Call(func=func, args=[ast.Name(id="__frame__", ctx=ast.Load())] + node.args[1:],
                            keywords=node.keywords)
            self.set("__frame__", FrameVal(fid))
            out = self.inline(func.id, call)
            if isinstance(out, FrameVal):
                return out
        if isinstance(func, ast.Lambda) and func.args.args:
            self.scopes.append({func.args.args[0].arg: FrameVal(fid)})
            try:
                out = self.value(func.body)
            finally:
                self.scopes.pop()
            if isinstance(out, FrameVal):
                return out
        return FrameVal(self._opaque_step(fid, "transform", node))

    def na_call(self, fid: int, m: str, node: ast.Call):
        code = self.segment(node)
        if m == "drop":
            subset = self.names_of(self.arg(node, 2, "subset"))
            label = f"drop rows with nulls{' in ' + ', '.join(subset) if subset else ''}"
            op = self.row_op(node, fid, "filter", label, ["isnull"], [Ref(s) for s in subset], tag="dropna")
            return FrameVal(self.b.star(fid, row_ops=[op]))
        value = self.arg(node, 0, "value", "to_replace")
        subset = self.names_of(self.arg(node, 1 if m == "fill" else 2, "subset"))
        if isinstance(value, ast.Dict):
            subset = [s for k in value.keys for s in (self.strings(k) or [])[:1]]
        fn = "fillna" if m == "fill" else "replace"
        label = (f"fill nulls with {self.text(value)}" if m == "fill" else f"replace {self.text(value)}") \
            if value is not None and not isinstance(value, ast.Dict) else f"{fn}({self.text(value) if value else ''})"
        op = self.b.op(self.key_of(node, fn), COLUMN, [fn], label, code, self.line_of(node), category="condition")
        if not subset:
            return FrameVal(self.b.frame(entries=[Entry(star=fid, ops=[op], wildcard=True)]))
        return FrameVal(self.b.frame(entries=[Entry(star=fid, exclude=frozenset(s.lower() for s in subset))] +
                                     [Entry(name=s, deps=[Dep(fid, s)], ops=[op]) for s in subset]))

    def group_call(self, gv: GroupVal, m: str, node: ast.Call):
        if m == "pivot":
            col = self.arg(node, 0, "pivot_col")
            return replace(gv, pivot=(self.px.read(col), col) if col is not None else None)
        aggs = []
        if m in ("agg", "aggregate"):
            for a in node.args:
                if isinstance(a, ast.Dict):                   # {"amount": "sum"}
                    for k, v in zip(a.keys, a.values):
                        col, fn = self.strings(k), self.strings(v)
                        if col and fn:
                            aggs.append((Expr(refs=[Ref(col[0])], functions=[fn[0].lower()], text=f"{fn[0]}({col[0]})",
                                              alias=f"{fn[0]}({col[0]})"), a))
                else:
                    aggs += self.select_exprs([a])
            for k in node.keywords:                           # pandas: total=("amount", "sum")
                if isinstance(k.value, ast.Tuple) and len(k.value.elts) == 2 and k.arg:
                    col, fn = self.strings(k.value.elts[0]), self.strings(k.value.elts[1])
                    if col and fn:
                        aggs.append((Expr(refs=[Ref(col[0])], functions=[fn[0].lower()], text=f"{fn[0]}({col[0]})",
                                          alias=k.arg), k.value))
        elif m == "count":
            aggs = [(Expr(functions=["count"], text="count()", alias="count"), node)]
        elif m in ("sum", "avg", "mean", "min", "max", "median", "std", "var", "first", "last", "nunique", "size"):
            cols = [n for a in node.args for n in self.names_of(a)]
            name = "avg" if m == "mean" else m
            aggs = [(Expr(refs=[Ref(c)], functions=[m], text=f"{name}({c})", alias=f"{name}({c})"), node) for c in cols]
            if not cols:
                aggs = [(Expr(functions=[m], text=f"{m}()", alias=m), node)]
        else:                                                 # applyInPandas, apply, ...: own code
            return FrameVal(self._opaque_step(gv.fid, m, node))
        fid = gv.fid
        reads = [r for k, _ in gv.keys for r in k.refs]
        names = [pretty(k.name) for k, _ in gv.keys]
        label = ("group by " + ", ".join(names)) if names else "aggregate all rows"
        gop = self.b.op(self.key_of(gv.node, "group"), ROW, ["group"], label, self.segment(gv.node),
                        self.line_of(gv.node), category="group", reads=[Dep(fid, r.name, r.qualifier, r.hint)
                                                                        for r in reads])
        entries = [self.entry(fid, k, n, code=self.text(n)) for k, n in gv.keys]
        code = self.segment(node)
        for e, n in aggs:
            entries.append(self.entry(fid, e, n, code=code if n is node else self.text(n)))
        if gv.pivot is not None:
            pe, pn = gv.pivot
            deps = [Dep(fid, r.name, r.qualifier) for r in pe.refs] + \
                   [d for x in entries[len(gv.keys):] for d in x.deps]
            op = self.b.op(self.key_of(gv.node, "pivot"), COLUMN, ["pivot"] + [f for e, _ in aggs for f in e.functions],
                           f"pivot on {pretty(pe.text)}", self.text(pn), getattr(pn, "lineno", 0), category="aggregate")
            entries = entries[:len(gv.keys)] + [Entry(name=f"(one column per {pretty(pe.text)} value)", deps=deps,
                                                      ops=[op])]
            self.b.note(f"Cell {self.b.cell}: a pivot makes one column per value it finds, so those columns are shown "
                        "as one.")
        return FrameVal(self.b.frame(entries=entries, row_ops=[gop], position=(self.b.cell, self.line_of(node))))

    # ================================================================ writes
    def writer_call(self, w: WriterVal, m: str, node: ast.Call):
        if m in ("format", "using"):
            v = self.strings(node.args[0]) if node.args else None
            return replace(w, fmt=v[0].lower() if v else w.fmt)
        if m == "mode":
            v = self.strings(self.arg(node, 0, "saveMode"))
            return replace(w, mode=v[0].lower() if v else w.mode)
        if m == "option" and len(node.args) >= 2:
            k = self.strings(node.args[0])
            return replace(w, path=node.args[1]) if k and k[0].lower() == "path" else w
        if m == "foreachBatch":                  # streaming: the function given each batch is where the writes are
            fn = self.arg(node, 0, "func")
            if isinstance(fn, ast.Name) and fn.id in self.helpers and self.depth < MAX_INLINE:
                self.set("__batch__", FrameVal(w.fid))
                self.inline(fn.id, ast.Call(func=fn, args=[ast.Name(id="__batch__", ctx=ast.Load()),
                                                           ast.Constant(0)], keywords=[]))
            elif isinstance(fn, ast.Lambda) and fn.args.args:
                self.scopes.append({fn.args.args[0].arg: FrameVal(w.fid)})
                try:
                    self.value(fn.body)
                finally:
                    self.scopes.pop()
            return w
        if m in ("option", "options", "partitionBy", "bucketBy", "sortBy", "clusterBy", "outputMode", "trigger",
                 "queryName", "partitionedBy", "tableProperty", "foreach"):
            return w
        mode = w.mode or next((self.strings(k.value)[0].lower() for k in node.keywords
                               if k.arg == "mode" and self.strings(k.value)), "")
        if w.v2 and m in ("append", "overwrite", "overwritePartitions", "create", "replace", "createOrReplace"):
            self.write_table(w.fid, w.table, node, f"writeTo().{m}", m.lower())
            return None
        if m in ("saveAsTable", "toTable"):
            vals = self.strings(self.arg(node, 0, "name", "tableName"))
            if vals:
                self.write_table(w.fid, split_dotted(vals[0]), node, m, mode or "create")
            return None
        if m == "insertInto":
            vals = self.strings(self.arg(node, 0, "tableName"))
            over = self.arg(node, 1, "overwrite")
            if vals:
                self.write_table(w.fid, split_dotted(vals[0]), node, m,
                                 "overwrite" if isinstance(over, ast.Constant) and over.value else "append",
                                 by_position=True)
            return None
        if m == "synapsesql":
            vals = self.strings(self.arg(node, 0, "tableName"))
            if vals:
                self.write_table(w.fid, split_dotted(vals[0]), node, m, mode or "create", kind="warehouse_table")
            return None
        if m in ("save", "start") or m in FILE_FORMATS:
            self.write_path(w.fid, self.arg(node, 0, "path") or w.path, node, f"write.{m}", mode or "create")
            return None
        if m == "jdbc":
            self.b.note(f"Cell {self.b.cell}, line {node.lineno}: a JDBC target is outside Fabric; it isn't shown.")
        return None

    def write_table(self, fid: int, parts: list, node, verb: str, mode: str, by_position: bool = False,
                    kind: str = "table") -> None:
        ref = self.b.table(parts, kind=kind) if parts else None
        if ref is None:
            if parts and any("__dyn" in p for p in parts):
                self.b.note(f"Cell {self.b.cell}, line {node.lineno}: a table name is built while the notebook runs.")
            return
        self.b.sink(Sink(ref, fid, verb, mode, self.b.cell, self.line_of(node), by_position=by_position,
                         code=self.text(self.stmt) if self.stmt is not None else self.text(node)))

    def write_path(self, fid: int, path_node, node, verb: str, mode: str) -> None:
        vals = self.strings(path_node) if path_node is not None else None
        if not vals:
            if path_node is not None:
                self.b.note(f"Cell {self.b.cell}, line {node.lineno}: a path is built while the notebook runs.")
            return
        ref = self.b.table([], vals[0])
        if ref is None:
            return
        self.b.sink(Sink(ref, fid, verb, mode, self.b.cell, self.line_of(node),
                         code=self.text(self.stmt) if self.stmt is not None else self.text(node)))

    def write_deltalake(self, node: ast.Call) -> None:
        data = self.value(self.arg(node, 1, "data"))
        if not isinstance(data, FrameVal):
            return
        mode = next((self.strings(k.value)[0] for k in node.keywords if k.arg == "mode" and self.strings(k.value)),
                    "error")
        self.write_path(data.fid, self.arg(node, 0, "table_or_uri"), node, "write_deltalake", mode)

    # ================================================================ Delta Lake
    def delta_call(self, dv: DeltaVal, m: str, node: ast.Call):
        if m == "alias":
            vals = self.strings(self.arg(node, 0, "aliasName"))
            return DeltaVal(dv.ref, vals[0] if vals else dv.alias)
        if m in ("toDF", "to_pandas", "to_pyarrow_table", "to_pyarrow_dataset"):
            fid = self.b.source(dv.ref, node.lineno)
            return FrameVal(self.b.star(fid, alias=dv.alias) if dv.alias else fid)
        if m == "merge":
            src = self.value(self.arg(node, 0, "source"))
            if isinstance(src, FrameVal):
                return MergeVal(dv, src.fid, self.arg(node, 1, "condition"), node)
            return None
        if m == "update":
            return self.delta_update(dv, node)
        return None

    def merge_call(self, mv: MergeVal, m: str, node: ast.Call):
        if m in ("whenMatchedUpdateAll", "whenNotMatchedInsertAll"):
            mv.every = True
            return mv
        if m in ("whenMatchedUpdate", "whenNotMatchedInsert", "whenNotMatchedBySourceUpdate"):
            d = next((k.value for k in node.keywords if k.arg in ("set", "values")), None)
            if d is None and node.args and isinstance(node.args[-1], ast.Dict):
                d = node.args[-1]
            if isinstance(d, ast.Dict):
                for k, v in zip(d.keys, d.values):
                    target = self.strings(k)
                    if target:
                        mv.assignments.append(self._assignment(target[0].split(".")[-1], v, mv))
            return mv
        if m in ("whenMatchedDelete", "whenNotMatchedBySourceDelete", "withSchemaEvolution"):
            return mv
        if m == "execute":
            self.finish_merge(mv)
        return None

    def _targets(self, dv: DeltaVal) -> set:
        return {dv.alias.lower(), getattr(dv.ref, "table", "").lower(), getattr(dv.ref, "label", "").lower()} - {""}

    def _assignment(self, target: str, v, mv: MergeVal) -> Assignment:
        vals = self.strings(v)
        e = sql_expression_text(vals[0]) if vals else self.px.read(v)
        own = self._targets(mv.target)
        deps = [Dep(mv.src, r.name, r.qualifier, r.hint) for r in e.refs if r.qualifier.lower() not in own]
        ops = []
        if not (e.column and e.refs and e.refs[0].name.lower() == target.lower()):
            ops.append(self.b.op(f"{v.lineno}:{v.col_offset}:set", COLUMN, e.functions,
                                 f"{target} = {pretty(e.text)}", f"{target} = {e.text}", v.lineno,
                                 category="rename" if e.column else ""))
        return Assignment(target, Entry(name=target, deps=deps, ops=ops))

    def finish_merge(self, mv: MergeVal) -> None:
        cond_text, reads = "", []
        if mv.cond is not None:
            e = self.condition(mv.cond)
            cond_text = pretty(e.text)
            own = self._targets(mv.target)
            reads = [Dep(mv.src, r.name, r.qualifier, r.hint) for r in e.refs if r.qualifier.lower() not in own]
        op = self.b.op(self.key_of(mv.node, "merge"), ROW, ["merge"], f"merge on {cond_text}" if cond_text else "merge",
                       self.text(self.stmt) if self.stmt is not None else self.segment(mv.node),
                       self.line_of(mv.node), category="merge", reads=reads, detail="DeltaTable.merge")
        self.b.sink(Sink(mv.target.ref, mv.src, "DeltaTable.merge", "merge", self.b.cell, self.line_of(mv.node),
                         merge_op=op, assignments=mv.assignments, all_columns=mv.every,
                         code=self.text(self.stmt) if self.stmt is not None else ""))

    def delta_update(self, dv: DeltaVal, node: ast.Call) -> None:
        src = self.b.source(dv.ref, node.lineno)
        if dv.alias:
            src = self.b.star(src, alias=dv.alias)
        cond = self.arg(node, 0, "condition")
        d = self.arg(node, 1, "set")
        reads, cond_text = [], "every row"
        if cond is not None and not isinstance(cond, ast.Dict):
            e = self.condition(cond)
            reads, cond_text = [Dep(src, r.name, r.qualifier) for r in e.refs], pretty(e.text)
        elif isinstance(cond, ast.Dict):
            d = cond
        op = self.b.op(self.key_of(node, "update"), ROW, ["merge"], f"update where {cond_text}", self.segment(node),
                       self.line_of(node), category="merge", reads=reads, detail="DeltaTable.update")
        assignments = []
        if isinstance(d, ast.Dict):
            for k, v in zip(d.keys, d.values):
                target = self.strings(k)
                if target:
                    vals = self.strings(v)
                    e = sql_expression_text(vals[0]) if vals else self.px.read(v)
                    deps = [Dep(src, r.name, r.qualifier, r.hint) for r in e.refs]
                    ops = [] if e.column and e.refs and e.refs[0].name.lower() == target[0].lower() else \
                        [self.b.op(f"{v.lineno}:{v.col_offset}:set", COLUMN, e.functions,
                                   f"{target[0]} = {pretty(e.text)}", f"{target[0]} = {e.text}", v.lineno)]
                    assignments.append(Assignment(target[0], Entry(name=target[0], deps=deps, ops=ops)))
        self.b.sink(Sink(dv.ref, src, "DeltaTable.update", "update", self.b.cell, self.line_of(node), merge_op=op,
                         assignments=assignments, all_columns=False, code=self.text(self.stmt)))


def read_notebook_columns(info: NotebookInfo, resolve_table: TableResolver, seed: Optional[dict] = None,
                          imported: Optional[dict] = None) -> Program:
    """What a notebook does to columns. Never raises: a failure is reported in Program.error.
    imported: functions of the notebooks it %runs (Program.helpers of each, with the notebook's name)."""
    reader = NotebookReader(resolve_table, seed, imported)
    try:
        return reader.read(info)
    except RecursionError:
        p = reader.b.p
        p.error = "The notebook's code is nested too deeply to follow."
        return p
    except Exception as e:  # noqa: BLE001 - the map must keep working whatever the code looks like
        p = reader.b.p
        p.error = f"The column lineage couldn't be worked out ({type(e).__name__}: {e})"[:300]
        return p
