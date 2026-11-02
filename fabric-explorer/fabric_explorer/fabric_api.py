"""Fabric, Power BI and OneLake REST calls: paging, long-running operations and throttling.

The same method as pims-explorer's FabricClient: requests plus the one shared SignIn. Item
definitions come from getDefinition, which is a long-running operation:
    POST .../getDefinition  -> 200 with the definition, or 202 + x-ms-operation-id
    GET  /operations/{id}   -> poll until Succeeded
    GET  /operations/{id}/result -> the definition; its parts are base64 encoded
Every call is recorded in a small log, shown in the app under Requests.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from collections import deque
from typing import Callable, Optional
from urllib.parse import quote, urlparse

from .auth import FABRIC_SCOPE, STORAGE_SCOPE

FABRIC_API = "https://api.fabric.microsoft.com/v1"
POWERBI_API = "https://api.powerbi.com/v1.0/myorg"
ONELAKE_DFS = "https://onelake.dfs.fabric.microsoft.com"
ONELAKE_TABLES = "https://onelake.table.fabric.microsoft.com/delta"   # Unity Catalog compatible, read-only


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str, code: str = "", url: str = ""):
        self.status, self.code, self.message, self.url = status, code, message, url
        super().__init__(f"{status} {code}: {message}" if status else message)


def _header(headers, name: str) -> str:
    for k, v in (headers or {}).items():
        if k.lower() == name.lower():
            return str(v)
    return ""


def error_from(resp, url: str) -> ApiError:
    code, msg = "", ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            err = body if ("errorCode" in body or "message" in body) else (body.get("error") or {})
            if isinstance(err, dict):
                code = str(err.get("errorCode") or err.get("code") or "")
                msg = str(err.get("message") or "")
            if not msg:
                msg = json.dumps(body)[:300]
    except ValueError:
        msg = (getattr(resp, "text", "") or "")[:300]
    return ApiError(resp.status_code, msg or getattr(resp, "reason", "") or "request failed", code, url)


class RequestLog:
    """The last few hundred API calls, for the Requests panel."""

    def __init__(self, size: int = 400):
        self._items: deque = deque(maxlen=size)
        self._n = 0
        self._lock = threading.Lock()

    def add(self, method: str, url: str, status: int, ms: float, note: str = "") -> None:
        p = urlparse(url)
        host = {"api.fabric.microsoft.com": "Fabric", "api.powerbi.com": "Power BI",
                "onelake.dfs.fabric.microsoft.com": "OneLake",
                "onelake.table.fabric.microsoft.com": "OneLake tables"}.get(p.netloc, p.netloc)
        path = p.path.replace("/v1.0/myorg", "").replace("/v1", "", 1)
        with self._lock:
            self._n += 1
            self._items.append({"n": self._n, "t": time.time(), "method": method, "api": host,
                                "path": path + (f"?{p.query}" if p.query else ""), "status": status,
                                "ms": round(ms), "note": note})

    def since(self, n: int) -> tuple[list, int]:
        with self._lock:
            return [e for e in self._items if e["n"] > n], self._n


class FabricClient:
    def __init__(self, signin, http=None, log: Optional[RequestLog] = None,
                 sleep: Optional[Callable[[float], None]] = None, max_retries: int = 4):
        if http is None:
            import requests
            http = requests.Session()
        self.signin = signin
        self.http = http
        self.log = log or RequestLog()
        self._sleep = sleep
        self.max_retries = max_retries

    def sleep(self, seconds: float) -> None:
        (self._sleep or time.sleep)(seconds)

    def _send(self, method: str, url: str, scope: str, body=None, headers=None, timeout: float = 60):
        for attempt in range(self.max_retries + 1):
            hdrs = {"Authorization": f"Bearer {self.signin.token(scope)}", **(headers or {})}
            t0 = time.perf_counter()
            try:
                if method == "GET":
                    r = self.http.get(url, headers=hdrs, timeout=timeout)
                else:
                    r = self.http.post(url, json=body if body is not None else {}, headers=hdrs, timeout=timeout)
            except Exception as e:  # noqa: BLE001 - network trouble: retry, then report plainly
                self.log.add(method, url, 0, (time.perf_counter() - t0) * 1000, type(e).__name__)
                if attempt < self.max_retries:
                    self.sleep(min(2 ** attempt, 8))
                    continue
                raise ApiError(0, f"Couldn't reach {urlparse(url).netloc}: {e}", "NetworkError", url) from e
            self.log.add(method, url, r.status_code, (time.perf_counter() - t0) * 1000)
            if r.status_code in (429, 503) and attempt < self.max_retries:
                try:
                    wait = float(_header(r.headers, "Retry-After") or 2 ** attempt)
                except ValueError:
                    wait = 2 ** attempt
                self.sleep(min(max(wait, 1.0), 30.0))
                continue
            return r
        return r

    def get_json(self, url: str, scope: str = FABRIC_SCOPE, headers=None) -> dict:
        r = self._send("GET", url, scope, headers=headers)
        if not (200 <= r.status_code < 300):
            raise error_from(r, url)
        return r.json() if r.content else {}

    def get_paged(self, url: str, scope: str = FABRIC_SCOPE, key: str = "value", max_pages: int = 500) -> list:
        """All pages of a list API (continuationUri / continuationToken)."""
        out, next_url, pages = [], url, 0
        while next_url and pages < max_pages:
            data = self.get_json(next_url, scope)
            pages += 1
            out.extend(data.get(key) or [])
            nxt = data.get("continuationUri") or ""
            if not nxt and data.get("continuationToken"):
                sep = "&" if "?" in url else "?"
                nxt = f"{url}{sep}continuationToken={quote(str(data['continuationToken']), safe='')}"
            next_url = nxt
        return out

    def post(self, url: str, body=None, scope: str = FABRIC_SCOPE) -> tuple[int, dict, dict]:
        r = self._send("POST", url, scope, body=body)
        if r.status_code not in (200, 201, 202):
            raise error_from(r, url)
        return r.status_code, dict(r.headers), (r.json() if r.content else {})

    def get_definition(self, url: str, timeout: float = 180) -> dict:
        """POST a getDefinition url, wait for the operation if needed; {part path: bytes}."""
        status, headers, body = self.post(url)
        if status == 202:
            op = _header(headers, "x-ms-operation-id") or _header(headers, "Location").rstrip("/").split("/")[-1]
            if not op:
                raise ApiError(202, "The definition request was accepted but returned no operation to follow.")
            waited, pause = 0.0, 0.6
            while True:
                self.sleep(pause)
                waited += pause
                state = self.get_json(f"{FABRIC_API}/operations/{op}")
                st = str(state.get("status", "")).lower()
                if st == "succeeded":
                    body = self.get_json(f"{FABRIC_API}/operations/{op}/result")
                    break
                if st in ("failed", "undefined", "canceled", "cancelled"):
                    err = state.get("error") or {}
                    raise ApiError(0, str(err.get("message") or f"The definition operation {st}."),
                                   str(err.get("errorCode") or "OperationFailed"), url)
                if waited > timeout:
                    raise ApiError(0, "Timed out waiting for the item definition.", "Timeout", url)
                try:
                    pause = min(float(_header(headers, "Retry-After") or 2), 3.0)
                except ValueError:
                    pause = 2.0
        parts = (body or {}).get("definition", {}).get("parts", [])
        out = {}
        for p in parts:
            try:
                out[str(p.get("path", ""))] = base64.b64decode(p.get("payload") or "")
            except (ValueError, TypeError):
                out[str(p.get("path", ""))] = b""
        return out

    def get_text(self, url: str, scope: str = STORAGE_SCOPE) -> str:
        """A file's content (OneLake), as text."""
        r = self._send("GET", url, scope)
        if not (200 <= r.status_code < 300):
            raise error_from(r, url)
        return r.text if isinstance(getattr(r, "text", None), str) else r.content.decode("utf-8", "replace")

    def onelake_paths(self, workspace_id: str, directory: str, recursive: bool = False) -> list:
        """List a OneLake folder (ADLS Gen2 'List Paths'); used for schema-enabled lakehouses."""
        url = (f"{ONELAKE_DFS}/{workspace_id}?resource=filesystem&recursive={'true' if recursive else 'false'}"
               f"&directory={quote(directory)}")
        data = self.get_json(url, STORAGE_SCOPE, headers={"x-ms-version": "2023-11-03"})
        return data.get("paths") or []
