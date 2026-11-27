#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""Asana Inbox via the public REST API (notifications endpoints).

    ./asana_api.py inbox [--details] [-n N]      # unread items, grouped per task
    ./asana_api.py archive ID... [--yes]         # mark an item's notifications read (dry run without --yes)
    ./asana_api.py raw [-n N]                    # raw notification JSON, for debugging

Auth: a personal access token (https://app.asana.com/0/my-apps) in ASANA_TOKEN or in
~/.config/asana-inbox/token (chmod 600). Workspace: --workspace / ASANA_WORKSPACE, needed only
if your account has more than one.

The API lists notifications one by one (no threads) with only id/name/type of what they are
about, so this module groups unread notifications per task and fetches task details (projects,
due date, assignee, completion, description) separately, in batches, via `ApiInbox.details`.
"""

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

import httpx

API = "https://app.asana.com/api/1.0"
APP = "https://app.asana.com"
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "asana-inbox"
TOKEN_FILE = CONFIG / "token"

NOTIFICATION_FIELDS = ("created_at,created_by.name,is_read,story_resource_subtype,"
                       "work_objects.name,work_objects.resource_type,story.text,story.html_text,"
                       "story.type,story.reaction_summary.emoji_base,story.reaction_summary.count,"
                       "story.reaction_summary.reacted")
TASK_FIELDS = ["name", "completed", "completed_at", "due_on", "due_at", "assignee.name",
               "projects.name", "html_notes"]
# GET /messages/{gid} isn't in the API reference but works (checked 2026-11-27): title, text
# (plain-text body), parents (the team/project it was posted to), likes, followers.
MESSAGE_FIELDS = ["title", "text", "html_notes", "parents.name", "parents.resource_type"]
USER_FIELDS = ["name", "email"]  # for mentions: the name shown, the email on hover
FIELDS = {"tasks": TASK_FIELDS, "messages": MESSAGE_FIELDS, "users": USER_FIELDS}
PAGE_SIZE = 100  # API maximum for notifications
BATCH_SIZE = 10  # API maximum actions per /batch request
MARK_SIZE = 50  # API maximum notification gids per markRead/markUnread
WORKERS = 8  # concurrent /batch requests

# Notification subtype -> why you got it, most important first. The label is the event's "what"
# and a task item's kind is its most important one. Unlisted subtypes: subtype with spaces,
# ranked after "follower" (e.g. "enum custom field changed").
REASONS = {
    "assigned": "assigned",
    "at_mention": "mention",
    "comment_added": "comment",
    "approval_status_changed": "approval",
    "due_date_changed": "due date",
    "marked_complete": "completed",
    "marked_incomplete": "reopened",
    "unassigned": "unassigned",
    "follower_added": "follower",
    "added_to_project": "added to project",
    "story_reaction_added": "reaction",
    "liked": "liked",
}
RANK = {label: i for i, label in enumerate(REASONS.values())}
# Kinds of notifications about several (or no) objects at once, e.g. a daily summary's tasks.
SUMMARY_KINDS = {"daily_summary": "daily summary", "overdue_tasks_reminder": "overdue reminder",
                 "request_shareable_access_story": "access request"}
OTHER_RANK = RANK["follower"] + 0.5


def reason(labels) -> str:
    """The most important of some reason labels (see REASONS)."""
    return min(labels, key=lambda label: RANK.get(label, OTHER_RANK))
ADDED_TO = re.compile(r" added this task to (.+)$")


class ApiError(RuntimeError):
    pass


def load_token(token: str | None = None) -> str:
    token = token or os.environ.get("ASANA_TOKEN")
    if not token and TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
    if not token:
        raise ApiError("No Asana token. Create a personal access token at "
                       f"https://app.asana.com/0/my-apps and put it in ASANA_TOKEN or {TOKEN_FILE}.")
    return token


def ts(iso: str | None) -> float | None:
    """API timestamp ("2026-11-27T12:02:48.233Z") -> epoch seconds."""
    return datetime.fromisoformat(iso).timestamp() if iso else None


class Client:
    """Thin REST client: auth, retries on 429/5xx, pagination, /batch.

    `token` is an access token, or a callable returning the current one (read on every request,
    so an OAuth caller can refresh it). `refresh`, if set, is called once per request on HTTP 401
    with the rejected token; it should get a new token (so `token()` returns it) or raise. Both
    may be called from several threads at once (see `tasks`)."""

    def __init__(self, token: str | Callable[[], str], timeout: float = 30,
                 refresh: Callable[[str], None] | None = None):
        self.token: Callable[[], str] = token if callable(token) else (lambda: token)
        self.refresh = refresh
        self.http = httpx.Client(base_url=API, timeout=timeout, headers={
            "Accept": "application/json", "User-Agent": "asana-inbox"})

    def request(self, method: str, path: str, **kw) -> dict:
        refreshed = False
        attempt = 0
        while True:
            token = self.token()
            try:
                r = self.http.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kw)
            except httpx.TransportError as e:
                if attempt == 4:
                    raise ApiError(f"{method} {path}: {e}") from e
                time.sleep(2 ** attempt)
                attempt += 1
                continue
            if r.status_code == 401 and self.refresh and not refreshed:
                refreshed = True  # once: a second 401 is a real error
                self.refresh(token)
                continue
            if (r.status_code == 429 or r.status_code >= 500) and attempt < 4:
                time.sleep(float(r.headers.get("Retry-After") or 2 ** attempt))
                attempt += 1
                continue
            if r.status_code >= 400:
                raise ApiError(f"{method} {path}: HTTP {r.status_code}: {self._errors(r)}")
            return r.json()

    @staticmethod
    def _errors(r: httpx.Response) -> str:
        try:
            return "; ".join(e.get("message", "?") for e in r.json()["errors"])
        except (ValueError, KeyError, TypeError):
            return r.text[:200]

    def me(self) -> dict:
        return self.request("GET", "/users/me", params={"opt_fields": "name,workspaces.name"})["data"]

    def notifications(self, workspace: str, fields: str = NOTIFICATION_FIELDS) -> Iterator[dict]:
        """All Inbox notifications that aren't archived, newest first (read ones included:
        the API has no unread filter). Offsets expire after ~15 minutes."""
        params = {"workspace": workspace, "limit": PAGE_SIZE, "opt_fields": fields}
        while True:
            body = self.request("GET", "/users/me/notifications", params=params)
            yield from body["data"]
            offset = (body.get("next_page") or {}).get("offset")
            if not offset:
                return
            params["offset"] = offset

    def batch_get(self, requests: list[tuple[str, list[str]]]) -> list[tuple[int, dict]]:
        """GET several (path, fields) in one /batch request: [(status, body)] in the same order."""
        actions = [{"method": "get", "relative_path": p, "options": {"fields": f}} for p, f in requests]
        res = self.request("POST", "/batch", json={"data": {"actions": actions}})["data"]
        return [(r.get("status_code") or 0, r.get("body") or {}) for r in res]

    def tasks(self, gids: list[str], fields: list[str] = TASK_FIELDS) -> Iterator[dict[str, dict | None]]:
        """Fetch tasks; see `objects`. Yields {gid: task or None} per completed batch."""
        for got in self.objects([("tasks", g) for g in gids], {"tasks": fields}):
            yield {gid: obj for (_, gid), obj in got.items()}

    def objects(self, refs: list[tuple[str, str]], fields: dict[str, list[str]] = FIELDS,
                errors: dict[tuple[str, str], str] | None = None
                ) -> Iterator[dict[tuple[str, str], dict | None]]:
        """Fetch (collection, gid) objects, e.g. ("tasks", "123") or ("messages", "456"), in
        /batch requests of 10, several at once. Yields {ref: object, or None if it's gone or not
        visible to you} as each batch completes, in completion order. Why an object is None goes
        into `errors` (if given) before its batch is yielded, e.g. "HTTP 403: Forbidden"."""
        errors = {} if errors is None else errors

        def one(chunk: list[tuple[str, str]]) -> dict[tuple[str, str], dict | None]:
            out: dict[tuple[str, str], dict | None] = {}
            reqs = [(f"/{coll}/{gid}", fields[coll]) for coll, gid in chunk]
            for ref, (path, f), (status, body) in zip(chunk, reqs, self.batch_get(reqs), strict=True):
                if status == 200:
                    out[ref] = body.get("data")
                elif status == 429 or status >= 500:  # retried one by one (request() backs off)
                    try:
                        out[ref] = self.request("GET", path, params={"opt_fields": ",".join(f)})["data"]
                    except ApiError as e:
                        out[ref], errors[ref] = None, str(e)
                else:
                    msgs = "; ".join(e.get("message", "?") for e in body.get("errors") or [])
                    out[ref], errors[ref] = None, f"HTTP {status}" + (f": {msgs}" if msgs else "")
            return out

        chunks = [refs[i:i + BATCH_SIZE] for i in range(0, len(refs), BATCH_SIZE)]
        ex = ThreadPoolExecutor(WORKERS)
        try:
            for fut in as_completed([ex.submit(one, c) for c in chunks]):
                yield fut.result()
        finally:
            ex.shutdown(wait=False, cancel_futures=True)  # consumer stopped early: drop the rest

    def mark_read(self, workspace: str, gids: list[str], read: bool = True) -> list[str]:
        """Mark notifications read (or unread); returns the gids Asana couldn't update."""
        path = f"/users/me/notifications/{'markRead' if read else 'markUnread'}"
        failed: list[str] = []
        for i in range(0, len(gids), MARK_SIZE):
            body = {"data": {"workspace": workspace, "notification_gids": gids[i:i + MARK_SIZE]}}
            failed += self.request("POST", path, json=body)["data"].get("failed_gids") or []
        return failed


# ---------------------------------------------------------------- rich text
#
# Asana's plain `text`/`notes` turn mentions into profile links (/1/<ws>/profile/<id>), and a
# profile id isn't a user gid, so they can't be looked up. The rich-text versions (`html_text`,
# `html_notes`) carry the user gid instead: <a data-asana-type="user" data-asana-gid="…">@Name</a>.
# They become "runs": a list of plain strings, {"user": gid, "text": "@Name"} and
# {"link": href, "text": label}. UIs render runs with the item's `people` ({gid: {name, email}}).

_BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "blockquote", "pre"}


class _RunsParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.runs: list = []
        self.anchor: tuple[dict, list[str]] | None = None  # attrs, text inside <a>

    def add(self, s: str) -> None:
        if self.anchor is not None:
            self.anchor[1].append(s)
        elif self.runs and isinstance(self.runs[-1], str):
            self.runs[-1] += s
        else:
            self.runs.append(s)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.anchor = (dict(attrs), [])
        elif tag == "br":
            self.add("\n")
        elif tag == "li":
            self.add("- ")
        elif tag == "hr":
            self.add("\n———\n")

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            attrs, parts = self.anchor
            self.anchor = None
            label = "".join(parts)
            if attrs.get("data-asana-type") == "user" and attrs.get("data-asana-gid"):
                self.runs.append({"user": attrs["data-asana-gid"], "text": label})
            elif attrs.get("href"):
                self.runs.append({"link": attrs["href"], "text": label or attrs["href"]})
            else:
                self.add(label)
        elif tag in _BLOCKS:
            self.add("\n")

    def handle_data(self, data):
        self.add(data)


def html_runs(html: str | None) -> list:
    """Asana rich text (html_text / html_notes) -> runs (see above)."""
    p = _RunsParser()
    p.feed(html or "")
    p.close()
    runs = [re.sub(r"\n{3,}", "\n\n", r) if isinstance(r, str) else r for r in p.runs]
    if runs and isinstance(runs[0], str):
        runs[0] = runs[0].lstrip()
    if runs and isinstance(runs[-1], str):
        runs[-1] = runs[-1].rstrip()
    return [r for r in runs if r != ""]


def plain_text(runs: list, people: dict | None = None) -> str:
    """Runs as plain text: mentions as @Name, links as "label (href)" unless the label is the href."""
    out = []
    for r in runs:
        if isinstance(r, str):
            out.append(r)
        elif "user" in r:
            name = ((people or {}).get(r["user"]) or {}).get("name")
            out.append(f"@{name}" if name else r["text"])
        else:
            out.append(r["text"] if r["text"] == r["link"] else f"{r['text']} ({r['link']})")
    return "".join(out)


def user_gids(runs: list | None) -> set[str]:
    return {r["user"] for r in runs or [] if isinstance(r, dict) and "user" in r}


def event(n: dict) -> dict:
    """One notification as an event-list entry: {time, who, what, text, rich, story, reactions};
    rich = the story's runs (None if Asana sent no rich text), text = their plain version,
    story = the comment's gid (None for other stories), reactions = [{emoji, count, reacted}]
    (reacted: whether one of them is mine). Skin-tone variants count under their base emoji."""
    sub = n.get("story_resource_subtype") or "?"
    story = n.get("story") or {}
    rich = html_runs(story["html_text"]) if story.get("html_text") else None
    return {
        "time": ts(n.get("created_at")) or 0,
        "who": (n.get("created_by") or {}).get("name"),
        "what": REASONS.get(sub, sub.replace("_", " ")),
        "text": plain_text(rich) if rich is not None else story.get("text") or "",
        "rich": rich,
        "story": story.get("gid") if story.get("type") == "comment" else None,
        "reactions": [{"emoji": r["emoji_base"], "count": r.get("count") or 0, "reacted": bool(r.get("reacted"))}
                      for r in story.get("reaction_summary") or [] if r.get("emoji_base")],
    }


def group(notifs: list[dict], workspace: str) -> list[dict]:
    """Unread notifications -> inbox items, newest activity first. Notifications about a single
    task (or message) share one item, id = its gid; anything else (daily summaries, overdue
    reminders: several or no work objects) is an item of its own, id = the notification gid.

    Task details aren't in notifications: those items get `task_id` and `pending: True`, with
    `projects` guessed from "added this task to X" stories until `ApiInbox.details` fills them."""
    items: dict[str, dict] = {}
    for n in notifs:
        objs = n.get("work_objects") or []
        sub = n.get("story_resource_subtype") or ""
        single = objs[0] if len(objs) == 1 else None
        key = single["gid"] if single else n["gid"]
        it = items.get(key)
        if it is None:
            it = items[key] = {
                "id": key, "task_id": None, "message_id": None, "type": "?", "title": "", "projects": [], "urls": {},
                "url": "", "completed": None, "assignee": None, "mine": False, "due_date": None,
                "due_time": None, "description": "", "description_rich": None, "people": {},
                "notifications": [], "events": [], "subtypes": set(), "updated": 0.0, "pending": False,
            }
            day = (n.get("created_at") or "")[:10]
            if single and single.get("resource_type") == "task":
                it.update(task_id=key, type="task", title=single.get("name") or key, pending=True,
                          url=f"{APP}/1/{workspace}/task/{key}")
            elif single and single.get("resource_type") == "message":
                it.update(message_id=key, type="message", title=single.get("name") or key,
                          pending=True, url=f"{APP}/1/{workspace}/message/{key}")
            elif single:
                it.update(type=single.get("resource_type") or "?", title=single.get("name") or key)
            else:
                kind = SUMMARY_KINDS.get(sub, sub.replace("_", " ") or "?")
                it.update(type=kind, title=f"{kind.capitalize()} for {day}",
                          description="\n".join(f"- {o.get('name')}" for o in objs))
        it["notifications"].append(n["gid"])
        it["events"].append(event(n))
        it["subtypes"].add(sub)
        it["updated"] = max(it["updated"], ts(n.get("created_at")) or 0)
        if m := ADDED_TO.search((n.get("story") or {}).get("text") or ""):
            if m[1] not in it["projects"]:
                it["projects"].append(m[1])
    for it in items.values():
        it["events"].sort(key=lambda e: e["time"])
        if it["type"] == "task":  # kind = why you got it, e.g. "mention" over "comment"
            it["type"] = reason(e["what"] for e in it["events"])
        it["subtypes"] = sorted(it["subtypes"])
    return sorted(items.values(), key=lambda it: -it["updated"])


class ApiInbox:
    """Inbox as plain dicts, for the TUI or the web UI (see inbox_core.ApiBackend)."""

    def __init__(self, token: str | Callable[[], str] | None = None, workspace: str | None = None,
                 n: int = 0, refresh: Callable[[str], None] | None = None):
        """`token`: a personal access token (default: ASANA_TOKEN or the token file), or a
        callable returning the current OAuth access token; `refresh`: see `Client`."""
        self.client = Client(token if callable(token) else load_token(token))
        self.client.refresh = refresh
        me = self.client.me()
        self.me: str = me["gid"]
        self.me_name: str = me.get("name") or ""
        workspaces = me.get("workspaces") or []
        if workspace:
            self.workspace = workspace
        elif len(workspaces) == 1:
            self.workspace = workspaces[0]["gid"]
        else:
            raise ApiError("Pick a workspace with --workspace or ASANA_WORKSPACE: " + ", ".join(
                f"{w['gid']} ({w.get('name')})" for w in workspaces))
        self.n = n  # scan at most this many notifications (0 = all)
        self.notifications: dict[str, list[str]] = {}  # item id -> unread notification gids
        self.users: dict[str, dict] = {}  # user gid -> {name, email}, for mentions; kept across loads

    def list_items(self, log: Callable[[str], None] | None = None) -> list[dict]:
        """Unread items, without task details (see `details`). One request per 100 notifications."""
        unread, seen = [], 0
        for n in self.client.notifications(self.workspace):
            seen += 1
            if not n.get("is_read"):
                unread.append(n)
            if log and seen % PAGE_SIZE == 0:
                log(f"loading… {seen} notifications, {len(unread)} unread")
            if self.n and seen >= self.n:
                break
        items = group(unread, self.workspace)
        self.notifications = {it["id"]: it["notifications"] for it in items}
        return items

    def details(self, items: list[dict]) -> Iterator[dict[str, dict]]:
        """Details per completed batch: {item id: fields}, where fields update the item.
        Tasks: projects, urls, completed, assignee, mine, due_date, due_time, description(_rich);
        messages: projects = where it was posted, description(_rich) = the message; both with
        pending=False, plus inaccessible=True if it can't be read. Also `people` ({gid: {name,
        email}}) for users mentioned in an item's events or description, as they're looked up."""
        by_ref: dict[tuple[str, str], list[str]] = {}
        mentioned: dict[str, set[str]] = {}  # item id -> user gids it mentions
        for it in items:
            if it.get("task_id"):
                by_ref.setdefault(("tasks", it["task_id"]), []).append(it["id"])
            elif it.get("message_id"):
                by_ref.setdefault(("messages", it["message_id"]), []).append(it["id"])
            for e in it.get("events") or []:
                mentioned.setdefault(it["id"], set()).update(user_gids(e.get("rich")))
        errors: dict[tuple[str, str], str] = {}

        def people(iid: str) -> dict:
            return {g: self.users[g] for g in sorted(mentioned.get(iid, ())) if g in self.users}

        def fetch(refs: list[tuple[str, str]]) -> Iterator[dict[str, dict]]:
            for got in self.client.objects(refs, errors=errors):
                out: dict[str, dict] = {}
                for (coll, gid), obj in got.items():
                    if coll == "users":
                        self.users[gid] = {"name": (obj or {}).get("name"), "email": (obj or {}).get("email")}
                        for iid, gids in mentioned.items():
                            if gid in gids:
                                out.setdefault(iid, {})
                        continue
                    fields = (self.task_fields(gid, obj) if coll == "tasks"
                              else self.message_fields(obj, errors.get((coll, gid))))
                    for iid in by_ref[(coll, gid)]:
                        out[iid] = dict(fields)
                        mentioned.setdefault(iid, set()).update(user_gids(fields.get("description_rich")))
                for iid, fields in out.items():
                    fields["people"] = people(iid)
                yield out

        def unknown_users() -> list[tuple[str, str]]:
            return [("users", g) for g in sorted(set().union(*mentioned.values()) - set(self.users))]

        # Users mentioned in events can go with the first round; those only in descriptions
        # are known after it, so they get a second round.
        yield from fetch(list(by_ref) + unknown_users())
        if more := unknown_users():
            yield from fetch(more)

    @staticmethod
    def message_fields(msg: dict | None, error: str | None = None) -> dict:
        if msg is None:
            # Shown where the body would be, so it's clear why it's missing (e.g. an OAuth token
            # limited to narrower scopes than full permissions).
            hint = (" Messages need a token with full permissions: a personal access token, or "
                    "OAuth without a narrower --scopes." if "Full permissions" in (error or "") else "")
            return {"pending": False, "inaccessible": True,
                    "description": f"(Couldn't load the message: {error or 'not found'}.{hint})"}
        rich = html_runs(msg["html_notes"]) if msg.get("html_notes") else None
        return {
            "pending": False,
            "projects": [p.get("name") or p["gid"] for p in msg.get("parents") or [] if p.get("gid")],
            "description": plain_text(rich) if rich is not None else msg.get("text") or "",
            "description_rich": rich,
        }

    def task_fields(self, gid: str, task: dict | None) -> dict:
        if task is None:
            return {"pending": False, "inaccessible": True}
        projects = [p for p in task.get("projects") or [] if p.get("gid")]
        names = [p.get("name") or p["gid"] for p in projects]
        assignee = task.get("assignee") or {}
        rich = html_runs(task["html_notes"]) if task.get("html_notes") else None
        return {
            "pending": False,
            "projects": names,
            # One link per project, so a UI can link via the project it groups by.
            "urls": {name: f"{APP}/1/{self.workspace}/project/{p['gid']}/task/{gid}"
                     for name, p in zip(names, projects)},
            "completed": (ts(task.get("completed_at")) or time.time()) if task.get("completed") else None,
            "assignee": assignee.get("name"),
            "mine": assignee.get("gid") == self.me,
            "due_time": ts(task.get("due_at")),
            "due_date": None if task.get("due_at") else task.get("due_on"),
            "description": plain_text(rich) if rich is not None else "",
            "description_rich": rich,
        }

    def create_task(self, name: str, due_on: str, notes: str = "") -> dict:
        """Create a task assigned to you, due on `due_on` (YYYY-MM-DD), in your My Tasks (no
        project). Returns it with gid, name, due_on and permalink_url."""
        data = {"name": name, "assignee": self.me, "due_on": due_on, "workspace": self.workspace}
        if notes:
            data["notes"] = notes
        return self.client.request("POST", "/tasks", json={"data": data},
                                   params={"opt_fields": "name,due_on,permalink_url"})["data"]

    def react(self, story: str, emoji: str, reacted: bool) -> None:
        """Add (or remove) my `emoji` reaction to a comment. The `reactions` field of PUT /stories isn't
        in the API reference yet; Asana staff described it in the forum (2026-10-26)."""
        body = {"data": {"reactions": [{"emoji": emoji, "reacted": reacted}]}}
        self.client.request("PUT", f"/stories/{story}", json=body)

    def archive(self, ids: list[str]) -> list[str]:
        """Mark all unread notifications of these items read: the same as archiving them in the
        TUI's unread view. Returns notification gids Asana couldn't update."""
        gids = [g for i in ids for g in self.notifications.get(i, [])]
        return self.client.mark_read(self.workspace, gids) if gids else []


# ---------------------------------------------------------------- CLI


def print_items(items: list[dict]) -> None:
    for it in items:
        when = datetime.fromtimestamp(it["updated"]).strftime("%Y-%m-%d %H:%M")
        print(f"{it['id']:<18} {when}  n={len(it['notifications']):<2} {it['type']:<17} {it['title']}")
        extra = []
        if it["projects"]:
            extra.append("projects: " + ", ".join(it["projects"]))
        if it.get("assignee"):
            extra.append("assignee: " + it["assignee"] + (" (me)" if it["mine"] else ""))
        if it.get("due_date") or it.get("due_time"):
            extra.append("due: " + (it["due_date"] or datetime.fromtimestamp(it["due_time"]).isoformat()))
        if it.get("completed"):
            extra.append("done")
        if it.get("inaccessible"):
            extra.append("task not accessible")
        if extra:
            print(f"{'':18} {'; '.join(extra)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", default=os.environ.get("ASANA_WORKSPACE"),
                    help="workspace gid (env ASANA_WORKSPACE; default: your only workspace)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    inbox = sub.add_parser("inbox", help="list unread items")
    inbox.add_argument("--details", action="store_true", help="also fetch task details")
    arch = sub.add_parser("archive", help="mark items' notifications read (dry run unless --yes)")
    arch.add_argument("id", nargs="+", help="item ids as shown by `inbox`")
    arch.add_argument("--yes", action="store_true", help="actually mark read")
    raw = sub.add_parser("raw", help="print raw notification JSON (contains internal data)")
    for p in (inbox, arch, raw):
        p.add_argument("-n", type=int, default=0, help="scan at most N notifications (0 = all)")
    args = ap.parse_args()

    ibx = ApiInbox(workspace=args.workspace, n=args.n)
    if args.cmd == "raw":
        for i, n in enumerate(ibx.client.notifications(ibx.workspace)):
            if args.n and i >= args.n:
                break
            print(json.dumps(n))
        return
    started = time.monotonic()
    items = ibx.list_items(log=lambda s: print(s, file=sys.stderr))
    print(f"{len(items)} items in {time.monotonic() - started:.1f}s", file=sys.stderr)
    if args.cmd == "inbox":
        if args.details:
            started, by_id = time.monotonic(), {it["id"]: it for it in items}
            for update in ibx.details(items):
                for iid, fields in update.items():
                    by_id[iid].update(fields)
            print(f"details for {sum(1 for it in items if it['task_id'])} task items in "
                  f"{time.monotonic() - started:.1f}s", file=sys.stderr)
        print_items(items)
        return
    chosen = [it for it in items if it["id"] in set(args.id)]
    if missing := set(args.id) - {it["id"] for it in chosen}:
        print(f"not among the unread items: {sorted(missing)}", file=sys.stderr)
    print_items(chosen)
    if not chosen:
        return
    if not args.yes:
        print(f"dry run: would mark {sum(len(it['notifications']) for it in chosen)} notifications "
              "read; pass --yes to do it", file=sys.stderr)
        return
    failed = ibx.archive([it["id"] for it in chosen])
    print("done" + (f"; Asana couldn't update {failed}" if failed else ""), file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except ApiError as e:
        sys.exit(str(e))
