"""What the parsers find in code and definitions."""
import base64
import json

from fabric_explorer.parse.model import model_tables, parse_m
from fabric_explorer.parse.notebook import analyze_notebook, read_notebook
from fabric_explorer.parse.onelake import files_folder, parse_location, table_from_rel
from fabric_explorer.parse.pipeline import analyze_pipeline
from fabric_explorer.parse.sql import sql_refs

WS = "11111111-2222-3333-4444-555555555555"
LH = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def refs_of(cells, seed=None, meta=None):
    nb = {"nbformat": 4, "metadata": meta or {"language_info": {"name": "python"}},
          "cells": [{"cell_type": "code", "source": c, "metadata": {}} for c in cells]}
    refs, facts = analyze_notebook(read_notebook({"notebook-content.ipynb": json.dumps(nb).encode()}), seed=seed)
    return refs, facts


def summary(refs):
    return [(r.op, ".".join(r.parts) or r.path or r.target) for r in refs if not r.dynamic]


# ---------------------------------------------------------------- OneLake paths
def test_locations():
    loc = parse_location(f"abfss://{WS}@onelake.dfs.fabric.microsoft.com/{LH}/Tables/dbo/tags")
    assert (loc.workspace, loc.item, loc.area, loc.rel) == (WS, LH, "Tables", ["dbo", "tags"])
    loc = parse_location("abfss://My WS@onelake.dfs.fabric.microsoft.com/bronze.Lakehouse/Files/raw/x.csv")
    assert (loc.workspace, loc.item, loc.item_type, loc.area) == ("My WS", "bronze", "Lakehouse", "Files")
    loc = parse_location(f"https://onelake.dfs.fabric.microsoft.com/{WS}/{LH}/Files/a")
    assert loc.item == LH and loc.area == "Files"
    for rel in ("Tables/tags", "/lakehouse/default/Tables/tags", "file:/lakehouse/default/Tables/tags"):
        loc = parse_location(rel)
        assert loc.default and loc.area == "Tables" and loc.rel == ["tags"], rel
    assert parse_location("abfss://raw@contoso.dfs.core.windows.net/sap").external
    assert parse_location("data.csv") is None and parse_location("select 1") is None


def test_table_and_folder_from_path():
    assert table_from_rel(["dbo", "tags"], True) == ("dbo", "tags")
    assert table_from_rel(["tags", "_delta_log"], None) == (None, "tags")
    assert table_from_rel(["tags"], False) == (None, "tags")
    assert files_folder(["raw", "pims", "*.csv"]) == "Files/raw/pims"
    assert files_folder(["raw", "pims"]) == "Files/raw/pims"


# ---------------------------------------------------------------- SQL
def test_sql_reads_writes_and_locals():
    sql = """
    WITH recent AS (SELECT * FROM silver.tags WHERE x = 'FROM fake.table')  -- FROM comment.table
    INSERT INTO gold.fact SELECT * FROM recent r, other_table o JOIN lh_ref.ism.codes c ON r.k = c.k;
    MERGE INTO gold.dim USING silver.docs ON 1 = 1 WHEN MATCHED THEN UPDATE SET *;
    CREATE OR REPLACE TEMP VIEW v AS SELECT * FROM bronze.raw;
    SELECT * FROM v;
    CREATE TABLE t2 USING DELTA AS SELECT * FROM range(10);
    DELETE FROM gold.old WHERE 1 = 0;
    OPTIMIZE gold.fact;
    VACUUM delta.`/lakehouse/default/Tables/x` RETAIN 1 HOURS;
    """
    got = {(r.op, ".".join(r.parts) or r.path) for r in sql_refs(sql)}
    assert ("read", "silver.tags") in got and ("write", "gold.fact") in got
    assert ("read", "lh_ref.ism.codes") in got and ("read", "other_table") in got
    assert ("write", "gold.dim") in got and ("read", "silver.docs") in got
    assert ("read", "bronze.raw") in got and ("write", "t2") in got
    assert ("write", "gold.old") in got and ("maintain", "gold.fact") in got
    assert ("maintain", "/lakehouse/default/Tables/x") in got
    names = {n for _, n in got}
    assert not names & {"recent", "v", "fake.table", "comment.table", "DELTA", "range"}
    assert ("read", "gold.old") not in got                       # DELETE FROM isn't a read


def test_sql_paths_and_use():
    refs = sql_refs("SELECT * FROM delta_scan('/lakehouse/default/Tables/f')", paths_only=True)
    assert [(r.op, r.path) for r in refs] == [("read", "/lakehouse/default/Tables/f")]
    assert [(r.op, r.parts) for r in sql_refs("USE silver")] == [("use", ["silver"])]
    assert sql_refs("SELECT * FROM [dbo].[DocumentTags] t")[0].parts == ["dbo", "DocumentTags"]


# ---------------------------------------------------------------- Python
def test_spark_reads_and_writes():
    refs, _ = refs_of(['''
raw = "Files/raw/tags/"
df = spark.read.format("csv").option("header", True).load(raw + "*.csv")
df.write.mode("overwrite").saveAsTable("bronze.tags")
spark.read.table("silver.docs").writeTo("gold.docs").createOrReplace()
df.write.format("delta").save("Tables/out")
spark.read.option("path", "Files/x/").format("parquet").load()
df.writeStream.toTable("bronze.stream")
'''])
    got = summary(refs)
    assert ("read", "Files/raw/tags/*.csv") in got and ("write", "bronze.tags") in got
    assert ("read", "silver.docs") in got and ("write", "gold.docs") in got
    assert ("write", "Tables/out") in got and ("read", "Files/x/") in got and ("write", "bronze.stream") in got


def test_values_through_constants_loops_dicts_and_functions():
    refs, _ = refs_of(['''
mapping = {"cms_tags": "fact_tags", "dcs_docs": "dim_docs"}
for src, dst in mapping.items():
    spark.read.table(f"silver.{src}").write.saveAsTable(f"gold.{dst}")
for t in ["a", "b"]:
    spark.sql(f"OPTIMIZE silver.{t}")
def load(name, lake="bronze"):
    return spark.table(f"{lake}.{name}")
load("tags")
'''])
    got = summary(refs)
    for want in [("read", "silver.cms_tags"), ("read", "silver.dcs_docs"), ("write", "gold.fact_tags"),
                 ("write", "gold.dim_docs"), ("maintain", "silver.a"), ("maintain", "silver.b"), ("read", "bronze.tags")]:
        assert want in got, want


def test_delta_tables_temp_views_and_dynamic_names():
    refs, _ = refs_of(['''
from delta.tables import DeltaTable
clean.createOrReplaceTempView("tags_clean")
target = DeltaTable.forName(spark, "silver.tags")
target.alias("t").merge(clean.alias("s"), "t.id = s.id").whenMatchedUpdateAll().execute()
DeltaTable.forPath(spark, "Tables/x").optimize().executeCompaction()
spark.sql("INSERT INTO silver.hist SELECT * FROM tags_clean")
spark.sql(f"INSERT OVERWRITE TABLE gold.f SELECT * FROM {unknown}")
spark.read.table(f"{lh}.{tbl}")
'''])
    got = summary(refs)
    assert ("write", "silver.tags") in got and ("maintain", "Tables/x") in got
    assert ("write", "silver.hist") in got and ("write", "gold.f") in got
    assert not any(n == "tags_clean" for _, n in got)
    shown = sorted(r.shown for r in refs if r.dynamic)
    assert shown == ["{lh}.{tbl}", "{unknown}"]


def test_notebook_runs_files_sempy_and_python_notebooks():
    refs, facts = refs_of(['%run nb_config\n%pip install x', '''
dag = {"activities": [{"name": "a", "path": "nb_a"}, {"name": "b", "path": "nb_b", "workspace": "Other WS"}]}
notebookutils.notebook.runMultiple(dag)
mssparkutils.notebook.run("nb_c", 90, {}, "11111111-2222-3333-4444-555555555555")
notebookutils.fs.cp("Files/in", "Files/out", True)
import sempy.fabric as fabric
fabric.read_table("PIMS Model", "Tags")
fabric.refresh_dataset("PIMS Model")
pdf.to_parquet("/lakehouse/default/Files/export/x.parquet")
write_deltalake("/lakehouse/default/Tables/kpi", pdf, mode="overwrite")
duckdb.sql("SELECT * FROM delta_scan('/lakehouse/default/Tables/fact')")
pd.read_csv("not/a/lakehouse/path.csv")
'''])
    runs = [(r.target, r.workspace) for r in refs if r.kind == "notebook"]
    assert runs == [("nb_config", ""), ("nb_a", ""), ("nb_b", "Other WS"), ("nb_c", WS)]
    got = summary(refs)
    assert ("read", "Files/in") in got and ("write", "Files/out") in got
    assert ("read", "PIMS Model") in got and ("refresh", "PIMS Model") in got
    assert ("write", "/lakehouse/default/Files/export/x.parquet") in got
    assert ("write", "/lakehouse/default/Tables/kpi") in got and ("read", "/lakehouse/default/Tables/fact") in got
    assert not any("not/a" in n for _, n in got)
    assert facts["runTargets"] == ["nb_config"]


def test_constants_from_run_seed_and_exports():
    refs, _ = refs_of(['spark.read.table(f"{bronze_lakehouse}.tags")'], seed={"bronze_lakehouse": ["bronze"]})
    assert summary(refs) == [("read", "bronze.tags")]
    _, facts = refs_of(['bronze = "bronze"\nfor t in ["a"]:\n    pass\ndef f(x="y"):\n    pass'])
    assert facts["exports"] == {"bronze": ["bronze"]}               # top-level names only


# ---------------------------------------------------------------- notebook definitions
def test_ipynb_dependencies_and_cell_languages():
    meta = {"language_info": {"name": "python"}, "kernel_info": {"name": "synapse_pyspark"},
            "dependencies": {"lakehouse": {"default_lakehouse": LH.upper(), "default_lakehouse_name": "silver",
                                           "default_lakehouse_workspace_id": WS, "known_lakehouses": [{"id": LH}, {"id": WS}]},
                             "environment": {"environmentId": WS, "workspaceId": WS}}}
    nb = {"nbformat": 4, "metadata": meta, "cells": [
        {"cell_type": "markdown", "source": ["# title"], "metadata": {}},
        {"cell_type": "code", "source": ["SELECT * FROM silver.tags"], "metadata": {"microsoft": {"language": "sparksql"}}},
        {"cell_type": "code", "source": "this is ( not python", "metadata": {}},
        {"cell_type": "code", "source": 'val df = spark.read.table("bronze.raw")\ndf.write.saveAsTable("silver.x")',
         "metadata": {"microsoft": {"language": "scala"}}}]}
    info = read_notebook({"notebook-content.ipynb": json.dumps(nb).encode()})
    assert info.default_lakehouse == {"id": LH, "name": "silver", "workspace_id": WS}
    assert info.known_lakehouses == [LH, WS] and info.environment["id"] == WS
    refs, facts = analyze_notebook(info)
    assert ("read", "silver.tags") in summary(refs)
    assert ("read", "bronze.raw") in summary(refs) and ("write", "silver.x") in summary(refs)
    assert facts["regexCells"] == [3] and refs[0].cell == 2


def test_fabric_source_format():
    text = '''# Fabric notebook source

# METADATA ********************

# META {
# META   "kernel_info": {"name": "synapse_pyspark"},
# META   "dependencies": {"lakehouse": {"default_lakehouse": "%s", "default_lakehouse_name": "bronze"}}
# META }

# CELL ********************

df = spark.read.table("raw_tags")

# METADATA ********************

# META {
# META   "language": "python"
# META }

# CELL ********************

%%%%sql
INSERT INTO silver.tags SELECT * FROM bronze.raw_tags

# METADATA ********************

# META {
# META   "language": "sparksql"
# META }
''' % LH
    info = read_notebook({"notebook-content.py": text.encode()})
    assert info.format == "fabricGitSource" and info.default_lakehouse["id"] == LH
    refs, _ = analyze_notebook(info)
    assert summary(refs) == [("read", "raw_tags"), ("write", "silver.tags"), ("read", "bronze.raw_tags")]
    assert [r.cell for r in refs] == [1, 2, 2]


# ---------------------------------------------------------------- pipelines and models
def test_pipeline_activities():
    content = {"properties": {"activities": [
        {"name": "Loop", "type": "ForEach", "typeProperties": {"activities": [
            {"name": "Run nb", "type": "TridentNotebook", "typeProperties": {"notebookId": LH, "workspaceId": WS}}]}},
        {"name": "Copy", "type": "Copy", "typeProperties": {
            "source": {"datasetSettings": {"type": "SqlServerTable", "typeProperties": {"schema": "dbo", "table": "T"}}},
            "sink": {"datasetSettings": {"type": "LakehouseTable", "typeProperties": {"table": {"value": "@item()", "type": "Expression"}},
                                         "linkedService": {"properties": {"type": "Lakehouse", "typeProperties": {"artifactId": LH}}}}}}},
        {"name": "Refresh", "type": "PBISemanticModelRefresh", "typeProperties": {"datasetId": WS}}]}}
    refs = analyze_pipeline(content)
    assert [(r.op, r.activity) for r in refs] == [("run", "Run nb"), ("read", "Copy"), ("write", "Copy"), ("refresh", "Refresh")]
    assert refs[0].item_id == LH and refs[1].external == "SqlServerTable dbo.T"
    assert refs[2].item_id == LH and refs[2].dynamic


def test_model_sources():
    bim = {"model": {"defaultMode": "directLake", "expressions": [
        {"name": "DL", "expression": [f'let S = AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/{WS}/{LH}") in S']},
        {"name": "DB", "expression": f'Sql.Database("x.datawarehouse.fabric.microsoft.com", "{WS}")'}],
        "tables": [
            {"name": "A", "partitions": [{"source": {"type": "entity", "entityName": "fact", "expressionSource": "DL"}}],
             "columns": [{"name": "Key", "sourceColumn": "key_id", "dataType": "int64", "isHidden": True},
                         {"name": "Name", "sourceColumn": "Name", "dataType": "string"},
                         {"type": "calculated", "name": "Label", "dataType": "string", "expression": "[Name]"},
                         {"type": "rowNumber", "name": "RowNumber-1", "isHidden": True}]},
            {"name": "B", "partitions": [{"mode": "import", "source": {"type": "m", "expression": [
                "let S = Lakehouse.Contents(null),", f'N = S{{[workspaceId="{WS}"]}}[Data],',
                f'L = N{{[lakehouseId="{LH}"]}}[Data],', 'T = L{[Id="dim",ItemKind="Table"]}[Data] in T']}}]},
            {"name": "C", "partitions": [{"source": {"type": "entity", "entityName": "hist", "expressionSource": "DB"}}]},
            {"name": "LocalDateTable_1", "partitions": [{"source": {"type": "calculated"}}]}]}}
    t = {x["table"]: x for x in model_tables(bim)}
    assert set(t) == {"A", "B", "C"}
    assert (t["A"]["mode"], t["A"]["source"]["item_id"], t["A"]["entity"]) == ("Direct Lake", LH, "fact")
    assert (t["B"]["mode"], t["B"]["source"]["item_id"], t["B"]["entity"]) == ("Import", LH, "dim")
    assert t["C"]["source"]["connector"] == "sql" and t["C"]["source"]["item_id"] == WS
    assert t["A"]["columns"] == [                                                   # row numbers left out
        {"name": "Key", "source": "key_id", "dataType": "int64", "hidden": True, "calculated": False},
        {"name": "Name", "source": "Name", "dataType": "string", "hidden": False, "calculated": False},
        {"name": "Label", "source": "", "dataType": "string", "hidden": False, "calculated": True}]
    assert t["B"]["columns"] == []
    assert parse_m("") == {}
    assert base64  # keep the import for readers adding payload tests
