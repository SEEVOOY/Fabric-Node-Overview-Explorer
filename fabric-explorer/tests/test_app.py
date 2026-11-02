"""Links, the session (against the demo tenant), the local server, sign-in and the command line."""
import http.client
import json
import threading
import time
import types

import pytest

from fabric_explorer import auth
from fabric_explorer.__main__ import _workspace_ref, load_settings, main
from fabric_explorer.auth import SignIn
from fabric_explorer.demo import ID, WS, WS2, demo_client, gid
from fabric_explorer.links import item_url, parse_link
from fabric_explorer.server import make_server, serve_in_thread
from fabric_explorer.session import Session

G = "317d67d5-1a2b-4c3d-8e9f-0123456789ab"
N = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# ---------------------------------------------------------------- pasted links
def test_workspace_addresses():
    assert parse_link(f"https://app.powerbi.com/groups/{G}/list?experience=fabric-developer").workspace_id == G
    ref = parse_link(f' "https://app.fabric.microsoft.com/groups/{G.upper()}/synapsenotebooks/{N}?ctid={N}" ')
    assert (ref.workspace_id, ref.item_type, ref.item_id, ref.tenant_id) == (G, "Notebook", N, N)
    assert parse_link(f"https://app.fabric.microsoft.com/groups/{G}/lakehouses/{N}").item_type == "Lakehouse"
    assert parse_link(f"https://msit.powerbi.com/groups/{G}/datasets/{N}/details").item_type == "SemanticModel"
    assert parse_link("https://app.powerbi.com/groups/me/list").workspace_id == "me"
    assert parse_link(G).workspace_id == G
    assert parse_link(f"abfss://{G}@onelake.dfs.fabric.microsoft.com/{N}/Tables/x").item_id == N
    assert parse_link(f"https://api.fabric.microsoft.com/v1/workspaces/{G}/items").workspace_id == G


def test_bad_addresses_explain_themselves():
    short = parse_link("https://app.powerbi.com/groups/317d67d5-00000000-000000000000/list?experience=fabric-developer")
    assert not short.ok and "isn't complete" in short.error
    assert "doesn't contain a workspace" in parse_link("https://app.powerbi.com/home").error
    assert not parse_link("hello").ok and not parse_link("").ok


def test_portal_links():
    url = item_url("https://app.fabric.microsoft.com", G, "Notebook", N)
    assert url == f"https://app.fabric.microsoft.com/groups/{G}/synapsenotebooks/{N}?experience=fabric-developer"
    assert "/datasets/" in item_url("https://app.powerbi.com", G, "SemanticModel", N, N) and f"ctid={N}" in \
        item_url("https://app.powerbi.com", G, "SemanticModel", N, N)
    assert item_url("https://x", G, "SomethingNew", N).endswith(f"/groups/{G}/list?experience=fabric-developer")


# ---------------------------------------------------------------- session on the demo tenant
@pytest.fixture
def demo():
    client = demo_client(latency=None, sleep=lambda s: None)
    return client, Session(client, default_workspace=WS)


def calls(client, since=0):
    return client.http.calls[since:]


def edges_of(res):
    names = {n["id"]: n.get("fullName") or n["label"] for n in res["nodes"]}
    return {(e["kind"], names[e["source"]], names[e["target"]]) for e in res["edges"]}


def test_opening_a_workspace_reads_only_the_high_level_map(demo):
    client, s = demo
    w = s.open_workspace(WS)
    assert len(w["items"]) == 28 and len(w["folders"]) == 6
    assert w["lakehouses"][ID["lh_reference"]]["schemas"] and not w["lakehouses"][ID["bronze"]]["schemas"]
    urls = [u for _, u in calls(client)]
    assert len(urls) == 5 and not any("getDefinition" in u for u in urls)      # items list came in 2 pages
    assert next(i for i in w["items"] if i["name"] == "wh_reporting")["scannable"]


def test_notebook_scan_with_run_constants_cache_and_rescan(demo):
    client, s = demo
    s.open_workspace(WS)
    res = s.scan(WS, ID["nb20"])
    got = edges_of(res)
    assert ("read", "bronze.cms_tags_raw", "nb_20_bronze_to_silver_tags") in got          # via %run nb_00_config
    assert ("write", "nb_20_bronze_to_silver_tags", "silver.cms_tags_current") in got     # DeltaTable merge
    assert ("default_lakehouse", "nb_20_bronze_to_silver_tags", "silver") in got
    assert res["details"]["includedFrom"] == ["nb_00_config"] and res["alsoScanned"] == [ID["nb00"]]
    n = len(client.http.calls)
    assert s.scan(WS, ID["nb20"]) is res and len(client.http.calls) == n                 # cached
    s.scan(WS, ID["nb20"], refresh=True)
    assert any("getDefinition" in u for _, u in calls(client, n))                         # rescan fetches again


def test_parallel_reads_share_one_fetch_and_wait_for_run_notebooks(demo):
    client, s = demo
    s.open_workspace(WS)
    out = []
    jobs = [ID["nb20"], ID["nb00"], ID["nb20"], ID["nb00"]]
    threads = [threading.Thread(target=lambda i=i: out.append(s.scan(WS, i))) for i in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    defs = [u for m, u in client.http.calls if m == "POST" and "getDefinition" in u]
    assert len(defs) == 2                                                                  # one per notebook
    nb20 = next(r for r in out if r["itemId"] == ID["nb20"])
    assert ("read", "bronze.cms_tags_raw", "nb_20_bronze_to_silver_tags") in edges_of(nb20)


def test_lakehouses_models_reports_pipelines_and_other_workspaces(demo):
    client, s = demo
    s.open_workspace(WS)
    silver = s.scan(WS, ID["silver"])
    assert silver["summary"] == "4 tables, 1 shortcut." and silver["details"]["tablesVia"] == "Lakehouse API"
    assert ("shortcut", "lh_reference.ism.discipline_codes", "silver.ref_disciplines") in edges_of(silver)
    ref = s.scan(WS, ID["lh_reference"])
    assert ref["details"]["tablesVia"] == "OneLake" and ref["summary"] == "3 tables, 1 folder."
    tags = next(t for t in s.scan(WS, ID["model"])["details"]["tables"] if t["table"] == "Tags")
    assert tags["source"].endswith(":fact_pims_tags") and {"name": "PrimKey", "source": "PrimKey", "dataType": "int64",
                                                            "hidden": True, "calculated": False} in tags["columns"]
    model = edges_of(s.scan(WS, ID["model"]))
    assert ("model_source", "silver.cms_tags_history", "PIMS Model") in model             # SQL endpoint -> lakehouse
    assert ("model_source", "gold.fact_pims_tags", "PIMS Model") in model
    assert ("report_model", "PIMS Model", "PIMS Tags") in edges_of(s.scan(WS, ID["rep1"]))
    pl = edges_of(s.scan(WS, ID["pl"]))
    assert ("run", "pl_daily_load", "nb_40_orchestrate") in pl                           # inside a ForEach
    assert ("write", "pl_daily_load", "bronze.cms_documents_tags_raw") in pl
    nb10 = s.scan(WS, ID["nb10"])
    shared = next(n for n in nb10["nodes"] if n["label"] == "shared_master_data")
    assert shared["external"] and shared["workspaceName"] == "Shared Data (prod)" and shared["workspaceId"] == WS2
    assert ("read", "lh_reference.ism.pro_nomenclature", "nb_10_ingest_pims_tags") in edges_of(nb10)


def test_lakehouse_folders_and_table_columns(demo):
    client, s = demo
    s.open_workspace(WS)
    bronze = s.scan(WS, ID["bronze"])
    labels = {n["id"]: n["label"] for n in bronze["nodes"]}
    assert sorted(labels[f] for f in bronze["details"]["folders"]) == ["Files/archive", "Files/raw"]
    assert bronze["summary"] == "4 tables, 2 folders."
    cols = s.columns(WS, ID["silver"], "", "cms_tags_current")              # OneLake table API
    assert cols["via"] == "OneLake table API" and cols["columns"][:2] == [
        {"name": "PrimKey", "type": "bigint", "nullable": True}, {"name": "TagNo", "type": "string", "nullable": True}]
    n = len(client.http.calls)
    assert s.columns(WS, ID["silver"], "", "CMS_TAGS_CURRENT") is cols and len(client.http.calls) == n   # cached
    s.columns(WS, ID["silver"], "", "cms_tags_current", refresh=True)
    assert len(client.http.calls) > n
    kpi = s.columns(WS, ID["gold"], "", "kpi_tags_by_domain")               # only its Delta log knows
    assert kpi["via"] == "Delta log" and [c["name"] for c in kpi["columns"]] == ["Domain", "tags"]
    assert kpi["columns"][1]["type"] == "long"
    ism = s.columns(WS, ID["lh_reference"], "ism", "discipline_codes")      # schema-enabled lakehouse
    assert [c["name"] for c in ism["columns"]] == ["Code", "Discipline"]
    gone = s.columns(WS, ID["bronze"], "", "no_such_table")
    assert gone["columns"] == [] and "wasn't found" in gone["hint"]
    with pytest.raises(ValueError):
        s.columns(WS, "", "", "x")


def test_unload_forgets_what_was_read(demo):
    client, s = demo
    s.open_workspace(WS)
    s.scan(WS, ID["nb70"])
    assert len(s.open_workspace(WS)["scans"]) == 1
    assert s.unload(WS, [ID["nb70"], ID["nb10"]]) == 1                    # nb_10 was never read
    assert s.open_workspace(WS)["scans"] == [] and s.cached_scan(WS, ID["nb70"]) is None
    n = len(client.http.calls)
    s.scan(WS, ID["nb70"])
    assert any("getDefinition" in u for _, u in client.http.calls[n:])      # read fresh from Fabric


def test_errors_and_unsupported_items(demo):
    client, s = demo
    s.open_workspace(WS)
    denied = s.scan(WS, ID["nbx"])
    assert denied["status"] == "error" and "Contributor" in denied["hint"]
    assert s.scan(WS, ID["wh"])["status"] == "unsupported"
    scratch = s.scan(WS, ID["nb99"])
    assert any(n["label"] == "(no default lakehouse)" for n in scratch["nodes"])
    checks = s.scan(WS, ID["nb60"])
    assert [u["shown"] for u in checks["unresolved"]] == ["{lakehouse_name}.{table_name}"]


def test_throttling_retries_and_my_workspace(demo):
    client, s = demo
    client.http.fail["/items"] = [2, 429, "TooManyRequests"]
    assert len(s.open_workspace(WS)["items"]) == 28
    assert s.resolve_link("https://app.powerbi.com/groups/me/list")["workspaceId"] == gid("ws:me")
    assert "error" in s.resolve_link("https://app.powerbi.com/groups/317d67d5-0000/list")


# ---------------------------------------------------------------- local server
@pytest.fixture
def server(demo):
    _, s = demo
    srv = make_server(s, "127.0.0.1", 0)
    serve_in_thread(srv)
    yield srv
    srv.shutdown()
    srv.server_close()


def request(srv, method, path, body=None, key=True, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
    conn.putrequest(method, path, skip_host=True)
    conn.putheader("Host", host or f"127.0.0.1:{srv.server_address[1]}")
    if key:
        conn.putheader("X-Session", srv.key)
    data = json.dumps(body).encode() if body is not None else b""
    if body is not None:
        conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", str(len(data)))
    conn.endheaders(data)
    r = conn.getresponse()
    raw = r.read()
    return r.status, dict(r.getheaders()), raw


def test_server_guards_and_api(server):
    status, headers, page = request(server, "GET", "/", key=False)
    assert status == 200 and b"Fabric Explorer" in page and "script-src 'self'" in headers["Content-Security-Policy"]
    assert request(server, "GET", "/static/app.js", key=False)[0] == 200
    assert request(server, "GET", "/api/hello", key=False)[0] == 401
    assert request(server, "GET", "/api/hello", host="evil.example:80")[0] == 403
    hello = json.loads(request(server, "GET", "/api/hello")[2])
    assert hello["workspaceId"] == WS and hello["demo"] is False
    w = json.loads(request(server, "GET", f"/api/workspace?id={WS}")[2])
    assert len(w["items"]) == 28 and w["scans"] == []
    res = json.loads(request(server, "POST", "/api/scan", {"workspaceId": WS, "itemId": ID["nb30"]})[2])
    assert res["status"] == "ok" and res["summary"].startswith("Reads 4 tables")
    assert len(json.loads(request(server, "GET", f"/api/workspace?id={WS}")[2])["scans"]) == 1
    link = json.loads(request(server, "GET", f"/api/link?text=https://app.powerbi.com/groups/{WS}/list")[2])
    assert link["workspaceId"] == WS
    cols = json.loads(request(server, "POST", "/api/columns", {"workspaceId": WS, "lakehouseId": ID["gold"],
                                                                "table": "dim_documents"})[2])
    assert cols["columns"][0]["name"] == "DocumentID" and cols["via"] == "OneLake table API"
    log = json.loads(request(server, "GET", "/api/log?since=0")[2])
    assert log["last"] >= 6 and log["entries"][0]["api"] == "Fabric"
    assert request(server, "GET", "/api/workspace", key=True)[0] == 400
    gone = json.loads(request(server, "POST", "/api/unload", {"workspaceId": WS, "itemIds": [ID["nb30"]]})[2])
    assert gone == {"unloaded": 1} and json.loads(request(server, "GET", f"/api/workspace?id={WS}")[2])["scans"] == []


# ---------------------------------------------------------------- one sign-in
class FakeCredential:
    def __init__(self):
        self.interactive, self.silent = 0, []

    def authenticate(self, scopes):
        self.interactive += 1
        time.sleep(0.05)
        return types.SimpleNamespace(serialize=lambda: '{"username": "you@contoso.com"}', username="you@contoso.com")

    def get_token(self, scope):
        self.silent.append(scope)
        return types.SimpleNamespace(token=f"token-{scope}", expires_on=time.time() + 3600)


def test_parallel_first_requests_sign_in_once(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "RECORD_FILE", tmp_path / "auth_record.json")
    cred = FakeCredential()
    s = SignIn(credential=cred)
    scopes = [auth.FABRIC_SCOPE, auth.POWERBI_SCOPE, auth.STORAGE_SCOPE] * 3
    threads = [threading.Thread(target=lambda sc=sc: s.token(sc)) for sc in scopes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert cred.interactive == 1 and sorted(cred.silent) == sorted(set(scopes))
    assert s.account() == "you@contoso.com" and (tmp_path / "auth_record.json").exists()
    assert auth.sign_out() is True and auth.sign_out() is False


# ---------------------------------------------------------------- command line
def test_config_from_pims_explorer_and_placeholders(tmp_path):
    old = tmp_path / "config.py"
    old.write_text('import os\nBACKEND = "rest"\nREST = dict(workspace_id="https://app.powerbi.com/groups/'
                   f'{G}/list?experience=fabric-developer", dataset_id="x", tenant_id=None, client_id=None)\n')
    settings = load_settings(str(tmp_path))
    ref, problem = _workspace_ref(settings["REST"]["workspace_id"])
    assert ref.workspace_id == G and not problem
    assert _workspace_ref("00000000-0000-0000-0000-000000000000") == (None, "")
    assert _workspace_ref("0000000000000000") == (None, "")
    assert _workspace_ref("")[0] is None


def test_cli_scan_prints_the_links(capsys, monkeypatch):
    import fabric_explorer.demo as d
    monkeypatch.setattr(d, "demo_client", lambda **kw: demo_client(latency=None, sleep=lambda s: None))
    assert main(["--demo", "--scan", "nb_30_silver_to_gold"]) == 0
    out = capsys.readouterr().out
    assert "nb_30_silver_to_gold (Notebook)" in out and "silver.cms_tags_current" in out and "Cell 1, line 6" in out
