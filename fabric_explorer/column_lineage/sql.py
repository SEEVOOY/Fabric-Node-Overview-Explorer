"""SQL statements -> frames, definitions and writes.

Covers what notebooks use to move data: SELECT (with CTEs, subqueries, joins, WHERE, GROUP BY,
HAVING, QUALIFY, DISTINCT, LIMIT, UNION, LATERAL VIEW), INSERT INTO / OVERWRITE, CREATE [OR REPLACE]
TABLE / VIEW ... AS, temporary views and CACHE TABLE, MERGE INTO and UPDATE. DELETE, OPTIMIZE and the
like don't move columns, so they don't appear in the column lineage (the map shows them).

The same reader serves DuckDB SQL in Python notebooks (duck=True): there a name in FROM is a pandas
DataFrame of the notebook (DuckDB's replacement scans), and tables are read with delta_scan('path').
"""
from __future__ import annotations

from typing import Callable, Optional

from ..parse.sql import DYN, FORMATS
from .catalog import COLUMN, ROW
from .expressions import (RESERVED, Expr, close_of, split_top, sql_expr, sql_select_item, tokenize,
                          top_word)
from .model import Assignment, Builder, Dep, Entry, Sink

CLAUSES = ("FROM", "WHERE", "GROUP", "HAVING", "QUALIFY", "WINDOW", "ORDER", "LIMIT", "OFFSET", "LATERAL",
           "CLUSTER", "DISTRIBUTE", "SORT")
SET_OPS = {"UNION", "INTERSECT", "EXCEPT", "MINUS"}
JOIN_WORDS = {"JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "SEMI", "ANTI", "NATURAL", "OUTER"}
NOT_ALIAS = RESERVED | set(CLAUSES) | JOIN_WORDS | SET_OPS | {"ON", "USING", "TABLESAMPLE", "PIVOT", "UNPIVOT",
                                                              "WHEN", "SET", "VALUES"}
PATH_FUNCTIONS = {"delta_scan", "read_parquet", "parquet_scan", "read_csv", "read_csv_auto", "read_json",
                  "read_json_auto", "iceberg_scan", "read_delta"}
AGGREGATES = {"sum", "count", "avg", "mean", "min", "max", "first", "last", "collect_list", "collect_set",
              "count_distinct", "approx_count_distinct", "stddev", "variance", "percentile", "percentile_approx",
              "median", "any_value", "max_by", "min_by", "bool_and", "bool_or", "count_if", "array_agg",
              "listagg", "string_agg", "mode", "first_value", "last_value"}


class SqlReader:
    def __init__(self, b: Builder, frame_var: Optional[Callable[[str], Optional[int]]] = None):
        self.b = b
        self.frame_var = frame_var or (lambda name: None)
        self.sql, self.line0, self.key0, self.duck = "", 1, "", False

    def run(self, sql: str, line: int = 1, key: str = "", duck: bool = False) -> Optional[int]:
        """Read SQL text. line: the cell line its first character is on; key: a prefix for op keys.
        Returns the frame of the last query (what spark.sql(...) gives back), if any."""
        self.sql, self.line0, self.key0, self.duck = sql, line, key, duck
        last = None
        for st in split_top(tokenize(sql), ";"):
            if not st:
                continue
            try:
                r = self.statement(st)
            except (IndexError, ValueError, KeyError) as e:            # odd SQL: skip it, keep the rest
                self.b.note(f"Cell {self.b.cell}: a SQL statement couldn't be followed ({type(e).__name__}).")
                r = None
            if r is not None:
                last = r
        return last

    # ------------------------------------------------------------------ helpers
    def _line(self, t) -> int:
        return self.line0 + self.sql.count("\n", 0, t.start)

    def _key(self, t, tag: str) -> str:
        return f"{self.key0}{t.start}:{tag}"

    def _text(self, toks) -> str:
        return self.sql[toks[0].start:toks[-1].end] if toks else ""

    def _string(self, t) -> str:
        return self.sql[t.start + 1:t.end - 1] if t.kind == "str" else t.text

    @staticmethod
    def _name(toks, i) -> tuple[list, int]:
        parts = []
        while i < len(toks) and toks[i].kind == "ident":
            parts.append(toks[i].text)
            if i + 2 < len(toks) and toks[i + 1].text == "." and toks[i + 2].kind == "ident":
                i += 2
                continue
            i += 1
            break
        return parts, i

    def _op(self, t, scope: str, functions, label: str, code: str, category: str = "", reads=(), detail: str = "",
            tag: str = "") -> str:
        return self.b.op(self._key(t, tag or scope), scope, functions, label, code, self._line(t), category=category,
                         reads=reads, detail=detail)

    def _table(self, parts) -> Optional[object]:
        if len(parts) == 2 and parts[0].lower() in FORMATS and ("/" in parts[1] or ":" in parts[1]):
            return self.b.table([], parts[1])
        if any(DYN in p for p in parts):
            self.b.note(f"Cell {self.b.cell}: a table name is built while the notebook runs, so its columns can't be "
                        "followed.")
            return None
        return self.b.table(parts)

    # ------------------------------------------------------------------ statements
    def statement(self, st) -> Optional[int]:
        i, ctes = 0, {}
        if st[0].up == "WITH":
            i, ctes = self._ctes(st, 1, {})
        if i >= len(st):
            return None
        head = st[i].up
        if head in ("SELECT", "VALUES", "TABLE", "FROM") or st[i].text == "(":
            return self.query(st[i:], ctes)
        if head == "INSERT":
            return self._insert(st, i, ctes)
        if head in ("CREATE", "REPLACE"):
            return self._create(st, i, ctes)
        if head == "MERGE":
            return self._merge(st, i, ctes)
        if head == "UPDATE":
            return self._update(st, i, ctes)
        if head == "USE":
            j = i + 1 + (1 if i + 1 < len(st) and st[i + 1].up in ("DATABASE", "SCHEMA") else 0)
            parts, _ = self._name(st, j)
            if parts:
                self.b.database = ".".join(parts)
            return None
        if head == "CACHE":
            j = top_word(st, "AS", i)
            if j > 0:
                names = [t for t in st[i:j] if t.kind == "ident" and t.up not in ("CACHE", "LAZY", "TABLE")]
                if names:
                    self.b.views[names[-1].text.lower()] = self.query(st[j + 1:], ctes)
        return None

    def _ctes(self, st, i, ctes) -> tuple[int, dict]:
        ctes = dict(ctes)
        if i < len(st) and st[i].up == "RECURSIVE":
            i += 1
        while i < len(st) and st[i].kind == "ident":
            name = st[i].text
            i += 1
            cols = []
            if i < len(st) and st[i].text == "(":
                close = close_of(st, i)
                cols = [t.text for t in st[i + 1:close] if t.kind == "ident"]
                i = close + 1
            if i < len(st) and st[i].up == "AS":
                i += 1
            if i >= len(st) or st[i].text != "(":
                break
            close = close_of(st, i)
            fid = self.query(st[i + 1:close], ctes)
            if cols:
                fid = self.b.frame(entries=[Entry(star=fid)], label=name, rename_to=cols)
            ctes[name.lower()] = fid
            i = close + 1
            if i < len(st) and st[i].text == ",":
                i += 1
                continue
            break
        return i, ctes

    def _insert(self, st, i, ctes) -> None:
        i += 1
        mode = "append"
        if i < len(st) and st[i].up == "OVERWRITE":
            mode = "overwrite"
        i += 1
        if i < len(st) and st[i].up == "TABLE":
            i += 1
        parts, i = self._name(st, i)
        if i < len(st) and st[i].up == "PARTITION":
            i = close_of(st, i + 1) + 1
        cols = []
        if i < len(st) and st[i].text == "(" and i + 1 < len(st) and st[i + 1].up not in ("SELECT", "WITH"):
            close = close_of(st, i)
            cols = [t.text for t in st[i + 1:close] if t.kind == "ident"]
            i = close + 1
        by_name = False
        if i + 1 < len(st) and st[i].up == "BY" and st[i + 1].up == "NAME":
            by_name, i = True, i + 2
        if i < len(st) and st[i].up == "WITH":
            j, ctes = self._ctes(st, i + 1, ctes)
            i = j
        if i >= len(st):
            return None
        fid = self.query(st[i:], ctes)
        ref = self._table(parts)
        if ref is not None and fid is not None:
            self.b.sink(Sink(ref, fid, "INSERT " + ("OVERWRITE" if mode == "overwrite" else "INTO"), mode,
                             self.b.cell, self._line(st[0]), by_position=not by_name and not cols, columns=cols,
                             code=self._text(st)))
        return None

    def _create(self, st, i, ctes) -> None:
        replace_ = st[i].up == "REPLACE"
        i += 1
        if i + 1 < len(st) and st[i].up == "OR" and st[i + 1].up == "REPLACE":
            replace_, i = True, i + 2
        temp = False
        while i < len(st) and st[i].up in ("TEMP", "TEMPORARY", "GLOBAL", "EXTERNAL", "MATERIALIZED", "LIVE",
                                           "STREAMING"):
            temp = temp or st[i].up in ("TEMP", "TEMPORARY")
            i += 1
        if i >= len(st) or st[i].up not in ("TABLE", "VIEW"):
            return None
        kind = st[i].up
        i += 1
        if i + 2 < len(st) and st[i].up == "IF" and st[i + 1].up == "NOT":
            i += 3
        parts, i = self._name(st, i)
        j = top_word(st, "AS", i)
        if j < 0 or not parts:
            return None                                                  # a table definition, no data
        body = st[j + 1:]
        if body and body[0].up == "WITH":
            k, ctes = self._ctes(body, 1, ctes)
            body = body[k:]
        fid = self.query(body, ctes)
        if temp:
            self.b.views[parts[-1].lower()] = fid
            return None
        ref = self._table(parts)
        if ref is not None:
            self.b.sink(Sink(ref, fid, f"CREATE {'OR REPLACE ' if replace_ else ''}{kind}",
                             "replace" if replace_ else "create", self.b.cell, self._line(st[0]),
                             code=self._text(st)))
        return None

    def _merge(self, st, i, ctes) -> None:
        i += 1
        if i < len(st) and st[i].up == "INTO":
            i += 1
        parts, i = self._name(st, i)
        talias = ""
        if i < len(st) and st[i].up == "AS":
            i += 1
        if i < len(st) and st[i].kind == "ident" and st[i].up != "USING":
            talias, i = st[i].text, i + 1
        if i >= len(st) or st[i].up != "USING":
            return None
        src, i, _, salias = self._relation(st, i + 1, ctes)
        if i >= len(st) or st[i].up != "ON":
            return None
        w = top_word(st, "WHEN", i)
        cond = st[i + 1:w if w > 0 else len(st)]
        e = sql_expr(cond, self.sql)
        tq = {talias.lower(), parts[-1].lower()} if parts else {talias.lower()}
        reads = [Dep(src, r.name, r.qualifier) for r in e.refs if r.qualifier.lower() not in tq]
        op = self._op(st[0], ROW, ["merge"], f"merge on {e.text}", self._text(st), category="merge",
                      reads=reads, detail="MERGE INTO", tag="merge")
        assignments, every = [], False
        while 0 < w < len(st):
            nxt = top_word(st, "WHEN", w + 1)
            clause = st[w + 1:nxt if nxt > 0 else len(st)]
            t = top_word(clause, "THEN")
            action = clause[t + 1:] if t >= 0 else []
            if action and action[0].up in ("UPDATE", "INSERT") and any(x.text == "*" for x in action[:3]):
                every = True
            elif action and action[0].up == "UPDATE":
                assignments += self._set(action[2:], src, tq, op)
            elif action and action[0].up == "INSERT" and len(action) > 1 and action[1].text == "(":
                close = close_of(action, 1)
                cols = [x.text for x in action[2:close] if x.kind == "ident"]
                v = top_word(action, "VALUES", close + 1)
                if v > 0 and v + 1 < len(action):
                    vclose = close_of(action, v + 1)
                    for col, item in zip(cols, split_top(action[v + 2:vclose])):
                        assignments.append(self._assign(col, item, src, tq, op))
            w = nxt
        ref = self._table(parts)
        if ref is not None:
            self.b.sink(Sink(ref, src, "MERGE INTO", "merge", self.b.cell, self._line(st[0]), merge_op=op,
                             assignments=assignments, all_columns=every, code=self._text(st)))
        return None

    def _set(self, toks, src, tq, op) -> list:
        out = []
        for item in split_top(toks):
            eq = next((k for k, t in enumerate(item) if t.text in ("=", "==")), -1)
            if eq > 0:
                out.append(self._assign(item[eq - 1].text, item[eq + 1:], src, tq, op))
        return out

    def _assign(self, target: str, toks, src, tq, op) -> Assignment:
        """target = expression (MERGE ... UPDATE SET / INSERT VALUES); the target's own columns don't count."""
        e = sql_expr(toks, self.sql)
        deps = [Dep(src, r.name, r.qualifier) for r in e.refs if r.qualifier.lower() not in tq]
        ops = []
        if toks and not (e.column and e.refs and e.refs[0].name.lower() == target.lower()):
            text = f"{target} = {e.text}"
            ops.append(self._op(toks[0], COLUMN, e.functions, text, text, category="rename" if e.column else ""))
        return Assignment(target, Entry(name=target, deps=deps, ops=ops))

    def _update(self, st, i, ctes) -> None:
        parts, i = self._name(st, i + 1)
        alias = ""
        if i < len(st) and st[i].up == "AS":
            i += 1
        if i < len(st) and st[i].kind == "ident" and st[i].up != "SET":
            alias, i = st[i].text, i + 1
        if i >= len(st) or st[i].up != "SET" or not parts:
            return None
        ref = self._table(parts)
        if ref is None:
            return None
        src = self.b.star(self.b.source(ref, self._line(st[0])), alias=alias or parts[-1])
        w = top_word(st, "WHERE", i)
        reads = []
        if w > 0:
            reads = [Dep(src, r.name, r.qualifier) for r in sql_expr(st[w + 1:], self.sql).refs]
        cond = " ".join(self._text(st[w + 1:]).split()) if w > 0 else "every row"
        op = self._op(st[0], ROW, ["merge"], f"update where {cond}", self._text(st), category="merge",
                      reads=reads, detail="UPDATE", tag="update")
        assignments = []
        for item in split_top(st[i + 1:w if w > 0 else len(st)]):
            eq = next((k for k, t in enumerate(item) if t.text in ("=", "==")), -1)
            if eq > 0:
                e = sql_expr(item[eq + 1:], self.sql)
                target = item[eq - 1].text
                deps = [Dep(src, r.name, r.qualifier) for r in e.refs]
                ops = [] if e.column and e.refs[0].name.lower() == target.lower() else \
                    [self._op(item[0], COLUMN, e.functions, f"{target} = {e.text}", self._text(item))]
                assignments.append(Assignment(target, Entry(name=target, deps=deps, ops=ops)))
        self.b.sink(Sink(ref, src, "UPDATE", "update", self.b.cell, self._line(st[0]), merge_op=op,
                         assignments=assignments, all_columns=False, code=self._text(st)))
        return None

    # ------------------------------------------------------------------ queries
    def query(self, toks, ctes) -> int:
        if toks and toks[0].up == "WITH":
            i, ctes = self._ctes(toks, 1, ctes)
            toks = toks[i:]
        pieces, ops, start, depth = [], [], 0, 0
        for j, t in enumerate(toks):
            if t.text in ("(", "["):
                depth += 1
            elif t.text in (")", "]"):
                depth -= 1
            elif depth == 0 and t.up in SET_OPS and j > start:
                pieces.append(toks[start:j])
                ops.append(t)
                start = j + 1 + (1 if j + 1 < len(toks) and toks[j + 1].up in ("ALL", "DISTINCT") else 0)
        pieces.append(toks[start:])
        frames = [self.select_core(p, ctes) for p in pieces]
        fid = frames[0]
        for t, f in zip(ops, frames[1:]):
            if t.up == "UNION":
                op = self._op(t, ROW, ["union"], "union", self._text(toks), category="join", tag="union")
                fid = self.b.frame(combine="union", parents=[fid, f], row_ops=[op], label="union")
            else:
                op = self._op(t, ROW, [t.text.lower()], t.text.lower(), self._text(toks), category="join",
                              tag=t.up.lower())
                fid = self.b.star(fid, row_ops=[op])
        return fid

    def select_core(self, toks, ctes) -> int:
        if not toks:
            return self.b.opaque("empty query", self.line0)
        if toks[0].text == "(":
            close = close_of(toks, 0)
            fid = self.query(toks[1:close], ctes)
            rest = toks[close + 1:]
            k = top_word(rest, "LIMIT")
            if k >= 0:
                fid = self.b.star(fid, row_ops=[self._op(rest[k], ROW, ["limit"], self._text(rest[k:]),
                                                         self._text(rest[k:]), category="filter", tag="limit")])
            return fid
        if toks[0].up == "VALUES":
            return self._values(toks)
        if toks[0].up == "TABLE":
            fid, _, _, _ = self._relation(toks, 1, ctes)
            return fid
        if toks[0].up == "FROM":                       # FROM t SELECT ...
            s = top_word(toks, "SELECT")
            if s > 0:
                toks = [toks[s]] + toks[:s] + toks[s + 1:]
        if toks[0].up != "SELECT":
            return self.b.opaque("not a query", self.line0)
        i = 1
        distinct = False
        if i < len(toks) and toks[i].up in ("DISTINCT", "ALL"):
            distinct, i = toks[i].up == "DISTINCT", i + 1
        marks, depth = [], 0
        for j in range(i, len(toks)):
            t = toks[j]
            if t.text in ("(", "["):
                depth += 1
            elif t.text in (")", "]"):
                depth -= 1
            elif depth == 0 and t.up in CLAUSES:
                if t.up in ("GROUP", "ORDER", "CLUSTER", "DISTRIBUTE", "SORT") and \
                        not (j + 1 < len(toks) and toks[j + 1].up == "BY"):
                    continue
                if t.up == "LATERAL" and not (j + 1 < len(toks) and toks[j + 1].up == "VIEW"):
                    continue
                marks.append(j)
        marks.append(len(toks))
        items = split_top(toks[i:marks[0]])
        clauses: dict = {}
        laterals = []
        for a, b in zip(marks, marks[1:]):
            word = toks[a].up
            body = toks[a + 1:b]
            if word in ("GROUP", "ORDER", "CLUSTER", "DISTRIBUTE", "SORT"):
                body = body[1:]                        # BY
            if word == "LATERAL":
                laterals.append(toks[a:b])
            else:
                clauses.setdefault(word, (toks[a], body))
        fid = self.from_clause(clauses["FROM"][1], ctes) if "FROM" in clauses else \
            self.b.frame(label="(no FROM)", position=(self.b.cell, self.line0))
        for lv in laterals:
            fid = self._lateral(fid, lv)
        if "WHERE" in clauses:
            fid = self._filter(fid, *clauses["WHERE"], "where")
        exprs = [sql_select_item(it, self.sql) for it in items]
        group = clauses.get("GROUP")
        aggregated = any("over" not in e.functions and any(f in AGGREGATES for f in e.functions) for e in exprs)
        if group is not None or aggregated:
            keys_toks = split_top(group[1]) if group is not None else []
            reads, labels = [], []
            for k in keys_toks:
                if len(k) == 1 and k[0].kind == "num" and 0 < int(float(k[0].text)) <= len(exprs):
                    ke = exprs[int(float(k[0].text)) - 1]
                elif len(k) == 1 and k[0].up == "ALL":
                    for ke in exprs:
                        if not any(f in AGGREGATES for f in ke.functions):
                            reads += [Dep(fid, r.name, r.qualifier) for r in ke.refs]
                            labels.append(ke.name)
                    continue
                else:
                    ke = sql_expr(k, self.sql)
                reads += [Dep(fid, r.name, r.qualifier) for r in ke.refs]
                labels.append(ke.text)
            where = group[0] if group is not None else toks[0]
            code = "GROUP BY " + self._text(group[1]) if group is not None else "aggregate over all rows"
            gop = self._op(where, ROW, ["group"], ("group by " + ", ".join(labels)) if labels else
                           "aggregate all rows", code, category="group", reads=reads, tag="group")
            fid = self.b.frame(entries=[self._entry(e, it, fid) for e, it in zip(exprs, items)], row_ops=[gop],
                               position=(self.b.cell, self._line(where)))
            if "HAVING" in clauses:
                fid = self._filter(fid, *clauses["HAVING"], "having")
        else:
            fid = self.b.frame(entries=[self._entry(e, it, fid) for e, it in zip(exprs, items)],
                               position=(self.b.cell, self._line(toks[0])))
        if "QUALIFY" in clauses:
            fid = self._filter(fid, *clauses["QUALIFY"], "qualify")
        if distinct:
            fid = self.b.star(fid, row_ops=[self._op(toks[1], ROW, ["distinct"], "distinct rows", "SELECT DISTINCT",
                                                     category="filter", tag="distinct")])
        if "LIMIT" in clauses:
            t, body = clauses["LIMIT"]
            fid = self.b.star(fid, row_ops=[self._op(t, ROW, ["limit"], "limit " + self._text(body),
                                                     "LIMIT " + self._text(body), category="filter", tag="limit")])
        return fid

    def _entry(self, e: Expr, toks, fid: int) -> Entry:
        if e.star is not None:
            return Entry(star=fid, qualifier=e.star)
        deps = [Dep(fid, r.name, r.qualifier) for r in e.refs]
        if e.column and not e.renames:
            return Entry(name=e.name, deps=deps)
        text = " ".join(self._text(toks).split())
        op = self._op(toks[0], COLUMN, e.functions, text, text, category="rename" if e.column else "")
        return Entry(name=e.name, deps=deps, ops=[op])

    def _filter(self, fid: int, word_tok, toks, word: str) -> int:
        e = sql_expr(toks, self.sql)
        text = " ".join(self._text(toks).split())
        op = self._op(word_tok, ROW, ["filter"] + e.functions, f"{word} {text}", f"{word.upper()} {text}",
                      category="filter", reads=[Dep(fid, r.name, r.qualifier) for r in e.refs], tag=word)
        return self.b.star(fid, row_ops=[op])

    def _values(self, toks) -> int:
        close = close_of(toks, 1) if len(toks) > 1 and toks[1].text == "(" else len(toks) - 1
        n = len(split_top(toks[2:close])) if close > 2 else 1
        op = self._op(toks[0], COLUMN, ["lit"], "VALUES (…)", self._text(toks), category="constant", tag="values")
        return self.b.frame(entries=[Entry(name=f"col{k + 1}", ops=[op]) for k in range(n)], label="VALUES")

    def _lateral(self, fid: int, toks) -> int:
        """LATERAL VIEW [OUTER] explode(x) t AS a, b"""
        i = 2 + (1 if len(toks) > 2 and toks[2].up == "OUTER" else 0)
        if i >= len(toks) or toks[i].kind != "ident":
            return fid
        close = close_of(toks, i + 1)
        e = sql_expr(toks[i:close + 1], self.sql)
        a = top_word(toks, "AS", close)
        names = [t.text for t in toks[a + 1:] if t.kind == "ident"] if a > 0 else ["col"]
        op = self._op(toks[0], COLUMN, e.functions, self._text(toks), self._text(toks), tag="lateral")
        deps = [Dep(fid, r.name, r.qualifier) for r in e.refs]
        return self.b.frame(entries=[Entry(star=fid)] + [Entry(name=nm, deps=deps, ops=[op]) for nm in names])

    def from_clause(self, toks, ctes) -> int:
        fid, i, n = None, 0, len(toks)
        while i < n:
            words = []
            if toks[i].text == ",":
                words, i = ["CROSS", "JOIN"], i + 1
            while i < n and toks[i].up in JOIN_WORDS and not (i + 1 < n and toks[i + 1].text == "("):
                words.append(toks[i].up)
                i += 1
            if i >= n:
                break
            start_tok = toks[i - 1] if words and i > 0 else toks[i]
            rel, i, label, _ = self._relation(toks, i, ctes)
            if fid is None:
                fid = rel
                continue
            cond, using = [], []
            if i < n and toks[i].up == "ON":
                j = i + 1
                depth = 0
                while j < n:
                    t = toks[j]
                    if t.text in ("(", "["):
                        depth += 1
                    elif t.text in (")", "]"):
                        depth -= 1
                    elif depth == 0 and (t.text == "," or (t.up in JOIN_WORDS and not (j + 1 < n and
                                                                                     toks[j + 1].text == "("))):
                        break
                    j += 1
                cond, i = toks[i + 1:j], j
            elif i < n and toks[i].up == "USING" and i + 1 < n and toks[i + 1].text == "(":
                close = close_of(toks, i + 1)
                using = [t.text for t in toks[i + 2:close] if t.kind == "ident"]
                i = close + 1
            fid = self._join(fid, rel, words, cond, using, label, start_tok)
        return fid if fid is not None else self.b.opaque("no table", self.line0)

    def _join(self, left: int, right: int, words, cond, using, label: str, tok) -> int:
        kind = " ".join(w.lower() for w in words if w not in ("JOIN", "OUTER")) or "inner"
        semi = "SEMI" in words or "ANTI" in words
        pre = self.b.frame(entries=[Entry(star=left), Entry(star=right)], label="join input")
        reads = []
        cond_text = ""
        if cond:
            e = sql_expr(cond, self.sql)
            reads = [Dep(pre, r.name, r.qualifier) for r in e.refs]
            cond_text = " on " + e.text
        for u in using:
            reads += [Dep(left, u), Dep(right, u)]
            cond_text = " using " + ", ".join(using)
        op = self._op(tok, ROW, ["join"], f"{kind} join {label}{cond_text}",
                      " ".join(f"{kind.upper()} JOIN {label}{cond_text}".split()), category="join", reads=reads,
                      detail=f"{kind} join", tag="join")
        entries = [Entry(star=left)]
        if not semi:
            entries.append(Entry(star=right, exclude=frozenset(u.lower() for u in using)))
        return self.b.frame(entries=entries, row_ops=[op], label=f"{kind} join")

    def _relation(self, toks, i, ctes) -> tuple[int, int, str, str]:
        """(frame, next index, label, qualifier) for one item of a FROM clause."""
        n = len(toks)
        label, qual = "", ""
        if i < n and toks[i].up == "LATERAL":
            i += 1
        t = toks[i]
        if t.text == "(":
            close = close_of(toks, i)
            inner = toks[i + 1:close]
            if inner and (inner[0].up in ("SELECT", "WITH", "VALUES", "TABLE", "FROM") or inner[0].text == "("):
                fid = self.query(inner, ctes)
            else:
                fid = self.from_clause(inner, ctes)
            i, label = close + 1, "(subquery)"
        elif t.kind == "ident" and i + 1 < n and toks[i + 1].text == "(":
            fn = t.text.lower()
            close = close_of(toks, i + 1)
            args = toks[i + 2:close]
            if fn in PATH_FUNCTIONS and args and args[0].kind == "str":
                path = self._string(args[0])
                ref = self.b.table([], path)
                fid = self.b.source(ref, self._line(t)) if ref is not None else \
                    self.b.opaque(f"{fn}('{path}')", self._line(t))
                label = ref.label if ref is not None else fn
            elif fn == "range":
                op = self._op(t, COLUMN, ["sequence"], self._text(toks[i:close + 1]), self._text(toks[i:close + 1]),
                              category="number", tag="range")
                fid, label = self.b.frame(entries=[Entry(name="id", ops=[op])], label="range"), "range"
            else:
                fid, label = self.b.opaque(f"the table function {fn}()", self._line(t)), fn
            i = close + 1
        else:
            parts, i = self._name(toks, i)
            name = parts[-1] if parts else "?"
            label, qual = ".".join(parts), name
            local = ctes.get(name.lower()) if len(parts) == 1 else None
            if local is None and len(parts) == 1:
                local = self.b.view(name)
            if local is None and len(parts) == 1 and self.duck:     # DuckDB reads a pandas variable by its name
                local = self.frame_var(name)
            if local is not None:
                fid = local
            elif self.duck:
                fid = self.b.opaque(f"{label} (not a lakehouse table)", self._line(t), label)
            else:
                ref = self._table(parts)
                fid = self.b.source(ref, self._line(t)) if ref is not None else \
                    self.b.opaque(f"the table {label}", self._line(t), label)
                if ref is not None:
                    label = ref.label
        if i < n and toks[i].up == "TABLESAMPLE":
            i = close_of(toks, i + 1) + 1
        if i < n and toks[i].up == "AS":
            i += 1
        if i < n and toks[i].kind == "ident" and (toks[i].quoted or toks[i].up not in NOT_ALIAS):
            qual, i = toks[i].text, i + 1
            if i < n and toks[i].text == "(":            # t(a, b): new names for its columns
                close = close_of(toks, i)
                names = [x.text for x in toks[i + 1:close] if x.kind == "ident"]
                fid = self.b.frame(entries=[Entry(star=fid)], rename_to=names, label=qual)
                i = close + 1
        if qual:
            fid = self.b.star(fid, alias=qual, label=qual)
        return fid, i, label or qual, qual
