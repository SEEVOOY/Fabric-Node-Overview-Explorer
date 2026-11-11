"""Column-level lineage: what notebook code does to each column, and the two views of it."""
import http.client
import json

import pytest

from fabric_explorer.column_lineage import TableRef, read_notebook_columns, resolve
from fabric_explorer.column_lineage.catalog import catalog_json, categories_of, category_of
from fabric_explorer.column_lineage.expressions import (ExprContext, PyExprReader, split_top, sql_expression_text,
                                                        sql_select_item, tokenize)
from fabric_explorer.column_lineage.views import NotebookLineage, datasets_view, notebook_view
from fabric_explorer.demo import ID, WS, demo_client
from fabric_explorer.parse.notebook import read_notebook
from fabric_explorer.server import make_server, serve_in_thread
from fabric_explorer.session import Session


def table_ref(kind, parts, path, database):
    name = ".".join(parts) if parts else path.split("Tables/")[-1].strip("/")
    return TableRef(id=f"table:{name.lower()}", label=name, table=name.split(".")[-1])


def program(*cells, seed=None):
    nb = {"nbformat": 4, "metadata": {"language_info": {"name": "python"}},
          "cells": [{"cell_type": "code", "source": c, "metadata": {}} for c in cells]}
    return read_notebook_columns(read_notebook({"notebook-content.ipynb": json.dumps(nb).encode()}), table_ref, seed)


def lineage(prog, schemas=None):
    return resolve(prog, lambda ref: [(c, "string") for c in schemas[ref.id[6:]]]
                   if schemas and ref.id[6:] in schemas else None)


def written(res, table):
    """{column: (status, [source columns], [definitions on its first path])}"""
    out = {}
    for w in res.writes:
        if w.sink.table.id != f"table:{table}":
            continue
        for c in w.columns:
            sources = sorted({f"{p[0][1][6:]}.{p[0][2]}" for p in c.paths if p[0][0] == "c"})
            ops = [res.program.ops[n[1]].label for p in c.paths[:1] for n in p if n[0] == "o"]
            out[c.name] = (c.status, sources, ops)
    return out


def letters(prog):
    return {op.label: "".join(categories_of([op.category])) for op in prog.ops.values()}


# ---------------------------------------------------------------- what Spark functions do
def test_catalog():
    cats = {c["key"]: c for c in catalog_json()["categories"]}
    assert len(cats) == 17 and len({c["letter"] for c in cats.values()}) == 17
    assert (cats["text"]["letter"], cats["filter"]["scope"], cats["merge"]["letter"]) == ("S", "row", "M")
    assert category_of("regexp_replace") == "text" and category_of("datediff") == "date"
    assert category_of("my_cleaner") == "custom" and category_of("TRY_DIVIDE") == "number"
    assert categories_of(["upper", "when", "cast"]) == ["condition", "cast", "text"]


def test_sql_expressions():
    sql = ("d.DocumentID, upper(trim(d.Title)) AS Title, n.Description DisciplineName, "
           "CASE WHEN Status IS NULL THEN 'Unknown' ELSE Status END AS Status, CAST(Revision AS INT) rev, "
           "ROW_NUMBER() OVER (PARTITION BY DocumentNo ORDER BY RevisionDate DESC NULLS LAST) AS rn, "
           "current_timestamp() AS loaded_at, t.*, transform(arr, x -> x + 1) AS a2, "
           "date_add(Created, INTERVAL 1 DAY) AS nxt, 'x' src")
    items = {e.name: e for e in (sql_select_item(t, sql) for t in split_top(tokenize(sql)))}
    assert items["DocumentID"].column and [(r.qualifier, r.name) for r in items["DocumentID"].refs] == [("d", "DocumentID")]
    assert items["Title"].functions == ["upper", "trim"]
    assert items["DisciplineName"].renames
    assert items["Status"].functions[:2] == ["case", "isnull"] and items["rev"].functions == ["cast"]
    assert [r.name for r in items["rn"].refs] == ["DocumentNo", "RevisionDate"] and "over" in items["rn"].functions
    assert not items["loaded_at"].refs and items["t.*"].star == "t"
    assert [r.name for r in items["a2"].refs] == ["arr"]                      # x is a lambda variable
    assert [r.name for r in items["nxt"].refs] == ["Created"] and items["src"].literal
    cond = sql_expression_text("TagNo IS NOT NULL AND Plant IN ('A', 'B')")
    assert [r.name for r in cond.refs] == ["TagNo", "Plant"]


def test_pyspark_expressions():
    class Ctx(ExprContext):
        def strings(self, node):
            return [node.value] if hasattr(node, "value") and isinstance(node.value, str) else None

        def frame_var(self, name):
            return 7 if name == "df" else None

    import ast
    read = PyExprReader(Ctx()).read
    e = read(ast.parse('F.upper(F.trim(F.col("TagNo"))).alias("Tag")', mode="eval").body)
    assert (e.name, [r.name for r in e.refs], e.functions) == ("Tag", ["TagNo"], ["upper", "trim"])
    e = read(ast.parse('F.date_format("Created", "yyyy-MM")', mode="eval").body)
    assert [r.name for r in e.refs] == ["Created"]                              # the format is a value
    e = read(ast.parse('F.when(F.col("s").isNull(), "Unknown").otherwise(F.col("s"))', mode="eval").body)
    assert [r.name for r in e.refs] == ["s"] and "when" in e.functions
    e = read(ast.parse('df["x"] + 1', mode="eval").body)
    assert e.refs[0].hint == 7 and e.functions == ["+"]
    e = read(ast.parse('F.expr("upper(Title) AS T")', mode="eval").body)
    assert (e.name, e.functions) == ("T", ["upper"])
    assert read(ast.parse('F.col("a").alias("b")', mode="eval").body).renames


# ---------------------------------------------------------------- SQL
def test_sql_join_where_rename_and_conditions():
    prog = program("%%sql\nCREATE OR REPLACE TABLE silver.docs AS\n"
                   "SELECT d.Id, upper(d.Title) AS Title, n.Description AS Discipline\n"
                   "FROM bronze.docs_raw d LEFT JOIN ref.codes n ON d.Code = n.Code\nWHERE d.Id IS NOT NULL")
    res = lineage(prog, {"bronze.docs_raw": ["Id", "Title", "Code"], "ref.codes": ["Code", "Description"],
                         "silver.docs": ["Id", "Title", "Discipline"]})
    w = written(res, "silver.docs")
    assert w["Id"][0] == "unchanged" and w["Id"][2][:2] == ["left join ref.codes on d.Code = n.Code",
                                                           "where d.Id IS NOT NULL"]
    assert w["Title"][0] == "altered" and w["Title"][2][-1] == "upper(d.Title) AS Title"
    assert w["Discipline"] == ("renamed", ["ref.codes.description"], w["Discipline"][2])
    conds = {(p[0][2], prog.ops[p[-1][1]].category) for p in res.conditions}
    assert ("code", "join") in conds and ("id", "filter") in conds                # the join keys and the filter


def test_sql_insert_by_position_cte_union_merge_update():
    res = lineage(program(
        'spark.sql("""INSERT INTO gold.t SELECT k, v * 2 FROM silver.s""")',
        "%%sql\nWITH x AS (SELECT k, v FROM silver.a UNION ALL SELECT k, v FROM silver.b)\n"
        "INSERT OVERWRITE gold.u SELECT k, sum(v) AS total FROM x GROUP BY k",
        "%%sql\nMERGE INTO gold.m t USING silver.s s ON t.k = s.k\n"
        "WHEN MATCHED THEN UPDATE SET t.v = s.v + 1\nWHEN NOT MATCHED THEN INSERT (k, v) VALUES (s.k, s.v)",
        "%%sql\nUPDATE gold.m SET v = abs(v) WHERE v < 0"),
        {"silver.s": ["k", "v"], "silver.a": ["k", "v"], "silver.b": ["k", "v"], "gold.t": ["key", "value"]})
    t = written(res, "gold.t")
    assert t["key"][:2] == ("renamed", ["silver.s.k"]) and t["value"][0] == "new"   # INSERT goes by position
    u = written(res, "gold.u")
    assert u["k"][1] == ["silver.a.k", "silver.b.k"] and u["total"][0] == "new"
    assert "union" in u["k"][2] and "group by k" in u["k"][2]
    m = [w for w in res.writes if w.sink.verb == "MERGE INTO"][0]
    assert {c.name for c in m.columns} == {"v", "k"} and m.sink.mode == "merge"
    assert "table:gold.m" in res.read_first                                             # UPDATE reads it first


# ---------------------------------------------------------------- pyspark
def test_dataframe_steps_share_definitions():
    prog = program(
        "from pyspark.sql import functions as F\n"
        'df = spark.read.table("bronze.raw")\n'
        'df = df.dropDuplicates(["Id"]).filter(F.col("Id").isNotNull())\n'
        'df = df.withColumn("Name", F.initcap(F.trim("Name"))).withColumnRenamed("Cd", "Code")\n'
        'for c in ["A", "B"]:\n'
        '    df = df.withColumn(c, F.upper(F.col(c)))\n'
        'df = df.drop("Tmp").withColumn("loaded", F.current_timestamp())\n'
        'df.write.mode("overwrite").saveAsTable("silver.clean")')
    res = lineage(prog, {"bronze.raw": ["Id", "Name", "Cd", "A", "B", "Tmp"]})
    w = written(res, "silver.clean")
    assert set(w) == {"Id", "Name", "Code", "A", "B", "loaded"}                   # Tmp dropped
    assert w["Id"] == ("unchanged", ["bronze.raw.id"], ["dropDuplicates(Id)", "filter Id.isNotNull()"])
    assert w["Name"][0] == "altered" and w["Code"][:2] == ("renamed", ["bronze.raw.cd"])
    assert w["loaded"][:2] == ("new", [])
    upper = [k for k, o in prog.ops.items() if o.label.startswith("upper(c)")]
    assert len(upper) == 1 and all(("o", upper[0]) in col.paths[0] for wr in res.writes for col in wr.columns
                                   if col.name in ("A", "B"))                # one node for the loop
    rows = [o for o in prog.ops.values() if o.scope == "row"]
    assert {o.category for o in rows} == {"filter"}                            # dedupe and filter: shared


def test_loops_pair_sources_with_targets_and_unknown_columns():
    prog = program('pairs = {"a": "x", "b": "y"}\n'
                   "for src, dst in pairs.items():\n"
                   '    spark.read.table(f"silver.{src}").write.saveAsTable(f"gold.{dst}")')
    res = lineage(prog)                                                        # no columns known
    assert {w.sink.table.id: w.columns[0].paths[0][0][1] for w in res.writes} == \
        {"table:gold.x": "table:silver.a", "table:gold.y": "table:silver.b"}
    prog = program('df = spark.table("s.t")\n'
                   'df.select("Id", F.upper("Name").alias("Name")).write.saveAsTable("g.t")')
    res = lineage(prog)
    assert [c.name for c in res.sources["table:s.t"]] == ["Id", "Name", "*"]       # learned from the code
    assert written(res, "g.t")["Name"][0] == "altered"


def test_every_column_loop_helpers_groupby_join_union():
    prog = program(
        "from pyspark.sql import functions as F\n"
        "def clean(c):\n    return F.trim(F.lower(F.col(c)))\n"
        "def keep_valid(d):\n    return d.filter('Valid = 1')\n"
        'a = keep_valid(spark.table("s.a"))\n'
        "for c in a.columns:\n    a = a.withColumn(c, F.trim(F.col(c)))\n"
        'b = spark.table("s.b").withColumn("Code", clean("Code"))\n'
        'j = a.join(b, "Id", "left").groupBy("Plant").agg(F.sum("Qty").alias("qty"), F.count("*").alias("n"))\n'
        'u = j.unionByName(spark.table("s.c"))\n'
        'u.write.saveAsTable("g.out")')
    res = lineage(prog, {"s.a": ["Id", "Plant", "Valid"], "s.b": ["Id", "Code", "Qty"], "s.c": ["Plant", "qty", "n"]})
    w = written(res, "g.out")
    assert set(w) == {"Plant", "qty", "n"}
    assert w["Plant"][1] == ["s.a.plant", "s.c.plant"] and w["qty"][1] == ["s.b.qty", "s.c.qty"]
    labels = set(letters(prog))
    assert {"filter Valid = 1", "trim(c)", "group by Plant", "unionByName"} <= labels
    trim = next(o for o in prog.ops.values() if o.label == "trim(c)")
    assert trim.cell == 1 and trim.line == 8                                   # every column of a, one node
    assert next(o for o in prog.ops.values() if "clean" in o.detail).functions == ["trim", "lower"]


def test_delta_merge_temp_view_duckdb_and_pandas():
    prog = program(
        "from delta.tables import DeltaTable\n"
        'src = spark.table("s.t").alias("s")\n'
        'src.createOrReplaceTempView("v")\n'
        '(DeltaTable.forName(spark, "g.t").alias("t").merge(src, "t.Id = s.Id")\n'
        '  .whenMatchedUpdate(set={"Name": "upper(s.Name)"}).whenNotMatchedInsertAll().execute())\n'
        'spark.sql("INSERT INTO g.h SELECT *, current_date() AS d FROM v")',
        "import duckdb\nfrom deltalake import write_deltalake\n"
        "k = duckdb.sql(\"SELECT Plant, count(*) AS n FROM delta_scan('/lakehouse/default/Tables/g/t') "
        'GROUP BY Plant").df()\n'
        'write_deltalake("/lakehouse/default/Tables/g/kpi", k, mode="overwrite")\n'
        'pdf = spark.table("s.t").toPandas()\npdf["Label"] = pdf["Name"].str.upper()\n'
        'pdf.to_parquet("/lakehouse/default/Files/out/p.parquet")')
    res = lineage(prog, {"s.t": ["Id", "Name"]})
    merge = written(res, "g.t")
    assert merge["Name"][0] == "altered" and merge["Name"][2][0] == "merge on t.Id = s.Id"
    assert merge["Id"][0] == "unchanged"
    assert written(res, "g.h")["d"][:2] == ("new", [])
    assert written(res, "g/kpi")["n"][2] == ["group by Plant", "count(*) AS n"]
    out = written(res, "lakehouse/default/files/out/p.parquet")
    assert out["Label"][0] == "new" and out["Label"][1] == ["s.t.name"]


# ---------------------------------------------------------------- the views
def _demo_lineages():
    client = demo_client(latency=None, sleep=lambda s: None)
    s = Session(client, default_workspace=WS)
    s.open_workspace(WS)
    for key in ("nb10", "nb11", "nb20", "nb21", "nb30", "nb70"):
        s.scan(WS, ID[key])
    return client, s


def test_notebook_view_direct_and_full():
    client, s = _demo_lineages()
    v = s.column_lineage.notebook(WS, ID["nb20"])
    roles = {d["label"]: d["role"] for d in v["datasets"]}
    assert roles == {"cms_tags_raw": "source", "cms_tags_current": "target", "cms_tags_history": "target"}
    current = next(d for d in v["datasets"] if d["label"] == "cms_tags_current")
    status = {c["name"]: c["status"] for c in current["columns"]}
    assert status["TagNo"] == "altered" and status["Plant"] == "altered" and status["Domain"] == "unchanged"
    ops = {o["label"]: o for o in v["ops"]}
    assert ops["dropDuplicates(TagNo)"]["letters"] == "F" and len(ops["dropDuplicates(TagNo)"]["lanes"]) >= 14
    assert ops["trim(c)"]["letters"] == "S" and ops["merge on t.TagNo = s.TagNo"]["letters"] == "M"
    assert [l["label"] for l in ops["trim(c)"]["lanes"]] == ["Plant", "Project", "System"]   # a lane each
    raw = next(d for d in v["datasets"] if d["label"] == "cms_tags_raw")
    assert next(c for c in raw["columns"] if c["name"] == "ingested_at")["used"] is False   # dropped
    flow = v["flows"][0]
    assert all(0 <= i < len(v["edges"]) for i in flow["edges"])
    full = s.column_lineage.notebook(WS, ID["nb20"], scope="full")
    names = [n["name"] for n in full["notebooks"]]
    assert names[0] == "nb_20_bronze_to_silver_tags" and "nb_10_ingest_pims_tags" in names
    assert "nb_30_silver_to_gold" in names and full["unread"] > 0
    with pytest.raises(ValueError):
        s.column_lineage.notebook(WS, ID["silver"])


def test_datasets_view_changed_columns_and_thread():
    client, s = _demo_lineages()
    o = s.column_lineage.datasets(WS)
    n = len(client.http.calls)
    assert s.column_lineage.datasets(WS)["edges"] == o["edges"] and len(client.http.calls) == n   # columns cached
    changed = {d["label"]: {c["name"]: c["status"] for c in d["columns"] if c["status"] != "feeds"}
               for d in o["datasets"]}
    assert changed["cms_tags_raw"] == {"ingested_at": "new"}
    assert changed["dcs_documents"]["DisciplineName"] == "renamed" and changed["dcs_documents"]["Revision"] == "altered"
    assert changed["cms_documents_tags"]["link_key"] == "new"
    nb30 = next(nb for nb in o["notebooks"] if nb["name"] == "nb_30_silver_to_gold")
    assert ("AR", "count() AS count ▸ count → tags") in [(r["letters"], r["label"]) for r in nb30["rows"]]
    kinds = {e["kind"] for e in o["edges"]}
    assert {"relation", "change"} <= kinds
    focus = next(d["id"] for d in o["datasets"] if d["label"] == "cms_tags_current")
    thread = s.column_lineage.datasets(WS, focus=focus, scope="direct")
    assert sorted(nb["name"] for nb in thread["notebooks"]) == ["nb_20_bronze_to_silver_tags", "nb_30_silver_to_gold"]
    up = s.column_lineage.datasets(WS, focus=focus, scope="full", direction="in")
    assert sorted(nb["name"] for nb in up["notebooks"]) == ["nb_10_ingest_pims_tags", "nb_20_bronze_to_silver_tags"]


def test_programs_stay_out_of_the_scan_and_go_with_unload():
    client, s = _demo_lineages()
    assert "_columnProgram" not in s.scan(WS, ID["nb20"]) and s.program(WS, ID["nb20"]) is not None
    s.unload(WS, [ID["nb20"]])
    assert s.program(WS, ID["nb20"]) is None
    assert s.column_lineage.notebook(WS, ID["nb20"])["datasets"]                 # read again on demand


def test_server_endpoints_and_page_files():
    client, s = _demo_lineages()
    srv = make_server(s, "127.0.0.1", 0)
    serve_in_thread(srv)
    try:
        def call(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=data, headers={"X-Session": srv.key,
                                                           "Host": f"127.0.0.1:{srv.server_address[1]}",
                                                           "Content-Type": "application/json"})
            r = conn.getresponse()
            return r.status, r.getheader("Content-Type"), r.read()
        status, _, body = call("GET", "/api/column-lineage/catalog")
        assert status == 200 and len(json.loads(body)["categories"]) == 17
        status, _, body = call("POST", "/api/column-lineage/notebook", {"workspaceId": WS, "notebookId": ID["nb21"]})
        v = json.loads(body)
        assert status == 200 and v["mode"] == "notebook" and any(d["label"] == "dcs_documents" for d in v["datasets"])
        status, _, body = call("POST", "/api/column-lineage/datasets", {"workspaceId": WS, "allColumns": True})
        assert status == 200 and json.loads(body)["allColumns"] is True
        assert call("POST", "/api/column-lineage/notebook", {"workspaceId": WS, "notebookId": "nope"})[0] == 400
        for path in ("/static/cll/view.mjs", "/static/cll/layout.mjs", "/static/cll/render.mjs"):
            status, ctype, _ = call("GET", path)
            assert status == 200 and ctype.startswith("text/javascript"), path
        status, ctype, _ = call("GET", "/static/cll/cll.css")
        assert status == 200 and ctype.startswith("text/css")
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------- pipelines and Power BI
def test_pipeline_copy_mapping_and_dax_and_report_formats():
    from fabric_explorer.column_lineage.pipelines import read_pipeline_columns
    from fabric_explorer.column_lineage.powerbi import dax, read_model, read_report
    content = {"properties": {"activities": [{"name": "Copy it", "type": "Copy", "typeProperties": {
        "source": {"type": "LakehouseTableSource"}, "sink": {"type": "LakehouseTableSink", "tableActionOption": "Append"},
        "translator": {"mappings": [{"source": {"name": "A", "type": "Int32"}, "sink": {"name": "a_id", "type": "Int64"}},
                                    {"source": {"path": "$['B']"}, "sink": {"name": "B"}}]}}}]}}
    prog = read_pipeline_columns(content, lambda st, side: TableRef(id=f"table:{side}", label=side, table=side))
    res = lineage(prog, {"source": ["A", "B", "C"]})
    assert {c: v[0] for c, v in written(res, "sink").items()} == {"a_id": "renamed", "B": "unchanged"}
    assert {o.to_json()["tag"] for o in prog.ops.values()} == {"A1"} and prog.sinks[0].mode == "append"
    refs, fns = dax("CALCULATE(SUM('Fact sales'[Amount]), Dates[Year] = 2024) // [Ignored]\n + [Other measure]")
    assert refs == [("Fact sales", "Amount"), ("Dates", "Year"), ("", "Other measure")] and fns == ["calculate", "sum"]
    model = read_model({"model": {"tables": [{"name": "T", "columns": [{"name": "X", "sourceColumn": "x"}],
                                              "measures": [{"name": "M", "expression": "SUM(T[X])"},
                                                           {"name": "N", "expression": "[M] * 2"}]}]}},
                       {"T": TableRef(id="table:t", label="t")})
    x = next(f for f in model.fields if f.name == "X")
    assert x.source == ("table:t", "x") and [u.name for u in model.users(x)] == ["M"]
    assert [u.name for u in model.users(model.find("T", ("", "M")))] == ["N"]
    assert model.find("T", ("", "M")).op_json("m")["letters"] == "A"
    pbir = {"definition/pages/p/page.json": b'{"displayName": "Main"}',
            "definition/pages/p/visuals/v/visual.json": json.dumps({"name": "v", "visual": {"visualType": "card", "query": {
                "queryState": {"Values": {"projections": [{"field": {"Measure": {"Expression": {"SourceRef": {
                    "Entity": "T"}}, "Property": "N"}}}]}}}}}).encode()}
    legacy = {"report.json": json.dumps({"sections": [{"displayName": "S", "visualContainers": [{"config": json.dumps(
        {"name": "c", "singleVisual": {"visualType": "table", "prototypeQuery": {"From": [{"Name": "t", "Entity": "T"}],
                                       "Select": [{"Column": {"Expression": {"SourceRef": {"Source": "t"}},
                                                              "Property": "X"}}]}}})}]}]}).encode()}
    assert [(v.label, v.fields) for v in read_report(pbir, "m").visuals] == [("Main: card", [("T", "N")])]
    assert [(v.label, v.fields) for v in read_report(legacy, "m").visuals] == [("S: table", [("T", "X")])]


def _bi_session():
    client, s = _demo_lineages()
    for key in ("pl", "model", "rep1", "rep2"):
        s.scan(WS, ID[key])
    return client, s


def test_columns_into_power_bi_in_both_views():
    client, s = _bi_session()
    o = s.column_lineage.datasets(WS)
    cards = {c["name"]: c for c in o["cards"]}
    assert {"PIMS Model", "PIMS Tags", "Document status", "pl_daily_load"} <= set(cards)
    gold = next(d for d in o["datasets"] if d["label"] == "fact_pims_tags")
    assert {c["name"]: c["status"] for c in gold["columns"]}["TagNo"] == "carried"       # copied on by nb_30
    assert "Tags[TagNo]" in [r["label"] for r in cards["PIMS Model"]["rows"]]
    assert "Overview: Tag list" in [r["label"] for r in cards["PIMS Tags"]["rows"]]
    dax_ops = {op["label"].split(" =")[0]: op for op in o["ops"] if op["id"].startswith("dax:")}
    assert dax_ops["[Tags per plant]"]["tag"] == "DAX" and dax_ops["[Plants with tags]"]["letters"] == "A"
    kinds = {e["kind"] for e in o["edges"]}
    assert {"relation", "change", "copy", "bi"} <= kinds
    pl = cards["pl_daily_load"]
    assert pl["kind"] == "pipeline" and [r["letters"] for r in pl["rowOps"]] == ["P"]
    full = s.column_lineage.notebook(WS, ID["nb20"], scope="full")
    assert {c["name"] for c in full["cards"]} == {"PIMS Model", "PIMS Tags"}
    assert s.column_lineage.notebook(WS, ID["nb20"])["cards"] == []                     # direct: no Power BI
    pipe = s.column_lineage.notebook(WS, ID["pl"])
    target = next(d for d in pipe["datasets"] if d["role"] == "target")
    assert {c["name"]: c["status"] for c in target["columns"]}["tag_no"] == "renamed"


def test_export_rows_and_openlineage():
    client, s = _bi_session()
    x = s.column_lineage.export(WS)
    assert x["workspace"]["id"] == WS and x["columns"][0] == "target"
    row = next(r for r in x["rows"] if r["target"] == "silver.cms_tags_current" and r["target_column"] == "TagNo")
    assert row["source"] == "bronze.cms_tags_raw" and row["column_definitions"].startswith("S upper(trim(TagNo))")
    assert "F dropDuplicates(TagNo)" in row["row_definitions"] and row["via"] == "nb_20_bronze_to_silver_tags"
    assert any(r["via_type"] == "Report" and r["source_column"] == "[Tags per plant]" for r in x["rows"])
    job = next(j for j in x["openlineage"] if j["job"]["name"] == "nb_20_bronze_to_silver_tags")
    out = next(o for o in job["outputs"] if o["name"] == "silver.cms_tags_current")
    facet = out["facets"]["columnLineage"]
    assert facet["_schemaURL"].endswith("ColumnLineageDatasetFacet.json")
    field = facet["fields"]["TagNo"]["inputFields"][0]
    assert (field["name"], field["field"]) == ("bronze.cms_tags_raw", "TagNo")
    assert ("DIRECT", "TRANSFORMATION") in [(t["type"], t["subtype"]) for t in field["transformations"]]


def test_datasets_view_focused_on_a_semantic_model_or_a_report():
    client, s = _demo_lineages()                       # the model and reports aren't read yet: focusing reads them
    o = s.column_lineage.datasets(WS, focus=f"item:{ID['model']}")
    assert o["focusKind"] == "SemanticModel" and o["direction"] == "in"
    cards = {c["name"]: c for c in o["cards"]}
    assert {"PIMS Model", "PIMS Tags", "Document status"} <= set(cards)
    model_rows = {r["label"] for r in cards["PIMS Model"]["rows"]}
    assert {"Tags[Domain]", "Documents[Discipline]", "Tags[TagNo]"} <= model_rows       # every column it reads
    assert "nb_30_silver_to_gold" in cards and "nb_70_kpi_duckdb" not in cards         # upstream only
    r = s.column_lineage.datasets(WS, focus=f"item:{ID['rep2']}")
    names = {c["name"] for c in r["cards"]}
    assert "Document status" in names and "PIMS Tags" not in names and "PIMS Model" in names
    with pytest.raises(ValueError):
        s.column_lineage.datasets(WS, focus=f"item:{ID['nb20']}")


def test_helpers_from_run_notebooks_and_functions_that_name_their_columns():
    client, s = _demo_lineages()
    v = s.column_lineage.notebook(WS, ID["nb20"])                 # add_site_code comes from %run nb_00_config
    site = next(o for o in v["ops"] if "split" in o["functions"])
    assert site["where"].startswith("nb_00_config, cell 2") and "Adds SiteCode" in site["detail"]
    current = next(d for d in v["datasets"] if d["label"] == "cms_tags_current")
    assert {c["name"]: c["status"] for c in current["columns"]}["SiteCode"] == "new"
    w = s.column_lineage.notebook(WS, ID["nb21"])                 # flag_blanks can't be seen: what it names
    flag = next(o for o in w["ops"] if o["label"].startswith("flag_blanks"))
    assert sorted(l["label"] for l in flag["lanes"]) == ["document_id", "tag_no"]       # only these go through it
    assert "names the columns" in flag["detail"]
    links = next(d for d in w["datasets"] if d["label"] == "cms_documents_tags")
    assert {c["name"]: c["status"] for c in links["columns"]}["has_blanks"] == "new"
    prog = program('df = spark.table("s.t")\ndf2 = mystery(df)\ndf2.write.saveAsTable("g.t")')
    assert lineage(prog, {"s.t": ["a"]}).writes[0].columns[0].paths       # the thread goes on through it


def test_drawing_classes_are_not_styled_by_the_page():
    """A class of the lineage drawing that the page's stylesheet also styles with geometry (height: 48px on
    .bar once cut every manifold short) changes the drawing: they must not share names."""
    import re
    from pathlib import Path
    web = Path(__file__).resolve().parent.parent / "fabric_explorer" / "web"
    drawn = set()
    for m in re.finditer(r'class="([^"$]+)"', (web / "cll" / "render.mjs").read_text()):
        drawn.update(c for c in m.group(1).split() if c and "{" not in c)
    css = (web / "app.css").read_text()
    clash = {}
    for c in drawn:
        for m in re.finditer(r"(?:^|[\s,>+~(])\." + re.escape(c) + r"(?![\w-])[^{]*\{([^}]*)\}", css, re.M):
            props = {p.split(":")[0].strip() for p in m.group(1).split(";") if ":" in p}
            if props & {"height", "width", "x", "y", "r", "rx", "ry", "cx", "cy", "d", "transform"}:
                clash[c] = props
    assert clash == {}


def test_all_columns_draws_every_columns_whole_path():
    client, s = _demo_lineages()
    o = s.column_lineage.datasets(WS, all_columns=True)
    gold = next(d for d in o["datasets"] if d["label"] == "fact_pims_tags")
    domain = next(c for c in gold["columns"] if c["name"] == "Domain")
    assert domain["status"] == "unchanged" and domain["used"]                 # copied by nb_30: drawn too
    into = [f for f in o["flows"] if f["to"] == domain["id"]]
    assert into and into[0]["kind"] == "copy"
    changed_only = s.column_lineage.datasets(WS)
    assert not any(f["to"] == domain["id"] for f in changed_only["flows"])


def test_the_notebook_view_always_has_both_sides():
    from fabric_explorer.column_lineage.views import notebook_view
    prog = program('df = spark.table("s.a")\ndf.write.saveAsTable("g.b")\n'
                   'class Job:\n    def run(self):\n        spark.table("s.c").write.saveAsTable("g.d")')
    res = lineage(prog, {"s.a": ["x"]})
    nb = NotebookLineage({"id": "item:nb", "itemId": "nb", "name": "nb", "url": "", "type": "Notebook"}, res)
    v = notebook_view(nb, [], "direct", None, ([(TableRef(id="table:s.c", label="s.c"), [("y", "int")], {})],
                                              [(TableRef(id="table:g.d", label="g.d"), None, {})]))
    roles = {d["label"]: d["role"] for d in v["datasets"]}
    assert roles == {"s.a": "source", "g.b": "target", "s.c": "source", "g.d": "target"}
    hub = next(c for c in v["cards"] if c.get("hub"))
    assert {(e["fromNode"], e["toNode"]) for e in v["edges"] if e["kind"] == "relation"} == \
        {("table:s.c", hub["id"]), (hub["id"], "table:g.d")}
    stream = program("def upsert(batch, batch_id):\n    batch.write.mode('append').saveAsTable('g.s')\n"
                     "spark.readStream.table('s.a').writeStream.foreachBatch(upsert).start()")
    assert [w.sink.table.id for w in lineage(stream).writes] == ["table:g.s"]
