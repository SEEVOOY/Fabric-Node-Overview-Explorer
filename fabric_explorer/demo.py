"""A demo workspace behind a fake HTTP layer that answers like the real APIs.

`python run.py --demo` explores it without signing in, and the tests use it. Only the transport is
fake: the real FabricClient, session, scanners and server run on top of it, so paging, the
getDefinition long-running operation, throttling (429) and errors all take the real code paths.
"""
from __future__ import annotations

import base64
import json
import random
import threading
import time
import uuid
from urllib.parse import parse_qs, unquote, urlparse

_NS = uuid.UUID("6f1e4c5a-1b2c-4d3e-8f90-a1b2c3d4e5f6")


def gid(name: str) -> str:
    return str(uuid.uuid5(_NS, name))


WS = gid("ws:pims")
WS2 = gid("ws:shared")
TENANT = gid("tenant")

FOLDERS = [("f_ingest", "01 Ingest", ""), ("f_transform", "02 Transform", ""), ("f_helpers", "Helpers", "f_transform"),
           ("f_serve", "03 Serve", ""), ("f_storage", "04 Storage", ""), ("f_sandbox", "99 Sandbox", "")]

ITEMS = [  # (key, name, type, folder)
    ("bronze", "bronze", "Lakehouse", "f_storage"), ("silver", "silver", "Lakehouse", "f_storage"),
    ("gold", "gold", "Lakehouse", "f_storage"), ("lh_reference", "lh_reference", "Lakehouse", "f_storage"),
    ("sql_bronze", "bronze", "SQLEndpoint", "f_storage"), ("sql_silver", "silver", "SQLEndpoint", "f_storage"),
    ("sql_gold", "gold", "SQLEndpoint", "f_storage"), ("sql_ref", "lh_reference", "SQLEndpoint", "f_storage"),
    ("nb00", "nb_00_config", "Notebook", "f_helpers"),
    ("nb10", "nb_10_ingest_pims_tags", "Notebook", "f_ingest"),
    ("nb11", "nb_11_ingest_documents", "Notebook", "f_ingest"),
    ("nb20", "nb_20_bronze_to_silver_tags", "Notebook", "f_transform"),
    ("nb21", "nb_21_bronze_to_silver_documents", "Notebook", "f_transform"),
    ("nb30", "nb_30_silver_to_gold", "Notebook", "f_transform"),
    ("nb40", "nb_40_orchestrate", "Notebook", "f_transform"),
    ("nb50", "nb_50_maintenance", "Notebook", "f_transform"),
    ("nb60", "nb_60_semantic_checks", "Notebook", "f_serve"),
    ("nb70", "nb_70_kpi_duckdb", "Notebook", "f_serve"),
    ("nb99", "nb_99_scratch", "Notebook", "f_sandbox"),
    ("nbx", "nb_restricted_hr_extract", "Notebook", "f_sandbox"),
    ("pl", "pl_daily_load", "DataPipeline", "f_ingest"),
    ("model", "PIMS Model", "SemanticModel", "f_serve"),
    ("rep1", "PIMS Tags", "Report", "f_serve"), ("rep2", "Document status", "Report", "f_serve"),
    ("env", "env_pims_spark", "Environment", ""), ("sjd", "sjd_nightly_compaction", "SparkJobDefinition", ""),
    ("dflow", "df_reference_codes", "Dataflow", ""), ("wh", "wh_reporting", "Warehouse", ""),
]
ID = {key: gid(f"item:{key}") for key, *_ in ITEMS}
ID["shared_lh"] = gid("item:shared_lh")
SQL_OF = {"bronze": "sql_bronze", "silver": "sql_silver", "gold": "sql_gold", "lh_reference": "sql_ref"}

TABLES = {
    "bronze": ["cms_tags_raw", "dcs_documents_raw", "cms_documents_tags_raw", "old_tags_backup"],
    "silver": ["cms_tags_current", "cms_tags_history", "dcs_documents", "cms_documents_tags"],
    "gold": ["fact_pims_tags", "dim_documents", "bridge_documents_tags", "kpi_tags_by_domain"],
}
SCHEMA_TABLES = {"dbo": ["project_master"], "ism": ["pro_nomenclature", "discipline_codes"]}
WH_TABLES = {"dbo": ["rpt_tags_by_plant", "rpt_documents_status"], "staging": ["stg_tags"]}     # wh_reporting
FILES = {"bronze": ["raw", "archive"], "gold": ["exports", "external"], "lh_reference": ["reference_docs"]}

_TAG = ["PrimKey", "TagNo", "Description", "Domain", "Plant", "Project", "System", "SubSystem", "TagCategory",
        "Discipline", "CreatedBy", "Created", "UpdatedBy", "Updated"]
_DOC = ["DocumentID", "DocumentNo", "Title", "Discipline", "DocumentType", "Revision", "RevisionDate", "Status",
        "Plant", "Project"]
_LINK = ["tag_no", "document_id", "Plant", "Project"]
COLUMNS = {
    "rpt_tags_by_plant": ["Plant", "Domain", "tags", "loaded_at"],
    "rpt_documents_status": ["Status", "Discipline", "documents", "loaded_at"],
    "stg_tags": ["TagNo", "Plant", "Domain", "Description", "loaded_at"],
    "cms_tags_raw": _TAG + ["ingested_at"], "old_tags_backup": _TAG, "cms_tags_current": _TAG,
    "cms_tags_history": _TAG + ["loaded_at"], "dcs_documents_raw": _DOC, "dcs_documents": _DOC + ["DisciplineName"],
    "cms_documents_tags_raw": _LINK, "cms_documents_tags": _LINK, "bridge_documents_tags": _LINK,
    "fact_pims_tags": _TAG, "dim_documents": _DOC, "kpi_tags_by_domain": ["Domain", "tags"],
    "pro_nomenclature": ["Code", "Description", "Discipline", "ValidFrom"], "discipline_codes": ["Code", "Discipline"],
    "project_master": ["Project", "ProjectName", "Plant", "Operator"], "ref_disciplines": ["Code", "Discipline"],
    "plants": ["PlantCode", "PlantName", "Country"],
}
LOG_ONLY = {"kpi_tags_by_domain", "rpt_tags_by_plant", "rpt_documents_status", "stg_tags"}   # read from their Delta logs


def _col_type(name: str) -> str:
    low = name.lower()
    if low.endswith(("_at", "date")) or name in ("Created", "Updated", "ValidFrom"):
        return "timestamp"
    return "bigint" if name in ("tags", "PrimKey") else "int" if name == "Revision" else "string"


def _delta_log(table: str) -> str:
    fields = [{"name": c, "type": "long" if _col_type(c) == "bigint" else _col_type(c), "nullable": True,
               "metadata": {}} for c in COLUMNS[table]]
    return "\n".join(json.dumps(x) for x in (
        {"commitInfo": {"timestamp": 1761700000000, "operation": "CREATE TABLE AS SELECT"}},
        {"protocol": {"minReaderVersion": 1, "minWriterVersion": 2}},
        {"metaData": {"id": gid("meta:" + table), "format": {"provider": "parquet", "options": {}},
                      "schemaString": json.dumps({"type": "struct", "fields": fields}), "partitionColumns": []}}))


def _mcols(names, hidden=()):
    """Model columns that read the source column of the same name."""
    kinds = {"timestamp": "dateTime", "bigint": "int64", "int": "int64"}
    return [{"name": c, "sourceColumn": c, "dataType": kinds.get(_col_type(c), "string"), **({"isHidden": True} if c in hidden else {})}
            for c in names]


def _cells(*cells):
    return list(cells)


NOTEBOOKS = {
    "nb00": dict(lakehouse=None, params=True, cells=_cells(
        '# parameters\nbronze_lakehouse = "bronze"\nsilver_lakehouse = "silver"\ngold_lakehouse = "gold"\n'
        'run_date = "2026-10-29"\n')),
    "nb10": dict(lakehouse="bronze", known=("lh_reference",), env="env", cells=_cells(
        "#md # Ingest PIMS tags\nRaw CSV drops from PIMS land in Files/raw/pims/tags.",
        'from pyspark.sql import functions as F\n\nraw_path = "Files/raw/pims/tags/"\n'
        'df = (spark.read.format("csv")\n      .option("header", "true")\n      .load(raw_path + "*.csv"))\n'
        'df = df.withColumn("ingested_at", F.current_timestamp())\n'
        'df.write.mode("overwrite").format("delta").saveAsTable("cms_tags_raw")',
        f'nomenclature = spark.read.format("delta").load(\n'
        f'    "abfss://{WS}@onelake.dfs.fabric.microsoft.com/{ID["lh_reference"]}/Tables/ism/pro_nomenclature")\n'
        f'plants = spark.read.load("abfss://{WS2}@onelake.dfs.fabric.microsoft.com/{ID["shared_lh"]}/Tables/plants")\n'
        'display(nomenclature.join(plants, "PlantCode").limit(5))')),
    "nb11": dict(lakehouse="bronze", cells=_cells(
        'docs = spark.read.json("Files/raw/dcs/documents/*.json")\n'
        'docs.write.format("delta").mode("append").save("Tables/dcs_documents_raw")\n'
        'notebookutils.fs.cp("Files/raw/dcs/documents", "Files/archive/dcs/documents", True)')),
    "nb20": dict(lakehouse="silver", known=("bronze",), env="env", cells=_cells(
        "%run nb_00_config",
        'from delta.tables import DeltaTable\n\n'
        'tags = spark.read.table(f"{bronze_lakehouse}.cms_tags_raw")\n'
        'clean = tags.dropDuplicates(["TagNo"]).filter("TagNo IS NOT NULL")\n'
        'clean.createOrReplaceTempView("tags_clean")\n\n'
        'target = DeltaTable.forName(spark, "silver.cms_tags_current")\n'
        '(target.alias("t")\n    .merge(clean.alias("s"), "t.TagNo = s.TagNo")\n'
        '    .whenMatchedUpdateAll()\n    .whenNotMatchedInsertAll()\n    .execute())',
        'spark.sql("""\nINSERT INTO silver.cms_tags_history\n'
        'SELECT *, current_timestamp() AS loaded_at FROM tags_clean\n""")')),
    "nb21": dict(lakehouse="silver", known=("bronze", "lh_reference"), cells=_cells(
        "%%sql\nCREATE OR REPLACE TABLE silver.dcs_documents AS\n"
        "SELECT d.DocumentID, d.Title, n.Description AS DisciplineName\n"
        "FROM bronze.dcs_documents_raw d\n"
        "LEFT JOIN lh_reference.ism.pro_nomenclature n ON d.Discipline = n.Code",
        'links = spark.table("bronze.cms_documents_tags_raw")\n'
        'links.write.mode("overwrite").saveAsTable("silver.cms_documents_tags")')),
    "nb30": dict(lakehouse="gold", known=("silver",), cells=_cells(
        'source_tables = {\n    "cms_tags_current": "fact_pims_tags",\n    "dcs_documents": "dim_documents",\n}\n'
        'for src, dst in source_tables.items():\n'
        '    df = spark.read.table(f"silver.{src}")\n'
        '    df.write.mode("overwrite").saveAsTable(f"gold.{dst}")',
        'links = spark.sql("SELECT * FROM silver.cms_documents_tags")\n'
        'links.write.mode("overwrite").saveAsTable("gold.bridge_documents_tags")\n'
        'export = spark.table("gold.fact_pims_tags").toPandas()\n'
        'export.to_parquet("/lakehouse/default/Files/exports/fact_pims_tags.parquet")',
        'import com.microsoft.spark.fabric\n'
        'by_plant = spark.table("gold.fact_pims_tags").groupBy("Plant", "Domain").count().withColumnRenamed("count", "tags")\n'
        'by_plant.write.mode("overwrite").synapsesql("wh_reporting.dbo.rpt_tags_by_plant")')),
    "nb40": dict(lakehouse="silver", cells=_cells(
        'dag = {\n    "activities": [\n'
        '        {"name": "tags", "path": "nb_20_bronze_to_silver_tags", "timeoutPerCellInSeconds": 600},\n'
        '        {"name": "documents", "path": "nb_21_bronze_to_silver_documents"},\n'
        '        {"name": "gold", "path": "nb_30_silver_to_gold", "dependencies": ["tags", "documents"]},\n'
        '    ]\n}\nnotebookutils.notebook.runMultiple(dag)',
        'mssparkutils.notebook.run("nb_50_maintenance", 1800)')),
    "nb50": dict(lakehouse="silver", cells=_cells(
        'tables = ["silver.cms_tags_current", "silver.cms_tags_history", "gold.fact_pims_tags"]\n'
        'for t in tables:\n    spark.sql(f"OPTIMIZE {t}")\n    spark.sql(f"VACUUM {t} RETAIN 168 HOURS")')),
    "nb60": dict(lakehouse="gold", kernel="jupyter", cells=_cells(
        'import sempy.fabric as fabric\n\ntags = fabric.read_table("PIMS Model", "Tags")\nprint(len(tags))\n'
        'fabric.refresh_dataset("PIMS Model")',
        'lakehouse_name = notebookutils.runtime.context["defaultLakehouseName"]\n'
        'table_name = notebookutils.widgets.get("table")\n'
        'df = spark.read.table(f"{lakehouse_name}.{table_name}")')),
    "nb70": dict(lakehouse="gold", kernel="jupyter", cells=_cells(
        'import duckdb\nfrom deltalake import write_deltalake\n\n'
        'kpi = duckdb.sql("""\n    SELECT Domain, COUNT(*) AS tags\n'
        "    FROM delta_scan('/lakehouse/default/Tables/fact_pims_tags')\n    GROUP BY Domain\n\"\"\").df()\n"
        'write_deltalake("/lakehouse/default/Tables/kpi_tags_by_domain", kpi, mode="overwrite")')),
    "nb99": dict(lakehouse=None, cells=_cells(
        'df = spark.read.table("cms_tags_current")\ndisplay(df.limit(10))')),
}


def _ipynb(spec: dict) -> dict:
    meta = {"language_info": {"name": "python"}, "kernel_info": {"name": spec.get("kernel", "synapse_pyspark")},
            "dependencies": {}}
    lh = spec.get("lakehouse")
    if lh:
        meta["dependencies"]["lakehouse"] = {
            "default_lakehouse": ID[lh], "default_lakehouse_name": lh, "default_lakehouse_workspace_id": WS,
            "known_lakehouses": [{"id": ID[k]} for k in (lh, *spec.get("known", ()))]}
    if spec.get("env"):
        meta["dependencies"]["environment"] = {"environmentId": ID[spec["env"]], "workspaceId": WS}
    cells = []
    for i, src in enumerate(spec["cells"]):
        if src.startswith("#md "):
            cells.append({"cell_type": "markdown", "source": src[4:].splitlines(True), "metadata": {}})
            continue
        lang = "sparksql" if src.lstrip().startswith("%%sql") else "python"
        cmeta = {"microsoft": {"language": lang, "language_group": "synapse_pyspark"}}
        if spec.get("params") and i == 0:
            cmeta["tags"] = ["parameters"]
        cells.append({"cell_type": "code", "source": src.splitlines(True), "execution_count": None, "outputs": [],
                      "metadata": cmeta})
    return {"nbformat": 4, "nbformat_minor": 5, "cells": cells, "metadata": meta}


def _m(lines):
    return lines


MODEL = {"name": "PIMS Model", "compatibilityLevel": 1604, "model": {
    "defaultMode": "directLake",
    "expressions": [
        {"name": "DirectLake - gold", "kind": "m", "expression": _m([
            "let", f'    Source = AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/{WS}/{ID["gold"]}", '
                   "[HierarchicalNavigation=true])", "in", "    Source"])},
        {"name": "DatabaseQuery", "kind": "m", "expression":
            f'let database = Sql.Database("x6eps4xrq2xudenlfv6naeo3i4-demo.datawarehouse.fabric.microsoft.com", '
            f'"{ID["sql_silver"]}") in database'}],
    "tables": [
        {"name": "Tags", "columns": _mcols(["PrimKey", "TagNo", "Description", "Domain", "Plant", "Project", "Discipline", "Updated"],
                                         hidden={"PrimKey"}) + [
            {"type": "calculated", "name": "Tag label", "dataType": "string", "expression": "[TagNo] & \" \" & [Description]"},
            {"type": "rowNumber", "name": "RowNumber-2662979B", "dataType": "int64", "isHidden": True}],
         "partitions": [{"name": "Tags", "mode": "directLake", "source": {
            "type": "entity", "entityName": "fact_pims_tags", "schemaName": "dbo", "expressionSource": "DirectLake - gold"}}]},
        {"name": "Documents", "columns": _mcols(["DocumentID", "DocumentNo", "Title", "Discipline", "Status", "Plant"], hidden={"DocumentID"}),
         "partitions": [{"name": "Documents", "mode": "directLake", "source": {
            "type": "entity", "entityName": "dim_documents", "expressionSource": "DirectLake - gold"}}]},
        {"name": "Document tags", "columns": _mcols(["tag_no", "document_id", "Plant"], hidden={"tag_no", "document_id"}),
         "partitions": [{"name": "Document tags", "mode": "directLake", "source": {
            "type": "entity", "entityName": "bridge_documents_tags", "expressionSource": "DirectLake - gold"}}]},
        {"name": "Tag history", "columns": _mcols(["TagNo", "Updated"]) + [
            {"name": "Loaded at", "sourceColumn": "loaded_at", "dataType": "dateTime"}],
         "partitions": [{"name": "Tag history", "source": {
            "type": "entity", "entityName": "cms_tags_history", "schemaName": "dbo", "expressionSource": "DatabaseQuery"}}]},
        {"name": "Measures", "partitions": [{"name": "Measures", "mode": "import", "source": {
            "type": "calculated", "expression": 'ROW("Total tags", BLANK())'}}]},
    ]}}

PIPELINE = {"properties": {"activities": [
    {"name": "Copy document links", "type": "Copy", "dependsOn": [], "typeProperties": {
        "source": {"type": "SqlServerSource", "datasetSettings": {
            "type": "SqlServerTable", "typeProperties": {"schema": "dbo", "table": "DocumentTags"}}},
        "sink": {"type": "LakehouseTableSink", "tableActionOption": "Overwrite", "datasetSettings": {
            "type": "LakehouseTable", "typeProperties": {"table": "cms_documents_tags_raw"},
            "linkedService": {"name": "bronze", "properties": {"type": "Lakehouse", "typeProperties": {
                "artifactId": ID["bronze"], "workspaceId": WS, "rootFolder": "Tables"}}}}}}},
    {"name": "Ingest tags", "type": "TridentNotebook", "typeProperties": {"notebookId": ID["nb10"], "workspaceId": WS}},
    {"name": "Ingest documents", "type": "TridentNotebook",
     "typeProperties": {"notebookId": ID["nb11"], "workspaceId": WS}},
    {"name": "For each domain", "type": "ForEach", "typeProperties": {"activities": [
        {"name": "Transform", "type": "TridentNotebook",
         "typeProperties": {"notebookId": ID["nb40"], "workspaceId": WS}}]}},
    {"name": "Refresh model", "type": "PBISemanticModelRefresh",
     "typeProperties": {"datasetId": ID["model"], "groupId": WS}},
]}}

SJD = {"executableFile": "abfss://.../Main/compact.py", "defaultLakehouseArtifactId": ID["silver"],
       "mainClass": "", "additionalLakehouseIds": [ID["gold"]], "language": "Python"}
MASHUP = (f'section Section1;\nshared ref_codes = let\n    Source = Lakehouse.Contents(null),\n'
          f'    Nav = Source{{[workspaceId="{WS}"]}}[Data],\n    Lake = Nav{{[lakehouseId="{ID["lh_reference"]}"]}}[Data],\n'
          f'    Codes = Lake{{[Schema="ism",Id="discipline_codes",ItemKind="Table"]}}[Data]\nin\n    Codes;\n')


def _b64(obj) -> str:
    raw = obj if isinstance(obj, bytes) else (obj.encode() if isinstance(obj, str) else json.dumps(obj).encode())
    return base64.b64encode(raw).decode()


def _definition(key: str):
    platform = {"path": ".platform", "payload": _b64({"metadata": {"type": "x", "displayName": key},
                                                      "config": {"logicalId": gid("logical:" + key)}}),
                "payloadType": "InlineBase64"}
    if key in NOTEBOOKS:
        return [{"path": "notebook-content.ipynb", "payload": _b64(_ipynb(NOTEBOOKS[key])), "payloadType": "InlineBase64"},
                platform]
    if key == "model":
        return [{"path": "definition.pbism", "payload": _b64({"version": "4.0"}), "payloadType": "InlineBase64"},
                {"path": "model.bim", "payload": _b64(MODEL), "payloadType": "InlineBase64"}, platform]
    if key == "pl":
        return [{"path": "pipeline-content.json", "payload": _b64(PIPELINE), "payloadType": "InlineBase64"}, platform]
    if key == "sjd":
        return [{"path": "SparkJobDefinitionV1.json", "payload": _b64(SJD), "payloadType": "InlineBase64"}, platform]
    if key == "dflow":
        return [{"path": "mashup.pq", "payload": _b64(MASHUP), "payloadType": "InlineBase64"},
                {"path": "queryMetadata.json", "payload": _b64({"formatVersion": "202502"}), "payloadType": "InlineBase64"},
                platform]
    if key == "env":
        return [{"path": "Setting/Sparkcompute.yml", "payload": _b64("instance_pool_id: null\n"), "payloadType": "InlineBase64"},
                platform]
    if key in ("rep1", "rep2"):
        pbir = {"version": "4.0", "datasetReference": {"byConnection": {
            "connectionString": f"Data Source=powerbi://api.powerbi.com/v1.0/myorg/PIMS;semanticmodelid={ID['model']}"}}}
        return [{"path": "definition.pbir", "payload": _b64(pbir), "payloadType": "InlineBase64"}, platform]
    return None


class FakeResponse:
    def __init__(self, status: int, body=None, headers=None, raw: str | None = None):
        self.status_code = status
        self._body = body if raw is None else None
        self.headers = headers or {}
        self.content = raw.encode() if raw is not None else (b"" if body is None else json.dumps(body).encode())
        self.text = self.content.decode()
        self.reason = ""

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class DemoSignIn:
    def token(self, scope: str = "") -> str:
        return "demo-token"

    def account(self) -> str:
        return "you@contoso.com"


class DemoHttp:
    """requests.Session look-alike answering for the demo tenant."""

    def __init__(self, latency=(0.05, 0.25), lro: bool = True, page_size: int = 20, polls: int = 1):
        self.latency, self.lro, self.page_size, self.polls = latency, lro, page_size, polls
        self.calls: list = []
        self.fail: dict = {}             # url fragment -> [count, status, errorCode]
        self._ops: dict = {}
        self._lock = threading.Lock()

    def get(self, url, headers=None, timeout=None, params=None):
        return self._route("GET", url, None)

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002 - requests' own name
        return self._route("POST", url, json)

    # ------------------------------------------------------------------ data
    def _item(self, key, name, typ, folder, ws=WS):
        out = {"id": ID[key], "displayName": name, "type": typ, "description": "", "workspaceId": ws}
        if folder:
            out["folderId"] = gid(f"folder:{folder}")
        return out

    def _items(self, ws):
        if ws == WS:
            return [self._item(*i) for i in ITEMS]
        if ws == WS2:
            return [{"id": ID["shared_lh"], "displayName": "shared_master_data", "type": "Lakehouse",
                     "description": "", "workspaceId": WS2}]
        return None

    def _lakehouse(self, key, ws=WS):
        if ws == WS2:
            return {**self._items(WS2)[0], "properties": {"oneLakeTablesPath": "", "oneLakeFilesPath": "",
                                                          "sqlEndpointProperties": {"id": gid("sql:shared"),
                                                                                    "connectionString": "shared.datawarehouse.fabric.microsoft.com",
                                                                                    "provisioningStatus": "Success"}}}
        props = {"oneLakeTablesPath": f"https://onelake.dfs.fabric.microsoft.com/{WS}/{ID[key]}/Tables",
                 "oneLakeFilesPath": f"https://onelake.dfs.fabric.microsoft.com/{WS}/{ID[key]}/Files",
                 "sqlEndpointProperties": {"id": ID[SQL_OF[key]], "provisioningStatus": "Success",
                                           "connectionString": "x6eps4xrq2xudenlfv6naeo3i4-demo.datawarehouse.fabric.microsoft.com"}}
        if key == "lh_reference":
            props["defaultSchema"] = "dbo"
        return {**self._item(key, key, "Lakehouse", "f_storage"), "properties": props}

    def _page(self, url, values, key="value"):
        q = parse_qs(urlparse(url).query)
        start = int(q.get("continuationToken", ["0"])[0] or 0)
        chunk = values[start:start + self.page_size]
        body = {key: chunk}
        if start + self.page_size < len(values):
            token = str(start + self.page_size)
            base = url.split("?")[0]
            body["continuationToken"] = token
            body["continuationUri"] = f"{base}?continuationToken={token}"
        return FakeResponse(200, body)

    # ------------------------------------------------------------------ routing
    def _route(self, method, url, body):
        with self._lock:
            self.calls.append((method, url))
            for frag, spec in self.fail.items():
                if frag in url and spec[0] > 0:
                    spec[0] -= 1
                    return FakeResponse(spec[1], {"requestId": "demo", "errorCode": spec[2], "message": spec[2]},
                                        {"Retry-After": "1"})
        if self.latency:
            time.sleep(random.uniform(*self.latency))
        p = urlparse(url)
        if p.netloc == "api.fabric.microsoft.com":
            return self._fabric(method, url, [unquote(x) for x in p.path.split("/") if x])
        if p.netloc == "api.powerbi.com":
            segs = [x for x in p.path.split("/") if x]
            if len(segs) == 6 and segs[2] == "groups" and segs[4] == "reports":
                key = next((k for k, v in ID.items() if v == segs[5]), "")
                if key in ("rep1", "rep2"):
                    return FakeResponse(200, {"id": segs[5], "name": key, "datasetId": ID["model"],
                                              "datasetWorkspaceId": WS, "reportType": "PowerBIReport"})
            return FakeResponse(404, {"error": {"code": "ItemNotFound", "message": "Report not found"}})
        if p.netloc == "onelake.table.fabric.microsoft.com":
            return self._tables_api(p)
        if p.netloc == "onelake.dfs.fabric.microsoft.com":
            q = parse_qs(p.query)
            if "resource" not in q:                                   # reading a file
                name = unquote(p.path).rstrip("/").split("/")
                if len(name) > 3 and name[-2] == "_delta_log" and name[-3] in LOG_ONLY:
                    return FakeResponse(200, raw=_delta_log(name[-3]) if name[-1].endswith("0.json") else
                                        json.dumps({"commitInfo": {"operation": "WRITE"}}) + "\n" +
                                        json.dumps({"add": {"path": "part-0001.parquet", "size": 1024}}))
                return FakeResponse(404, {"error": {"code": "PathNotFound", "message": "The specified path does not exist."}})
            directory = q.get("directory", [""])[0].strip("/")
            for key, names in FILES.items():
                if directory == f"{ID[key]}/Files":
                    return FakeResponse(200, {"paths": [{"name": f"{directory}/{n}", "isDirectory": "true"} for n in names]})
            if directory.endswith("/Files"):
                return FakeResponse(200, {"paths": []})
            for t in LOG_ONLY:
                if directory.endswith(f"/{t}/_delta_log"):                # Tables/<table> or Tables/<schema>/<table>
                    return FakeResponse(200, {"paths": [{"name": f"{directory}/{i:020d}.json", "contentLength": "900"}
                                                        for i in range(2)] + [{"name": f"{directory}/_last_checkpoint"}]})
            if p.path.strip("/") == WS and directory == f"{ID['lh_reference']}/Tables":
                return FakeResponse(200, {"paths": [{"name": f"{ID['lh_reference']}/Tables/{s}", "isDirectory": "true"}
                                                    for s in SCHEMA_TABLES]})
            if p.path.strip("/") == WS and directory == f"{ID['wh']}/Tables":
                return FakeResponse(200, {"paths": [{"name": f"{ID['wh']}/Tables/{s}", "isDirectory": "true"} for s in WH_TABLES]})
            for schema, tables in WH_TABLES.items():
                if directory == f"{ID['wh']}/Tables/{schema}":
                    return FakeResponse(200, {"paths": [{"name": f"{directory}/{t}", "isDirectory": "true"} for t in tables]})
            for schema, tables in SCHEMA_TABLES.items():
                if directory == f"{ID['lh_reference']}/Tables/{schema}":
                    return FakeResponse(200, {"paths": [{"name": f"{directory}/{t}", "isDirectory": "true"}
                                                        for t in tables]})
            return FakeResponse(404, {"error": {"code": "PathNotFound", "message": "The specified path does not exist."}})
        return FakeResponse(404, {"errorCode": "NotFound", "message": "Unknown host"})

    def _tables_api(self, p):
        """OneLake table API (Delta / Unity Catalog flavour): GET .../tables/<item>.<schema>.<table>"""
        segs = [unquote(x) for x in p.path.split("/") if x]
        q = parse_qs(p.query)
        if len(segs) < 7 or segs[0] != "delta" or segs[3:6] != ["api", "2.1", "unity-catalog"] or segs[6] != "tables":
            return FakeResponse(404, {"error_code": "NOT_FOUND", "message": "Unknown route"})
        ws, item = segs[1].lower(), segs[2].lower()
        full = segs[7] if len(segs) > 7 else ""
        parts = full.split(".")
        if len(parts) == 3:
            catalog, schema, table = parts
        else:
            catalog, schema, table = q.get("catalog_name", [item])[0], q.get("schema_name", ["dbo"])[0], full
        key = next((k for k, v in ID.items() if v == item), "")
        known = (TABLES.get(key, []) + (["ref_disciplines"] if key == "silver" else []) if ws == WS else
                 (["plants"] if ws == WS2 and key == "shared_lh" else []))
        if key == "lh_reference":
            known = SCHEMA_TABLES.get(schema, [])
        elif schema != "dbo":
            known = []
        if table not in known or table in LOG_ONLY or catalog.lower() != item:
            return FakeResponse(404, {"error_code": "TABLE_DOES_NOT_EXIST", "message": f"Table '{table}' does not exist."})
        return FakeResponse(200, {"name": table, "catalog_name": item, "schema_name": schema, "table_type": None,
                                  "data_source_format": "DELTA", "storage_location": f"https://onelake.dfs.fabric.microsoft.com/{ws}/{item}/Tables/{table}",
                                  "columns": [{"name": c, "type_text": None, "type_json": None, "type_name": _col_type(c),
                                               "position": i, "nullable": True} for i, c in enumerate(COLUMNS[table])]})

    def _fabric(self, method, url, segs):
        segs = segs[1:]                                              # drop "v1"
        if segs == ["workspaces"] and method == "GET":
            return self._page(url, [{"id": WS, "displayName": "PIMS Data Platform (dev)", "type": "Workspace"},
                                    {"id": WS2, "displayName": "Shared Data (prod)", "type": "Workspace"},
                                    {"id": gid("ws:me"), "displayName": "My workspace", "type": "Personal"}])
        if segs[:1] == ["operations"] and method == "GET":
            op = self._ops.get(segs[1])
            if op is None:
                return FakeResponse(404, {"errorCode": "OperationNotFound", "message": "No such operation"})
            if len(segs) == 3 and segs[2] == "result":
                return FakeResponse(200, {"definition": {"parts": op["parts"]}})
            op["polls"] += 1
            done = op["polls"] >= op["needs"]
            return FakeResponse(200, {"status": "Succeeded" if done else "Running",
                                      "percentComplete": 100 if done else 50})
        if len(segs) < 2 or segs[0] != "workspaces":
            return FakeResponse(404, {"errorCode": "NotFound", "message": "Unknown route"})
        ws, rest = segs[1].lower(), segs[2:]
        items = self._items(ws)
        if items is None:
            return FakeResponse(403, {"errorCode": "InsufficientPrivileges", "message": "The caller is not authorized "
                                      "to access this workspace."})
        if not rest:
            return FakeResponse(200, {"id": ws, "displayName": "PIMS Data Platform (dev)" if ws == WS else
                                      "Shared Data (prod)", "type": "Workspace", "capacityId": gid("capacity")})
        if rest == ["items"]:
            return self._page(url, items)
        if rest == ["folders"]:
            if ws != WS:
                return self._page(url, [])
            return self._page(url, [{"id": gid(f"folder:{k}"), "displayName": n, "workspaceId": WS,
                                     **({"parentFolderId": gid(f"folder:{p}")} if p else {})} for k, n, p in FOLDERS])
        if rest == ["lakehouses"]:
            if ws == WS2:
                return self._page(url, [self._lakehouse("shared_lh", WS2)])
            return self._page(url, [self._lakehouse(k) for k, _, t, _ in ITEMS if t == "Lakehouse"])
        item_id = rest[1].lower() if len(rest) > 1 else ""
        key = next((k for k, v in ID.items() if v == item_id), "")
        known = {i["id"] for i in items}
        if item_id not in known:
            return FakeResponse(404, {"errorCode": "ItemNotFound", "message": "The requested item was not found"})
        if rest[0] == "items" and len(rest) == 2:
            return FakeResponse(200, next(i for i in items if i["id"] == item_id))
        if rest[0] == "lakehouses" and len(rest) == 2:
            return FakeResponse(200, self._lakehouse(key, ws))
        if rest[0] == "lakehouses" and rest[2:] == ["tables"]:
            if key == "lh_reference":
                return FakeResponse(400, {"errorCode": "UnsupportedOperationForSchemasEnabledLakehouse",
                                          "message": "The operation is not supported for a lakehouse with schemas "
                                                     "enabled."})
            names = TABLES.get(key, ["plants"] if ws == WS2 else [])
            return self._page(url, [{"type": "Managed", "name": n, "format": "Delta",
                                     "location": f"abfss://{ws}@onelake.dfs.fabric.microsoft.com/{item_id}/Tables/{n}"}
                                    for n in names], key="data")
        if rest[0] == "items" and rest[2:] == ["shortcuts"]:
            if key == "silver":
                return self._page(url, [{"path": "Tables", "name": "ref_disciplines", "target": {
                    "type": "OneLake", "oneLake": {"workspaceId": WS, "itemId": ID["lh_reference"],
                                                   "path": "Tables/ism/discipline_codes"}}}])
            if key == "gold":
                return self._page(url, [{"path": "Files/external", "name": "sap_exports", "target": {
                    "type": "AdlsGen2", "adlsGen2": {"location": "https://contosodatalake.dfs.core.windows.net",
                                                     "subpath": "/sap/exports", "connectionId": gid("conn")}}}])
            return self._page(url, [])
        if method == "POST" and rest[-1] == "getDefinition":
            if key == "nbx":
                return FakeResponse(403, {"requestId": "demo", "errorCode": "InsufficientPrivileges",
                                          "message": "The caller does not have sufficient permissions to perform this "
                                                     "operation."})
            parts = _definition(key)
            if parts is None:
                return FakeResponse(400, {"requestId": "demo", "errorCode": "OperationNotSupportedForItem",
                                          "message": "Operation not supported for requested item"})
            if not self.lro:
                return FakeResponse(200, {"definition": {"parts": parts}})
            op = str(uuid.uuid4())
            self._ops[op] = {"parts": parts, "polls": 0, "needs": self.polls}
            return FakeResponse(202, None, {"Location": f"https://api.fabric.microsoft.com/v1/operations/{op}",
                                            "x-ms-operation-id": op, "Retry-After": "1"})
        return FakeResponse(404, {"errorCode": "NotFound", "message": f"Unknown route {'/'.join(rest)}"})


def demo_client(latency=(0.05, 0.25), lro: bool = True, sleep=None, polls: int = 1):
    """A FabricClient wired to the demo tenant."""
    from .fabric_api import FabricClient
    return FabricClient(DemoSignIn(), http=DemoHttp(latency=latency, lro=lro, polls=polls), sleep=sleep)
