"""A data pipeline definition (pipeline-content.json) -> what it runs, reads and writes.

Activities are walked recursively (ForEach, If, Switch, Until hold their own activities):
    TridentNotebook              typeProperties.notebookId / workspaceId        -> runs a notebook
    ExecutePipeline / InvokePipeline  pipelineId or pipeline.referenceName      -> runs a pipeline
    SparkJobDefinition           sparkJobDefinitionId                           -> runs a Spark job
    RefreshDataflow              dataflowId                                     -> runs a dataflow
    *SemanticModelRefresh        datasetId / semanticModelId                    -> refreshes a model
    Copy (source / sink)         linkedService artifactId + table / folderPath  -> reads / writes
    Lookup, GetMetadata          dataset settings                               -> reads
    Delete                       dataset settings                               -> writes
"""
from __future__ import annotations

from dataclasses import dataclass

from .onelake import is_guid

CONTAINERS = ("activities", "ifTrueActivities", "ifFalseActivities", "defaultActivities")


@dataclass
class PipelineRef:
    op: str                 # run | refresh | read | write
    activity: str           # activity name
    activity_type: str
    item_id: str = ""       # the item the activity points at (notebook, pipeline, lakehouse, ...)
    workspace_id: str = ""
    type_hint: str = ""     # Notebook, DataPipeline, SparkJobDefinition, Dataflow, SemanticModel, Lakehouse...
    item_name: str = ""     # pipelines can name their target instead of giving an ID
    table: str = ""
    schema: str = ""
    path: str = ""          # Files/... for file datasets
    external: str = ""      # a store outside Fabric (connection type and object)
    side: str = ""          # source | sink
    dynamic: bool = False   # an @expression instead of a fixed value


def _val(v):
    """Plain value, or ('', True) for pipeline expressions like {"value": "@pipeline()...", "type": "Expression"}."""
    if isinstance(v, dict):
        inner = v.get("value")
        if isinstance(inner, str):
            return (inner, inner.startswith("@"))
        return ("", False)
    if isinstance(v, str):
        return (v, v.startswith("@"))
    return ("" if v is None else str(v), False)


def walk_activities(acts):
    for a in acts or []:
        if not isinstance(a, dict):
            continue
        yield a
        tp = a.get("typeProperties") or {}
        for key in CONTAINERS:
            yield from walk_activities(tp.get(key))
        for case in tp.get("cases") or []:
            if isinstance(case, dict):
                yield from walk_activities(case.get("activities"))


def _store(node: dict) -> dict:
    """Item / table / path of a Copy source or sink (or any dataset settings)."""
    if not isinstance(node, dict):
        return {}
    ds = node.get("datasetSettings") or node.get("dataset") or {}
    ls = ds.get("linkedService") or node.get("linkedService") or {}
    props = ls.get("properties") if isinstance(ls, dict) else {}
    props = props or {}
    ltp = props.get("typeProperties") or {}
    dtp = ds.get("typeProperties") or {}
    out = {"ltype": str(props.get("type") or ""), "dtype": str(ds.get("type") or node.get("type") or "")}
    for key, val in (("item_id", ltp.get("artifactId") or node.get("lakehouseId") or node.get("artifactId")),
                     ("workspace_id", ltp.get("workspaceId") or node.get("workspaceId")),
                     ("root", ltp.get("rootFolder")),
                     ("table", dtp.get("table") or dtp.get("tableName") or node.get("table")),
                     ("schema", dtp.get("schema")),
                     ("folder", (dtp.get("location") or {}).get("folderPath")),
                     ("file", (dtp.get("location") or {}).get("fileName"))):
        v, dyn = _val(val)
        out[key] = "" if dyn else v          # an @expression is only known when the pipeline runs
        if dyn:
            out["dynamic"] = True
    return out


def _ref_from_store(st: dict, op: str, name: str, typ: str, side: str) -> PipelineRef:
    hint = {"lakehouse": "Lakehouse", "datawarehouse": "Warehouse", "warehouse": "Warehouse"}.get(
        st.get("ltype", "").lower(), "")
    ref = PipelineRef(op, name, typ, item_id=st.get("item_id", "").lower() if is_guid(st.get("item_id")) else "",
                      workspace_id=st.get("workspace_id", "").lower() if is_guid(st.get("workspace_id")) else "",
                      type_hint=hint or ("Lakehouse" if "lakehouse" in st.get("dtype", "").lower() else ""),
                      side=side, dynamic=bool(st.get("dynamic")))
    if st.get("table"):
        ref.table, ref.schema = st["table"], st.get("schema", "")
    elif st.get("folder") or st.get("file") or st.get("root") == "Files":
        path = "/".join(p for p in ("Files", st.get("folder", ""), st.get("file", "")) if p)
        ref.path = path
    if not ref.item_id:
        obj = ".".join(p for p in (st.get("schema"), st.get("table")) if p) or st.get("folder", "")
        ref.external = " ".join(p for p in (st.get("dtype") or st.get("ltype"), obj) if p) or "external store"
    return ref


def analyze_pipeline(content: dict) -> list:
    acts = ((content or {}).get("properties") or content or {}).get("activities") or []
    refs: list = []
    for a in walk_activities(acts):
        name, typ = str(a.get("name") or ""), str(a.get("type") or "")
        tp = a.get("typeProperties") or {}
        low = typ.lower()
        if low == "tridentnotebook":
            nid, dyn = _val(tp.get("notebookId"))
            ws, _ = _val(tp.get("workspaceId"))
            refs.append(PipelineRef("run", name, typ, item_id=nid.lower() if is_guid(nid) else "",
                                    workspace_id=ws.lower() if is_guid(ws) else "", type_hint="Notebook", dynamic=dyn))
        elif low in ("executepipeline", "invokepipeline"):
            pid, dyn = _val(tp.get("pipelineId"))
            ref_name = ((tp.get("pipeline") or {}).get("referenceName") or "") if isinstance(tp.get("pipeline"), dict) else ""
            ws, _ = _val(tp.get("workspaceId"))
            refs.append(PipelineRef("run", name, typ, item_id=pid.lower() if is_guid(pid) else "",
                                    workspace_id=ws.lower() if is_guid(ws) else "", type_hint="DataPipeline",
                                    item_name="" if is_guid(ref_name) else ref_name, dynamic=dyn))
            if is_guid(ref_name) and not refs[-1].item_id:
                refs[-1].item_id = ref_name.lower()
        elif "sparkjob" in low:
            sid, dyn = _val(tp.get("sparkJobDefinitionId") or tp.get("sparkJobDefinition"))
            ws, _ = _val(tp.get("workspaceId"))
            refs.append(PipelineRef("run", name, typ, item_id=sid.lower() if is_guid(sid) else "",
                                    workspace_id=ws.lower() if is_guid(ws) else "", type_hint="SparkJobDefinition",
                                    dynamic=dyn))
        elif low == "refreshdataflow":
            did, dyn = _val(tp.get("dataflowId"))
            ws, _ = _val(tp.get("workspaceId"))
            refs.append(PipelineRef("run", name, typ, item_id=did.lower() if is_guid(did) else "",
                                    workspace_id=ws.lower() if is_guid(ws) else "", type_hint="Dataflow", dynamic=dyn))
        elif "semanticmodel" in low or "datasetrefresh" in low or low == "pbisemanticmodelrefresh":
            mid, dyn = _val(tp.get("datasetId") or tp.get("semanticModelId"))
            ws, _ = _val(tp.get("groupId") or tp.get("workspaceId"))
            refs.append(PipelineRef("refresh", name, typ, item_id=mid.lower() if is_guid(mid) else "",
                                    workspace_id=ws.lower() if is_guid(ws) else "", type_hint="SemanticModel",
                                    dynamic=dyn))
        elif low == "copy":
            for side, op in (("source", "read"), ("sink", "write")):
                st = _store(tp.get(side) or {})
                if st:
                    refs.append(_ref_from_store(st, op, name, typ, side))
        elif low in ("lookup", "getmetadata", "delete"):
            st = _store(tp)
            if st and (st.get("item_id") or st.get("table") or st.get("folder")):
                refs.append(_ref_from_store(st, "write" if low == "delete" else "read", name, typ, ""))
    return refs
