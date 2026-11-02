"""One browser sign-in for every API this tool calls (Fabric, Power BI and OneLake).

The same method as pims-explorer: azure-identity's InteractiveBrowserCredential opens the
Microsoft sign-in page in your browser, you sign in with your own work account, and every token
after that (for any API) is fetched silently from that one sign-in. No Azure CLI, no app secrets
and no pasted bearer tokens.

All token requests go through SignIn.token(scope) under one lock, so parallel scans never open
more than one browser window. The sign-in is remembered between starts where possible
(azure-identity's encrypted token cache plus a record of the account in
~/.fabric-explorer/auth_record.json). `python run.py --sign-out` forgets it.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
STORAGE_SCOPE = "https://storage.azure.com/.default"

RECORD_FILE = Path.home() / ".fabric-explorer" / "auth_record.json"
CACHE_NAME = "fabric_explorer"


class SignIn:
    def __init__(self, tenant_id: Optional[str] = None, client_id: Optional[str] = None,
                 login_hint: Optional[str] = None, remember: bool = True, credential=None):
        self._lock = threading.Lock()
        self._tokens: dict = {}
        self.record = None
        self.remember = remember
        if credential is not None:                      # tests, or a caller's own credential
            self.cred = credential
            return
        from azure.identity import InteractiveBrowserCredential
        kw = {k: v for k, v in dict(tenant_id=tenant_id, client_id=client_id, login_hint=login_hint).items() if v}
        if remember:
            try:
                from azure.identity import AuthenticationRecord, TokenCachePersistenceOptions
                kw["cache_persistence_options"] = TokenCachePersistenceOptions(name=CACHE_NAME)
                if RECORD_FILE.exists():
                    self.record = AuthenticationRecord.deserialize(RECORD_FILE.read_text(encoding="utf-8"))
                    kw["authentication_record"] = self.record
                self.cred = InteractiveBrowserCredential(**kw)
                return
            except Exception:  # noqa: BLE001 - no persistent cache here: sign in once per run
                kw.pop("cache_persistence_options", None)
                kw.pop("authentication_record", None)
                self.record = None
                self.remember = False
        self.cred = InteractiveBrowserCredential(**kw)

    def token(self, scope: str = FABRIC_SCOPE) -> str:
        with self._lock:
            t = self._tokens.get(scope)
            if t is not None and t.expires_on - time.time() > 300:
                return t.token
            if self.record is None and hasattr(self.cred, "authenticate"):
                # the one browser sign-in; every later scope is silent
                self.record = self.cred.authenticate(scopes=[scope])
                self._save_record()
            t = self.cred.get_token(scope)
            self._tokens[scope] = t
            return t.token

    def account(self) -> str:
        """The signed-in account's email, once known."""
        return str(getattr(self.record, "username", "") or "")

    def _save_record(self):
        if not self.remember or self.record is None or not hasattr(self.record, "serialize"):
            return
        try:
            RECORD_FILE.parent.mkdir(parents=True, exist_ok=True)
            RECORD_FILE.write_text(self.record.serialize(), encoding="utf-8")
        except OSError:
            pass


def sign_out() -> bool:
    """Forget the remembered account (the next start signs in again). True if there was one."""
    if RECORD_FILE.exists():
        RECORD_FILE.unlink()
        return True
    return False
