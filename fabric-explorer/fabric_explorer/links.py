"""Links in both directions.

item_url(...)   an item -> its page in the Fabric portal ("Open in Fabric")
parse_link(...) a pasted address -> workspace ID (+ the item, when the address points at one)

Any of these work as a workspace reference, in config.py, on the command line or in the app:

    317d67d5-....                                                         (the ID itself)
    https://app.powerbi.com/groups/317d67d5-.../list?experience=fabric-developer
    https://app.fabric.microsoft.com/groups/317d67d5-.../synapsenotebooks/<notebook id>
    https://app.powerbi.com/groups/me/list                                (My workspace)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

from .parse.onelake import is_guid, parse_location

# The URL segment the portal uses for each item type. Types missing here link to the workspace.
SEGMENTS = {
    "Notebook": "synapsenotebooks",
    "Lakehouse": "lakehouses",
    "SemanticModel": "datasets",
    "Report": "reports",
    "DataPipeline": "pipelines",
    "Warehouse": "datawarehouses",
    "SQLEndpoint": "lakewarehouses",
    "SparkJobDefinition": "sparkjobdefinitions",
    "Environment": "sparkenvironments",
    "Dashboard": "dashboards",
    "PaginatedReport": "rdlreports",
    "Datamart": "datamarts",
    "Dataflow": "dataflows-gen2",
    "Eventstream": "eventstreams",
    "Eventhouse": "eventhouses",
    "KQLDatabase": "databases",
    "MLModel": "mlmodels",
    "MLExperiment": "mlexperiments",
    "MirroredDatabase": "mirroreddatabases",
    "SQLDatabase": "sqldatabases",
}
SEGMENT_TYPES = {seg: t for t, seg in SEGMENTS.items()}
SEGMENT_TYPES.update({"semanticmodels": "SemanticModel", "notebooks": "Notebook", "datapipelines": "DataPipeline",
                      "warehouses": "Warehouse", "environments": "Environment", "dataflows": "Dataflow"})

_GROUPS = re.compile(r"/(?:groups|workspaces)/(?P<ws>[^/?#\s]+)(?:/(?P<seg>[^/?#\s]+)(?:/(?P<item>[^/?#\s]+))?)?",
                     re.I)
EXPERIENCE = "fabric-developer"


def _query(base: str, tenant_id: Optional[str]) -> str:
    q = f"experience={EXPERIENCE}"
    if tenant_id and is_guid(tenant_id):
        q += f"&ctid={tenant_id}"
    return f"{base}?{q}"


def workspace_url(portal: str, workspace_id: str, tenant_id: Optional[str] = None) -> str:
    return _query(f"{portal.rstrip('/')}/groups/{quote(workspace_id)}/list", tenant_id)


def item_url(portal: str, workspace_id: str, item_type: str, item_id: str, tenant_id: Optional[str] = None) -> str:
    """The item's page in the portal; the workspace list for types without a known page."""
    seg = SEGMENTS.get(item_type)
    if not seg or not item_id:
        return workspace_url(portal, workspace_id, tenant_id)
    path = f"{portal.rstrip('/')}/groups/{quote(workspace_id)}/{seg}/{quote(item_id)}"
    if item_type == "SemanticModel":
        path += "/details"
    return _query(path, tenant_id)


@dataclass
class LinkRef:
    workspace_id: str = ""     # a GUID, or "me" for My workspace
    item_id: str = ""          # when the address points at an item
    item_type: str = ""        # the item type the address names (synapsenotebooks -> Notebook)
    tenant_id: str = ""        # ctid=... in the address (guest access to another tenant)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.workspace_id) and not self.error


def parse_link(text) -> LinkRef:
    """A workspace reference from a pasted GUID or Fabric / Power BI address."""
    s = str(text or "").strip().strip("\"'<> ")
    if not s:
        return LinkRef(error="Paste a workspace address or ID.")
    if is_guid(s):
        return LinkRef(workspace_id=s.lower())
    tenant = ""
    try:
        ctid = parse_qs(urlparse(s).query).get("ctid", [""])[0]
        tenant = ctid.lower() if is_guid(ctid) else ""
    except ValueError:
        pass
    m = _GROUPS.search(s)
    if m:
        ws = unquote(m.group("ws"))
        if ws.lower() == "me":
            ref = LinkRef(workspace_id="me", tenant_id=tenant)
        elif is_guid(ws):
            ref = LinkRef(workspace_id=ws.lower(), tenant_id=tenant)
        else:
            return LinkRef(error=f"The workspace ID in that address isn't complete ({ws[:60]}). Copy the address "
                                 "from the browser's address bar while the workspace is open.")
        seg, item = (m.group("seg") or "").lower(), m.group("item") or ""
        if seg in SEGMENT_TYPES and is_guid(item):
            ref.item_id, ref.item_type = item.lower(), SEGMENT_TYPES[seg]
        return ref
    loc = parse_location(s)
    if loc is not None and loc.workspace and is_guid(loc.workspace):
        return LinkRef(workspace_id=loc.workspace,
                       item_id=loc.item if loc.item and is_guid(loc.item) else "")
    if "://" in s or "/" in s:
        return LinkRef(error="That address doesn't contain a workspace. Open the workspace in Fabric and copy the "
                             "address; it contains /groups/ followed by the workspace ID.")
    return LinkRef(error="That isn't a workspace ID or address. It should look like "
                         "https://app.fabric.microsoft.com/groups/<workspace ID>/list")
