"""A notebook's program, run against the datasets' real columns -> the path every column takes.

A path is a tuple of nodes from where a value starts to where it is written:

    ("c", <table id>, <column, lower case>)   a dataset's column ("*": the columns that couldn't be read)
    ("o", <op key>)                           a definition the value passes through

Frames only ever refer to frames made before them, so they are worked out in order, without
recursion. Columns of a table that can't be read are learned from the code: a name the code uses
on it becomes one of its columns (marked as inferred), and a "*" stands for the rest. Learning a
name means working the frames out again, so this runs at most three times.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from .catalog import COLUMN
from .model import Dep, Frame, Program, Sink, TableRef

MAX_PATHS = 150
STAR = "*"

# a table -> its columns [(name, type)], or None when they can't be read
ColumnsOf = Callable[[TableRef], Optional[list]]


@dataclass(frozen=True)
class RCol:
    name: str
    paths: tuple
    quals: frozenset = frozenset()
    frames: frozenset = frozenset()
    type: str = ""
    inferred: bool = False
    star: bool = False


@dataclass
class SourceColumn:
    name: str
    type: str = ""
    inferred: bool = False                 # named by the code; the table's columns couldn't be read
    star: bool = False                     # the table's other columns


@dataclass
class SinkColumn:
    name: str
    type: str = ""
    paths: tuple = ()
    status: str = ""                       # unchanged | renamed | altered | new | kept | unknown
    in_table: bool = True                  # the table, as read from OneLake, has it
    inferred: bool = False
    star: bool = False


@dataclass
class Write:
    sink: Sink
    columns: list = field(default_factory=list)


@dataclass
class Resolution:
    program: Program
    sources: dict                          # table id -> [SourceColumn]
    writes: list                           # [Write]
    conditions: list                       # paths ending at a row operation that looks at the column
    read_first: set                        # tables read before they are written (an update in place)
    notes: list

    def status_of(self, table_id: str, column: str) -> str:
        best = ""
        for w in self.writes:
            if w.sink.table.id == table_id:
                for c in w.columns:
                    if c.name.lower() == column.lower():
                        best = _stronger(best, c.status)
        return best


_RANK = {"": 0, "kept": 1, "unknown": 2, "unchanged": 3, "renamed": 4, "altered": 5, "new": 6}


def _stronger(a: str, b: str) -> str:
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


def _cap(paths) -> tuple:
    out = list(dict.fromkeys(paths))
    return tuple(out[:MAX_PATHS])


def _extend(paths, ops) -> tuple:
    if not ops:
        return tuple(paths)
    tail = tuple(("o", k) for k in ops)
    out = []
    for p in paths:
        q = p
        for t in tail:
            if not q or q[-1] != t:
                q = q + (t,)
        out.append(q)
    return _cap(out)


class Resolver:
    def __init__(self, program: Program, columns_of: ColumnsOf):
        self.p = program
        self.columns_of = columns_of
        self.found: dict = defaultdict(dict)        # table id -> {lower: name} learned from the code
        self.schemas: dict = {}
        self.notes: list = []
        self.cols: dict = {}
        self.conds: dict = {}
        self.writes: list = []

    def run(self) -> Resolution:
        for _ in range(3):
            before = sum(len(v) for v in self.found.values())
            self._pass()
            if sum(len(v) for v in self.found.values()) == before:
                break
        return self._result()

    # ------------------------------------------------------------------ one pass
    def _pass(self) -> None:
        self.cols, self.conds, self.notes = {}, {}, []
        for f in self.p.frames:
            self.cols[f.id] = self._frame(f)
            self._reads(f)
        self.writes = [self._sink(s) for s in self.p.sinks]

    def _schema(self, ref: TableRef) -> Optional[list]:
        if ref.id not in self.schemas:
            self.schemas[ref.id] = self.columns_of(ref)
        return self.schemas[ref.id]

    def _source(self, f: Frame) -> list:
        ref = f.source
        quals = frozenset(q for q in (f.alias.lower(),) if q)
        cols = self._schema(ref)
        out = []
        if cols is not None:
            for name, typ in cols:
                out.append(RCol(name, ((("c", ref.id, name.lower()),),), quals, frozenset([f.id]), typ))
        else:
            for low, name in self.found[ref.id].items():
                out.append(RCol(name, ((("c", ref.id, low),),), quals, frozenset([f.id]), inferred=True))
            out.append(RCol(STAR, ((("c", ref.id, STAR),),), quals, frozenset([f.id]), star=True))
        return out

    def _frame(self, f: Frame) -> list:
        if f.source is not None:
            return self._source(f)
        if f.opaque:
            return [RCol(STAR, (), frozenset(), frozenset([f.id]), star=True)]
        if f.combine:
            return self._union(f)
        out, slots = [], {}                         # a replaced or renamed column keeps its place, as in Spark
        for e in f.entries:
            if e.is_star:
                for c in self.cols.get(e.star, []):
                    if not c.star and c.name.lower() in e.exclude:
                        slots.setdefault(c.name.lower(), len(out))
                        out.append(None)
                        continue
                    if e.qualifier and e.qualifier.lower() not in c.quals:
                        continue
                    out.append(RCol(c.name, _extend(c.paths, list(f.row_ops) + list(e.ops)), c.quals,
                                    c.frames | {f.id}, c.type, c.inferred, c.star))
                continue
            paths = []
            for d in e.deps:
                for c in self._lookup(d):
                    paths.extend(c.paths)
            if paths:
                paths = _extend(_extend(paths, f.row_ops), e.ops)
            elif f.row_ops or e.ops:                  # made here: current_timestamp(), count(), lit(...)
                paths = _extend(((("o", (list(f.row_ops) + list(e.ops))[0]),),),
                                (list(f.row_ops) + list(e.ops))[1:])
            rc = RCol(e.name, tuple(paths), frozenset(), frozenset([f.id]))
            at = slots.pop(e.name.lower(), None)
            if at is None and len(e.deps) == 1:
                at = slots.pop(e.deps[0].name.lower(), None)
            if at is None:
                out.append(rc)
            else:
                out[at] = rc
        seen, uniq = {}, []
        for c in (c for c in out if c is not None):                                 # a later column with the same name replaces an earlier one
            key = (c.name.lower(), c.star, c.quals)
            if key in seen:
                uniq[seen[key]] = c
            else:
                seen[key] = len(uniq)
                uniq.append(c)
        out = uniq
        if f.alias:
            q = frozenset([f.alias.lower()])
            out = [RCol(c.name, c.paths, q, c.frames, c.type, c.inferred, c.star) for c in out]
        if f.rename_to:
            renamed = []
            for i, c in enumerate(out):
                if c.star or i >= len(f.rename_to):
                    renamed += out[i:]
                    break
                renamed.append(RCol(f.rename_to[i], c.paths, c.quals, c.frames, c.type, c.inferred, c.star))
            out = renamed
        return out

    def _union(self, f: Frame) -> list:
        parents = [self.cols.get(p, []) for p in f.parents]
        if not parents:
            return []
        first, out = parents[0], []
        if f.combine == "union":                      # by position
            for i, c in enumerate(first):
                paths = list(c.paths)
                for other in parents[1:]:
                    if c.star or i >= len(other) or any(x.star for x in other[:i + 1]):
                        paths += [p for x in other if (x.name.lower() == c.name.lower() or x.star) for p in x.paths]
                    else:
                        paths += list(other[i].paths)
                out.append(RCol(c.name, _extend(paths, f.row_ops), c.quals, c.frames | {f.id}, c.type, c.inferred,
                                c.star))
            return out
        names = []                                    # by name
        for cols in parents:
            for c in cols:
                key = (c.name.lower(), c.star)
                if key not in [(n.lower(), s) for n, s in names]:
                    names.append((c.name, c.star))
        for name, star in names:
            paths, typ = [], ""
            for cols in parents:
                for c in cols:
                    if c.name.lower() == name.lower() and c.star == star:
                        paths += list(c.paths)
                        typ = typ or c.type
            out.append(RCol(name, _extend(paths, f.row_ops), frozenset(), frozenset([f.id]), typ, star=star))
        return out

    def _lookup(self, d: Dep) -> list:
        cols = self.cols.get(d.frame)
        if cols is None:
            return []
        name, q = d.name.lower(), d.qualifier.lower()
        cands = [c for c in cols if not c.star and c.name.lower() == name]
        if q:
            qualified = [c for c in cands if q in c.quals]
            if qualified:
                cands = qualified
            elif not any(q in c.quals for c in cols):  # q.name: a field of the struct column q
                struct = [c for c in cols if not c.star and c.name.lower() == q]
                if struct:
                    return struct[:1]
            elif cands:
                cands = []
        if d.hint >= 0:
            hinted = [c for c in cands if d.hint in c.frames]
            cands = hinted or cands
        if cands:
            return cands[:1]
        out = []                                      # not known: it comes with the columns that couldn't be read
        for s in cols:
            if not s.star or (q and s.quals and q not in s.quals) or (d.hint >= 0 and d.hint not in s.frames):
                continue
            paths = []
            for p in s.paths:
                if p and p[0][0] == "c" and p[0][2] == STAR:
                    self.found[p[0][1]].setdefault(name, d.name)
                    paths.append((("c", p[0][1], name),) + p[1:])
            if paths:
                out.append(RCol(d.name, tuple(paths), s.quals, s.frames, inferred=True))
        if not out and not any(c.star for c in cols):
            note = f"The code uses a column {d.name} that isn't among the columns read for it."
            if note not in self.notes:
                self.notes.append(note)
        return out[:1]

    def _reads(self, f: Frame) -> None:
        """What row operations look at (filter conditions, join keys, group keys): condition paths."""
        for k in f.row_ops:
            op = self.p.ops.get(k)
            if op is None:
                continue
            for d in op.reads:
                if d.frame not in self.cols:
                    continue
                for c in self._lookup(d):
                    for p in c.paths:
                        self.conds[p + (("o", k),)] = True

    def _sink(self, s: Sink) -> Write:
        cols = self.cols.get(s.frame, [])
        target = self._schema(s.table) if s.table.kind == "table" else None
        merge = [s.merge_op] if s.merge_op else []
        written, assigned = [], set()
        for a in s.assignments:
            paths = []
            for d in a.entry.deps:
                for c in self._lookup(d):
                    paths.extend(c.paths)
            if paths:
                paths = _extend(_extend(paths, merge), a.entry.ops)
            elif merge or a.entry.ops:
                chain = merge + list(a.entry.ops)
                paths = _extend(((("o", chain[0]),),), chain[1:])
            written.append((a.target, tuple(paths), None))
            assigned.add(a.target.lower())
        if s.all_columns:
            for c in cols:
                if not c.star and c.name.lower() in assigned:
                    continue
                written.append((c.name, _extend(c.paths, merge), c))
        names = s.columns or ([n for n, _ in target] if s.by_position and target else [])
        if names and not any(c is not None and c.star for _, _, c in written):
            if len(names) == len(written) or s.columns:     # by position; Spark refuses a count that differs
                written = [(names[i] if i < len(names) else w[0], w[1], w[2]) for i, w in enumerate(written)]
            else:
                self.notes.append(f"{s.verb} into {s.table.label} (cell {s.cell}, line {s.line}) writes "
                                  f"{len(written)} columns into a table with {len(names)}; they are matched by "
                                  "name here.")
        out = []
        if target is None:
            for name, paths, c in written:
                out.append(SinkColumn(name, c.type if c else "", paths, inferred=bool(c and c.inferred),
                                      star=bool(c and c.star)))
        else:
            by = {n.lower(): (n, t) for n, t in target}
            seen = set()
            stars = [paths for _, paths, c in written if c is not None and c.star]
            for name, paths, c in written:
                if c is not None and c.star:
                    continue
                k = name.lower()
                if k in by:
                    if k in seen:
                        continue
                    seen.add(k)
                    out.append(SinkColumn(by[k][0], by[k][1], paths))
                else:
                    out.append(SinkColumn(name, c.type if c else "", paths, in_table=False))
            for n, t in target:
                if n.lower() in seen:
                    continue
                paths = []
                for sp in stars:                      # comes with the columns of a table that couldn't be read
                    for p in sp:
                        if p and p[0][0] == "c" and p[0][2] == STAR:
                            self.found[p[0][1]].setdefault(n.lower(), n)
                            paths.append((("c", p[0][1], n.lower()),) + p[1:])
                if paths:
                    out.append(SinkColumn(n, t, tuple(paths), inferred=True))
                else:
                    out.append(SinkColumn(n, t, (), status="kept"))
        for c in out:
            if c.status != "kept":
                c.status = self._status(c.name, c.paths)
        return Write(s, out)

    def _status(self, name: str, paths) -> str:
        if not paths:
            return "unknown"
        ops = [n[1] for p in paths for n in p if n[0] == "o" and n[1] in self.p.ops and
               self.p.ops[n[1]].scope == COLUMN]
        starts = [p[0] for p in paths]
        cols = [s for s in starts if s[0] == "c"]
        low = name.lower()
        same = any(s[2] in (low, STAR) for s in cols)
        if not ops:
            if cols and len(cols) == len(starts):
                return "unchanged" if all(s[2] in (low, STAR) for s in cols) else "renamed"
            return "new"
        if all(self.p.ops[k].category == "rename" for k in ops):
            return "unchanged" if same and all(s[2] in (low, STAR) for s in cols) and cols else "renamed"
        if all(self.p.ops[k].category in ("rename", "cast") for k in ops) and cols and len(cols) == len(starts) \
                and len({s[2] for s in cols}) == 1:          # one column, another name and type: still that column
            return "altered" if same else "renamed"
        return "altered" if same else "new"

    # ------------------------------------------------------------------ result
    def _result(self) -> Resolution:
        sources = {}
        reads = defaultdict(list)
        for f in self.p.frames:
            if f.source is None:
                continue
            ref = f.source
            reads[ref.id].append(f.position)
            if ref.id in sources:
                continue
            cols = self._schema(ref)
            if cols is not None:
                sources[ref.id] = [SourceColumn(n, t) for n, t in cols]
            else:
                sources[ref.id] = [SourceColumn(n, inferred=True) for n in self.found[ref.id].values()] + \
                                  [SourceColumn(STAR, star=True)]
        writes_at = defaultdict(list)
        for s in self.p.sinks:
            writes_at[s.table.id].append((s.cell, s.line))
        read_first = {t for t in reads if t in writes_at and min(reads[t]) <= max(writes_at[t])}
        return Resolution(self.p, sources, self.writes, list(self.conds), read_first,
                          list(dict.fromkeys(self.p.notes + self.notes)))


def resolve(program: Program, columns_of: ColumnsOf) -> Resolution:
    return Resolver(program, columns_of).run()
