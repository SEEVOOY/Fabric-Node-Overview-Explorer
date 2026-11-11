"""What a notebook does to columns, written down without knowing the tables' columns yet.

Reading the code gives a small program:

    Frame       a table as the code sees it at one point: a dataset read, or what one step makes out
                of earlier frames (a DataFrame, a temporary view, a CTE, a subquery)
    Entry       one column of a frame: its name, the columns it is computed from (Deps) and the
                definitions applied to it; or a star, which passes on a parent frame's columns
    Op          a definition: one piece of the notebook's code that does something to the data.
                Column scope: changes the values of the columns it is applied to. Row scope: changes
                the rows; every column entering the frame passes through it.
    Sink        a write: a frame's columns going into a dataset

The resolver (resolve.py) runs the program against the datasets' real columns, which are only known
when they are read from OneLake, and turns it into the paths each column takes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from .catalog import CATEGORIES, COLUMN, ROW, categories_of


@dataclass
class TableRef:
    """A dataset the code reads or writes, placed on the map's nodes (same ids as the map)."""
    id: str                     # table:<lakehouse id>:<key>, file:..., missing:..., ext:...
    label: str
    full_name: str = ""
    kind: str = "table"         # table | file | missing | external
    lakehouse: str = ""         # map node id of its lakehouse or warehouse
    lakehouse_name: str = ""
    workspace_id: str = ""
    item_id: str = ""           # the lakehouse or warehouse item (to read the columns from OneLake)
    schema: str = ""
    table: str = ""
    as_written: str = ""        # the name or path as the code wrote it

    @property
    def readable(self) -> bool:
        """Whether its columns can be read from OneLake."""
        return self.kind == "table" and bool(self.item_id and self.workspace_id and self.table)


@dataclass(frozen=True)
class Dep:
    """A column an expression reads, as written, in the frame it is evaluated in.

    qualifier: the alias in front of it (t.TagNo). hint: df["TagNo"] names the frame of df, which
    tells two same-named columns of a join apart (-1: no hint)."""
    frame: int
    name: str
    qualifier: str = ""
    hint: int = -1


@dataclass
class Entry:
    """One column of a frame, or (star >= 0) all the columns of a parent frame, minus exclude."""
    name: str = ""
    deps: list = field(default_factory=list)
    ops: list = field(default_factory=list)          # op keys, column scope, in the order applied
    star: int = -1
    exclude: frozenset = frozenset()                 # lower-case names the star leaves out
    qualifier: str = ""                              # t.* : only the parent's columns qualified t
    wildcard: bool = False                           # the ops apply to every column of the star

    @property
    def is_star(self) -> bool:
        return self.star >= 0


@dataclass
class Frame:
    id: int
    entries: list = field(default_factory=list)
    row_ops: list = field(default_factory=list)      # op keys applied to every column entering this frame
    source: Optional[TableRef] = None                # a dataset read
    position: tuple = (0, 0)                         # (cell, line) where it is made
    alias: str = ""                                  # the qualifier its columns get (FROM t AS x, df.alias("x"))
    combine: str = ""                                # "union" (by position) or "union_by_name": parents stacked
    parents: list = field(default_factory=list)      # the frames a union stacks
    rename_to: list = field(default_factory=list)    # toDF("a", "b") / t(a, b): new names, by position
    label: str = ""                                  # what the code calls it: a variable, view or CTE name
    opaque: str = ""                                 # why its columns can't be followed, if they can't


@dataclass
class Op:
    """A definition node. key is unique in the notebook; the same code (a loop) gives the same key,
    so the columns it is applied to all go through one node."""
    key: str
    category: str
    scope: str
    label: str
    code: str
    cell: int = 0
    line: int = 0
    functions: list = field(default_factory=list)
    categories: list = field(default_factory=list)
    reads: list = field(default_factory=list)        # [Dep]: what a row operation looks at (conditions, keys)
    detail: str = ""
    tag: str = ""                                    # under the line in its balloon (default: cell:line)
    where: str = ""                                  # where the code is, in words (default: Cell n, line m)

    def to_json(self) -> dict:
        cats = self.categories or [self.category]
        return {"key": self.key, "category": self.category, "categories": cats, "scope": self.scope,
                "letters": "".join(CATEGORIES[c].letter for c in cats if c in CATEGORIES)[:3],
                "label": self.label, "code": self.code, "cell": self.cell, "line": self.line,
                "tag": self.tag or f"{self.cell}:{self.line}", "where": self.where or f"Cell {self.cell}, line {self.line}",
                "functions": self.functions[:12], "detail": self.detail}


@dataclass
class Assignment:
    """MERGE / UPDATE: one target column set from an expression over the source frame."""
    target: str
    entry: Entry


@dataclass
class Sink:
    table: TableRef
    frame: int
    verb: str                                        # saveAsTable, INSERT INTO, MERGE INTO, write_deltalake, ...
    mode: str = ""                                   # overwrite | append | merge | update | create | ...
    cell: int = 0
    line: int = 0
    by_position: bool = False                        # insertInto / INSERT INTO: columns go by position
    columns: list = field(default_factory=list)      # INSERT INTO t (a, b): the target's columns, in order
    merge_op: str = ""                               # MERGE / UPDATE: the row operation
    assignments: list = field(default_factory=list)  # [Assignment]
    all_columns: bool = True                         # MERGE ... UPDATE SET * / InsertAll: every column
    code: str = ""


@dataclass
class Program:
    frames: list = field(default_factory=list)
    ops: dict = field(default_factory=dict)
    sinks: list = field(default_factory=list)
    tables: dict = field(default_factory=dict)       # id -> TableRef
    notes: list = field(default_factory=list)        # what couldn't be followed, in plain words
    error: str = ""                                  # the analysis itself failed (the map is unaffected)
    helpers: dict = field(default_factory=dict)      # its functions, for notebooks that %run it (not sent on)

    @property
    def sources(self) -> list:
        return [f for f in self.frames if f.source is not None]


# (kind, name parts, path, current database) -> the dataset; kind: table | path | warehouse_table
TableResolver = Callable[[str, list, str, str], Optional[TableRef]]


class Builder:
    """Makes the frames, ops and sinks of one notebook. Shared by the Python and SQL readers."""

    def __init__(self, resolve_table: TableResolver, cell: int = 0):
        self.p = Program()
        self.resolve_table = resolve_table
        self.cell = cell
        self.database = ""                           # USE x / spark.catalog.setCurrentDatabase(x)
        self.views: dict = {}                        # temp view name (lower) -> frame id
        self.origin = ""                             # the %run notebook whose function is being read, if any

    # ------------------------------------------------------------------ frames
    def frame(self, **kw) -> int:
        f = Frame(id=len(self.p.frames), **kw)
        self.p.frames.append(f)
        return f.id

    def get(self, fid: int) -> Frame:
        return self.p.frames[fid]

    def table(self, parts: list, path: str = "", kind: str = "") -> Optional[TableRef]:
        kind = kind or ("path" if path else "table")
        ref = self.resolve_table(kind, [p for p in parts if p], path, self.database)
        if ref is not None:
            self.p.tables.setdefault(ref.id, ref)
        return ref

    def source(self, ref: TableRef, line: int, label: str = "") -> int:
        return self.frame(source=ref, position=(self.cell, line), label=label or ref.label)

    def view(self, name: str) -> Optional[int]:
        return self.views.get(name.lower().split(".")[-1].strip("`"))

    def opaque(self, why: str, line: int, label: str = "") -> int:
        return self.frame(opaque=why, position=(self.cell, line), label=label)

    def star(self, parent: int, row_ops=(), exclude=(), alias: str = "", label: str = "") -> int:
        """A frame with all of parent's columns, through row operations."""
        return self.frame(entries=[Entry(star=parent, exclude=frozenset(x.lower() for x in exclude))],
                          row_ops=list(row_ops), alias=alias, label=label, position=(self.cell, 0))

    # ------------------------------------------------------------------ definitions
    def op(self, key: str, scope: str, functions, label: str, code: str, line: int, category: str = "",
           reads=(), detail: str = "") -> str:
        """A definition node; the same key (the same code run again, in a loop) gives the same node."""
        key = f"{self.origin + '#' if self.origin else ''}{self.cell}:{key}"
        cur = self.p.ops.get(key)
        if cur is not None:
            for d in reads:
                if d not in cur.reads:
                    cur.reads.append(d)
            return key
        cats = categories_of(functions) if functions else []
        if category:
            cats = [category] + [c for c in cats if c != category]
        if not cats:
            cats = ["constant"] if scope == COLUMN else ["filter"]
        if scope == ROW:
            cats = [c for c in cats if CATEGORIES[c].scope == ROW] or cats[:1]
        self.p.ops[key] = Op(key=key, category=cats[0], scope=scope, label=_short(label), code=_code(code),
                             cell=self.cell, line=line, functions=list(dict.fromkeys(functions)),
                             categories=cats[:3], reads=list(reads), detail=detail)
        if self.origin:                              # code of a notebook brought in with %run
            self.p.ops[key].where = f"{self.origin}, cell {self.cell}, line {line}"
        return key

    def note(self, text: str) -> None:
        if text not in self.p.notes and len(self.p.notes) < 40:
            self.p.notes.append(text)

    def sink(self, s: Sink) -> None:
        self.p.tables.setdefault(s.table.id, s.table)
        self.p.sinks.append(s)


def _short(text: str, n: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _code(text: str, n: int = 900) -> str:
    text = str(text).strip("\n")
    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    indent = min((len(ln) - len(ln.lstrip()) for ln in lines if ln.strip()), default=0)
    text = "\n".join(ln[indent:] for ln in lines)
    return text if len(text) <= n else text[: n - 1] + "…"
