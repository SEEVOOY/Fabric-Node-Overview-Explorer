"""The local web server behind the explorer page.

It listens on 127.0.0.1 only. Every /api call must carry the random session key that is put in
the page address when the app opens it (after the #, so it never leaves the browser), and the
Host header must be this server, so other web pages can't use it.

    GET  /api/hello                        version, account, start workspace
    GET  /api/workspaces                   workspaces you can open
    GET  /api/link?text=...                a pasted ID or address -> workspace (+ item)
    GET  /api/workspace?id=...&refresh=1   the high-level map of a workspace
    POST /api/scan {workspaceId, itemId, refresh}   one item's content (cached)
    POST /api/unload {workspaceId, itemIds}         forget what was read for those items
    POST /api/columns {workspaceId, lakehouseId, schema, table, refresh}   a table's column names
    GET  /api/log?since=n                  recent Fabric / Power BI / OneLake requests
    GET  /api/column-lineage/catalog       the definition categories (letters) for the legend
    POST /api/column-lineage/notebook {workspaceId, notebookId, scope, refresh}
                                           what one notebook does to each column (scope direct | full)
    POST /api/column-lineage/datasets {workspaceId, focus, scope, direction, allColumns, refresh}
                                           the columns that change between datasets
    POST /api/column-lineage/export {workspaceId, refresh}
                                           every column path read, as rows and OpenLineage facets
"""
from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .fabric_api import ApiError
from .scanners import explain

WEB = Path(__file__).parent / "web"
# Content types are fixed here rather than taken from the system: on Windows the registry can map .js
# to text/plain, and browsers won't run a script served like that (or any ES module).
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
                 ".svg": "image/svg+xml; charset=utf-8"}


def _static_files() -> dict:
    """The page's files: index.html, and everything under web/ by its path below /static/."""
    files = {"/": "index.html", "/index.html": "index.html", "/favicon.svg": "favicon.svg"}
    for path in WEB.rglob("*"):
        if path.is_file() and path.suffix in CONTENT_TYPES and "tests" not in path.parts:
            files["/static/" + path.relative_to(WEB).as_posix()] = path.relative_to(WEB).as_posix()
    return files


STATIC = _static_files()
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


class ExplorerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, session):
        super().__init__(address, Handler)
        self.session = session
        self.key = secrets.token_urlsafe(24)
        host, port = self.server_address[:2]
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/#k={self.key}"


class Handler(BaseHTTPRequestHandler):
    server_version = "FabricExplorer"
    sys_version = ""

    def log_message(self, fmt, *args):            # keep the console quiet
        pass

    # ------------------------------------------------------------------ helpers
    def _send(self, status: int, body: bytes, ctype: str, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        return (self.headers.get("Host") or "") in self.server.allowed_hosts

    def _key_ok(self) -> bool:
        return hmac.compare_digest(self.headers.get("X-Session") or "", self.server.key)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 1_000_000:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _guard(self) -> bool:
        if not self._host_ok():
            self._json(403, {"error": "Requests must come from the explorer page itself."})
            return False
        return True

    def _api(self, fn):
        if not self._key_ok():
            self._json(401, {"error": "This page belongs to an earlier run of Fabric Explorer.",
                             "hint": "Open the address the app printed when it started."})
            return
        try:
            self._json(200, fn())
        except ApiError as e:
            self._json(502, {"error": e.message or str(e), "status": e.status, "code": e.code, "hint": explain(e)})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": f"{type(e).__name__}: {e}", "hint": explain(e)})

    # ------------------------------------------------------------------ routes
    def do_GET(self):
        if not self._guard():
            return
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        s = self.server.session
        if url.path in STATIC:
            name = STATIC[url.path]
            path = WEB / name
            if not path.is_file():
                self._json(404, {"error": "not found"})
                return
            ctype = CONTENT_TYPES.get(path.suffix) or mimetypes.guess_type(name)[0] or "application/octet-stream"
            self._send(200, path.read_bytes(), ctype, {"Content-Security-Policy": CSP} if name.endswith(".html")
                       else None)
        elif url.path == "/api/hello":
            self._api(s.hello)
        elif url.path == "/api/workspaces":
            self._api(lambda: {"value": s.workspaces(refresh=q.get("refresh") == "1")})
        elif url.path == "/api/link":
            self._api(lambda: s.resolve_link(q.get("text", "")))
        elif url.path == "/api/workspace":
            def load():
                ws = q.get("id", "")
                if not ws:
                    raise ValueError("Missing workspace id.")
                return s.open_workspace(ws, refresh=q.get("refresh") == "1")
            self._api(load)
        elif url.path == "/api/column-lineage/catalog":
            self._api(s.column_lineage.catalog)
        elif url.path == "/api/log":
            def log():
                entries, last = s.client.log.since(int(q.get("since") or 0))
                return {"entries": entries, "last": last}
            self._api(log)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard():
            return
        url = urlparse(self.path)
        if url.path == "/api/scan":
            body = self._body()
            s = self.server.session
            self._api(lambda: s.scan(str(body.get("workspaceId") or ""), str(body.get("itemId") or ""),
                                     refresh=bool(body.get("refresh"))))
        elif url.path == "/api/unload":
            body = self._body()
            s = self.server.session
            ids = body.get("itemIds") if isinstance(body.get("itemIds"), list) else []
            self._api(lambda: {"unloaded": s.unload(str(body.get("workspaceId") or ""), [str(i) for i in ids])})
        elif url.path == "/api/columns":
            body = self._body()
            s = self.server.session
            self._api(lambda: s.columns(str(body.get("workspaceId") or ""), str(body.get("lakehouseId") or ""),
                                        str(body.get("schema") or ""), str(body.get("table") or ""),
                                        refresh=bool(body.get("refresh"))))
        elif url.path == "/api/column-lineage/notebook":
            body = self._body()
            s = self.server.session
            self._api(lambda: s.column_lineage.notebook(
                str(body.get("workspaceId") or ""), str(body.get("notebookId") or ""),
                scope="full" if body.get("scope") == "full" else "direct", refresh=bool(body.get("refresh"))))
        elif url.path == "/api/column-lineage/export":
            body = self._body()
            s = self.server.session
            self._api(lambda: s.column_lineage.export(str(body.get("workspaceId") or ""),
                                                      refresh=bool(body.get("refresh"))))
        elif url.path == "/api/column-lineage/datasets":
            body = self._body()
            s = self.server.session
            self._api(lambda: s.column_lineage.datasets(
                str(body.get("workspaceId") or ""), focus=str(body.get("focus") or ""),
                scope="direct" if body.get("scope") == "direct" else "full",
                direction=body.get("direction") if body.get("direction") in ("in", "out") else "both",
                all_columns=bool(body.get("allColumns")), refresh=bool(body.get("refresh"))))
        else:
            self._json(404, {"error": "not found"})


def make_server(session, host: str = "127.0.0.1", port: int = 0) -> ExplorerServer:
    return ExplorerServer((host, port), session)


def serve_in_thread(server: ExplorerServer) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t
