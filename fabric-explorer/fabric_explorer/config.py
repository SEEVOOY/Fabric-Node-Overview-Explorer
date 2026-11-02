"""Settings for Fabric Explorer.

The REST block is the same as in pims-explorer, so you can paste yours unchanged, or start with
--config <path to pims-explorer's config.py>.

workspace_id takes either the workspace ID or the workspace's address copied from the browser:
    https://app.powerbi.com/groups/<workspace ID>/list?experience=fabric-developer
Leave it empty to pick a workspace (or paste an address) in the app instead.
"""
from __future__ import annotations

REST = dict(
    workspace_id="",        # workspace ID, or the workspace address from your browser
    dataset_id=None,        # not used by this tool; kept so pims-explorer's block can be pasted as is
    tenant_id=None,         # only if your account signs in to a different tenant (guest access)
    client_id=None,         # only if your tenant requires its own Entra app for sign-in
)

# Your work email, to pre-fill the Microsoft sign-in page (optional).
LOGIN_HINT = None

# Where "Open in Fabric" links point. "https://app.powerbi.com" works too.
PORTAL_URL = "https://app.fabric.microsoft.com"

# How many items "Scan all" reads at the same time (the Fabric API throttles bursts).
SCAN_CONCURRENCY = 4

# Item types shown on the map as soon as a workspace opens; the other groups start folded.
OPEN_GROUPS = ["Lakehouse", "Notebook"]

# The local page the explorer runs in. If the port is taken, a free one is used instead.
HOST = "127.0.0.1"
PORT = 8765
