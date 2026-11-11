"""One expression -> the columns it reads, the functions it applies and the name it gets.

Two readers produce the same Expr:

    PyExprReader   pyspark Column expressions in Python code: F.upper(F.col("TagNo")).alias("Tag"),
                   df["a"] + 1, F.when(...).otherwise(...), F.expr("..."), pandas df["a"].str.upper()
    sql_expr       Spark SQL text: upper(t.TagNo) AS Tag, CASE WHEN ... END, CAST(x AS INT)

The SQL side works on the text after parse.sql.mask(), so keywords inside strings and comments
don't count; positions still point into the original text, for showing the code.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from ..parse.sql import DYN, mask
from .catalog import (BARE_FUNCTIONS, COLUMN_METHODS, CUSTOM_METHODS, IGNORED_METHODS, LITERAL_ARGS,
                      OPERATOR_FUNCTIONS, TYPE_NAMES)


@dataclass(frozen=True)
class Ref:
    """A column as written: name, the qualifier in front of it, and the DataFrame it was taken from."""
    name: str
    qualifier: str = ""
    hint: int = -1


@dataclass
class Expr:
    refs: list = field(default_factory=list)          # [Ref]
    functions: list = field(default_factory=list)     # outermost first
    text: str = ""
    alias: str = ""                                   # .alias("x") / AS x
    star: Optional[str] = None                        # "*" -> "", "t.*" -> "t"
    column: bool = False                              # a bare column, nothing done to it
    literal: bool = False                             # a fixed value
    unknown: bool = False                             # couldn't be read (a variable holding who knows what)
    wildcard: bool = False                            # stands for every column (a loop over df.columns)
    helper: str = ""                                  # a helper function of the notebook that was read through

    @property
    def name(self) -> str:
        """The name of the column it makes."""
        if self.alias:
            return self.alias
        if self.column and self.refs:
            return self.refs[0].name.split(".")[-1]
        return self.text

    @property
    def renames(self) -> bool:
        return bool(self.column and self.alias and self.refs and
                    self.alias.lower() != self.refs[0].name.split(".")[-1].lower())


def _merge(text: str, parts, functions=()) -> Expr:
    refs, fns = [], list(functions)
    wildcard, helper = False, ""
    for p in parts:
        for r in p.refs:
            if r not in refs:
                refs.append(r)
        fns.extend(p.functions)
        wildcard = wildcard or p.wildcard
        helper = helper or p.helper
    return Expr(refs=refs, functions=fns, text=text, wildcard=wildcard, helper=helper)


def ref_of(text: str, hint: int = -1) -> Ref:
    """'t.TagNo' -> qualifier t, name TagNo (the resolver tries struct fields when t isn't a qualifier)."""
    text = text.strip().strip("`")
    if "." in text and not text.startswith(".") and "`" not in text:
        q, _, n = text.rpartition(".")
        return Ref(n, q, hint)
    return Ref(text, "", hint)


# ======================================================================= Spark SQL
@dataclass(frozen=True)
class Tok:
    kind: str           # ident | num | str | op
    text: str           # identifiers: the name without backticks
    start: int
    end: int
    quoted: bool = False

    @property
    def up(self) -> str:
        return self.text.upper() if self.kind == "ident" and not self.quoted else ""


_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<bq>`(?:[^`]|``)*`)
  | (?P<ident>[A-Za-z_][\w$]*)
  | (?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?(?:BD|[LSYDF])?(?![\w$]))
  | (?P<str>'[^']*'|"[^"]*")
  | (?P<var>\$\{[^}]*\}|:[A-Za-z_]\w*)
  | (?P<op>->|<=>|<>|!=|<=|>=|==|\|\||&&|::|[-+*/%<>=!~^&|.,;()\[\]{}?@#:])
  | (?P<other>.)
""", re.X | re.S | re.I)


def tokenize(sql: str, masked: Optional[str] = None) -> list:
    """Tokens of a SQL text (comments and string contents blanked first, positions kept)."""
    text = mask(sql) if masked is None else masked
    out = []
    for m in _TOKEN.finditer(text):
        kind = m.lastgroup
        if kind in ("ws", "other"):
            continue
        if kind == "bq":
            out.append(Tok("ident", sql[m.start() + 1:m.end() - 1].replace("``", "`"), m.start(), m.end(), True))
        elif kind == "var":
            out.append(Tok("ident", f"{DYN}var__", m.start(), m.end()))
        else:
            out.append(Tok(kind, m.group(0), m.start(), m.end()))
    return out


# words that are never column names inside an expression
RESERVED = {
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "NULL", "TRUE", "FALSE", "CASE", "WHEN", "THEN", "ELSE", "END",
    "AS", "IS", "IN", "LIKE", "ILIKE", "RLIKE", "REGEXP", "BETWEEN", "DISTINCT", "ALL", "EXISTS", "CAST",
    "TRY_CAST", "OVER", "PARTITION", "BY", "ORDER", "ASC", "DESC", "NULLS", "INTERVAL", "ESCAPE", "DIV", "BOTH",
    "LEADING", "TRAILING", "ON", "JOIN", "INNER", "FULL", "CROSS", "SEMI", "ANTI", "NATURAL", "USING", "LIMIT",
    "UNION", "INTERSECT", "EXCEPT", "MINUS", "HAVING", "QUALIFY", "WINDOW", "WITH", "VALUES", "UNBOUNDED",
    "PRECEDING", "FOLLOWING", "FILTER", "WITHIN", "GROUP", "LATERAL", "COLLATE", "FOR", "ANY", "SOME",
}
_UNITS = {"YEAR", "YEARS", "MONTH", "MONTHS", "WEEK", "WEEKS", "DAY", "DAYS", "HOUR", "HOURS", "MINUTE", "MINUTES",
          "SECOND", "SECONDS", "MILLISECOND", "MILLISECONDS", "MICROSECOND", "MICROSECONDS", "TO"}
_FRAME_WORDS = {"ROWS", "RANGE", "CURRENT", "ROW", "UNBOUNDED", "PRECEDING", "FOLLOWING", "AND", "BETWEEN"}
_KEYWORD_FUNCS = {"CASE": "case", "IN": "isin", "BETWEEN": "between", "LIKE": "like", "ILIKE": "ilike",
                  "RLIKE": "rlike", "REGEXP": "rlike", "NOT": "not", "AND": "and", "OR": "or", "EXISTS": "exists",
                  "DIV": "div"}
_ENDS = {"ident", "num", "str"}


def _is_end(t: Tok) -> bool:
    """Whether a token can end an expression (so an identifier after it is an alias)."""
    if t.kind == "ident":
        return t.quoted or t.up not in RESERVED or t.up in ("END", "NULL", "TRUE", "FALSE")
    return t.kind in ("num", "str") or t.text in (")", "]")


def close_of(toks: list, i: int) -> int:
    """Index of the bracket closing the one at i (or the last index)."""
    depth = 0
    for j in range(i, len(toks)):
        if toks[j].text in ("(", "["):
            depth += 1
        elif toks[j].text in (")", "]"):
            depth -= 1
            if depth == 0:
                return j
    return len(toks) - 1


def split_top(toks: list, sep: str = ",") -> list:
    """Split tokens on sep at the top level (outside brackets)."""
    out, cur, depth = [], [], 0
    for t in toks:
        if t.text in ("(", "["):
            depth += 1
        elif t.text in (")", "]"):
            depth -= 1
        if depth == 0 and t.kind == "op" and t.text == sep:
            out.append(cur)
            cur = []
            continue
        cur.append(t)
    if cur:
        out.append(cur)
    return out


def top_word(toks: list, word: str, start: int = 0) -> int:
    """Index of a keyword at the top level (outside brackets), or -1."""
    depth = 0
    for i in range(start, len(toks)):
        t = toks[i]
        if t.text in ("(", "["):
            depth += 1
        elif t.text in (")", "]"):
            depth -= 1
        elif depth == 0 and t.up == word:
            return i
    return -1


def split_alias(toks: list) -> tuple[list, list]:
    """(expression tokens, aliases): x AS y, x y, explode(m) AS (k, v)."""
    i = top_word(toks, "AS")
    if i > 0:
        return toks[:i], [x.text for x in toks[i + 1:] if x.kind == "ident"]
    if len(toks) >= 2 and toks[-1].kind == "ident" and (toks[-1].quoted or toks[-1].up not in RESERVED) and \
            _is_end(toks[-2]) and toks[-2].text != "." and toks[-1].up not in _UNITS and \
            top_word(toks, "INTERVAL") < 0:
        return toks[:-1], [toks[-1].text]
    return toks, []


def _lambda_vars(toks: list) -> set:
    """x -> ..., (x, y) -> ...: names that are lambda variables, not columns."""
    out = set()
    for i, t in enumerate(toks):
        if t.text != "->":
            continue
        j = i - 1
        if j >= 0 and toks[j].kind == "ident":
            out.add(toks[j].text.lower())
        elif j >= 0 and toks[j].text == ")":
            while j >= 0 and toks[j].text != "(":
                if toks[j].kind == "ident":
                    out.add(toks[j].text.lower())
                j -= 1
    return out


def sql_expr(toks: list, sql: str) -> Expr:
    """What one SQL expression reads and does. toks: its tokens; sql: the text they point into."""
    if not toks:
        return Expr(literal=True)
    text = " ".join(sql[toks[0].start:toks[-1].end].split())
    if len(toks) == 1 and toks[0].text == "*":
        return Expr(text="*", star="")
    if len(toks) == 3 and toks[0].kind == "ident" and toks[1].text == "." and toks[2].text == "*":
        return Expr(text=text, star=toks[0].text)
    lambdas = _lambda_vars(toks)
    functions: list = []
    refs: list = []
    stack: list = []                              # what each open bracket belongs to ("over", a function, "")
    in_interval = False
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        prev = toks[i - 1] if i else None
        nxt = toks[i + 1] if i + 1 < n else None
        if t.text == "(":
            opener = "over" if prev is not None and prev.up == "OVER" else \
                (prev.text.lower() if prev is not None and prev.kind == "ident" else "")
            stack.append(opener)
            i += 1
            continue
        if t.text == "[":
            functions.append("getitem")
            stack.append("[")
            i += 1
            continue
        if t.text in (")", "]"):
            if stack:
                stack.pop()
            i += 1
            continue
        if t.kind == "op":
            in_interval = False
            if t.text == "*" and (prev is None or prev.text in ("(", ",")):
                pass                              # count(*)
            elif t.text == "::":
                functions.append("cast")
                i += 2                            # x::int: skip the type name
                continue
            elif t.text in OPERATOR_FUNCTIONS:
                functions.append(OPERATOR_FUNCTIONS[t.text])
            i += 1
            continue
        if t.kind in ("num", "str"):
            i += 1
            continue
        up = t.up
        if nxt is not None and nxt.text == "(" and not t.quoted:          # a function call
            name = t.text.lower()
            if up in ("CAST", "TRY_CAST"):        # CAST(<value> AS <type>): read the value, skip the type
                close = close_of(toks, i + 1)
                inner = toks[i + 2:close]
                k = top_word(inner, "AS")
                sub = sql_expr(inner[:k] if k >= 0 else inner, sql)
                functions.append(name)
                functions.extend(sub.functions)
                refs.extend(sub.refs)
                i = close + 1
                continue
            if up in _KEYWORD_FUNCS:
                functions.append(_KEYWORD_FUNCS[up])
            elif up == "OVER":
                functions.append("over")
            elif up in RESERVED and up not in ("FILTER", "LEFT", "RIGHT", "IF", "REPLACE"):
                pass
            elif name in TYPE_NAMES and name not in ("array", "map", "struct"):
                functions.append("cast")          # int(x), string(x), date(x)
            else:
                functions.append(name)
            i += 1
            continue
        if up == "INTERVAL":
            in_interval = True
            functions.append("make_interval")
            i += 1
            continue
        if in_interval and up in _UNITS:
            i += 1
            continue
        in_interval = False
        if up == "NULLS":
            i += 2 if nxt is not None and nxt.up in ("FIRST", "LAST") else 1
            continue
        if up == "IS":
            functions.append("isnull" if any(x.up == "NULL" for x in toks[i + 1:i + 3]) else "compare")
            i += 1
            continue
        if up in _KEYWORD_FUNCS:
            if not (stack and stack[-1] == "over" and up in ("AND", "BETWEEN")):
                functions.append(_KEYWORD_FUNCS[up])
            i += 1
            continue
        if up == "OVER":
            functions.append("over")
            i += 1
            continue
        if (stack and stack[-1] == "over" and up in _FRAME_WORDS) or up in RESERVED:
            i += 1
            continue
        if not t.quoted and t.text.lower() in BARE_FUNCTIONS:
            functions.append(t.text.lower())
            i += 1
            continue
        parts, j = [t.text], i                    # a column: a, t.a, t.struct.field
        while j + 2 < n and toks[j + 1].text == "." and toks[j + 2].kind == "ident":
            parts.append(toks[j + 2].text)
            j += 2
        if j + 2 < n and toks[j + 1].text == "." and toks[j + 2].text == "*":
            i = j + 3                             # count(t.*)
            continue
        if j + 1 < n and toks[j + 1].text == "(" and len(parts) > 1:
            functions.append(parts[-1].lower())   # db.my_function(...)
            i = j + 1
            continue
        if (j + 1 < n and toks[j + 1].text == "->") or parts[0].lower() in lambdas or any(DYN in p for p in parts):
            i = j + 1
            continue
        refs.append(Ref(parts[0]) if len(parts) == 1 else
                    Ref(parts[1], parts[0]))      # t.a; or struct.field when t isn't a qualifier
        i = j + 1
    seen, uniq = set(), []
    for r in refs:
        key = (r.name.lower(), r.qualifier.lower())
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    plain = all(t.kind == "ident" or t.text == "." for t in toks)
    column = len(uniq) == 1 and not functions and plain
    literal = not uniq and all(f in OPERATOR_FUNCTIONS.values() for f in functions)
    return Expr(refs=uniq, functions=functions, text=text, column=column, literal=literal)


def sql_select_item(toks: list, sql: str) -> Expr:
    """One item of a SELECT list: the expression and its alias."""
    body, names = split_alias(toks)
    e = sql_expr(body, sql) if body else Expr(literal=True)
    if names:
        e = replace(e, alias=names[0])
        if len(names) > 1:
            e.functions.append("as_many")
    return e


def sql_expression_text(text: str) -> Expr:
    """A SQL expression given as text (F.expr("..."), selectExpr("..."), filter("..."))."""
    toks = tokenize(text)
    return sql_select_item(toks, text) if toks else Expr(text=text, literal=True)


# ======================================================================= pyspark (Python)
F_MODULES = {"F", "f", "fn", "fns", "func", "funcs", "functions", "sf", "psf", "spark_functions", "SF", "fx"}
DATAFRAME_ATTRS = {"columns", "dtypes", "schema", "write", "writeStream", "na", "stat", "rdd", "sparkSession",
                   "isStreaming", "storageLevel", "shape", "index", "loc", "iloc", "T", "values", "empty"}


class ExprContext:
    """What the Python expression reader needs to know about the notebook (see dataframes.py)."""

    def strings(self, node) -> Optional[list]:           # every string value a node can have
        return None

    def items(self, node) -> Optional[list]:             # the strings in a list / tuple (cols = ["a", "b"])
        return None

    def frame_var(self, name: str) -> Optional[int]:      # DataFrame variable -> frame id
        return None

    def expr_var(self, name: str) -> Optional[Expr]:      # a variable holding a column expression
        return None

    def function_alias(self, name: str) -> Optional[str]:   # from pyspark.sql.functions import upper as up
        return None

    def is_functions_module(self, name: str) -> bool:     # F, functions, pyspark.sql.functions, ...
        return name in F_MODULES

    def helper(self, name: str) -> Optional[ast.FunctionDef]:   # a function of the notebook
        return None

    def is_udf(self, name: str) -> bool:
        return False

    def is_wildcard(self, name: str) -> bool:             # a loop variable over columns that can't be known
        return False

    def text(self, node) -> str:
        try:
            return ast.unparse(node)
        except Exception:  # noqa: BLE001
            return "?"


class _Inline(ast.NodeTransformer):
    def __init__(self, bindings: dict):
        self.bindings = bindings

    def visit_Name(self, node):
        return self.bindings.get(node.id, node)


class PyExprReader:
    def __init__(self, ctx: ExprContext):
        self.ctx = ctx

    def read(self, node, column_ctx: bool = True) -> Expr:
        e = self._e(node, column_ctx, 0)
        if not e.text:
            e.text = self.ctx.text(node)
        return e

    # ------------------------------------------------------------------
    def _names(self, node) -> Optional[list]:
        vals = self.ctx.strings(node)
        return [v for v in vals if isinstance(v, str)] if vals else None

    def _column(self, node, hint: int = -1) -> Expr:
        if isinstance(node, ast.Name) and self.ctx.is_wildcard(node.id):
            return Expr(text=node.id, wildcard=True, column=True)
        vals = self._names(node)
        if vals:
            refs = [ref_of(v, hint) for v in vals if v.strip() and v.strip() != "*"]
            if not refs and any(v.strip() == "*" for v in vals):
                return Expr(text="*", star="")
            if len(vals) == 1 and vals[0].strip().endswith(".*"):
                return Expr(text=vals[0], star=vals[0].strip()[:-2])
            return Expr(refs=refs, text=vals[0] if len(vals) == 1 else self.ctx.text(node), column=len(refs) == 1)
        return Expr(text=self.ctx.text(node), unknown=True)

    def _function_name(self, func) -> Optional[str]:
        if isinstance(func, ast.Attribute):
            root = func.value
            if isinstance(root, ast.Name) and self.ctx.is_functions_module(root.id):
                return func.attr.lower()
            dotted = []
            node = func.value
            while isinstance(node, ast.Attribute):
                dotted.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name):
                dotted.append(node.id)
                path = ".".join(reversed(dotted))
                if path in ("pyspark.sql.functions", "pyspark.sql.F"):
                    return func.attr.lower()
            return None
        if isinstance(func, ast.Name):
            alias = self.ctx.function_alias(func.id)
            return alias.lower() if alias else None
        return None

    def _e(self, node, colctx: bool, depth: int) -> Expr:
        ctx = self.ctx
        text = ctx.text(node)
        if depth > 40:
            return Expr(text=text, unknown=True)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and colctx:
                return self._column(node)
            return Expr(text=text, literal=True)
        if isinstance(node, (ast.JoinedStr, ast.BinOp)) and colctx:
            vals = self._names(node)
            if vals:
                return self._column(node)
        if isinstance(node, ast.Name):
            held = ctx.expr_var(node.id)
            if held is not None:
                return replace(held, refs=list(held.refs), functions=list(held.functions))
            if ctx.is_wildcard(node.id) and colctx:
                return Expr(text=node.id, wildcard=True, column=True)
            if colctx and self._names(node):
                return self._column(node)
            if ctx.frame_var(node.id) is not None:
                return Expr(text=text, unknown=True)
            return Expr(text=text, literal=not colctx, unknown=colctx)
        if isinstance(node, ast.Attribute):
            base = node.value
            if isinstance(base, ast.Name) and ctx.frame_var(base.id) is not None and node.attr not in DATAFRAME_ATTRS:
                return Expr(refs=[Ref(node.attr, "", ctx.frame_var(base.id))], text=text, column=True)
            inner = self._e(base, colctx, depth + 1)
            if node.attr in IGNORED_METHODS:
                return replace(inner, text=text)
            if inner.refs:
                return _merge(text, [inner], ["getfield"])
            return Expr(text=text, unknown=True)
        if isinstance(node, ast.Subscript):
            base = node.value
            if isinstance(base, ast.Name) and ctx.frame_var(base.id) is not None:
                key = node.slice
                vals = self._names(key)
                if vals:
                    return Expr(refs=[Ref(v, "", ctx.frame_var(base.id)) for v in vals], text=text,
                                column=len(vals) == 1)
                if isinstance(key, ast.Name) and ctx.is_wildcard(key.id):
                    return Expr(text=text, wildcard=True, column=True)
                inner = self._e(key, False, depth + 1)     # df[condition] (pandas filter) is not a column
                return replace(inner, unknown=True)
            inner = self._e(base, colctx, depth + 1)
            return _merge(text, [inner], ["getitem"])
        if isinstance(node, ast.Call):
            return self._call(node, colctx, depth, text)
        if isinstance(node, ast.BinOp):
            op = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Mod: "%", ast.Pow: "**",
                  ast.BitAnd: "&", ast.BitOr: "|", ast.FloorDiv: "/", ast.BitXor: "|"}.get(type(node.op), "+")
            parts = [self._e(node.left, False, depth + 1), self._e(node.right, False, depth + 1)]
            return _merge(text, parts, [OPERATOR_FUNCTIONS.get(op, op)])
        if isinstance(node, ast.Compare):
            parts = [self._e(x, False, depth + 1) for x in [node.left, *node.comparators]]
            return _merge(text, parts, ["compare"])
        if isinstance(node, ast.UnaryOp):
            inner = self._e(node.operand, False, depth + 1)
            fn = "not" if isinstance(node.op, (ast.Not, ast.Invert)) else "negative"
            return _merge(text, [inner], [fn])
        if isinstance(node, ast.BoolOp):
            parts = [self._e(x, False, depth + 1) for x in node.values]
            return _merge(text, parts, ["and" if isinstance(node.op, ast.And) else "or"])
        if isinstance(node, ast.IfExp):
            parts = [self._e(x, colctx, depth + 1) for x in (node.body, node.orelse)]
            return _merge(text, parts, ["if"])
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return _merge(text, [self._e(x, colctx, depth + 1) for x in node.elts])
        if isinstance(node, ast.Starred):
            return self._e(node.value, colctx, depth + 1)
        if isinstance(node, ast.Lambda):
            return Expr(text=text, functions=["lambda"], unknown=False)
        return Expr(text=text, unknown=True)

    def _args(self, call: ast.Call, literal_positions, depth: int, all_literal: bool = False) -> list:
        out = []
        pos = 0
        for a in call.args:
            if isinstance(a, ast.Starred):
                names = self.ctx.items(a.value) or self._names(a.value)
                if names is not None and not all_literal:
                    out.extend(self._column(ast.Constant(v)) for v in names)
                else:
                    out.append(self._e(a.value, not all_literal, depth + 1))
                pos += 1
                continue
            lit = all_literal or "*" in literal_positions or pos in literal_positions
            out.append(self._e(a, not lit, depth + 1))
            pos += 1
        for k in call.keywords:
            if k.value is not None:
                out.append(self._e(k.value, False, depth + 1))
        return out

    def _window(self, node, depth: int) -> Expr:
        """Window.partitionBy("a").orderBy("b") (or a variable holding one): the columns it uses."""
        if isinstance(node, ast.Name):
            held = self.ctx.expr_var(node.id)
            return held if held is not None else Expr(text=node.id)
        parts = []
        cur = node
        while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
            if cur.func.attr in ("partitionBy", "orderBy"):
                parts.extend(self._args(cur, frozenset(), depth))
            cur = cur.func.value
        return _merge(self.ctx.text(node), parts)

    def _call(self, node: ast.Call, colctx: bool, depth: int, text: str) -> Expr:
        ctx, func = self.ctx, node.func
        fname = self._function_name(func)
        if fname is not None:
            return self._function(fname, node, depth, text)
        if isinstance(func, ast.Attribute):
            method, recv = func.attr, func.value
            if isinstance(recv, ast.Name) and recv.id == "Window":
                return self._window(node, depth)
            if method in ("partitionBy", "orderBy", "rowsBetween", "rangeBetween") and \
                    isinstance(recv, ast.Call):
                return self._window(node, depth)
            if method in ("alias", "name", "as_"):
                inner = self._e(recv, colctx, depth + 1)
                names = self._names(node.args[0]) if node.args else None
                return replace(inner, alias=names[0] if names else inner.alias, text=text)
            if method == "over":
                inner = self._e(recv, colctx, depth + 1)
                w = self._window(node.args[0], depth) if node.args else Expr()
                return _merge(text, [inner, w], ["over"])
            if method in ("when", "otherwise"):
                inner = self._e(recv, colctx, depth + 1)
                return _merge(text, [inner] + self._args(node, frozenset(), depth, all_literal=True), [method])
            if method in IGNORED_METHODS:
                return replace(self._e(recv, colctx, depth + 1), text=text)
            if method in CUSTOM_METHODS:
                inner = self._e(recv, colctx, depth + 1)
                return _merge(text, [inner], [method + "()"])
            if method in COLUMN_METHODS:
                inner = self._e(recv, colctx, depth + 1)
                return _merge(text, [inner] + self._args(node, frozenset(), depth, all_literal=True),
                              [COLUMN_METHODS[method]])
            inner = self._e(recv, colctx, depth + 1)
            if inner.refs or inner.functions:
                return _merge(text, [inner] + self._args(node, frozenset(), depth, all_literal=True),
                              [method.lower()])
            return Expr(text=text, unknown=True)
        if isinstance(func, ast.Name):
            if ctx.is_udf(func.id):
                parts = self._args(node, frozenset(), depth)
                return _merge(text, parts, [func.id + "()"])
            fn = ctx.helper(func.id)
            if fn is not None:
                inlined = self._inline(fn, node, depth)
                if inlined is not None:
                    return replace(inlined, text=text, helper=func.id)
                return _merge(text, self._args(node, frozenset(), depth), [func.id + "()"])
            if func.id in ("str", "int", "float", "len", "round", "abs", "min", "max", "sum"):
                return Expr(text=text, literal=True, unknown=colctx)
            parts = self._args(node, frozenset(), depth)
            if any(p.refs for p in parts):
                return _merge(text, parts, [func.id + "()"])
            return Expr(text=text, unknown=True)
        if isinstance(func, ast.Call):             # F.udf(f, T.StringType())(F.col("a")), udf(...)(...)
            parts = self._args(node, frozenset(), depth)
            return _merge(text, parts, ["udf()"])
        return Expr(text=text, unknown=True)

    def _inline(self, fn: ast.FunctionDef, call: ast.Call, depth: int) -> Optional[Expr]:
        """A helper that returns a column expression: read its return expression with the call's arguments."""
        body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
        if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
            return None
        params = [a.arg for a in fn.args.args]
        bindings = {p: a for p, a in zip(params, call.args) if not isinstance(a, ast.Starred)}
        for k in call.keywords:
            if k.arg in params:
                bindings[k.arg] = k.value
        defaults = fn.args.defaults
        for p, d in zip(params[len(params) - len(defaults):], defaults):
            bindings.setdefault(p, d)
        import copy
        expr = _Inline(bindings).visit(copy.deepcopy(body[0].value))
        ast.fix_missing_locations(expr)
        return self._e(expr, True, depth + 5)

    def _function(self, fname: str, node: ast.Call, depth: int, text: str) -> Expr:
        if fname in ("col", "column"):
            if not node.args:
                return Expr(text=text, unknown=True)
            arg = node.args[0]
            if isinstance(arg, ast.Name) and self.ctx.is_wildcard(arg.id):
                return Expr(text=text, wildcard=True, column=True)
            e = self._column(arg)
            return replace(e, text=text)
        if fname in ("lit", "typedlit"):
            return Expr(text=text, functions=["lit"], literal=True)
        if fname == "expr":
            vals = self._names(node.args[0]) if node.args else None
            if vals:
                return replace(sql_expression_text(vals[0]), text=text)
            return Expr(text=text, unknown=True, functions=["expr"])
        if fname in ("broadcast", "asc", "desc", "asc_nulls_first", "asc_nulls_last", "desc_nulls_first",
                     "desc_nulls_last"):
            return replace(self._e(node.args[0], True, depth + 1), text=text) if node.args else Expr(text=text)
        if fname in ("udf", "pandas_udf"):
            return Expr(text=text, functions=["udf()"])
        if fname == "count" and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "*":
            return Expr(text=text, functions=["count"])
        parts = self._args(node, LITERAL_ARGS.get(fname, frozenset()), depth)
        return _merge(text, parts, [fname])
