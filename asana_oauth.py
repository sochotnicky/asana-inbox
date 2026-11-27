"""Asana OAuth 2 (authorization code + PKCE) for the web UI. No web framework here: just the
URLs, the token endpoint calls and a thread-safe, self-refreshing token holder.

App setup: https://app.asana.com/0/my-apps → create an app, add the redirect URI, and give it
full permissions (or, with --scopes, at least the scopes asked for, e.g. NARROW_SCOPES).
"""

import base64
import hashlib
import json
import os
import secrets
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://app.asana.com/-/oauth_authorize"
TOKEN_URL = "https://app.asana.com/-/oauth_token"
# Default: the app's full permissions (no scope parameter), since message text needs them.
# NARROW_SCOPES is everything else the tool uses, for `--scopes` when messages can go without.
NARROW_SCOPES = ("notifications:read notifications:write tasks:read tasks:write projects:read users:read "
                 "workspaces:read stories:read")
DEFAULT_SCOPES = "full"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "asana-inbox"
CLIENT_FILE = CONFIG / "oauth-client.json"
REFRESH_MARGIN = 120  # seconds before expiry to refresh proactively


class OAuthError(RuntimeError):
    """The token endpoint failed (network, 5xx, unexpected answer)."""


class AuthExpired(OAuthError):
    """The grant is gone (refresh token revoked/expired, or no refresh token): log in again."""


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge)."""
    verifier = secrets.token_urlsafe(64)  # 86 chars, within RFC 7636's 43..128
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str = DEFAULT_SCOPES


def load_client(client_id: str | None, client_secret: str | None,
                path: Path = CLIENT_FILE) -> tuple[str | None, str | None]:
    """Client id/secret from the arguments, else ASANA_CLIENT_ID / ASANA_CLIENT_SECRET, else the
    JSON file {"client_id": …, "client_secret": …}. (None, None) if nothing is configured."""
    client_id = client_id or os.environ.get("ASANA_CLIENT_ID")
    client_secret = client_secret or os.environ.get("ASANA_CLIENT_SECRET")
    if (not client_id or not client_secret) and path.exists():
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            print(f"warning: {path} is readable by others; run chmod 600 on it", file=sys.stderr)
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            raise SystemExit(f"Can't read {path}: {e}") from None
        client_id = client_id or data.get("client_id")
        client_secret = client_secret or data.get("client_secret")
    return client_id, client_secret


class OAuthClient:
    def __init__(self, config: OAuthConfig, http: httpx.Client | None = None):
        self.config = config
        self.http = http or httpx.Client(timeout=30, headers={"User-Agent": "asana-inbox"})

    def authorize_url(self, state: str, code_challenge: str) -> str:
        params = {"client_id": self.config.client_id, "redirect_uri": self.config.redirect_uri,
                  "response_type": "code", "state": state, "code_challenge": code_challenge,
                  "code_challenge_method": "S256"}
        # "full": no scope parameter, so an app registered with full permissions gets them all
        # (needed for endpoints no granular scope covers, such as /messages).
        if self.config.scopes.strip() not in ("", "full"):
            params["scope"] = self.config.scopes
        return AUTHORIZE_URL + "?" + urlencode(params)

    def exchange(self, code: str, code_verifier: str) -> dict:
        return self._token(grant_type="authorization_code", code=code, code_verifier=code_verifier)

    def refresh(self, refresh_token: str) -> dict:
        return self._token(grant_type="refresh_token", refresh_token=refresh_token)

    def _token(self, **form) -> dict:
        form |= {"client_id": self.config.client_id, "client_secret": self.config.client_secret,
                 "redirect_uri": self.config.redirect_uri}
        try:
            r = self.http.post(TOKEN_URL, data=form)
        except httpx.TransportError as e:
            raise OAuthError(f"token request failed: {e}") from e
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code in (400, 401) and body.get("error") in (
                "invalid_grant", "invalid_request", "unauthorized_client", "access_denied"):
            raise AuthExpired(f"{body.get('error')}: {body.get('error_description', '')}".rstrip(": "))
        if r.status_code >= 400 or "access_token" not in body:
            raise OAuthError(f"token endpoint: HTTP {r.status_code}: "
                             f"{body.get('error_description') or body.get('error') or r.text[:200]}")
        return body


class TokenSet:
    """One user's tokens. `access_token()` refreshes shortly before expiry; `refresh_rejected()`
    is the asana_api.Client refresh hook for a 401. Thread-safe: one refresh at a time, and
    threads that hit a 401 with an already-replaced token just retry with the new one."""

    def __init__(self, oauth: OAuthClient, token_response: dict, clock=time.time):
        self.oauth, self.clock = oauth, clock
        self.lock = threading.Lock()
        self.dead = False
        self.refreshes = 0
        self._set(token_response)
        self.user: dict = token_response.get("data") or {}  # Asana includes {gid, name, email}

    def _set(self, body: dict) -> None:
        self.access = body["access_token"]
        self.refresh_token = body.get("refresh_token") or getattr(self, "refresh_token", None)
        self.expires_at = self.clock() + float(body.get("expires_in") or 3600)

    def _refresh(self) -> None:  # with self.lock held
        if self.dead:
            raise AuthExpired("session ended")
        if not self.refresh_token:
            self.dead = True
            raise AuthExpired("no refresh token")
        try:
            body = self.oauth.refresh(self.refresh_token)
        except AuthExpired:
            self.dead = True
            raise
        self.refreshes += 1
        self._set(body)

    def access_token(self) -> str:
        with self.lock:
            if self.dead:
                raise AuthExpired("session ended")
            if self.clock() >= self.expires_at - REFRESH_MARGIN:
                try:
                    self._refresh()
                except AuthExpired:
                    raise
                except OAuthError:
                    if self.clock() >= self.expires_at:
                        raise
                    # Transient failure and the old token still works for a bit: use it.
            return self.access

    def refresh_rejected(self, stale: str) -> None:
        with self.lock:
            if self.access != stale and not self.dead:
                return  # another thread already refreshed
            self._refresh()
