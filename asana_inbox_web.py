#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["starlette>=0.37", "uvicorn>=0.30", "httpx>=0.27"]
# ///
"""Bulk-archive Asana Inbox items from a local web UI (same features as ./asana_inbox.py).

    ./asana_inbox_web.py                 # personal access token (ASANA_TOKEN or ~/.config/asana-inbox/token)
    ./asana_inbox_web.py --fake          # generated demo data, no Asana needed
    ASANA_CLIENT_ID=… ASANA_CLIENT_SECRET=… ./asana_inbox_web.py   # "Log in with Asana" (OAuth)

Modes: --fake; OAuth when a client id and secret are configured (--client-id/--client-secret,
ASANA_CLIENT_ID/ASANA_CLIENT_SECRET or ~/.config/asana-inbox/oauth-client.json), with one inbox
per browser session; otherwise the personal access token, shared by every tab. Tokens stay in
this process; the browser only gets an HttpOnly session cookie.

HTTP API (JSON; POSTs need Content-Type: application/json; 401 {"error", "login"} when OAuth
is on and the browser isn't logged in):
    GET  /api/config            {mode, fake, user, logged_in, logo_url, favicon_url, title_width,
                                project_width}
    GET  /api/items             snapshot {type: "items", generation, items, status}; loads on first use
    GET  /api/events            SSE: a snapshot first, then "details" (updated items), "status",
                                "error", "auth" (logged out) and new snapshots (after reload / archive)
    POST /api/reload            reload from Asana; returns the new snapshot
    POST /api/archive {ids}     archive (mark read) these items; returns {archived, items}
    POST /api/followup {id, text?, notes?, due?, archive?}
                                create a task assigned to me (text: its title, default "Follow up
                                on <item title>"; notes: its description, default the item's link; due:
                                YYYY-MM-DD or today/tomorrow/+N/weekday, default today), then
                                archive the item unless archive is false; returns {url, due, archived}
    POST /api/react {id, story, emoji, reacted}
                                add (reacted: true) or remove my emoji reaction on one of the
                                item's comments; returns that comment's {reactions}
    GET  /auth/login            OAuth: redirect to Asana (PKCE)
    GET  /auth/callback         OAuth: Asana redirects back here with code + state
    POST /auth/logout           end this browser's session
"""

import argparse
import asyncio
import html
import json
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Callable
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import (  # noqa: E402
    HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse)
from starlette.routing import Route  # noqa: E402

from asana_oauth import (  # noqa: E402
    DEFAULT_SCOPES, NARROW_SCOPES, AuthExpired, OAuthClient, OAuthConfig, OAuthError, TokenSet, load_client, pkce_pair)
from inbox_core import (  # noqa: E402
    PROJECT_WIDTH, TITLE_WIDTH, ApiBackend, Backend, FakeBackend, InboxItem, followup_name, parse_due,
    with_reaction)



def image_url(url: str | None, what: str) -> str | None:
    """A logo/favicon URL from the command line or env: https://, http:// or data:image/…"""
    if not url:
        return None
    if not re.match(r"(https?://[^/\s]+|data:image/[\w.+-]+[;,])", url):
        raise SystemExit(f"{what} must be an http(s):// or data:image/ URL, not {url!r}")
    return url


def img_src_override(value: str | None) -> str | None:
    """--img-src / ASANA_INBOX_IMG_SRC: CSP sources for images, used as is instead of the ones
    derived from the logo/favicon URLs. Only the img-src value: no ';' (no other directives)."""
    if not value or not value.strip():
        return None
    if re.search(r"[;,\r\n]", value):
        raise SystemExit("--img-src / ASANA_INBOX_IMG_SRC takes space-separated CSP sources only "
                         f"(e.g. \"'self' https://cdn.example.com\"), not {value!r}")
    return " ".join(value.split())


def image_sources(*urls: str | None) -> str:
    """CSP img-src for the page: itself plus exactly the origins of the configured images."""
    sources = ["'self'"]
    for url in urls:
        if not url:
            continue
        src = "data:" if url.startswith("data:") else "{0.scheme}://{0.netloc}".format(urlsplit(url))
        if src not in sources:
            sources.append(src)
    return " ".join(sources)

# Set when the server starts shutting down (Ctrl-C, docker stop): event streams then end at once,
# instead of holding up the graceful shutdown until they're cancelled.
STOPPING = threading.Event()


class Server(uvicorn.Server):
    def handle_exit(self, sig, frame) -> None:
        STOPPING.set()
        super().handle_exit(sig, frame)


SESSION_COOKIE = "asana_inbox_session"
STATE_COOKIE = "asana_inbox_oauth_state"
STATE_TTL = 600  # seconds a login attempt may take
SESSION_IDLE = 12 * 3600  # sessions unused this long are dropped


def item_json(it: InboxItem) -> dict:
    """InboxItem -> JSON: datetimes as epoch seconds (the browser formats them in local time)."""
    def ts(d: datetime | None) -> float | None:
        return d.timestamp() if d else None

    return {
        "id": it.id, "title": it.title, "project": it.project, "projects": it.projects,
        "kind": it.kind, "updated": ts(it.updated), "completed": ts(it.completed), "url": it.url,
        "assignee": it.assignee, "mine": it.mine, "due": ts(it.due), "due_has_time": it.due_has_time,
        "description": it.description, "description_rich": it.description_rich,
        "people": it.people, "events": it.events, "pending": it.pending,
    }


class InboxService:
    """One inbox's state for all its browser tabs, over any inbox_core backend. Thread-based like
    the TUI: list/archive run in a thread pool, `fill` in a thread of its own; each change goes
    to every subscriber (an asyncio queue per open SSE stream)."""

    def __init__(self, backend: Backend):
        self.backend = backend
        self.items: dict[str, InboxItem] = {}  # in listing order
        self.generation = 0  # bumped per load; detail updates from older loads are dropped
        self.loaded = False
        self.status = ""
        self.lock = threading.Lock()  # items / generation / subscribers
        self.load_lock = threading.Lock()  # one list_items at a time
        self.subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()

    # -- broadcasting (call with self.lock held)

    def _broadcast(self, msg: dict) -> None:
        for loop, q in list(self.subscribers):
            loop.call_soon_threadsafe(q.put_nowait, msg)

    def _snapshot(self) -> dict:
        return {"type": "items", "generation": self.generation, "status": self.status,
                "items": [item_json(it) for it in self.items.values()]}

    def _set_status(self, text: str) -> None:
        with self.lock:
            self.status = text
            self._broadcast({"type": "status", "text": text})

    def _error(self, e: Exception, what: str) -> None:
        with self.lock:
            self._broadcast({"type": "auth"} if isinstance(e, AuthExpired)
                            else {"type": "error", "text": f"{what} failed: {e}"})

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot()

    def subscribe(self, loop, q) -> dict:
        """Register an SSE stream; returns the snapshot it starts from (atomically, so no update
        falls between the two)."""
        with self.lock:
            self.subscribers.add((loop, q))
            return self._snapshot()

    def unsubscribe(self, loop, q) -> None:
        with self.lock:
            self.subscribers.discard((loop, q))

    def close(self) -> None:
        """Session ended: stop a running fill and tell open tabs."""
        with self.lock:
            self.generation += 1
            self._broadcast({"type": "auth"})

    # -- operations (blocking; run them in a thread)

    def ensure_loaded(self) -> None:
        if not self.loaded:
            self.reload(only_if_unloaded=True)

    def reload(self, only_if_unloaded: bool = False) -> None:
        with self.load_lock:
            if only_if_unloaded and self.loaded:
                return
            with self.lock:
                self.generation += 1  # stops a running fill
            self._set_status("loading…")
            try:
                items = self.backend.list_items(self._set_status)
            except Exception as e:
                self.loaded = True  # don't retry on every request; the user can press r
                with self.lock:
                    self.status = ""
                    self._broadcast(self._snapshot())
                self._error(e, "Load")
                raise
            with self.lock:
                self.generation += 1
                gen = self.generation
                self.items = {it.id: it for it in items}
                self.loaded = True
                self.status = ""
                self._broadcast(self._snapshot())
            if hasattr(self.backend, "fill") and any(it.pending for it in items):
                threading.Thread(target=self._fill, args=(gen, list(items)), daemon=True,
                                 name="fill").start()

    def _fill(self, gen: int, items: list[InboxItem]) -> None:
        try:
            with closing(self.backend.fill(items)) as updates_iter:
                for updates in updates_iter:
                    with self.lock:
                        if gen != self.generation:
                            return  # reloaded meanwhile; closing the generator stops the fetches
                        changed = []
                        for iid, fields in updates.items():
                            if (it := self.items.get(iid)) is not None:  # not archived meanwhile
                                for key, value in fields.items():
                                    setattr(it, key, value)
                                changed.append(item_json(it))
                        if changed:
                            self._broadcast({"type": "details", "generation": gen, "items": changed})
        except Exception as e:
            if gen == self.generation:
                self._error(e, "Loading details")

    def followup(self, item_id: str, text: str | None, due: date, archive: bool,
                 notes: str | None = None) -> tuple[str, bool]:
        """Create a follow-up task for an item (see inbox_core.Backend): title `text` (default
        "Follow up on <title>"), description `notes` (None: the item's link; "": none). Returns
        (its link, whether the item was archived). Raises KeyError for an unknown item."""
        if not hasattr(self.backend, "create_task"):
            raise RuntimeError("this backend can't create tasks")
        with self.lock:
            it = self.items[item_id]
            text = (text or "").strip() or followup_name(it)
            notes = it.url if notes is None else notes.strip()
        url = self.backend.create_task(text, due, notes)
        return url, bool(archive and self.archive([item_id]))

    def react(self, item_id: str, story: str, emoji: str, reacted: bool) -> list[dict]:
        """Add or remove my `emoji` reaction on a comment of an item; returns the comment's
        reactions. Raises KeyError for an unknown item or comment."""
        if not hasattr(self.backend, "react"):
            raise RuntimeError("this backend can't react")
        with self.lock:
            event = next((e for e in self.items[item_id].events if e.get("story") == story), None)
        if event is None:
            raise KeyError(story)
        self.backend.react(story, emoji, reacted)
        with self.lock:
            event["reactions"] = with_reaction(event.get("reactions") or [], emoji, reacted)
            if (it := self.items.get(item_id)) is not None:
                self._broadcast({"type": "details", "generation": self.generation, "items": [item_json(it)]})
            return event["reactions"]

    def archive(self, ids: list[str]) -> int:
        with self.lock:
            ids = [i for i in ids if i in self.items]
        if ids:
            self.backend.archive(ids)  # raises on failure: nothing is dropped then
        with self.lock:
            for i in ids:
                self.items.pop(i, None)
            self._broadcast(self._snapshot())
        return len(ids)


# ---------------------------------------------------------------- OAuth sessions


class Session:
    """One logged-in browser: its tokens and (created on first use) its own inbox."""

    def __init__(self, sid: str, tokens: TokenSet, make_backend: Callable[[TokenSet], Backend]):
        self.id, self.tokens, self.make_backend = sid, tokens, make_backend
        self.user: str = tokens.user.get("name") or ""
        self.service: InboxService | None = None
        self.lock = threading.Lock()
        self.last_seen = time.time()

    def get_service(self) -> InboxService:  # blocking: the first call talks to Asana
        with self.lock:
            if self.service is None:
                backend = self.make_backend(self.tokens)
                self.service = InboxService(backend)
                if not self.user:
                    self.user = getattr(getattr(backend, "api", None), "me_name", "")
            return self.service


class SessionStore:
    """In memory: sessions end when the server stops."""

    def __init__(self):
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()

    def create(self, tokens: TokenSet, make_backend) -> Session:
        s = Session(secrets.token_urlsafe(32), tokens, make_backend)
        with self.lock:
            self.sessions[s.id] = s
        return s

    def get(self, sid: str | None) -> Session | None:
        now = time.time()
        with self.lock:
            idle = [k for k, s in self.sessions.items() if now - s.last_seen > SESSION_IDLE]
        for k in idle:
            self.drop(k)
        with self.lock:
            s = self.sessions.get(sid or "")
            if s is not None:
                s.last_seen = now
            return s

    def drop(self, sid: str | None) -> None:
        with self.lock:
            s = self.sessions.pop(sid or "", None)
        if s is not None:
            s.tokens.dead = True
            if s.service is not None:
                s.service.close()


# ---------------------------------------------------------------- HTTP


def create_app(*, mode: str, allowed_hosts: set[str], service: InboxService | None = None,
               user: str = "", oauth: OAuthClient | None = None,
               make_backend: Callable[[TokenSet], Backend] | None = None,
               logo_url: str | None = None, favicon_url: str | None = None,
               img_src: str | None = None) -> Starlette:
    """mode "fake" / "token": one shared `service`. mode "oauth": a session per browser, each
    with an inbox from `make_backend(tokens)`; `oauth` does the Asana side. `logo_url` /
    `favicon_url`: optional images for the header and the browser tab (see image_url);
    `img_src`: the CSP img-src sources (default: 'self' plus those images' origins)."""
    csp = ("default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
           f"connect-src 'self'; img-src {img_src or image_sources(logo_url, favicon_url)}")
    store = SessionStore()
    pending: dict[str, tuple[str, float]] = {}  # OAuth state -> (PKCE verifier, created)
    pending_lock = threading.Lock()
    secure = bool(oauth and oauth.config.redirect_uri.startswith("https:"))
    redirect = urlsplit(oauth.config.redirect_uri) if oauth else None

    def guard(request: Request) -> Response | None:
        # Only same-machine pages: a Host check stops DNS rebinding, and requiring a JSON body
        # on POST forces a CORS preflight (which we never answer) for cross-site requests.
        # GETs (including the OAuth callback, protected by its state) skip the JSON check.
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in allowed_hosts:
            return JSONResponse({"error": "bad host"}, status_code=403)
        if request.method == "POST" and not request.headers.get("content-type", "").startswith(
                "application/json"):
            return JSONResponse({"error": "JSON body required"}, status_code=415)
        return None

    def unauthorized() -> Response:
        return JSONResponse({"error": "not logged in", "login": "/auth/login"}, status_code=401)

    def session_of(request: Request) -> Session | None:
        s = store.get(request.cookies.get(SESSION_COOKIE))
        if s is not None and s.tokens.dead:
            store.drop(s.id)
            return None
        return s

    async def service_for(request: Request) -> tuple[InboxService | None, Response | None]:
        if mode != "oauth":
            return service, None
        s = session_of(request)
        if s is None:
            return None, unauthorized()
        try:
            return await run_in_threadpool(s.get_service), None
        except AuthExpired:
            store.drop(s.id)
            return None, unauthorized()
        except Exception as e:
            return None, JSONResponse({"error": f"Couldn't open your inbox: {e}"}, status_code=502)

    def failed(request: Request, e: Exception, what: str) -> Response:
        if isinstance(e, AuthExpired):
            store.drop(request.cookies.get(SESSION_COOKIE))
            return unauthorized()
        return JSONResponse({"error": f"{what} failed: {e}"}, status_code=502)

    def page(title: str, message: str, status: int) -> HTMLResponse:
        body = (f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
                "<body style='font:15px system-ui,sans-serif;max-width:560px;margin:15vh auto;"
                "padding:0 16px'>"
                f"<h1 style='font-size:20px'>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
                "<p><a href='/auth/login'>Try again</a> · <a href='/'>Back to the inbox</a></p>")
        return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    # -- page

    async def index(request: Request) -> Response:
        return guard(request) or HTMLResponse(PAGE, headers={
            "Content-Security-Policy": csp, "Cache-Control": "no-store"})

    async def config(request: Request) -> Response:
        if bad := guard(request):
            return bad
        s = session_of(request) if mode == "oauth" else None
        return JSONResponse({
            "mode": mode, "fake": mode == "fake",
            "logged_in": mode != "oauth" or s is not None,
            "user": (s.user if s else user) or None,
            "logo_url": logo_url, "favicon_url": favicon_url,
            "title_width": TITLE_WIDTH, "project_width": PROJECT_WIDTH})

    # -- inbox API

    async def items(request: Request) -> Response:
        if bad := guard(request):
            return bad
        svc, err = await service_for(request)
        if err:
            return err
        try:
            await run_in_threadpool(svc.ensure_loaded)
        except Exception as e:
            return failed(request, e, "Load")
        return JSONResponse(svc.snapshot())

    async def reload(request: Request) -> Response:
        if bad := guard(request):
            return bad
        svc, err = await service_for(request)
        if err:
            return err
        try:
            await run_in_threadpool(svc.reload)
        except Exception as e:
            return failed(request, e, "Load")
        return JSONResponse(svc.snapshot())

    async def archive(request: Request) -> Response:
        if bad := guard(request):
            return bad
        svc, err = await service_for(request)
        if err:
            return err
        try:
            ids = (await request.json())["ids"]
            assert isinstance(ids, list) and all(isinstance(i, str) for i in ids)
        except (ValueError, KeyError, TypeError, AssertionError):
            return JSONResponse({"error": 'expected {"ids": ["…", …]}'}, status_code=400)
        try:
            n = await run_in_threadpool(svc.archive, ids)
        except Exception as e:
            return failed(request, e, "Archive")
        return JSONResponse({"archived": n, "items": len(svc.items)})

    async def followup(request: Request) -> Response:
        if bad := guard(request):
            return bad
        svc, err = await service_for(request)
        if err:
            return err
        try:
            body = await request.json()
            item_id, text, archive = body["id"], body.get("text"), body.get("archive", True)
            notes = body.get("notes")
            assert isinstance(item_id, str) and isinstance(text, str | None) and isinstance(archive, bool)
            assert isinstance(notes, str | None)
            due = parse_due(body.get("due") or "today")
        except ValueError as e:  # includes a due date parse_due can't read
            return JSONResponse({"error": str(e) or "bad JSON"}, status_code=400)
        except (KeyError, TypeError, AssertionError):
            return JSONResponse({"error": 'expected {"id": "…", "text"?: "…", "due"?: "YYYY-MM-DD", '
                                          '"archive"?: true}'}, status_code=400)
        try:
            url, archived = await run_in_threadpool(svc.followup, item_id, text, due, archive, notes)
        except KeyError:
            return JSONResponse({"error": "no such item (reload?)"}, status_code=404)
        except Exception as e:
            return failed(request, e, "Creating the follow-up")
        return JSONResponse({"url": url, "due": due.isoformat(), "archived": archived})

    async def react(request: Request) -> Response:
        if bad := guard(request):
            return bad
        svc, err = await service_for(request)
        if err:
            return err
        try:
            body = await request.json()
            item_id, story, emoji, reacted = body["id"], body["story"], body["emoji"], body["reacted"]
            assert isinstance(item_id, str) and isinstance(story, str) and isinstance(reacted, bool)
            assert isinstance(emoji, str) and 0 < len(emoji) <= 16
        except (ValueError, KeyError, TypeError, AssertionError):
            return JSONResponse({"error": 'expected {"id": "…", "story": "…", "emoji": "👍", "reacted": true}'},
                                status_code=400)
        try:
            reactions = await run_in_threadpool(svc.react, item_id, story, emoji, reacted)
        except KeyError:
            return JSONResponse({"error": "no such comment (reload?)"}, status_code=404)
        except Exception as e:
            return failed(request, e, "Reacting")
        return JSONResponse({"reactions": reactions})

    async def events(request: Request) -> Response:
        if bad := guard(request):
            return bad
        svc, err = await service_for(request)
        if err:
            return err
        loop, q = asyncio.get_running_loop(), asyncio.Queue()
        first = svc.subscribe(loop, q)
        if not svc.loaded:  # first visit: load in the background; the snapshot follows
            threading.Thread(target=lambda: _quiet(svc.ensure_loaded), daemon=True).start()

        async def stream():
            try:
                yield sse(first)
                quiet = 0.0
                while not STOPPING.is_set():  # checked twice a second
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=0.5)
                    except TimeoutError:
                        quiet += 0.5
                        if quiet >= 15:
                            quiet = 0.0
                            yield ": keepalive\n\n"
                        continue
                    quiet = 0.0
                    yield sse(msg)
                    if msg["type"] == "auth":
                        return
            finally:
                svc.unsubscribe(loop, q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # -- OAuth

    async def login(request: Request) -> Response:
        if bad := guard(request):
            return bad
        if mode != "oauth":
            return RedirectResponse("/", status_code=302)
        # Cookies are per host: log in on the redirect URI's host so the callback sees them.
        if request.headers.get("host") != redirect.netloc:
            return RedirectResponse(f"{redirect.scheme}://{redirect.netloc}/auth/login", status_code=302)
        state = secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair()
        now = time.time()
        with pending_lock:
            for k in [k for k, (_, t) in pending.items() if now - t > STATE_TTL]:
                del pending[k]
            if len(pending) > 1000:  # many abandoned logins: forget the oldest
                del pending[min(pending, key=lambda k: pending[k][1])]
            pending[state] = (verifier, now)
        resp = RedirectResponse(oauth.authorize_url(state, challenge), status_code=302)
        resp.set_cookie(STATE_COOKIE, state, max_age=STATE_TTL, path="/auth", httponly=True,
                        samesite="lax", secure=secure)
        return resp

    async def callback(request: Request) -> Response:
        if bad := guard(request):
            return bad
        if mode != "oauth":
            return RedirectResponse("/", status_code=302)
        q = request.query_params
        state = q.get("state") or ""
        with pending_lock:
            entry = pending.pop(state, None) if state else None  # one-time use
        if entry is None or time.time() - entry[1] > STATE_TTL:
            return page("Login link expired", "This login attempt is unknown, already used or "
                        "too old. Start again.", 400)
        if not secrets.compare_digest(request.cookies.get(STATE_COOKIE, ""), state):
            return page("Login failed", "This login was started in another browser or tab "
                        "profile. Start again here.", 400)
        if q.get("error"):
            return page("Login cancelled", f"Asana didn't authorize the app: {q.get('error')}"
                        + (f" ({q.get('error_description')})" if q.get("error_description") else ""), 400)
        if not q.get("code"):
            return page("Login failed", "Asana sent no authorization code.", 400)
        try:
            body = await run_in_threadpool(oauth.exchange, q["code"], entry[0])
        except OAuthError as e:
            return page("Login failed", f"Couldn't get a token from Asana: {e}", 502)
        store.drop(request.cookies.get(SESSION_COOKIE))  # replace any previous session
        s = store.create(TokenSet(oauth, body), make_backend)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, s.id, path="/", httponly=True, samesite="lax", secure=secure)
        resp.delete_cookie(STATE_COOKIE, path="/auth")
        return resp

    async def logout(request: Request) -> Response:
        if bad := guard(request):
            return bad
        store.drop(request.cookies.get(SESSION_COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax", secure=secure)
        return resp

    app = Starlette(routes=[
        Route("/", index),
        Route("/api/config", config),
        Route("/api/items", items),
        Route("/api/events", events),
        Route("/api/reload", reload, methods=["POST"]),
        Route("/api/archive", archive, methods=["POST"]),
        Route("/api/followup", followup, methods=["POST"]),
        Route("/api/react", react, methods=["POST"]),
        Route("/auth/login", login),
        Route("/auth/callback", callback),
        Route("/auth/logout", logout, methods=["POST"]),
    ])
    app.state.sessions, app.state.pending = store, pending  # for tests
    return app


def _quiet(fn) -> None:
    try:
        fn()
    except Exception:
        pass  # already broadcast as an "error" / "auth" event


def sse(msg: dict) -> str:
    return f"data: {json.dumps(msg, separators=(',', ':'))}\n\n"


# ---------------------------------------------------------------- page

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Asana Inbox</title>
<script>/* before first paint, so a picked theme doesn't flash */
try { const t = localStorage.getItem("asana-inbox.theme"); if (t === "light" || t === "dark") document.documentElement.dataset.theme = t; } catch (e) {}
</script>
<style>
/* ---- Accent colors (orange-red). Delete this block for the neutral indigo fallbacks below. ---- */
:root {
  --brand: #DE5833;         /* orange-red: header bar, focus ring, marks, borders */
  --brand-strong: #C2421C;  /* filled buttons; white text on it is 5.1:1 */
  --brand-text: #B83B17;    /* accent-colored text and links on light surfaces (5.7:1) */
  --brand-soft: #FCE9E3;    /* marked rows */
  --brand-soft-2: #F8D9CE;  /* marked row under the cursor */
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { --brand-text: #F0805F; --brand-soft: #4A2A20; --brand-soft-2: #5A3326; }
}
:root[data-theme="dark"] { --brand-text: #F0805F; --brand-soft: #4A2A20; --brand-soft-2: #5A3326; }
/* ---- end of accent colors ---- */

:root {
  color-scheme: light dark;
  --bg: #f5f5f4; --surface: #ffffff; --surface-2: #efefee; --text: #1d1d1b; --muted: #5f6368;
  --border: #e0e0de; --cursor: #e8e8e7;
  --accent: var(--brand, #5b5bd6); --accent-strong: var(--brand-strong, #4a4ac4);
  --accent-text: var(--brand-text, #4a4ac4); --mark: var(--brand-soft, #e9e9fb);
  --mark-cursor: var(--brand-soft-2, #dcdcf7);
  /* semantic: crimson and amber, kept apart from the orange-red accent */
  --overdue: #B00020; --today: #7A5000; --star: #7A5000; --danger: #B00020;
  --ok: #2e7d32; --shadow: 0 10px 40px rgba(0,0,0,.18);
}
/* Dark: follows the system unless the theme button picked one (html[data-theme]). */
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161618; --surface: #1f1f22; --surface-2: #2a2a2e; --text: #e8e8e6; --muted: #b0b0ad;
    --border: #38383d; --cursor: #36363c;
    --accent-text: var(--brand-text, #9d9df5); --mark: var(--brand-soft, #2c2c45);
    --mark-cursor: var(--brand-soft-2, #3a3a5c);
    --overdue: #FF8C99; --today: #E8C252; --star: #E8C252; --danger: #FF8C99;
    --ok: #6fcf73; --shadow: 0 10px 40px rgba(0,0,0,.6);
  }
}
:root[data-theme="dark"] {
    --bg: #161618; --surface: #1f1f22; --surface-2: #2a2a2e; --text: #e8e8e6; --muted: #b0b0ad;
    --border: #38383d; --cursor: #36363c;
    --accent-text: var(--brand-text, #9d9df5); --mark: var(--brand-soft, #2c2c45);
    --mark-cursor: var(--brand-soft-2, #3a3a5c);
    --overdue: #FF8C99; --today: #E8C252; --star: #E8C252; --danger: #FF8C99;
    --ok: #6fcf73; --shadow: 0 10px 40px rgba(0,0,0,.6);
}
:root[data-theme="light"] { color-scheme: light; }
:root[data-theme="dark"] { color-scheme: dark; }
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { background: var(--bg); color: var(--text); display: flex; flex-direction: column;
       font: 14px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
button, input { font: inherit; color: inherit; }
header { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px; padding: 10px 16px;
         border-top: 3px solid var(--accent); border-bottom: 1px solid var(--border);
         background: var(--surface); }
h1 { display: flex; align-items: center; gap: 8px; }
#logo { width: 22px; height: 22px; flex: none; }
#user { color: var(--muted); font-size: 13px; white-space: nowrap; }
#user strong { color: var(--text); font-weight: 600; }
#app { flex: 1; min-height: 0; display: flex; flex-direction: column; }
#app[hidden], #login[hidden] { display: none; }
#login { flex: 1; display: flex; align-items: center; justify-content: center; padding: 16px; }
.login-card { background: var(--surface); border: 1px solid var(--border); border-top: 3px solid var(--accent);
              border-radius: 10px; padding: 28px 32px; max-width: 420px; text-align: center; box-shadow: var(--shadow); }
.login-card img { width: 48px; height: 48px; }
.login-card h2 { margin: 10px 0 6px; font-size: 20px; }
.login-card p { color: var(--muted); margin: 0 0 18px; }
a.button { display: inline-block; text-decoration: none; border-radius: 6px; padding: 8px 18px; font-weight: 600; }
h1 { font-size: 16px; margin: 0; white-space: nowrap; }
.badge { font-size: 11px; padding: 1px 6px; border-radius: 9px; background: var(--mark);
         color: var(--accent-text); margin-left: 6px; vertical-align: 2px; }
#status { color: var(--muted); font-size: 13px; flex: 1; min-width: 200px; }
#status .loading { color: var(--accent-text); }
.toolbar { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; padding: 8px 16px;
           border-bottom: 1px solid var(--border); background: var(--surface); }
.toolbar .sep { width: 1px; align-self: stretch; background: var(--border); margin: 0 4px; }
button { background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px;
         padding: 3px 9px; cursor: pointer; white-space: nowrap; }
button:hover { border-color: var(--accent); }
button:focus-visible, input:focus-visible, textarea:focus-visible, a:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
button.primary, a.button.primary { background: var(--accent-strong); border: 1px solid var(--accent-strong); color: #fff; }
button.primary:hover { border-color: var(--text); }
#filter:focus { border-color: var(--accent); }
kbd { font: 11px ui-monospace, SFMono-Regular, Menlo, monospace; border: 1px solid var(--border);
      border-bottom-width: 2px; border-radius: 4px; padding: 0 4px; color: var(--muted);
      background: var(--surface); margin-left: 4px; }
#filter { width: 260px; max-width: 100%; padding: 3px 8px; border-radius: 6px;
          border: 1px solid var(--border); background: var(--bg); }
#filter.bad { border-color: var(--danger); }
main { flex: 1; overflow: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
thead th { position: sticky; top: 0; background: var(--surface-2); text-align: left; font-weight: 600;
           padding: 6px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; z-index: 1;
           user-select: none; }
thead th.sortable { cursor: pointer; }
thead th.sortable:hover { color: var(--accent-text); }
thead th.sorted::after { content: " ▾"; color: var(--accent-text); }
thead th.sorted.reversed::after { content: " ▴"; }
td { padding: 4px 8px; border-bottom: 1px solid var(--border); vertical-align: top;
     white-space: nowrap; }
tbody tr { cursor: default; scroll-margin-block: 90px 72px; }
tbody tr.group td { border-top: 1px solid var(--muted); }
tbody tr.group:first-child td { border-top: 0; }
tbody tr:hover { background: var(--surface-2); }
tbody tr.marked { background: var(--mark); }
tbody tr.cursor { background: var(--cursor); box-shadow: inset 3px 0 0 var(--accent); }
tbody tr.cursor.marked { background: var(--mark-cursor); }
td.mark { width: 24px; text-align: center; cursor: pointer; }
td.mark::before { content: "○"; color: var(--border); }
tr.marked td.mark::before { content: "●"; color: var(--accent-text); }
td.project { font-weight: 500; }
td.project.pending { color: var(--muted); }
td.done { width: 36px; text-align: center; color: var(--ok); }
td.title.mine { font-weight: 700; }
td.title, td.also { white-space: normal; min-width: 240px; }
.wrap-off td.title, .wrap-off td.also { white-space: nowrap; }
td.me { color: var(--star); font-weight: 700; }
td.overdue { color: var(--overdue); font-weight: 700; }
td.today { color: var(--today); font-weight: 600; }
td.duedone, td.muted { color: var(--muted); }
#empty { padding: 40px; text-align: center; color: var(--muted); }
#b-help { border: none; background: none; color: var(--muted); font-size: 13px; padding: 3px 4px; }
#b-help:hover { color: var(--accent-text); }
#b-help kbd { margin: 0 2px; }
.keys { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 4px 28px; }
.keys h3 { grid-column: 1 / -1; margin: 12px 0 2px; }
.keys div { display: flex; gap: 10px; align-items: baseline; padding: 2px 0; }
.keys div span.k { min-width: 72px; text-align: right; white-space: nowrap; }
.keys div span.k kbd { margin: 0 0 0 2px; color: var(--text); }
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,.35); display: flex;
           align-items: center; justify-content: center; padding: 16px; z-index: 10; }
.overlay[hidden] { display: none; }
.dialog { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
          box-shadow: var(--shadow); width: min(860px, 100%); max-height: 88vh; display: flex;
          flex-direction: column; }
.dialog.small { width: min(560px, 100%); }
.dialog h2 { font-size: 16px; margin: 0; padding: 14px 18px 8px; }
.dialog .body { overflow: auto; padding: 4px 18px 12px; }  /* top: room for a focused input's outline */
.dialog .buttons { display: flex; gap: 8px; padding: 12px 18px; border-top: 1px solid var(--border); }
.dialog .buttons .spacer { flex: 1; }
.dialog input[type=text] { width: 100%; padding: 6px 10px; border-radius: 6px;
          border: 1px solid var(--border); background: var(--bg); }
.hint { color: var(--muted); font-size: 12px; margin-top: 6px; }
.dialog textarea { width: 100%; padding: 6px 10px; border-radius: 6px; border: 1px solid var(--border);
          background: var(--bg); color: inherit; font: inherit; resize: vertical; }
.dialog input[type=date] { padding: 5px 8px; border-radius: 6px; border: 1px solid var(--border);
          background: var(--bg); color: inherit; color-scheme: light dark; }
.dialog label.field { display: block; margin: 10px 0 4px; color: var(--muted); font-size: 12px; }
.dialog label.check { display: flex; gap: 8px; align-items: center; margin-top: 12px; }
.dialog .quickdue { display: inline-flex; gap: 4px; margin-left: 8px; }
.dialog .quickdue button { padding: 3px 8px; font-size: 12px; }
dl.meta { display: grid; grid-template-columns: max-content 1fr; gap: 3px 14px; margin: 4px 0 12px; }
dl.meta dt { color: var(--muted); text-align: right; }
dl.meta dd { margin: 0; overflow-wrap: anywhere; }
a { color: var(--accent-text); }
h3 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted);
     margin: 16px 0 6px; }
.desc { white-space: pre-wrap; overflow-wrap: anywhere; }
.event { padding: 6px 0; border-top: 1px solid var(--border); }
.event .when { color: var(--muted); font-variant-numeric: tabular-nums; margin-right: 8px; }
.event .reactions { display: inline-flex; flex-wrap: wrap; gap: 4px; margin-left: 4px; vertical-align: middle; }
.event .react { padding: 0 6px; font-size: 12px; }
.event .react.on { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 15%, var(--surface-2)); }
.event .react.add { color: var(--muted); }
.event .palette { display: inline-flex; gap: 2px; }
.event .palette kbd { margin-left: 3px; font-size: 10px; }
.event .palette[hidden] { display: none; }
.event .text { white-space: pre-wrap; margin: 3px 0 0 18px; overflow-wrap: anywhere; }
.mention { color: var(--accent-text); background: var(--mark); font-weight: 600; border-radius: 4px;
           padding: 0 4px; white-space: nowrap; cursor: help; }
.loadingnote { color: var(--muted); font-style: italic; }
ul.confirm { list-style: none; margin: 0; padding: 0; }
ul.confirm li { padding: 3px 0; border-bottom: 1px solid var(--border); }
ul.confirm .proj { color: var(--muted); margin-right: 10px; }
ul.confirm .hiddenby { color: var(--today); font-style: italic; margin-left: 8px; }
#toasts { position: fixed; right: 16px; bottom: 44px; display: flex; flex-direction: column;
          gap: 8px; z-index: 20; max-width: min(480px, calc(100vw - 32px)); }
.toast { background: var(--surface); border: 1px solid var(--border); border-left: 4px solid var(--accent);
         border-radius: 6px; padding: 8px 12px; box-shadow: var(--shadow); overflow-wrap: anywhere; }
.toast.error { border-left-color: var(--danger); }
.toast.warning { border-left-color: var(--today); }
@media (max-width: 700px) { #filter { width: 100%; } }
</style>
</head>
<body>
<header>
  <h1><img id="logo" alt="" hidden>Asana Inbox<span id="fakebadge" class="badge" hidden>fake data</span></h1>
  <div id="status">connecting…</div>
  <button id="b-help" title="Keyboard shortcuts">Press <kbd>?</kbd> for shortcuts</button>
  <span id="user" hidden></span>
  <button id="b-theme" title="Theme: follow the system, light or dark (remembered in this browser)">◐ Auto</button>
  <button id="b-logout" hidden title="End this browser's session">Log out</button>
</header>
<section id="login" hidden>
  <div class="login-card">
    <img id="login-logo" alt="" hidden>
    <h2>Asana Inbox</h2>
    <p>Log in to see and bulk-archive your Asana Inbox. Your Asana token stays on this server.</p>
    <a class="button primary" href="/auth/login">Log in with Asana</a>
  </div>
</section>
<div id="app">
<div class="toolbar">
  <input id="filter" type="text" placeholder="Filter (regex)  l" spellcheck="false" autocomplete="off"
         title="Show only rows matching this regex (project, title, other projects, kind, assignee, 'done', 'mine'). Esc clears.">
  <button id="b-sort" title="Cycle sort: project → title → newest → oldest → due">Sort: project<kbd>s</kbd></button>
  <span class="sep"></span>
  <button id="b-markre" title="Mark shown rows matching a regex">Mark /re/<kbd>/</kbd></button>
  <button id="b-unmarkre" title="Unmark shown rows matching a regex">Unmark /re/<kbd>\</kbd></button>
  <button id="b-project" title="Mark / unmark all shown rows in the selected row's project">Project<kbd>p</kbd></button>
  <button id="b-done" title="Mark / unmark all shown completed tasks">Completed<kbd>c</kbd></button>
  <button id="b-all" title="Mark / unmark all shown rows">All<kbd>v</kbd></button>
  <span class="sep"></span>
  <button id="b-followup" title="Create a task for me, due today, linking to the selected item, and archive it">Follow-up<kbd>f</kbd></button>
  <button id="b-followup-ask" title="Create a follow-up task: choose its title, due date and whether to archive">Custom follow-up<kbd>F</kbd></button>
  <span class="sep"></span>
  <button id="b-wrap" title="Toggle shortening long titles and projects">Shorten<kbd>w</kbd></button>
  <button id="b-reload" title="Reload from Asana">Reload<kbd>r</kbd></button>
  <button id="b-archive" class="primary" title="Archive marked rows (asks first)">Archive 0<kbd>a</kbd></button>
</div>
<main id="main">
  <table id="table">
    <thead><tr>
      <th></th><th class="sortable" data-sort="project">Project</th>
      <th class="sortable" data-sort="done" title="Completed first">Done</th>
      <th class="sortable" data-sort="title">Title</th>
      <th class="sortable" data-sort="assignee" title="Mine first, unassigned last">Assignee</th>
      <th class="sortable" data-sort="due">Due</th>
      <th class="sortable" data-sort="kind" title="Most important reason first (assigned, mention, comment, …)">Kind</th>
      <th class="sortable" data-sort="newest">Updated</th>
      <th class="sortable" data-sort="also">Also in</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="empty" hidden></div>
</main>
</div>

<div id="ov-regex" class="overlay" hidden>
  <div class="dialog small" role="dialog" aria-modal="true" aria-labelledby="regex-title">
    <h2 id="regex-title">Mark shown rows matching</h2>
    <div class="body">
      <input id="regex-input" type="text" spellcheck="false" autocomplete="off">
      <div class="hint" id="regex-hint"></div>
    </div>
    <div class="buttons"><button id="regex-ok" class="primary">Mark</button>
      <button id="regex-cancel">Cancel</button></div>
  </div>
</div>

<div id="ov-detail" class="overlay" hidden>
  <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="detail-title">
    <h2 id="detail-title"></h2>
    <div class="body" id="detail-body"></div>
    <div class="buttons"><button id="detail-copy">Copy link<kbd>y</kbd></button>
      <button id="detail-open">Open in Asana<kbd>o</kbd></button>
      <button id="detail-followup">Follow-up<kbd>f</kbd></button>
      <button id="detail-followup-ask">Custom follow-up<kbd>F</kbd></button>
      <button id="detail-archive" class="primary">Archive<kbd>a</kbd></button>
      <span class="spacer"></span><button id="detail-close">Close<kbd>Esc</kbd></button></div>
  </div>
</div>

<div id="ov-confirm" class="overlay" hidden>
  <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
    <h2 id="confirm-title"></h2>
    <div class="body"><ul class="confirm" id="confirm-list"></ul></div>
    <div class="buttons"><button id="confirm-yes" class="primary">Archive</button>
      <button id="confirm-no">Cancel</button></div>
  </div>
</div>

<div id="ov-followup" class="overlay" hidden>
  <div class="dialog small" role="dialog" aria-modal="true" aria-labelledby="followup-title">
    <h2 id="followup-title">Create follow-up</h2>
    <div class="body">
      <div class="hint" id="followup-for"></div>
      <label class="field" for="followup-text">Title</label>
      <input id="followup-text" type="text" spellcheck="false" autocomplete="off">
      <label class="field" for="followup-notes">Description <span class="hint">(ctrl+Enter creates)</span></label>
      <textarea id="followup-notes" rows="4" spellcheck="false"></textarea>
      <label class="field" for="followup-due">Due</label>
      <input id="followup-due" type="date" required>
      <span class="quickdue"><button type="button" data-days="0">Today</button>
        <button type="button" data-days="1">Tomorrow</button><button type="button" data-days="7">+1 week</button></span>
      <label class="check"><input id="followup-archive" type="checkbox" checked> Archive this notification</label>
      <div class="hint">Assigned to you, in My Tasks.</div>
    </div>
    <div class="buttons"><button id="followup-ok" class="primary">Create</button>
      <button id="followup-cancel">Cancel</button></div>
  </div>
</div>

<div id="ov-help" class="overlay" hidden>
  <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="help-title">
    <h2 id="help-title">Keyboard shortcuts</h2>
    <div class="body keys">
      <h3>Moving around</h3>
      <div><span class="k"><kbd>↑</kbd><kbd>↓</kbd> / <kbd>j</kbd><kbd>k</kbd></span><span>move</span></div>
      <div><span class="k"><kbd>PgUp</kbd><kbd>PgDn</kbd></span><span>page</span></div>
      <div><span class="k"><kbd>Home</kbd><kbd>End</kbd></span><span>first / last row</span></div>
      <div><span class="k"><kbd>Enter</kbd></span><span>details</span></div>
      <h3>Marking</h3>
      <div><span class="k"><kbd>space</kbd></span><span>mark / unmark row</span></div>
      <div><span class="k"><kbd>p</kbd></span><span>mark row&#x27;s project</span></div>
      <div><span class="k"><kbd>c</kbd></span><span>mark completed</span></div>
      <div><span class="k"><kbd>v</kbd></span><span>mark / unmark all shown</span></div>
      <div><span class="k"><kbd>/</kbd></span><span>mark by regex</span></div>
      <div><span class="k"><kbd>\</kbd></span><span>unmark by regex</span></div>
      <h3>Viewing</h3>
      <div><span class="k"><kbd>l</kbd></span><span>filter (regex)</span></div>
      <div><span class="k"><kbd>Esc</kbd></span><span>clear filter</span></div>
      <div><span class="k"><kbd>s</kbd></span><span>cycle sort</span></div>
      <div><span class="k"><kbd>w</kbd></span><span>shorten long text</span></div>
      <div><span class="k"><kbd>r</kbd></span><span>reload</span></div>
      <h3>Acting</h3>
      <div><span class="k"><kbd>a</kbd></span><span>archive marked (asks)</span></div>
      <div><span class="k"><kbd>f</kbd></span><span>follow-up, due today</span></div>
      <div><span class="k"><kbd>F</kbd></span><span>custom follow-up</span></div>
      <div><span class="k"><kbd>o</kbd></span><span>open in Asana</span></div>
      <div><span class="k"><kbd>y</kbd></span><span>copy link</span></div>
      <h3>In the details view</h3>
      <div><span class="k"><kbd>a</kbd></span><span>archive this item</span></div>
      <div><span class="k"><kbd>f</kbd><kbd>F</kbd></span><span>follow-up / custom</span></div>
      <div><span class="k"><kbd>o</kbd><kbd>y</kbd></span><span>open / copy link</span></div>
      <div><span class="k"><kbd>e</kbd> <kbd>1</kbd>–<kbd>8</kbd></span><span>react to the newest comment</span></div>
      <div><span class="k"><kbd>Esc</kbd></span><span>close (the reactions first, if open)</span></div>
      <h3>In the follow-up dialog</h3>
      <div><span class="k"><kbd>Enter</kbd></span><span>create (one-line fields)</span></div>
      <div><span class="k"><kbd>Ctrl+Enter</kbd></span><span>create (anywhere)</span></div>
      <div><span class="k"><kbd>Esc</kbd></span><span>cancel</span></div>
    </div>
    <div class="buttons"><span class="spacer"></span><button id="help-close">Close<kbd>Esc</kbd></button></div>
  </div>
</div>

<div id="toasts" aria-live="polite"></div>

<script>
"use strict";
const $ = (id) => document.getElementById(id);
const SORTS = ["project", "title", "newest", "oldest", "due"];
const S = {
  items: [],          // in server (listing) order
  byId: new Map(),
  marked: new Set(),
  sort: "project",
  reverse: false,     // clicking the sorted column's header again flips it
  filter: null,       // RegExp or null
  shorten: true,
  cursorId: null,
  rows: [],           // items in display order (visible + sorted)
  generation: 0,
  status: "",
  cfg: {title_width: 72, project_width: 30, fake: false},
  modal: null,        // "regex" | "detail" | "confirm"
  detailId: null,
  busy: "",
};
try { const v = localStorage.getItem("asana-inbox.shorten"); if (v !== null) S.shorten = v === "1"; } catch (e) {}

// ---------- helpers mirroring inbox_core
const lower = (s) => (s || "").toLowerCase();
const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
function cmpTuple(a, b) { for (let i = 0; i < a.length; i++) { const c = cmp(a[i], b[i]); if (c) return c; } return 0; }
const SORT_KEYS = {
  project: (it) => [lower(it.project), lower(it.title)],
  title: (it) => [lower(it.title), lower(it.project)],
  newest: (it) => [-it.updated],
  oldest: (it) => [it.updated],
  due: (it) => [it.due === null ? 1 : 0, it.due === null ? Infinity : it.due, -it.updated],
  // Web-only (column headers; `s` cycles the five above, like the TUI):
  done: (it) => [it.completed ? 0 : 1, -(it.completed || 0), lower(it.project), lower(it.title)],
  assignee: (it) => [it.mine ? 0 : it.assignee ? 1 : 2, lower(it.assignee), lower(it.project), lower(it.title)],
  kind: (it) => [kindRank(it.kind), lower(it.kind), lower(it.project), lower(it.title)],
  also: (it) => { const a = alsoIn(it); return [a ? 0 : 1, lower(a), lower(it.title)]; },
};
// asana_api.REASONS order; kinds not in it (messages, summaries, …) after "follower", like there.
const REASON_ORDER = ["assigned", "mention", "comment", "approval", "due date", "completed", "reopened",
                      "unassigned", "follower", "added to project", "reaction", "liked"];
function kindRank(kind) { const i = REASON_ORDER.indexOf(kind); return i < 0 ? REASON_ORDER.indexOf("follower") + 0.5 : i; }
function alsoIn(it) { return it.projects.filter((p) => p !== it.project).join(", "); }
function sortRows(list) {
  const key = SORT_KEYS[S.sort], dir = S.reverse ? -1 : 1;
  return list.slice().sort((a, b) => dir * cmpTuple(key(a), key(b)));
}
function sortLabel() { return S.sort + (S.reverse ? " (reversed)" : ""); }
function matchText(it) {
  return `${it.project} ${it.title} ${it.projects.join(" ")} ${it.kind} ${it.completed ? "done" : ""} ` +
         `${it.assignee || ""} ${it.mine ? "mine" : ""}`;
}
function visible() { return S.filter ? S.items.filter((it) => S.filter.test(matchText(it))) : S.items; }
function ellipsize(s, w) { return s.length <= w ? s : s.slice(0, w - 1).trimEnd() + "…"; }
const cut = (s, w) => (S.shorten ? ellipsize(s, w) : s);
const pad = (n) => String(n).padStart(2, "0");
function fmt(ts, withTime = true) {
  if (ts === null || ts === undefined) return "";
  const d = new Date(ts * 1000);
  const day = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return withTime ? `${day} ${pad(d.getHours())}:${pad(d.getMinutes())}` : day;
}
function dueState(it) {
  if (it.due === null) return "";
  if (it.completed) return "duedone";
  const now = new Date(), due = new Date(it.due * 1000);
  const dayOf = (d) => d.getFullYear() * 10000 + d.getMonth() * 100 + d.getDate();
  if (it.due_has_time ? due < now : dayOf(due) < dayOf(now)) return "overdue";
  if (dayOf(due) === dayOf(now)) return "today";
  return "";
}
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v; else e.setAttribute(k, v);
  }
  for (const k of kids) e.append(k);
  return e;
}
// Rich text runs (see asana_api): strings, {user, text} mentions and {link, text} links.
// Mentions show "@Full Name" with the email on hover; only http(s) links become links.
function richNodes(runs, plain, people) {
  if (!runs) return [plain.trim()];
  return runs.map((r) => {
    if (typeof r === "string") return r;
    if (r.user) {
      const p = (people || {})[r.user] || {};
      return el("span", {class: "mention", text: p.name ? `@${p.name}` : r.text,
                         title: p.email || p.name || "(email not available)"});
    }
    return /^https?:\/\//i.test(r.link)
      ? el("a", {href: r.link, target: "_blank", rel: "noopener noreferrer", text: r.text}) : r.text;
  });
}
function toast(text, kind = "info", ms = 4000) {
  const t = el("div", {class: "toast " + kind, text});
  $("toasts").append(t);
  setTimeout(() => t.remove(), kind === "error" ? 15000 : ms);
}
function compile(pattern) {
  try { return new RegExp(pattern, "i"); } catch (e) { return null; }
}

// ---------- rendering
function render() {
  const shown = visible();
  S.rows = sortRows(shown);
  const tbody = $("rows"), frag = document.createDocumentFragment();
  let prev = null;
  for (const it of S.rows) {
    const first = it.project !== prev;
    const project = S.sort !== "project" || first ? it.project : "";
    prev = it.project;
    const ds = dueState(it);
    const tr = el("tr", {"data-id": it.id},
      el("td", {class: "mark", title: "Mark / unmark (space)"}),
      el("td", {class: "project" + (it.pending ? " pending" : ""), text: cut(project, S.cfg.project_width),
                title: project.length > S.cfg.project_width ? project : ""}),
      el("td", {class: "done", text: it.completed ? "✓" : ""}),
      el("td", {class: "title" + (it.mine ? " mine" : ""), text: cut(it.title, S.cfg.title_width)}),
      el("td", {class: it.mine ? "me" : "", text: it.mine ? "★ me" : (it.assignee || "")}),
      el("td", {class: ds, text: fmt(it.due, it.due_has_time)}),
      el("td", {class: "muted", text: it.kind}),
      el("td", {class: "muted", text: fmt(it.updated)}),
      el("td", {class: "also muted", text: alsoIn(it)}),
    );
    if (S.sort === "project" && first) tr.classList.add("group");
    if (S.marked.has(it.id)) tr.classList.add("marked");
    frag.append(tr);
  }
  tbody.replaceChildren(frag);
  if (!S.rows.some((it) => it.id === S.cursorId)) S.cursorId = S.rows.length ? S.rows[0].id : null;
  drawCursor(false);
  const empty = $("empty");
  empty.hidden = S.rows.length > 0;
  empty.textContent = S.items.length ? "No rows match the filter." : (S.status || S.busy ? "Loading…" : "Inbox zero.");
  document.querySelectorAll("thead th.sortable").forEach((th) => {
    const k = th.dataset.sort;
    const on = k === S.sort || (k === "newest" && S.sort === "oldest");
    th.classList.toggle("sorted", on);
    th.classList.toggle("reversed", on && (S.reverse || S.sort === "oldest"));
  });
  $("b-sort").firstChild.textContent = `Sort: ${sortLabel()}`;
  document.body.classList.toggle("wrap-off", !S.shorten);
  $("b-wrap").firstChild.textContent = S.shorten ? "Shorten: on" : "Shorten: off";
  updateStatus(shown);
}
function refreshMarks() {
  for (const tr of $("rows").children) tr.classList.toggle("marked", S.marked.has(tr.dataset.id));
  updateStatus(visible());
}
function updateStatus(shown) {
  const parts = [`${S.items.length} items`, `${S.marked.size} marked`, `sort: ${sortLabel()}`];
  const pending = S.items.reduce((n, it) => n + (it.pending ? 1 : 0), 0);
  if (pending) parts.push(`loading details: ${pending} left`);
  if (S.filter) {
    const shownIds = new Set(shown.map((it) => it.id));
    let hidden = 0; for (const id of S.marked) if (!shownIds.has(id)) hidden++;
    parts[0] = `${shown.length}/${S.items.length} shown`;
    parts.push(`filter: /${S.filter.source}/`);
    if (hidden) parts[1] += ` (${hidden} hidden)`;
  }
  const st = $("status");
  st.replaceChildren(parts.join(" · "));
  const busy = S.busy || S.status;
  if (busy) st.append(" · ", el("span", {class: "loading", text: busy}));
  $("b-archive").firstChild.textContent = `Archive ${S.marked.size}`;
}
function rowEl(id) { return $("rows").querySelector(`tr[data-id="${CSS.escape(id)}"]`); }
function drawCursor(scroll = true) {
  for (const tr of $("rows").querySelectorAll("tr.cursor")) tr.classList.remove("cursor");
  if (S.cursorId === null) return;
  const tr = rowEl(S.cursorId);
  if (!tr) return;
  tr.classList.add("cursor");
  if (scroll) tr.scrollIntoView({block: "nearest"});
}
function cursorIndex() { return S.rows.findIndex((it) => it.id === S.cursorId); }
function moveCursor(delta, absolute = false) {
  if (!S.rows.length) return;
  let i = absolute ? delta : cursorIndex() + delta;
  i = Math.max(0, Math.min(S.rows.length - 1, i));
  S.cursorId = S.rows[i].id;
  drawCursor();
}
function pageRows() {
  const tr = $("rows").firstElementChild;
  return tr ? Math.max(1, Math.floor($("main").clientHeight / tr.offsetHeight) - 2) : 10;
}
const current = () => (S.cursorId === null ? null : S.byId.get(S.cursorId) || null);

// ---------- data
function setItems(list) {
  S.items = list;
  S.byId = new Map(list.map((it) => [it.id, it]));
  for (const id of [...S.marked]) if (!S.byId.has(id)) S.marked.delete(id);
  render();
  if (S.modal === "detail") { if (S.byId.has(S.detailId)) renderDetail(); else closeModal(); }
}
let redrawTimer = null;
function applyDetails(list) {
  for (const upd of list) {
    const it = S.byId.get(upd.id);
    if (it) Object.assign(it, upd);
  }
  if (S.modal === "detail" && list.some((u) => u.id === S.detailId)) renderDetail();
  // Batches arrive in quick succession: one redraw for whatever came in meanwhile.
  if (!redrawTimer) redrawTimer = setTimeout(() => { redrawTimer = null; render(); }, 300);
}
async function api(path, body) {
  const opts = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"},
                                         body: JSON.stringify(body)};
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) { showLogin(); throw new Error("Not logged in"); }
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}
let es = null;
function connect() {
  if (es) es.close();
  es = new EventSource("/api/events");
  es.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "items") {
      S.generation = m.generation; S.status = m.status || ""; setItems(m.items);
    } else if (m.type === "details") {
      if (m.generation === S.generation) applyDetails(m.items);
    } else if (m.type === "status") {
      S.status = m.text; updateStatus(visible());
      if (!S.items.length) render();
    } else if (m.type === "error") {
      toast(m.text, "error");
    } else if (m.type === "auth") {
      showLogin();
    }
  };
  es.onerror = () => {
    if (es.readyState !== EventSource.CLOSED) {  // dropped: the browser reconnects by itself
      S.status = "reconnecting…"; updateStatus(visible()); return;
    }
    // The server refused the stream (401, or the inbox couldn't open): find out why.
    es = null;
    api("/api/items").then(() => setTimeout(connect, 2000), (e) => {
      if (!S.loggedOut) { toast(e.message, "error"); S.status = "disconnected: press r to retry"; updateStatus(visible()); }
    });
  };
}
function showLogin() {
  S.loggedOut = true;
  if (es) { es.close(); es = null; }
  closeModal();
  $("app").hidden = true; $("login").hidden = false;
  $("user").hidden = true; $("b-logout").hidden = true;
  $("status").textContent = "";
}
function showUser() {
  const u = $("user");
  u.hidden = !S.cfg.user;
  u.replaceChildren(...(S.cfg.user ? ["Logged in as ", el("strong", {text: S.cfg.user})] : []));
  $("b-logout").hidden = S.cfg.mode !== "oauth";
}
async function logout() {
  try { await api("/auth/logout", {}); } catch (e) {}
  location.reload();
}
$("b-logout").onclick = logout;

// ---------- actions
function toggleSet(ids) {
  const all = ids.length > 0 && ids.every((id) => S.marked.has(id));
  for (const id of ids) all ? S.marked.delete(id) : S.marked.add(id);
  return !all;
}
const actions = {
  toggle() {
    if (S.cursorId === null) return;
    S.marked.has(S.cursorId) ? S.marked.delete(S.cursorId) : S.marked.add(S.cursorId);
    refreshMarks(); moveCursor(1);
  },
  toggleProject() {
    const cur = current(); if (!cur) return;
    toggleSet(visible().filter((it) => it.project === cur.project).map((it) => it.id));
    refreshMarks();
  },
  toggleCompleted() {
    const ids = visible().filter((it) => it.completed).map((it) => it.id);
    if (!ids.length) { toast("No completed tasks shown"); return; }
    const marked = toggleSet(ids);
    toast(`${marked ? "Marked" : "Unmarked"} ${ids.length} completed tasks`);
    refreshMarks();
  },
  toggleAll() { toggleSet(visible().map((it) => it.id)); refreshMarks(); },
  cycleSort() {
    const i = SORTS.indexOf(S.sort);  // from a web-only column sort: start over at the first
    S.sort = SORTS[(i + 1) % SORTS.length]; S.reverse = false; render(); drawCursor();
  },
  toggleShorten() {
    S.shorten = !S.shorten;
    try { localStorage.setItem("asana-inbox.shorten", S.shorten ? "1" : "0"); } catch (e) {}
    render();
    toast(S.shorten ? "Long titles/projects shortened" : "Showing full titles/projects");
  },
  focusFilter() { const f = $("filter"); f.focus(); f.select(); },
  clearFilter() {
    if (!S.filter && !$("filter").value) return;
    $("filter").value = ""; $("filter").classList.remove("bad"); S.filter = null; render(); drawCursor();
  },
  copyLink(it = current()) {
    if (!it || !it.url) { toast("No URL for this item", "warning"); return; }
    copyText(it.url).then(() => toast(`Copied ${it.url}`), () => toast("Couldn't copy to the clipboard", "error"));
  },
  openLink(it = current()) {
    if (!it || !/^https:\/\//.test(it.url || "")) { toast("No URL for this item", "warning"); return; }
    window.open(it.url, "_blank", "noopener,noreferrer");
  },
  followup(it = current(), ask = false) {
    if (!it) return;
    if (ask) openFollowup(it); else createFollowup(it, {due: localDate(0), archive: true});
  },
  details(it = current()) { if (it) openDetail(it.id); },
  regex(mode) { openRegex(mode); },
  archive() { openConfirm(); },
  async reload() {
    S.busy = "loading…"; updateStatus(visible());
    try { await api("/api/reload", {}); if (!es) connect(); }
    catch (e) { if (!S.loggedOut) toast(e.message, "error"); }
    finally { S.busy = ""; updateStatus(visible()); }
  },
};
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const ta = el("textarea", {style: "position:fixed;opacity:0"}); ta.value = text;
  document.body.append(ta); ta.select();
  const ok = document.execCommand("copy"); ta.remove();
  if (!ok) throw new Error("copy failed");
}

// ---------- filter box
let filterTimer = null;
$("filter").addEventListener("input", () => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(() => {
    const v = $("filter").value;
    if (!v) { S.filter = null; $("filter").classList.remove("bad"); render(); return; }
    const rx = compile(v);
    $("filter").classList.toggle("bad", !rx);
    if (rx) { S.filter = rx; render(); }
  }, 120);
});
$("filter").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.preventDefault(); actions.clearFilter(); $("filter").blur(); }
  else if (e.key === "Enter" || e.key === "ArrowDown") { e.preventDefault(); $("filter").blur(); }
  e.stopPropagation();
});

// ---------- modals
function openModal(name) {
  S.modal = name;
  for (const n of ["regex", "detail", "confirm", "followup", "help"]) $("ov-" + n).hidden = n !== name;
}
function closeModal() {
  if (S.modal) $("ov-" + S.modal).hidden = true;
  S.modal = null; S.detailId = null;
}
for (const n of ["regex", "detail", "confirm", "followup", "help"]) {
  $("ov-" + n).addEventListener("mousedown", (e) => { if (e.target === e.currentTarget) closeModal(); });
}

let regexMode = "mark";
function openRegex(mode) {
  regexMode = mode;
  $("regex-title").textContent = mode === "mark" ? "Mark shown rows matching" : "Unmark shown rows matching";
  $("regex-ok").textContent = mode === "mark" ? "Mark" : "Unmark";
  const inp = $("regex-input");
  inp.value = ""; inp.placeholder = "regex: project, title, other projects, kind, assignee, 'done', 'mine'";
  regexHint();
  openModal("regex"); inp.focus();
}
function regexHint() {
  const v = $("regex-input").value, rx = v ? compile(v) : null;
  $("regex-hint").textContent = !v ? "Matches project, title, other projects, kind, assignee, plus 'done' and 'mine'."
    : !rx ? "Invalid regex" : `${visible().filter((it) => rx.test(matchText(it))).length} shown rows match`;
}
$("regex-input").addEventListener("input", regexHint);
function applyRegex() {
  const v = $("regex-input").value;
  if (!v) { closeModal(); return; }
  const rx = compile(v);
  if (!rx) { toast("Bad regex", "error"); return; }
  const ids = visible().filter((it) => rx.test(matchText(it))).map((it) => it.id);
  for (const id of ids) regexMode === "mark" ? S.marked.add(id) : S.marked.delete(id);
  closeModal();
  toast(`${regexMode === "mark" ? "Marked" : "Unmarked"} ${ids.length} items`);
  refreshMarks();
}
$("regex-ok").onclick = applyRegex;
$("regex-cancel").onclick = closeModal;
$("regex-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); applyRegex(); }
  else if (e.key === "Escape") { e.preventDefault(); closeModal(); }
  e.stopPropagation();
});

function openDetail(id) { S.detailId = id; renderDetail(); openModal("detail"); $("detail-close").focus(); }
function renderDetail() {
  const it = S.byId.get(S.detailId); if (!it) return;
  $("detail-title").textContent = it.title;
  const body = $("detail-body"); body.replaceChildren();
  if (it.pending) body.append(el("div", {class: "loadingnote", text: "(details still loading)"}));
  const dl = el("dl", {class: "meta"});
  const row = (k, v) => dl.append(el("dt", {text: k}), el("dd", {}, v));
  row("Kind", it.kind);
  row("Projects", it.projects.join(", ") || "-");
  row("Assignee", (it.mine ? "me" : it.assignee) || "-");
  if (it.due !== null) {
    const ds = dueState(it);
    const span = el("span", {text: fmt(it.due, it.due_has_time)});
    if (ds === "overdue") span.style.cssText = "color:var(--overdue);font-weight:700";
    else if (ds === "today") span.style.color = "var(--today)";
    else if (ds === "duedone") span.style.color = "var(--muted)";
    row("Due", span);
  }
  if (it.completed) row("Completed", fmt(it.completed));
  row("Link", it.url ? el("a", {href: it.url, target: "_blank", rel: "noopener noreferrer", text: it.url}) : "-");
  body.append(dl);
  if (it.description.trim()) body.append(el("h3", {text: it.kind === "message" ? "Message" : "Description"}), el("div", {class: "desc"}, ...richNodes(it.description_rich, it.description, it.people)));
  if (it.events.length) {
    body.append(el("h3", {text: "Notifications"}));
    for (const e of it.events) {
      const div = el("div", {class: "event"},
        el("span", {class: "when", text: e.time ? fmt(e.time) : "?"}),
        el("strong", {text: e.who || "Asana"}), ` · ${e.what}`);
      if (e.story) div.append(" ", reactionsEl(it, e));
      if ((e.text || "").trim()) div.append(el("div", {class: "text"}, ...richNodes(e.rich, e.text, it.people)));
      body.append(div);
    }
  }
}
const REACTION_EMOJI = ["👍", "🙏", "🙌", "🎉", "❤️", "😄", "👀", "✅"];
// A comment's reactions as chips (count; mine highlighted; click toggles mine) and a "+" that
// opens a palette of REACTION_EMOJI for adding one (keys: e, then 1-8, on the newest comment).
function reactionsEl(it, e) {
  const wrap = el("span", {class: "reactions", "data-story": e.story});
  const chip = (emoji, count, mine, label) => {
    const b = el("button", {class: "react" + (mine ? " on" : ""), text: label,
                            title: mine ? `Remove your ${emoji}` : `React with ${emoji}`});
    b.setAttribute("aria-pressed", mine ? "true" : "false");
    b.onclick = () => react(it, e, emoji, !mine, wrap);
    return b;
  };
  const reactions = e.reactions || [];
  for (const r of reactions) wrap.append(chip(r.emoji, r.count, r.reacted, `${r.emoji} ${r.count}`));
  const add = el("button", {class: "react add", text: "+", title: "Add a reaction"});
  add.setAttribute("aria-expanded", "false");
  const palette = el("span", {class: "palette", hidden: ""});
  REACTION_EMOJI.forEach((emoji, i) => {
    const mine = reactions.some((r) => r.emoji === emoji && r.reacted);
    const b = chip(emoji, 0, mine, emoji);
    b.append(el("kbd", {text: String(i + 1)}));
    palette.append(b);
  });
  add.onclick = () => {
    palette.hidden = !palette.hidden;
    add.setAttribute("aria-expanded", palette.hidden ? "false" : "true");
  };
  wrap.append(add, palette);
  return wrap;
}
// Keyboard: the newest comment's palette (none if the item has no comments).
function newestPalette() {
  const it = S.byId.get(S.detailId);
  const e = it && [...it.events].reverse().find((ev) => ev.story);
  const wrap = e && $("detail-body").querySelector(`.reactions[data-story="${CSS.escape(e.story)}"]`);
  return wrap && {wrap, add: wrap.querySelector(".add"), palette: wrap.querySelector(".palette")};
}
function detailReactKey(key) {
  const p = newestPalette();
  if (!p) { if (key === "e") toast("No comments to react to"); return false; }
  if (key === "e") { p.add.click(); if (!p.palette.hidden) p.wrap.scrollIntoView({block: "nearest"}); return true; }
  if (p.palette.hidden) return false;
  if (key === "Escape") { p.add.click(); return true; }
  const n = Number(key);
  if (n >= 1 && n <= REACTION_EMOJI.length) { p.palette.children[n - 1].click(); return true; }
  return false;
}
async function react(it, e, emoji, reacted, wrap) {
  for (const b of wrap.querySelectorAll("button")) b.disabled = true;
  try {
    const r = await api("/api/react", {id: it.id, story: e.story, emoji, reacted});
    e.reactions = r.reactions;
    if (S.modal === "detail" && S.detailId === it.id) renderDetail();
  } catch (err) {
    for (const b of wrap.querySelectorAll("button")) b.disabled = false;
    toast(`Reacting failed: ${err.message}`, "error");
  }
}
$("detail-close").onclick = closeModal;
$("detail-copy").onclick = () => actions.copyLink(S.byId.get(S.detailId));
$("detail-open").onclick = () => actions.openLink(S.byId.get(S.detailId));

// ---------- follow-ups
function localDate(days) {  // the browser's local date, days from today, as YYYY-MM-DD
  const d = new Date(); d.setDate(d.getDate() + days);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
let followupId = null;
function openFollowup(it) {
  if (S.modal) closeModal();
  followupId = it.id;
  $("followup-for").textContent = `For: ${it.title}`;
  $("followup-notes").value = it.url || "";
  $("followup-text").value = `Follow up on ${it.title}`;
  $("followup-due").value = localDate(0);
  $("followup-archive").checked = true;
  openModal("followup"); $("followup-text").focus(); $("followup-text").select();
}
function submitFollowup() {
  const it = S.byId.get(followupId), text = $("followup-text").value.trim(), due = $("followup-due").value;
  if (!it) { closeModal(); toast("That item is gone (archived or reloaded)", "warning"); return; }
  if (!text) { toast("The follow-up needs a title", "warning"); $("followup-text").focus(); return; }
  if (!due) { toast("Pick a due date", "warning"); $("followup-due").focus(); return; }
  closeModal();
  createFollowup(it, {text, notes: $("followup-notes").value, due, archive: $("followup-archive").checked});
}
async function createFollowup(it, {text, notes, due, archive}) {
  if (S.modal === "detail") closeModal();
  S.busy = "creating follow-up…"; updateStatus(visible());
  try {
    const r = await api("/api/followup", {id: it.id, text, notes, due, archive});
    S.marked.delete(it.id);
    const when = r.due === localDate(0) ? "today" : r.due;
    toast(`Follow-up due ${when}${r.archived ? ", notification archived" : ""}: ${r.url}`);
  } catch (e) { if (!S.loggedOut) toast(e.message, "error"); }
  finally { S.busy = ""; updateStatus(visible()); }
}
$("followup-ok").onclick = submitFollowup;
$("followup-cancel").onclick = closeModal;
for (const b of document.querySelectorAll("#ov-followup .quickdue button")) {
  b.onclick = () => { $("followup-due").value = localDate(Number(b.dataset.days)); };
}
$("ov-followup").addEventListener("keydown", (e) => {
  // Enter in a one-line field, or ctrl/cmd+Enter anywhere (the description takes plain Enter)
  if (e.key === "Enter" && (e.target.tagName === "INPUT" || e.ctrlKey || e.metaKey)) { e.preventDefault(); submitFollowup(); }
  else if (e.key === "Escape") { e.preventDefault(); closeModal(); }
  e.stopPropagation();
});
$("b-followup").onclick = () => actions.followup();
$("b-followup-ask").onclick = () => actions.followup(current(), true);
$("detail-followup").onclick = () => actions.followup(S.byId.get(S.detailId));
$("detail-followup-ask").onclick = () => actions.followup(S.byId.get(S.detailId), true);
$("detail-archive").onclick = () => archiveOne(S.byId.get(S.detailId));
// The item in the details view: archived without asking, since it's in plain sight.
async function archiveOne(it) {
  if (!it) return;
  closeModal();
  S.busy = "archiving 1…"; updateStatus(visible());
  try {
    await api("/api/archive", {ids: [it.id]});
    S.marked.delete(it.id);
    toast(`Archived “${it.title}”`);
  } catch (e) { if (!S.loggedOut) toast(e.message, "error"); }
  finally { S.busy = ""; updateStatus(visible()); }
}

let confirmIds = [];
function openConfirm() {
  if (!S.marked.size) { toast("Nothing marked", "warning"); return; }
  const shownIds = new Set(visible().map((it) => it.id));
  const marked = sortRows(S.items.filter((it) => S.marked.has(it.id)));  // in table order
  const hidden = marked.filter((it) => !shownIds.has(it.id)).length;
  confirmIds = marked.map((it) => it.id);
  $("confirm-title").textContent = `Archive these ${marked.length} items?` +
    (hidden ? ` (${hidden} hidden by the filter)` : "");
  $("confirm-list").replaceChildren(...marked.map((it) => {
    const li = el("li", {}, el("span", {class: "proj", text: it.project}), it.title);
    if (!shownIds.has(it.id)) li.append(el("span", {class: "hiddenby", text: "(hidden by filter)"}));
    return li;
  }));
  openModal("confirm"); $("confirm-yes").focus();
}
async function doArchive() {
  const ids = confirmIds; closeModal();
  if (!ids.length) return;
  S.busy = `archiving ${ids.length}…`; updateStatus(visible());
  try {
    const r = await api("/api/archive", {ids});
    for (const id of ids) S.marked.delete(id);
    toast(`Archived ${r.archived} items (press r to re-sync with Asana)`);
  } catch (e) { if (!S.loggedOut) toast(e.message, "error"); }
  finally { S.busy = ""; updateStatus(visible()); }
}
$("confirm-yes").onclick = doArchive;
$("confirm-no").onclick = closeModal;

// ---------- keyboard
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey || S.loggedOut) return;
  if (S.modal === "detail") {
    if (detailReactKey(e.key)) { e.preventDefault(); return; }
    if (e.key === "Enter" && e.target.closest?.(".reactions")) return;  // a focused reaction button
    if (["Escape", "Enter", "q"].includes(e.key)) { e.preventDefault(); closeModal(); }
    else if (e.key === "y") { e.preventDefault(); actions.copyLink(S.byId.get(S.detailId)); }
    else if (e.key === "o") { e.preventDefault(); actions.openLink(S.byId.get(S.detailId)); }
    else if (e.key === "f" || e.key === "F") { e.preventDefault(); actions.followup(S.byId.get(S.detailId), e.key === "F"); }
    else if (e.key === "a") { e.preventDefault(); archiveOne(S.byId.get(S.detailId)); }
    return;
  }
  if (S.modal === "help") {
    if (["Escape", "?", "q", "Enter"].includes(e.key)) { e.preventDefault(); closeModal(); }
    return;
  }
  if (S.modal === "confirm") {
    if (e.key === "Escape") { e.preventDefault(); closeModal(); }
    return;  // Enter activates the focused button; Tab moves between them
  }
  if (S.modal) return;
  if (e.target.tagName === "INPUT") return;
  const keys = {
    ArrowDown: () => moveCursor(1), ArrowUp: () => moveCursor(-1), j: () => moveCursor(1), k: () => moveCursor(-1),
    PageDown: () => moveCursor(pageRows()), PageUp: () => moveCursor(-pageRows()),
    Home: () => moveCursor(0, true), End: () => moveCursor(S.rows.length - 1, true),
    " ": actions.toggle, p: actions.toggleProject, c: actions.toggleCompleted, v: actions.toggleAll,
    "/": () => actions.regex("mark"), "\\": () => actions.regex("unmark"),
    l: actions.focusFilter, f: () => actions.followup(), F: () => actions.followup(current(), true),
    Escape: actions.clearFilter, s: actions.cycleSort, w: actions.toggleShorten,
    y: () => actions.copyLink(), o: () => actions.openLink(), a: actions.archive, "?": () => openHelp(), r: actions.reload, Enter: () => actions.details(),
  };
  const fn = keys[e.key];
  if (fn) { e.preventDefault(); if (e.target.tagName === "BUTTON") e.target.blur(); fn(); }
});

// ---------- mouse
$("rows").addEventListener("click", (e) => {
  const tr = e.target.closest("tr"); if (!tr) return;
  S.cursorId = tr.dataset.id; drawCursor(false);
  if (e.target.closest("td.mark")) actions.toggleMarkOnly(tr.dataset.id);
});
actions.toggleMarkOnly = (id) => { S.marked.has(id) ? S.marked.delete(id) : S.marked.add(id); refreshMarks(); };
$("rows").addEventListener("dblclick", (e) => {
  const tr = e.target.closest("tr"); if (tr && !e.target.closest("td.mark")) openDetail(tr.dataset.id);
});
document.querySelectorAll("thead th.sortable").forEach((th) => th.addEventListener("click", () => {
  const k = th.dataset.sort;
  if (k === "newest") {  // Updated: newest first, click again for oldest (the TUI's two sorts)
    S.sort = S.sort === "newest" ? "oldest" : "newest"; S.reverse = false;
  } else if (k === S.sort) {
    S.reverse = !S.reverse;
  } else {
    S.sort = k; S.reverse = false;
  }
  render(); drawCursor();
}));
$("b-sort").onclick = actions.cycleSort;
function openHelp() { openModal("help"); $("help-close").focus(); }
$("b-help").onclick = openHelp;
$("help-close").onclick = closeModal;

// ---------- theme: auto (system) → light → dark
const THEMES = {auto: "◐ Auto", light: "☀ Light", dark: "☾ Dark"};
function currentTheme() { return document.documentElement.dataset.theme || "auto"; }
function setTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  $("b-theme").textContent = THEMES[t];
  try { t === "auto" ? localStorage.removeItem("asana-inbox.theme") : localStorage.setItem("asana-inbox.theme", t); } catch (e) {}
}
$("b-theme").onclick = () => { const k = Object.keys(THEMES); setTheme(k[(k.indexOf(currentTheme()) + 1) % k.length]); };
setTheme(currentTheme());
$("b-markre").onclick = () => actions.regex("mark");
$("b-unmarkre").onclick = () => actions.regex("unmark");
$("b-project").onclick = actions.toggleProject;
$("b-done").onclick = actions.toggleCompleted;
$("b-all").onclick = actions.toggleAll;
$("b-wrap").onclick = actions.toggleShorten;
$("b-reload").onclick = actions.reload;
$("b-archive").onclick = actions.archive;

// ---------- start
render();
api("/api/config").then((c) => {
  S.cfg = c;
  $("fakebadge").hidden = !c.fake;
  // Optional logo and favicon (--logo-url / --favicon-url); the CSP allows only those origins.
  if (c.logo_url) for (const id of ["logo", "login-logo"]) { $(id).src = c.logo_url; $(id).hidden = false; }
  if (c.favicon_url) document.head.append(el("link", {rel: "icon", href: c.favicon_url}));
  if (!c.logged_in) { showLogin(); return; }
  showUser(); render(); connect();
}).catch((e) => { toast(`Can't reach the server: ${e.message}`, "error"); });
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fake", action="store_true", help="use generated data, no Asana")
    ap.add_argument("--workspace", default=os.environ.get("ASANA_WORKSPACE"),
                    help="workspace gid (env ASANA_WORKSPACE; default: your only workspace)")
    ap.add_argument("-n", type=int, default=0,
                    help="notifications to scan (0 = all); with --fake, the number of items (default 60)")
    ap.add_argument("--ignore-project", action="append",
                    default=[p for p in os.environ.get("ASANA_IGNORE_PROJECTS", "").split(",") if p],
                    help="don't group by this project if the task has others (repeatable; "
                         "env ASANA_IGNORE_PROJECTS, comma-separated)")
    ap.add_argument("--host", default=os.environ.get("ASANA_HOST", "127.0.0.1"),
                    help="address to bind (env ASANA_HOST; default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("ASANA_PORT", 8765)),
                    help="port (env ASANA_PORT; default 8765)")
    ap.add_argument("--allowed-host", action="append",
                    default=[h for h in os.environ.get("ASANA_ALLOWED_HOSTS", "").split(",") if h],
                    help="extra host name the page may be served under, e.g. inbox.example.com "
                         "behind a proxy (repeatable; env ASANA_ALLOWED_HOSTS, comma-separated). "
                         "localhost and the OAuth redirect URI's host are always allowed")
    ap.add_argument("--logo-url", default=os.environ.get("ASANA_INBOX_LOGO_URL"),
                    help="image shown next to the title (http(s):// or data:image/ URL; "
                         "env ASANA_INBOX_LOGO_URL; default: none)")
    ap.add_argument("--favicon-url", default=os.environ.get("ASANA_INBOX_FAVICON_URL"),
                    help="browser tab icon (env ASANA_INBOX_FAVICON_URL; default: the logo)")
    ap.add_argument("--img-src", default=os.environ.get("ASANA_INBOX_IMG_SRC"),
                    help="Content-Security-Policy img-src sources, space-separated, used instead of "
                         "the default ('self' plus the logo/favicon origins), e.g. "
                         "\"'self' https://cdn.example.com\" (env ASANA_INBOX_IMG_SRC)")
    oa = ap.add_argument_group("OAuth (on when a client id and secret are configured)")
    oa.add_argument("--client-id", help="Asana app client id (env ASANA_CLIENT_ID, or oauth-client.json)")
    oa.add_argument("--client-secret", help="client secret; prefer env ASANA_CLIENT_SECRET or "
                    "~/.config/asana-inbox/oauth-client.json (chmod 600)")
    oa.add_argument("--redirect-uri", default=os.environ.get("ASANA_REDIRECT_URI"),
                    help="must match the app's registered redirect URI exactly "
                         "(env ASANA_REDIRECT_URI; default http://localhost:PORT/auth/callback)")
    oa.add_argument("--scopes", default=os.environ.get("ASANA_SCOPES") or DEFAULT_SCOPES,
                    help="'full' for the app's full permissions (default; needed for message text), "
                         f"or space-separated scopes, e.g. '{NARROW_SCOPES}' (env ASANA_SCOPES)")
    args = ap.parse_args()
    ignore = set(args.ignore_project)
    allowed = {"127.0.0.1", "localhost", "::1", args.host, *args.allowed_host}
    logo_url = image_url(args.logo_url, "--logo-url / ASANA_INBOX_LOGO_URL")
    favicon_url = image_url(args.favicon_url, "--favicon-url / ASANA_INBOX_FAVICON_URL") or logo_url
    images = {"logo_url": logo_url, "favicon_url": favicon_url,
              "img_src": img_src_override(args.img_src)}
    shown = f"[{args.host}]" if ":" in args.host else args.host
    url = f"http://{shown}:{args.port}/"

    client_id, client_secret = (None, None) if args.fake else load_client(args.client_id,
                                                                         args.client_secret)
    if args.fake:
        # Rows first, details trickling in, like the API backend.
        backend: Backend = FakeBackend(n=args.n or 60, fill_delay=0.35, fill_batch=6, list_delay=0.3)
        app = create_app(mode="fake", allowed_hosts=allowed, service=InboxService(backend), **images)
        note = "  (fake data)"
    elif client_id and client_secret:
        redirect_uri = args.redirect_uri or f"http://localhost:{args.port}/auth/callback"
        parts = urlsplit(redirect_uri)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            ap.error(f"bad --redirect-uri {redirect_uri!r}")
        allowed.add(parts.hostname)
        oauth = OAuthClient(OAuthConfig(client_id, client_secret, redirect_uri, args.scopes))

        def make_backend(tokens: TokenSet) -> Backend:
            return ApiBackend(args.workspace, args.n, ignore, token=tokens.access_token,
                              refresh=tokens.refresh_rejected)

        app = create_app(mode="oauth", allowed_hosts=allowed, oauth=oauth, make_backend=make_backend,
                         **images)
        url = f"{parts.scheme}://{parts.netloc}/"
        note = f"  (OAuth; redirect URI {redirect_uri})"
    elif client_id or client_secret:
        raise SystemExit("OAuth needs both a client id and a client secret "
                         "(ASANA_CLIENT_ID / ASANA_CLIENT_SECRET)")
    else:
        try:
            api_backend = ApiBackend(args.workspace, args.n, ignore)
        except RuntimeError as e:  # ApiError: no token, bad token, several workspaces
            raise SystemExit(str(e)) from None
        app = create_app(mode="token", allowed_hosts=allowed, service=InboxService(api_backend),
                         user=api_backend.api.me_name, **images)
        note = "  (personal access token)"
    print(f"Asana Inbox web UI on {url}{note}")
    try:
        Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning",
                              timeout_graceful_shutdown=1)).run()
    except KeyboardInterrupt:  # uvicorn re-raises Ctrl-C once it has shut down
        pass


if __name__ == "__main__":
    main()
