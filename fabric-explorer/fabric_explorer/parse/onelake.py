"""Recognise lakehouse and OneLake locations in strings found in code and item definitions.

    Tables/tags, Files/raw/x.csv, /lakehouse/default/Files/...      -> the default lakehouse
    abfss://<ws>@onelake.dfs.fabric.microsoft.com/<item>/Tables/...  -> a specific item
    https://onelake.dfs.fabric.microsoft.com/<ws>/<item>/Files/...   -> a specific item
    abfss://container@account.dfs.core.windows.net/..., s3://...     -> external storage

<ws> and <item> are either GUIDs or names (an item name carries its type: bronze.Lakehouse).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import unquote

GUID_RE = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_GUID_FULL = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")

_ONELAKE_ABFS = re.compile(
    r"^abfss?://(?P<ws>[^@/]+)@(?P<host>[^/]*onelake[^/]*\.fabric\.microsoft\.com)(?P<path>/[^?#]*)?", re.I)
_ONELAKE_HTTPS = re.compile(
    r"^https?://(?P<host>[^/]*onelake[^/]*\.fabric\.microsoft\.com)/(?P<ws>[^/?#]+)(?P<path>/[^?#]*)?", re.I)
_EXTERNAL = re.compile(r"^(?:abfss?|wasbs?|s3a?|s3n|gs|adl|https?|jdbc:[a-z]+)://", re.I)
_DEFAULT_MOUNT = re.compile(r"^(?:file:)?/lakehouse/default/(?P<path>.*)$", re.I)
_ADLS = re.compile(r"^(?:abfss?|wasbs?)://(?P<container>[^@/]+)@(?P<account>[^./]+)\.[^/]+(?P<path>/[^?#]*)?", re.I)

ITEM_TYPES = {t.lower(): t for t in (
    "Lakehouse", "Warehouse", "KQLDatabase", "MirroredDatabase", "SQLDatabase", "SparkJobDefinition",
    "Notebook", "SemanticModel", "Environment", "MirroredWarehouse", "Datamart", "Eventhouse")}


def is_guid(value) -> bool:
    return bool(value) and bool(_GUID_FULL.match(str(value)))


@dataclass
class Location:
    area: str = ""                      # "Tables", "Files" or "" (item or workspace root)
    rel: list = field(default_factory=list)   # path segments after Tables/ or Files/
    workspace: Optional[str] = None     # GUID or name; None = the code's own workspace
    item: Optional[str] = None          # GUID or name; None = the default lakehouse
    item_type: Optional[str] = None     # "Lakehouse", ... when the item is written as name.Type
    default: bool = False               # relative path: the default lakehouse
    external: str = ""                  # not OneLake: the URL as written
    raw: str = ""

    @property
    def is_onelake(self) -> bool:
        return not self.external


def _split_item(seg: str) -> tuple[str, Optional[str]]:
    if is_guid(seg):
        return seg.lower(), None
    if "." in seg:
        name, suffix = seg.rsplit(".", 1)
        if suffix.lower() in ITEM_TYPES:
            return name, ITEM_TYPES[suffix.lower()]
    return seg, None


def _area(segs: list) -> tuple[str, list]:
    if segs and segs[0].lower() == "tables":
        return "Tables", segs[1:]
    if segs and segs[0].lower() == "files":
        return "Files", segs[1:]
    return "", segs


def parse_location(text) -> Optional[Location]:
    """A Location for strings that point into a lakehouse or other storage; None for anything else."""
    if not isinstance(text, str):
        return None
    s = text.strip().strip("\"'")
    if not s or "\n" in s or len(s) > 2000:
        return None
    m = _ONELAKE_ABFS.match(s) or _ONELAKE_HTTPS.match(s)
    if m:
        ws = unquote(m.group("ws"))
        segs = [unquote(p) for p in (m.group("path") or "").split("/") if p]
        if not segs:
            return Location(workspace=ws.lower() if is_guid(ws) else ws, raw=s)
        item, item_type = _split_item(segs[0])
        area, rel = _area(segs[1:])
        return Location(area=area, rel=rel, workspace=ws.lower() if is_guid(ws) else ws,
                        item=item, item_type=item_type, raw=s)
    if _EXTERNAL.match(s):
        return Location(external=s, raw=s)
    m = _DEFAULT_MOUNT.match(s)
    if m:
        area, rel = _area([p for p in m.group("path").split("/") if p])
        return Location(area=area, rel=rel, default=True, raw=s) if area else None
    segs = [p for p in s.replace("\\", "/").split("/") if p and p != "."]
    if segs and segs[0].lower() in ("tables", "files"):
        area, rel = _area(segs)
        return Location(area=area, rel=rel, default=True, raw=s)
    return None


def table_from_rel(rel: list, schemas_enabled: Optional[bool]) -> tuple[Optional[str], Optional[str]]:
    """(schema, table) from the segments after Tables/.

    Schema-enabled lakehouses store tables as Tables/<schema>/<table>; others as Tables/<table>.
    When it isn't known which kind the lakehouse is, a second segment that looks like a table
    folder (not _delta_log, not a file) is read as the table.
    """
    segs = [s for s in rel if s]
    if not segs:
        return None, None
    if schemas_enabled is True:
        return (segs[0], segs[1]) if len(segs) > 1 else (segs[0], None)
    if schemas_enabled is False or len(segs) == 1:
        return None, segs[0]
    second = segs[1]
    if second.startswith("_") or "." in second or "*" in second or "=" in second:
        return None, segs[0]
    return segs[0], second


def files_folder(rel: list) -> str:
    """The folder a Files/ reference points at: 'Files/raw/x/*.csv' and 'Files/raw/x/a.csv' -> 'Files/raw/x'."""
    segs = [s for s in rel if s]
    if segs and any(ch in segs[-1] for ch in ".*?{["):
        segs = segs[:-1]
    return "/".join(["Files", *segs])


def external_label(url: str) -> str:
    """A short name for storage outside OneLake."""
    m = _ADLS.match(url)
    if m:
        path = (m.group("path") or "").strip("/")
        return f"{m.group('account')}/{m.group('container')}" + (f"/{path}" if path else "")
    s = re.sub(r"^[a-z0-9:]+//", "", url, flags=re.I)
    return s[:80]
