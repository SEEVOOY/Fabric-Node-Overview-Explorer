"""python run.py [workspace address or ID] [options]

    python run.py                                   the workspace in config.py (or pick one in the app)
    python run.py "https://app.powerbi.com/groups/<id>/list?experience=fabric-developer"
    python run.py --config ..\\pims-explorer\\pims_explorer\\config.py
    python run.py --demo                            a built-in demo workspace, no sign-in
    python run.py --scan nb_20_bronze_to_silver_tags   print one item's reads and writes, then exit
    python run.py --sign-out                        forget the remembered sign-in
"""
from __future__ import annotations

import argparse
import errno
import importlib.util
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .links import parse_link

PLACEHOLDER = "00000000-0000-0000-0000-000000000000"


def load_settings(path: str | None = None) -> dict:
    """config.py settings; with path, the REST block (and other known settings) from another config.py."""
    from . import config as cfg
    settings = {k: getattr(cfg, k) for k in dir(cfg) if k.isupper()}
    settings["REST"] = dict(cfg.REST)
    if not path:
        return settings
    p = Path(path).expanduser()
    if p.is_dir():
        p = next((c for c in (p / "config.py", p / "pims_explorer" / "config.py") if c.is_file()), p / "config.py")
    if not p.is_file():
        raise SystemExit(f"No config file at {p}")
    spec = importlib.util.spec_from_file_location("_fabric_explorer_external_config", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rest = getattr(mod, "REST", None)
    if isinstance(rest, dict):
        for k in ("workspace_id", "tenant_id", "client_id"):
            if rest.get(k):
                settings["REST"][k] = rest[k]
    for k in ("LOGIN_HINT", "PORTAL_URL", "SCAN_CONCURRENCY", "OPEN_GROUPS", "HOST", "PORT"):
        if hasattr(mod, k):
            settings[k] = getattr(mod, k)
    return settings


def _workspace_ref(text: str):
    """(LinkRef or None, problem): None when nothing usable is configured."""
    text = str(text or "").strip()
    if not text or set(text) <= set("0-"):
        return None, ""
    ref = parse_link(text)
    if not ref.ok:
        return None, ref.error
    if ref.workspace_id == PLACEHOLDER:
        return None, ""
    return ref, ""


def _cli_scan(session, ws_id: str, what: str) -> int:
    data = session.open_workspace(ws_id)
    items = data["items"]
    want = what.strip().lower()
    hits = [i for i in items if i["id"] == want] or [i for i in items if i["name"].lower() == want] or \
        [i for i in items if want in i["name"].lower()]
    if not hits:
        print(f"No item matching {what!r} in {data['workspace']['name']}.")
        return 1
    it = hits[0]
    res = session.scan(ws_id, it["id"])
    nodes = {n["id"]: n for n in res["nodes"]}

    def name(i):
        n = nodes.get(i, {})
        return n.get("fullName") or n.get("label") or i

    print(f"{it['name']} ({it['type']})")
    print(f"  {res.get('summary') or ''}")
    if res.get("error"):
        print(f"  Couldn't read it: {res['error']}")
        if res.get("hint"):
            print(f"  {res['hint']}")
    for e in res["edges"]:
        ev = e["evidence"][0] if e["evidence"] else {}
        print(f"  {e['kind']:<17} {name(e['source'])}  ->  {name(e['target'])}   [{ev.get('where', '')}]")
    for u in res["unresolved"]:
        print(f"  not resolved      {u.get('shown')}   [{u.get('where')}]  {u.get('code', '')}")
    print(f"  {it['url']}")
    return 0


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog="python run.py", description="Map a Fabric workspace's lakehouses, notebooks "
                                 "and other items as a graph you can explore. Version " + __version__)
    ap.add_argument("workspace", nargs="?", help="the workspace address from your browser, or the workspace ID "
                    "(default: REST['workspace_id'] in config.py)")
    ap.add_argument("--config", metavar="FILE", help="read the REST block from another config.py, e.g. pims-explorer's")
    ap.add_argument("--demo", action="store_true", help="explore a built-in demo workspace, without signing in")
    ap.add_argument("--port", type=int, help="port for the local page (default from config.py)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    ap.add_argument("--scan", metavar="ITEM", help="print what one item (name or ID) reads, writes and runs, then exit")
    ap.add_argument("--sign-out", action="store_true", help="forget the remembered sign-in, then exit")
    args = ap.parse_args(argv)

    if args.sign_out:
        from .auth import sign_out
        print("Signed out. The next start opens the Microsoft sign-in page." if sign_out()
              else "There was no remembered sign-in.")
        return 0

    settings = load_settings(args.config)
    rest = settings["REST"]
    ref, problem = _workspace_ref(args.workspace or rest.get("workspace_id") or "")
    if problem:
        if args.workspace:
            print(problem)
            return 2
        print(f"config.py: {problem}\nYou can pick the workspace in the app instead.\n")
    tenant = rest.get("tenant_id") or (ref.tenant_id if ref else None)

    from .session import Session
    common = dict(portal=settings.get("PORTAL_URL", "https://app.fabric.microsoft.com"), tenant_id=tenant,
                  concurrency=int(settings.get("SCAN_CONCURRENCY", 4) or 4),
                  open_groups=settings.get("OPEN_GROUPS", ["Lakehouse", "Notebook"]),
                  start_item=ref.item_id if ref else "")
    if args.demo:
        from .demo import WS, demo_client
        session = Session(demo_client(), default_workspace=(ref.workspace_id if ref else WS), demo=True,
                          account="Demo workspace (no sign-in)", **common)
    else:
        from .auth import FABRIC_SCOPE, SignIn
        from .fabric_api import FabricClient
        signin = SignIn(tenant, rest.get("client_id"), settings.get("LOGIN_HINT"))
        print("Signing in with your Microsoft account. If you aren't signed in yet, a browser window opens.")
        try:
            signin.token(FABRIC_SCOPE)
        except KeyboardInterrupt:
            print("Sign-in cancelled.")
            return 1
        except Exception as e:  # noqa: BLE001 - show the reason plainly and stop
            print(f"Sign-in didn't complete: {e}")
            return 1
        print(f"Signed in as {signin.account() or 'your Microsoft account'}.")
        session = Session(FabricClient(signin), default_workspace=ref.workspace_id if ref else "",
                          account=signin.account(), **common)
    if session.default_workspace == "me":
        res = session.resolve_link("https://app.powerbi.com/groups/me/list")
        session.default_workspace = res.get("workspaceId", "")

    if args.scan:
        if not session.default_workspace:
            print("Give the workspace too: python run.py <workspace address> --scan <item>")
            return 2
        return _cli_scan(session, session.default_workspace, args.scan)

    from .server import make_server
    host = settings.get("HOST", "127.0.0.1")
    port = args.port if args.port is not None else int(settings.get("PORT", 0) or 0)
    try:
        server = make_server(session, host, port)
    except OSError as e:
        if port and e.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1), 10048, 10013):
            server = make_server(session, host, 0)
        else:
            raise
    print(f"\nFabric Explorer is running at\n  {server.url}\n\nKeep this window open while you explore. "
          "Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
