"""Table references in Spark SQL (and simple T-SQL): what a statement reads, writes or maintains.

Comments and string literals are blanked out first, so keywords inside them don't count.
CTE names and temporary views are local to the code and are not reported as tables.
Callers replace unresolved {expressions} with DYN placeholders (__dyn0__, ...) before parsing;
names containing a placeholder come back with dynamic=True.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DYN = "__dyn"

_PART = r"(?:`[^`\n]+`|\[[^\]\n]+\]|[A-Za-z_][\w$]*)"
NAME = rf"{_PART}(?:\s*\.\s*{_PART}){{0,3}}"
_PART_RE = re.compile(_PART)

FORMATS = {"delta", "parquet", "csv", "json", "orc", "text", "avro", "iceberg", "binaryfile", "xml", "jdbc",
           "hive"}
_NOT_TABLES = {"values", "select", "lateral", "unnest", "table", "with", "explode", "json_tuple", "range",
               "dual", "inline", "stack", "posexplode"}
_STOP = {"where", "group", "order", "having", "limit", "join", "left", "right", "inner", "full", "cross",
         "outer", "on", "union", "except", "intersect", "minus", "window", "lateral", "pivot", "unpivot",
         "tablesample", "using", "natural", "anti", "semi", "cluster", "distribute", "sort", "qualify",
         "when", "then", "set", "into", "values", "select", "from", "as", "insert", "merge", "update",
         "delete", "partition", "options", "with", "and", "or", "not", "is", "in", "like", "by", "offset"}

_WRITES = [
    (rf"\bINSERT\s+(?:INTO|OVERWRITE)\s+(?:TABLE\s+)?(?P<name>{NAME})", "write", "INSERT INTO"),
    (rf"\bMERGE\s+INTO\s+(?P<name>{NAME})", "write", "MERGE INTO"),
    (rf"\b(?:CREATE\s+(?:OR\s+REPLACE\s+)?|REPLACE\s+)(?:EXTERNAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
     rf"(?P<name>{NAME})", "write", "CREATE TABLE"),
    (rf"\bCREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?(?:MATERIALIZED\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?"
     rf"(?P<name>{NAME})", "write", "CREATE VIEW"),
    (rf"\bUPDATE\s+(?P<name>{NAME})\s+(?:(?:AS\s+)?[A-Za-z_]\w*\s+)?SET\b", "write", "UPDATE"),
    (rf"\bDELETE\s+FROM\s+(?P<name>{NAME})", "write", "DELETE"),
    (rf"\bTRUNCATE\s+TABLE\s+(?P<name>{NAME})", "write", "TRUNCATE"),
    (rf"\bDROP\s+(?:TABLE|VIEW)\s+(?:IF\s+EXISTS\s+)?(?P<name>{NAME})", "write", "DROP"),
    (rf"\bALTER\s+TABLE\s+(?P<name>{NAME})", "write", "ALTER TABLE"),
    (rf"\bCOPY\s+INTO\s+(?P<name>{NAME})", "write", "COPY INTO"),
    (rf"\bSELECT\b(?:(?!\bFROM\b)[^;])*?\bINTO\s+(?P<name>{NAME})", "write", "SELECT INTO"),
    (rf"\bOPTIMIZE\s+(?P<name>{NAME})", "maintain", "OPTIMIZE"),
    (rf"\bVACUUM\s+(?P<name>{NAME})", "maintain", "VACUUM"),
    (rf"\bANALYZE\s+TABLE\s+(?P<name>{NAME})", "maintain", "ANALYZE"),
    (rf"\bREFRESH\s+TABLE\s+(?P<name>{NAME})", "maintain", "REFRESH TABLE"),
]
_WRITES = [(re.compile(p, re.I), op, verb) for p, op, verb in _WRITES]
_READ = re.compile(rf"\b(?P<kw>FROM|JOIN|USING)\s+(?P<name>{NAME})(?![\w$])", re.I)
_USE = re.compile(rf"(?im)^\s*USE\s+(?:DATABASE\s+|SCHEMA\s+)?(?P<name>{NAME})\s*;?\s*$")
_CTE = re.compile(rf"(?:\bWITH\b(?:\s+RECURSIVE)?|,)\s*(?P<name>{_PART})\s+AS\s*\(", re.I)
_TEMP_VIEW = re.compile(
    rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:GLOBAL\s+)?TEMP(?:ORARY)?\s+VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"(?P<name>{NAME})", re.I)
_CACHE_TABLE = re.compile(rf"\bCACHE\s+(?:LAZY\s+)?TABLE\s+(?P<name>{NAME})\s+AS\b", re.I)
_FILE_FUNCS = re.compile(
    r"\b(?P<fn>delta_scan|read_parquet|parquet_scan|read_csv(?:_auto)?|read_json(?:_auto)?|iceberg_scan|"
    r"read_delta)\s*\(\s*\[?\s*'(?P<path>[^']+)'", re.I)
_ALIAS = re.compile(r"\s+(?:AS\s+)?(?P<alias>[A-Za-z_]\w*)", re.I)
_COMMA_NAME = re.compile(rf"\s*,\s*(?P<name>{NAME})(?![\w$])")


@dataclass
class SqlRef:
    op: str            # read | write | maintain | use
    verb: str          # FROM, JOIN, INSERT INTO, MERGE INTO, OPTIMIZE, ...
    parts: list        # identifier parts with quotes removed
    line: int          # 0-based line within the SQL text
    path: str = ""     # delta.`/path`, delta_scan('/path'): the path read
    dynamic: bool = False


def split_name(text: str) -> list:
    """'a.`b c`.[d]' -> ['a', 'b c', 'd']"""
    out = []
    for p in _PART_RE.findall(text):
        if p.startswith("`") and p.endswith("`"):
            p = p[1:-1]
        elif p.startswith("[") and p.endswith("]"):
            p = p[1:-1]
        out.append(p)
    return out


def mask(sql: str) -> str:
    """Blank comments and string literal contents, keeping positions and newlines."""
    out = list(sql)
    i, n = 0, len(sql)

    def blank(a, b):
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = sql[i]
        if c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
            continue
        if c == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
            continue
        if c in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            blank(i + 1, j)
            i = j + 1
            continue
        if c == "`":
            j = sql.find("`", i + 1)
            i = n if j < 0 else j + 1
            continue
        i += 1
    return "".join(out)


def _is_dynamic(parts) -> bool:
    return any(DYN in p for p in parts)


def sql_refs(sql: str, paths_only: bool = False, local_names=()) -> list[SqlRef]:
    """Every table (and file path) a SQL text reads, writes or maintains, in text order.

    local_names: names the surrounding code defines as temporary views (not lakehouse tables).
    """
    if not sql or not sql.strip():
        return []
    found: list[tuple[int, SqlRef]] = []
    for m in _FILE_FUNCS.finditer(sql):
        found.append((m.start(), SqlRef("read", m.group("fn"), [], sql.count("\n", 0, m.start()),
                                        path=m.group("path"))))
    if paths_only:
        return [r for _, r in sorted(found, key=lambda x: x[0])]

    text = mask(sql)
    local = {m.group("name").strip("`[]").lower() for m in _CTE.finditer(text)}
    local.update(str(n).lower() for n in local_names)
    for rx in (_TEMP_VIEW, _CACHE_TABLE):
        for m in rx.finditer(text):
            local.add(split_name(m.group("name"))[-1].lower())
    taken: set[int] = set()

    def line_of(pos):
        return text.count("\n", 0, pos)

    def as_path(parts):
        if len(parts) == 2 and parts[0].lower() in FORMATS and ("/" in parts[1] or ":" in parts[1]):
            return parts[1]
        return ""

    for rx, op, verb in _WRITES:
        for m in rx.finditer(text):
            start = m.start("name")
            if start in taken:
                continue
            parts = split_name(m.group("name"))
            if not parts:
                continue
            if len(parts) == 1 and parts[0].lower() in local and op == "write":
                taken.add(start)
                continue
            taken.add(start)
            path = as_path(parts)
            found.append((start, SqlRef(op, verb, [] if path else parts, line_of(start), path=path,
                                        dynamic=_is_dynamic(parts))))

    def add_read(start, name_text, verb, end):
        if start in taken:
            return
        if re.match(r"\s*\(", text[end:end + 40]):          # table-valued function or subquery
            return
        parts = split_name(name_text)
        if not parts:
            return
        low = parts[0].lower()
        if len(parts) == 1 and (low in _NOT_TABLES or low in local or (verb == "USING" and low in FORMATS)):
            return
        path = as_path(parts)
        taken.add(start)
        found.append((start, SqlRef("read", verb, [] if path else parts, line_of(start), path=path,
                                    dynamic=_is_dynamic(parts))))

    for m in _READ.finditer(text):
        verb = m.group("kw").upper()
        add_read(m.start("name"), m.group("name"), verb, m.end("name"))
        if verb != "FROM":
            continue
        pos = m.end("name")
        while True:                                        # FROM a x, b y, ...
            a = _ALIAS.match(text, pos)
            if a and a.group("alias").lower() not in _STOP:
                pos = a.end()
            c = _COMMA_NAME.match(text, pos)
            if not c:
                break
            add_read(c.start("name"), c.group("name"), "FROM", c.end("name"))
            pos = c.end("name")

    for m in _USE.finditer(text):
        parts = split_name(m.group("name"))
        if parts:
            found.append((m.start("name"), SqlRef("use", "USE", parts, line_of(m.start("name")),
                                                  dynamic=_is_dynamic(parts))))

    seen, out = set(), []
    for _, r in sorted(found, key=lambda x: x[0]):
        key = (r.op, r.verb, tuple(p.lower() for p in r.parts), r.line, r.path)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out
