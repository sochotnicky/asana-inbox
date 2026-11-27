"""UI-agnostic inbox model shared by the TUI (asana_inbox.py) and the web UI (asana_inbox_web.py).

No textual/rich imports here: just the item type, backends (API and fake), the dict -> item
mapping, sorting and regex matching.

A backend has `list_items(progress=None) -> list[InboxItem]` and `archive(ids)`. Optionally
`fill(items)` yields {item id: {InboxItem field: value}} as details arrive, for items returned
with pending=True; it runs in a worker thread after list_items. Optionally `react(story,
reacted)` adds or removes your 👍 on a comment (an event's "story").
"""

import dataclasses
import random
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol

from asana_api import reason


@dataclass
class InboxItem:
    id: str
    title: str
    project: str
    kind: str
    updated: datetime
    projects: list[str] = field(default_factory=list)  # all projects; `project` is the grouping one
    completed: datetime | None = None
    url: str = ""
    assignee: str | None = None
    mine: bool = False  # assigned to the current user
    due: datetime | None = None  # local time; midnight for date-only dues
    due_has_time: bool = False
    description: str = ""
    # Rich versions (asana_api runs: strings, {"user": gid, "text"}, {"link": href, "text"}), or
    # None for plain text only; events may carry "rich" too. people: {user gid: {name, email}}.
    description_rich: list | None = None
    people: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)  # {time, who, what, text, rich?, story?, reacted?}
    pending: bool = False  # details (projects, due, assignee, …) still loading

    def due_label(self) -> str:
        if self.due is None:
            return ""
        return self.due.strftime("%Y-%m-%d %H:%M" if self.due_has_time else "%Y-%m-%d")

    def due_state(self, now: datetime | None = None) -> str:
        """"" (no due / not urgent), "done", "overdue" or "today"."""
        if self.due is None:
            return ""
        if self.completed:
            return "done"
        now = now or datetime.now()
        overdue = self.due < now if self.due_has_time else self.due.date() < now.date()
        if overdue:
            return "overdue"
        if self.due.date() == now.date():
            return "today"
        return ""


class Backend(Protocol):
    def list_items(self, progress=None) -> list[InboxItem]: ...
    def archive(self, ids: list[str]) -> None: ...
    # Optional: `create_task(name, due: date, notes="") -> str` creates a task assigned to you and
    # returns its link (follow-ups, `f`/`F`). Backends without it can't create follow-ups.


def followup_name(it: InboxItem) -> str:
    """Default title of a follow-up task for an item; its link goes in the task's description."""
    return f"Follow up on {it.title}"


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_due(text: str, today: date | None = None) -> date:
    """A follow-up's due date from what the user typed: YYYY-MM-DD, "today", "tomorrow", "+N"
    (days from today), or a weekday name or its first three letters (the next one, not today).
    Raises ValueError with a readable message otherwise."""
    today = today or date.today()
    s = text.strip().lower()
    if s in ("", "today"):
        return today
    if s in ("tomorrow", "tmr"):
        return today + timedelta(days=1)
    if s.startswith("+") and s[1:].isdigit():
        return today + timedelta(days=int(s[1:]))
    for i, day in enumerate(WEEKDAYS):
        if len(s) >= 3 and day.startswith(s):
            return today + timedelta(days=(i - today.weekday() - 1) % 7 + 1)
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"Can't read due date {text!r}: use YYYY-MM-DD, today, tomorrow, +N "
                         "or a weekday") from None


def match_text(it: InboxItem) -> str:
    """What the filter / mark regexes search: project, title, other projects, kind, assignee,
    plus the words "done" (completed) and "mine" (assigned to you)."""
    done = "done" if it.completed else ""
    mine = "mine" if it.mine else ""
    return (f"{it.project} {it.title} {' '.join(it.projects)} {it.kind} {done} "
            f"{it.assignee or ''} {mine}")


SORTS = {
    "project": lambda it: (it.project.lower(), it.title.lower()),
    "title": lambda it: (it.title.lower(), it.project.lower()),
    "newest": lambda it: -it.updated.timestamp(),
    "oldest": lambda it: it.updated.timestamp(),
    # soonest/most overdue first; no due date last, then newest activity
    "due": lambda it: (it.due is None, it.due or datetime.max, -it.updated.timestamp()),
}

PROJECT_WIDTH = 30  # max chars for the Project column when shortening
TITLE_WIDTH = 72  # max chars for the Title column when shortening


def ellipsize(text: str, width: int = PROJECT_WIDTH) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"



PENDING_PROJECT = "…"  # Project column while details load


def primary_project(projects: list[str], kind: str, ignore: set[str], pending: bool = False) -> str:
    # Asana lists broad memberships (company goals, planning boards) first; the most
    # specific project tends to be last.
    useful = [p for p in projects if p not in ignore]
    if useful or projects:
        return (useful or projects)[-1]
    if pending:
        return PENDING_PROJECT
    return {"daily summary": "(daily summaries)",
            "overdue reminder": "(overdue reminders)", "message": "(messages)"}.get(
        kind, "(no project)")


def item_fields(d: dict, kind: str, ignore: set[str]) -> dict:
    """InboxItem fields from a backend dict (an asana_api item, or a partial update
    from asana_api details: then only the keys it has, plus the derived project/url)."""
    out: dict = {}
    if "projects" in d:
        project = primary_project(d["projects"], kind, ignore, d.get("pending", False))
        out.update(projects=d["projects"], project=project,
                   url=d.get("urls", {}).get(project) or d.get("url", ""))
        if d.get("inaccessible") and project == "(no project)":
            out["project"] = "(no access)"
    if "completed" in d:
        out["completed"] = datetime.fromtimestamp(d["completed"]) if d["completed"] else None
    if "due_date" in d or "due_time" in d:
        out["due"] = (datetime.fromtimestamp(d["due_time"]) if d.get("due_time")
                      else datetime.combine(date.fromisoformat(d["due_date"]), datetime.min.time())
                      if d.get("due_date") else None)
        out["due_has_time"] = bool(d.get("due_time"))
    for key in ("assignee", "mine", "description", "description_rich", "people", "events", "pending"):
        if key in d:
            out[key] = d[key]
    return out


def to_item(d: dict, ignore: set[str]) -> InboxItem:
    kind = d["type"] or "?"
    return InboxItem(id=d["id"], title=d["title"], kind=kind,
                     updated=datetime.fromtimestamp(d["updated"]),
                     **item_fields({"projects": [], **d}, kind, ignore))


class ApiBackend:
    """Real Asana inbox via the public REST API (asana_api.py). Rows appear as soon as the
    notifications are listed; task details arrive afterwards through `fill`."""

    def __init__(self, workspace: str | None, n: int, ignore: set[str], token=None, refresh=None):
        """`token`/`refresh`: passed to ApiInbox (default: the personal access token)."""
        from asana_api import ApiInbox  # needs httpx; only when used

        self.api = ApiInbox(token=token, workspace=workspace, n=n, refresh=refresh)
        self.ignore = ignore
        self.dicts: dict[str, dict] = {}  # item id -> asana_api item, as listed

    def list_items(self, progress=None) -> list[InboxItem]:
        self.dicts = {d["id"]: d for d in self.api.list_items(progress)}
        return [to_item(d, self.ignore) for d in self.dicts.values()]

    def fill(self, items: list[InboxItem]):
        listed = dict(self.dicts)  # a reload may replace self.dicts meanwhile
        # All items, not just pending ones: details also looks up people mentioned in events.
        dicts = [listed[it.id] for it in items if it.id in listed]
        for update in self.api.details(dicts):
            out = {}
            for iid, fields in update.items():
                # Accumulated over the listed item, so an unreadable task keeps its guessed
                # projects and link, and a later people-only update doesn't undo earlier ones.
                d = listed[iid] = {**listed[iid], **fields}
                out[iid] = item_fields(d, d["type"] or "?", self.ignore)
            yield out

    def create_task(self, name: str, due: date, notes: str = "") -> str:
        task = self.api.create_task(name, due.isoformat(), notes)
        return task.get("permalink_url") or f"https://app.asana.com/0/0/{task['gid']}"

    def react(self, story: str, reacted: bool) -> None:
        self.api.react(story, reacted)

    def archive(self, ids: list[str]) -> None:
        if failed := self.api.archive(ids):
            raise RuntimeError(f"Asana couldn't mark {len(failed)} notifications read")


# Fields that only arrive with task details (blanked while a fake item is "pending").
DETAIL_FIELDS = ("project", "projects", "completed", "url", "assignee", "mine", "due",
                 "due_has_time", "description")


class FakeBackend:
    """Generated inbox for trying the UI (--fake) and for docs/screenshots.py.

    With `fill_delay` (seconds), list_items returns task rows with their details still pending
    and `fill` delivers them in batches of `fill_batch`, sleeping `fill_delay` before each, like
    the API backend. Without it, everything is there at once. Archive drops items in memory."""

    def __init__(self, n: int = 60, fill_delay: float | None = None, fill_batch: int = 10,
                 list_delay: float = 0.0):
        self.fill_delay, self.fill_batch, self.list_delay = fill_delay, fill_batch, list_delay
        rnd = random.Random(1)
        project_names = ["Platform", "Website", "Mobile App", "Customer Support",
                    # longer than PROJECT_WIDTH, to exercise shortening (`w`)
                    "Infrastructure Reliability & Observability Initiatives"]
        verbs = ["Fix", "Investigate", "Rotate", "Upgrade", "Review"]
        things = ["login timeout", "flaky CI job", "database migration", "TLS certificate", "search index"]
        # Some titles longer than TITLE_WIDTH, to exercise shortening (`w`).
        long_tails = [
            ": follow up from the incident review, confirm alert thresholds and update the runbook",
            " across all regions before the maintenance window, then notify the on-call rotation",
            " (blocked on vendor support ticket; see thread for the latest workaround and ETA)",
        ]
        # Broad boards a task is often also in, shown in the "Also in" column.
        boards = ["Company Goals", "Team Roadmap", "Engineering Backlog", "Q4 Planning"]
        people = ["Alice", "Bob", "Carol", "Dan"]
        comments = [
            "Reproduced on staging, looks like a timeout in the auth proxy.",
            "Can we get this done before the release freeze?",
            "PR is up, would appreciate a review.",
            "Moved the due date, waiting on the vendor.",
            "Done on my side, please verify.",
        ]
        # Team/project messages: title, where it was posted, body.
        messages = [
            ("December quick wins", "Engineering",
             "Hi all,\n\nA few small things we can ship before the holidays:\n\n"
             "- retire the old staging cluster\n- make the flaky CI job non-blocking\n"
             "- document the on-call handover\n\nReply here if you want to pick one up."),
            ("New on-call rotation starts Monday", "Platform",
             "The new rotation is live from Monday. Check the schedule and swap shifts "
             "directly with each other; ping me if anything looks off."),
            ("Incidents now show on dashboards", "Infrastructure Reliability & Observability Initiatives",
             "Open incidents are now annotated on all service dashboards, so you can see at a glance "
             "whether a spike lines up with a known incident."),
        ]
        descriptions = [
            "Users report intermittent failures. Collect logs from the last week, find the common "
            "pattern and propose a fix.\n\nAcceptance: no failures in 7 days of monitoring.",
            "Follow-up from the last incident review.\n\n- update the runbook\n- add an alert\n"
            "- share findings in the weekly sync",
            "",
        ]
        now = datetime.now()

        def memberships(project: str) -> list[str]:
            return [*rnd.sample(boards, rnd.choice([0, 0, 1, 2, 3])), project]

        def events(updated: datetime) -> list[dict]:
            count = rnd.randint(1, 4)
            out, t = [], updated - timedelta(hours=rnd.randint(4, 30) * count)
            for _ in range(count):
                t += timedelta(hours=rnd.randint(2, 30), minutes=rnd.randint(0, 59))
                what = rnd.choice(["comment", "comment", "mention", "assigned", "due date", "follower",
                                   "reaction"])
                out.append({"time": min(t, updated).timestamp(), "who": rnd.choice(people),
                            "what": what, "text": rnd.choice(comments) if what in ("comment", "mention") else ""})
                if what == "mention":  # rich text with a mention, like asana_api; no extra rnd draws
                    gid = f"u{len(out) % len(people)}"
                    first = people[len(out) % len(people)]
                    out[-1]["rich"] = [{"user": gid, "text": f"@{first}"}, " " + out[-1]["text"]]
                    out[-1]["text"] = f"@{first} " + out[-1]["text"]
            return out

        # Mentioned people: full name shown, email on hover.
        directory = {f"u{k}": {"name": f"{p} Example", "email": f"{p.lower()}@example.com"}
                     for k, p in enumerate(people)}

        self.items: dict[str, InboxItem] = {}
        self.created: list[dict] = []  # follow-up tasks "created" in the demo
        self.reactions: dict[str, bool] = {}  # story -> reacted, set in the demo
        self.detailed: set[str] = set()  # ids whose details asana_api would fetch after listing
        for i in range(n):
            kind = rnd.choices(["task", "added to project", "message", "daily summary"],
                               weights=[14, 4, 1, 1])[0]
            updated = now - timedelta(hours=rnd.randint(1, 24 * 60))
            if kind == "daily summary":
                title, project, projects = f"Daily summary for {updated.date()}", "(daily summaries)", []
            else:
                title = f"{rnd.choice(verbs)} {rnd.choice(things)}" + (
                    rnd.choice(long_tails) if rnd.random() < 0.25 else "")
                project = rnd.choice(project_names)
                projects = memberships(project)
            task = kind in ("task", "added to project")
            mine = task and rnd.random() < 0.3
            item_events = events(updated) if kind != "daily summary" else [
                {"time": updated.timestamp(), "who": None, "what": "daily summary", "text": ""}]
            url = f"https://app.asana.com/1/0/project/0/task/{1000 + i}"
            description = rnd.choice(descriptions) if task else ""
            if kind == "added to project":  # only that happened; kind stays
                item_events = [{"time": updated.timestamp(), "who": people[i % len(people)],
                                "what": "added to project", "text": f"added this task to {project}"}]
            elif kind == "task":  # why you got it, as asana_api derives it
                kind = reason(e["what"] for e in item_events)
            if kind == "message":  # no extra rnd draws: keeps the other fake items unchanged
                title, project, description = messages[i % len(messages)]
                projects, url = [project], f"https://app.asana.com/1/0/message/{1000 + i}"
                author = people[i % len(people)]
                item_events = [{"time": updated.timestamp() - 3600, "who": author, "what": "created",
                                "text": f"{author} sent a message"},
                               *[e for e in item_events if e["what"] == "comment"]]
            for k, e in enumerate(item_events):
                if e["what"] in ("comment", "mention"):
                    e["story"], e["reacted"] = f"{i}.{k}", (i + k) % 5 == 0
            if task or kind == "message":
                self.detailed.add(str(i))
            self.items[str(i)] = InboxItem(
                id=str(i),
                title=title,
                project=project,
                projects=projects,
                kind=kind,
                updated=updated,
                completed=updated if task and rnd.random() < 0.2 else None,
                url=url,
                assignee="Me" if mine else (rnd.choice([None, *people]) if task else None),
                mine=mine,
                description=description,
                events=item_events,
                people={r["user"]: directory[r["user"]] for e in item_events for r in e.get("rich") or []
                        if isinstance(r, dict) and "user" in r},
                due=(now.replace(hour=0, minute=0, second=0, microsecond=0)
                     + timedelta(days=rnd.randint(-10, 20))) if task and rnd.random() < 0.5 else None,
            )

    def _has_details(self, it: InboxItem) -> bool:
        return it.id in self.detailed

    def list_items(self, progress=None) -> list[InboxItem]:
        if self.fill_delay is None:
            return list(self.items.values())
        if self.list_delay:
            if progress:
                progress("loading…")
            time.sleep(self.list_delay)
        # Copies with the details blanked, as asana_api lists tasks before their details.
        return [dataclasses.replace(it, project=PENDING_PROJECT, projects=[], completed=None, assignee=None,
                                    mine=False, due=None, due_has_time=False, description="",
                                    pending=True)
                if self._has_details(it) else dataclasses.replace(it)
                for it in self.items.values()]

    def fill(self, items: list[InboxItem]):
        ids = [it.id for it in items if it.pending and it.id in self.items]
        for i in range(0, len(ids), self.fill_batch):
            time.sleep(self.fill_delay or 0)
            out = {}
            for iid in ids[i:i + self.fill_batch]:
                if (real := self.items.get(iid)) is not None:  # archived meanwhile: skip
                    out[iid] = {k: getattr(real, k) for k in DETAIL_FIELDS} | {"pending": False}
            yield out

    def create_task(self, name: str, due: date, notes: str = "") -> str:
        """Remembered in `created` only: the demo never talks to Asana."""
        self.created.append({"name": name, "due": due, "notes": notes})
        return f"https://app.asana.com/1/0/task/{9000 + len(self.created)}"

    def react(self, story: str, reacted: bool) -> None:
        self.reactions[story] = reacted

    def archive(self, ids: list[str]) -> None:
        for i in ids:
            self.items.pop(i, None)
