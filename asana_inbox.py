#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["textual>=0.80", "httpx>=0.27"]
# ///
"""Bulk-archive Asana Inbox items from a TUI.

    ./asana_inbox.py                  # token in ASANA_TOKEN or ~/.config/asana-inbox/token
    ./asana_inbox.py --fake           # generated data, no Asana

Uses Asana's REST API with a personal access token (see `./asana_api.py --help`).
"""

import argparse
import json
import os
import re
import webbrowser
from datetime import date, datetime
from pathlib import Path

from rich.style import Style
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, Label, Static, TextArea

# The model and backends live in inbox_core (shared with the web UI); re-exported from here.
from inbox_core import (  # noqa: F401
    PENDING_PROJECT, PROJECT_WIDTH, SORTS, TITLE_WIDTH, ApiBackend, Backend, FakeBackend,
    InboxItem, ellipsize, followup_name, item_fields, match_text, parse_due, primary_project, to_item,
)


def due_text(it: InboxItem) -> Text | str:
    style = {"done": "dim", "overdue": "bold red", "today": "yellow"}.get(it.due_state())
    return Text(it.due_label(), style=style) if style else it.due_label()


SETTINGS = (Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
            / "asana-inbox" / "settings.json")


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def save_setting(key: str, value) -> None:
    settings = load_settings()
    if settings.get(key) == value:
        return
    settings[key] = value
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    except OSError:
        pass  # not worth failing the UI over


class RegexPrompt(ModalScreen[tuple[str, str] | None]):
    """Ask for a regex; returns (pattern, mode) where mode is "mark", "unmark" or "filter"."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
    PLACEHOLDERS = {
        "mark": "Mark shown items matching regex (project, title, other projects, kind, assignee, "
                "'done', 'mine')",
        "unmark": "Unmark shown items matching regex",
        "filter": "Show only items matching regex (empty = show all)",
    }

    def __init__(self, mode: str, value: str = ""):
        super().__init__()
        self.mode, self.value = mode, value

    def compose(self) -> ComposeResult:
        yield Input(value=self.value, placeholder=self.PLACEHOLDERS[self.mode])

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss((event.value, self.mode))


class FollowupPrompt(ModalScreen[tuple[str, str, date, bool] | None]):
    """Ask for a follow-up task's title, description and due date; returns (title, description,
    due, archive the item). Enter in a one-line field or ctrl+s creates it."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel"),
                Binding("ctrl+s", "submit", "Create", priority=True)]

    def __init__(self, item: InboxItem, text: str, notes: str):
        super().__init__()
        self.item, self.text, self.notes = item, text, notes

    def compose(self) -> ComposeResult:
        with Vertical(id="followup"):
            yield Label(Text.assemble(("Follow-up for: ", "bold"), self.item.title), id="followup-title")
            yield Label("Title")
            yield Input(value=self.text, id="followup-text")
            yield Label("Description (Enter adds a line; ctrl+s creates)")
            yield TextArea(self.notes, id="followup-notes")
            yield Label("Due (YYYY-MM-DD, today, tomorrow, +N, or a weekday)")
            yield Input(value="today", id="followup-due")
            yield Label(self.hint("today"), id="followup-hint")
            yield Checkbox("Archive this notification", value=True, id="followup-archive")
            with Horizontal(id="followup-buttons"):
                yield Button("Create", variant="primary", id="create")
                yield Button("Cancel", id="cancel")

    @staticmethod
    def hint(text: str) -> Text:
        try:
            return Text(f"→ {parse_due(text):%a %Y-%m-%d}", style="dim")
        except ValueError as e:
            return Text(str(e), style="bold red")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "followup-due":
            self.query_one("#followup-hint", Label).update(self.hint(event.value))

    def submit(self) -> None:
        text = self.query_one("#followup-text", Input).value.strip()
        try:
            due = parse_due(self.query_one("#followup-due", Input).value)
        except ValueError:
            self.query_one("#followup-due", Input).focus()
            return  # the hint already says what's wrong
        if not text:
            self.notify("The follow-up needs a title", severity="warning")
            return
        notes = self.query_one("#followup-notes", TextArea).text.strip()
        self.dismiss((text, notes, due, self.query_one("#followup-archive", Checkbox).value))

    def action_submit(self) -> None:
        self.submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "create":
            self.submit()
        else:
            self.dismiss(None)


def open_link(app: App, item: InboxItem | None) -> None:
    """Open the item's Asana link in the default browser (`o`)."""
    if item is None or not item.url:
        app.notify("No URL for this item", severity="warning")
        return
    if hasattr(app, "open_url"):  # Textual ≥ 0.83: also works when served with textual-web
        app.open_url(item.url)
    else:
        webbrowser.open(item.url)
    app.notify(f"Opened {item.url}")


MENTION = Style(bold=True, color="cyan")


def rich_text(runs: list | None, plain: str, people: dict, indent: str = "") -> Text:
    """Rich text runs (see asana_api) as a Rich Text: mentions as bold "@Full Name" that link to
    mailto:<email> (terminals with OSC 8 links show it on hover), other links clickable.
    Falls back to `plain` when there are no runs."""
    t = Text()
    if runs is None:
        t.append(plain.strip().replace("\n", "\n" + indent))
        return t
    for r in runs:
        if isinstance(r, str):
            t.append(r.replace("\n", "\n" + indent))
        elif "user" in r:
            person = people.get(r["user"]) or {}
            name = f"@{person['name']}" if person.get("name") else r["text"]
            email = person.get("email")
            t.append(name, style=MENTION + Style(link=f"mailto:{email}") if email else MENTION)
        else:
            t.append(r["text"], style=Style(underline=True, link=r["link"]))
    return t


class Detail(ModalScreen[None]):
    """Read-only view of one item: metadata, description, and its notifications."""

    BINDINGS = [
        Binding("escape,q,enter", "dismiss(None)", "Close"),
        Binding("y", "copy_url", "Copy URL"),
        Binding("o", "open_url", "Open link"),
        Binding("f", "followup(False)", "Follow-up"),
        Binding("F", "followup(True)", "Custom follow-up"),
        Binding("a", "archive", "Archive"),
    ]

    def __init__(self, item: InboxItem):
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        it = self.item
        t = Text()
        t.append(it.title + "\n", style="bold")
        if it.pending:
            t.append("(details still loading)\n", style="italic dim")
        meta = [("Kind", it.kind), ("Projects", ", ".join(it.projects) or "-"),
                ("Assignee", ("me" if it.mine else it.assignee) or "-")]
        if it.due:
            meta.append(("Due", due_text(it)))
        if it.completed:
            meta.append(("Completed", it.completed.strftime("%Y-%m-%d %H:%M")))
        meta.append(("Link", it.url or "-"))
        for k, v in meta:
            t.append(f"{k:>10}: ", style="dim")
            t.append(v if isinstance(v, Text) else str(v))
            t.append("\n")
        if it.description.strip():
            t.append("\nMessage\n" if it.kind == "message" else "\nDescription\n", style="bold underline")
            t.append_text(rich_text(it.description_rich, it.description, it.people))
            t.append("\n")
        if it.events:
            t.append("\nNotifications\n", style="bold underline")
            for e in it.events:
                when = datetime.fromtimestamp(e["time"]).strftime("%Y-%m-%d %H:%M") if e["time"] else "?"
                t.append(f"{when}  ", style="dim")
                t.append(e['who'] or 'Asana', style="bold")
                t.append(f" · {e['what']}\n")
                if e["text"].strip():
                    t.append("    ")
                    t.append_text(rich_text(e.get("rich"), e["text"], it.people, indent="    "))
                    t.append("\n")
        with VerticalScroll(id="detail"):
            yield Static(t)  # plain Text: no markup interpretation of task content

    def action_copy_url(self) -> None:
        if self.item.url:
            self.app.copy_to_clipboard(self.item.url)
            self.notify(f"Copied {self.item.url}")

    def action_open_url(self) -> None:
        open_link(self.app, self.item)

    def action_followup(self, ask: bool) -> None:
        self.dismiss(None)
        self.app.followup(self.item, ask)

    def action_archive(self) -> None:
        self.dismiss(None)
        self.app.archive_one(self.item)


class Confirm(ModalScreen[bool]):
    # Enter confirms (Archive has focus); Tab to Cancel; arrows/PgUp/PgDn scroll the list.
    AUTO_FOCUS = "#yes"
    BINDINGS = [
        Binding("escape", "dismiss(False)", "Cancel"),
        Binding("up", "scroll('scroll_up')", show=False),
        Binding("down", "scroll('scroll_down')", show=False),
        Binding("pageup", "scroll('scroll_page_up')", show=False),
        Binding("pagedown", "scroll('scroll_page_down')", show=False),
    ]

    def action_scroll(self, how: str) -> None:
        for lst in self.query("#confirm-list"):
            getattr(lst, how)(animate=False)

    def __init__(self, message: str, details: Text | None = None):
        super().__init__()
        self.message, self.details = message, details

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Label(self.message, id="confirm-msg")
            if self.details is not None:
                with VerticalScroll(id="confirm-list", can_focus=False):
                    yield Static(self.details)
            with Horizontal(id="confirm-buttons"):
                yield Button("Archive", variant="error", id="yes")
                yield Button("Cancel", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class InboxApp(App):
    CSS = """
    RegexPrompt, Confirm, Detail, FollowupPrompt { align: center middle; }
    #followup { width: 90; max-width: 90%; height: auto; padding: 1 2; border: thick $accent;
                background: $surface; }
    #followup-title { margin-bottom: 1; }
    #followup-hint { margin-bottom: 1; }
    #followup-notes { height: 6; }
    #followup-buttons { height: auto; margin-top: 1; }
    #followup-buttons Button { margin-right: 2; }
    #detail { width: 90%; height: auto; max-height: 90%; border: thick $accent; background: $surface; padding: 0 1; }
    RegexPrompt Input { width: 70%; }
    #confirm { width: 100; max-width: 90%; height: auto; max-height: 90%; padding: 1 2;
               border: thick $error; background: $surface; }
    #confirm-msg { width: 100%; text-style: bold; margin-bottom: 1; }
    #confirm-list { height: auto; max-height: 60vh; margin-bottom: 1; }
    #confirm-buttons { height: auto; width: 100%; }
    #confirm-buttons Button { margin-right: 2; }
    """
    BINDINGS = [
        Binding("space", "toggle", "Mark"),
        Binding("l", "filter", "Filter"),  # "limit", as in mutt; f is follow-up
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("slash", "regex('mark')", "Mark /re/"),
        Binding("backslash", "regex('unmark')", "Unmark /re/"),
        Binding("p", "toggle_project", "Mark project"),
        Binding("c", "toggle_completed", "Mark completed"),
        Binding("v", "toggle_all", "All/none"),  # as in ranger; a is archive
        Binding("s", "cycle_sort", "Sort"),
        Binding("w", "toggle_truncate", "Full width"),
        Binding("y", "copy_url", "Copy URL"),
        Binding("o", "open_url", "Open link"),
        Binding("f", "followup(False)", "Follow-up"),
        Binding("F", "followup(True)", "Custom follow-up"),
        Binding("a", "archive", "Archive marked"),
        Binding("r", "reload", "Reload"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, backend: Backend):
        super().__init__()
        self.backend = backend
        self.items: list[InboxItem] = []
        self.marked: set[str] = set()
        self.sort = "project"
        self.filter: re.Pattern | None = None
        self.truncate: bool = load_settings().get("truncate", True)
        self.generation = 0  # bumped per load; detail updates from older loads are dropped
        self.redraw_scheduled = False

    match_text = staticmethod(match_text)

    def visible(self) -> list[InboxItem]:
        """Items passing the filter; every bulk mark action works on these only."""
        if self.filter is None:
            return self.items
        return [it for it in self.items if self.filter.search(self.match_text(it))]

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        # Theme (picked via ctrl+p → "Change theme") persists across runs.
        saved = load_settings().get("theme")
        if saved in self.available_themes:
            self.theme = saved
        self.theme_changed_signal.subscribe(self, lambda theme: save_setting("theme", theme.name))
        table = self.query_one(DataTable)
        # Fixed width: an auto-sized empty column shrinks to 0 and in-place marks become invisible.
        self.mark_col = table.add_column("", width=1)
        table.add_column("Project")
        table.add_column("Done", width=4)
        table.add_columns("Title", "Assignee", "Due", "Kind", "Updated", "Also in")
        self.action_reload()

    def action_reload(self) -> None:
        self.generation += 1  # stops a running fill
        self.busy("loading…")
        self.run_worker(self._load, thread=True, exclusive=True, group="backend")

    def busy(self, what: str) -> None:
        self.sub_title = what

    def _load(self) -> None:  # worker thread
        try:
            items = self.backend.list_items(lambda s: self.call_from_thread(self.busy, s))
        except BaseException as e:  # noqa: BLE001 - show anything, don't kill the app
            self.call_from_thread(self.notify, f"Load failed: {e}", severity="error", timeout=15)
            self.call_from_thread(self.refresh_table)
            return
        self.call_from_thread(self._set_items, items)

    def _set_items(self, items: list[InboxItem]) -> None:
        self.items = items
        self.marked &= {it.id for it in self.items}
        self.refresh_table()
        if hasattr(self.backend, "fill") and any(it.pending for it in items):
            self.generation += 1
            gen = self.generation
            self.run_worker(lambda: self._fill(gen, list(items)), thread=True, exclusive=True,
                            group="details")

    def _fill(self, gen: int, items: list[InboxItem]) -> None:  # worker thread
        try:
            for updates in self.backend.fill(items):
                if gen != self.generation:
                    return  # reloaded meanwhile; closing the generator stops the fetches
                self.call_from_thread(self._apply_details, gen, updates)
        except BaseException as e:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Loading details failed: {e}", severity="error",
                                  timeout=15)

    def _apply_details(self, gen: int, updates: dict[str, dict]) -> None:
        if gen != self.generation:
            return
        for it in self.items:
            for key, value in updates.get(it.id, {}).items():
                setattr(it, key, value)
        # Batches arrive in quick succession: one redraw for whatever came in meanwhile.
        if not self.redraw_scheduled:
            self.redraw_scheduled = True
            self.set_timer(0.3, self._redraw)

    def _redraw(self) -> None:
        self.redraw_scheduled = False
        self.refresh_table()

    SCROLLOFF = 3  # rows of context kept above/below the cursor

    def refresh_marks(self) -> None:
        """Update just the mark column in place: no rebuild, so the view doesn't move."""
        table = self.query_one(DataTable)
        for row_key in table.rows:
            want = "●" if row_key.value in self.marked else ""
            if table.get_cell(row_key, self.mark_col) != want:
                table.update_cell(row_key, self.mark_col, want)
        self.update_status(self.visible())

    def keep_context(self) -> None:
        """Vim-style scrolloff: keep SCROLLOFF rows visible around the cursor."""
        table = self.query_one(DataTable)
        if not table.row_count:
            return
        row = table.cursor_row
        visible = table.scrollable_content_region.height - (1 if table.show_header else 0)
        if visible <= 2 * self.SCROLLOFF + 1:
            return
        top = table.scroll_y
        if row > top + visible - 1 - self.SCROLLOFF:
            table.scroll_to(y=row - visible + 1 + self.SCROLLOFF, animate=False)
        elif row < top + self.SCROLLOFF:
            table.scroll_to(y=max(0, row - self.SCROLLOFF), animate=False)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.call_after_refresh(self.keep_context)

    def refresh_table(self) -> None:
        """Full rebuild (sort/filter/load/archive); keeps cursor item and scroll offset."""
        table = self.query_one(DataTable)
        cursor_id = self.current_id()
        scroll_y = table.scroll_y
        table.clear()
        for col in table.columns.values():  # clear() keeps old widths; let columns shrink again
            if col.auto_width:
                col.content_width = col.label.cell_len
        prev_project = None
        shown = self.visible()
        for it in sorted(shown, key=SORTS[self.sort]):
            project = it.project if self.sort != "project" or it.project != prev_project else ""
            prev_project = it.project
            table.add_row(
                "●" if it.id in self.marked else "",
                self.cut(project, PROJECT_WIDTH),
                "✓" if it.completed else "",
                Text(self.cut(it.title, TITLE_WIDTH), style="bold" if it.mine else ""),
                Text("★ me", style="bold yellow") if it.mine else (it.assignee or ""),
                due_text(it),
                it.kind,
                it.updated.strftime("%Y-%m-%d %H:%M"),
                ", ".join(p for p in it.projects if p != it.project),  # last column: never cut
                key=it.id,
            )
        table.scroll_to(y=scroll_y, animate=False)
        if cursor_id in {it.id for it in shown}:
            table.move_cursor(row=table.get_row_index(cursor_id), scroll=False)
        self.update_status(shown)

    def cut(self, text: str, width: int) -> str:
        return ellipsize(text, width) if self.truncate else text

    def action_toggle_truncate(self) -> None:
        self.truncate = not self.truncate
        save_setting("truncate", self.truncate)
        self.refresh_table()
        self.notify("Long titles/projects shortened" if self.truncate else "Showing full titles/projects")

    def update_status(self, shown: list[InboxItem]) -> None:
        self.title = "Asana Inbox"
        parts = [f"{len(self.items)} items", f"{len(self.marked)} marked", f"sort: {self.sort}"]
        if pending := sum(1 for it in self.items if it.pending):
            parts.append(f"loading details: {pending} left")
        if self.filter is not None:
            hidden_marked = len(self.marked - {it.id for it in shown})
            parts[0] = f"{len(shown)}/{len(self.items)} shown"
            parts.append(f"filter: /{self.filter.pattern}/")
            if hidden_marked:
                parts[1] += f" ({hidden_marked} hidden)"
        self.sub_title = " · ".join(parts)

    def current_id(self) -> str | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def current_item(self) -> InboxItem | None:
        cid = self.current_id()
        return next((it for it in self.items if it.id == cid), None)

    def action_toggle(self) -> None:
        cid = self.current_id()
        if cid is None:
            return
        self.marked ^= {cid}
        self.refresh_marks()
        self.query_one(DataTable).action_cursor_down()

    def action_toggle_project(self) -> None:
        cur = self.current_item()
        if cur is None:
            return
        ids = {it.id for it in self.visible() if it.project == cur.project}
        self.marked = self.marked - ids if ids <= self.marked else self.marked | ids
        self.refresh_marks()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row: show its details."""
        item = next((it for it in self.items if it.id == event.row_key.value), None)
        if item is not None:
            self.push_screen(Detail(item))

    def action_open_url(self) -> None:
        open_link(self, self.current_item())

    def action_copy_url(self) -> None:
        """Copy the selected item's Asana link via OSC 52 (terminal clipboard escape).
        In tmux this needs `set -g set-clipboard on`."""
        cur = self.current_item()
        if cur is None or not cur.url:
            self.notify("No URL for this item", severity="warning")
            return
        self.copy_to_clipboard(cur.url)
        self.notify(f"Copied {cur.url}")

    def action_toggle_completed(self) -> None:
        ids = {it.id for it in self.visible() if it.completed}
        if not ids:
            self.notify("No completed tasks shown")
            return
        unmark = ids <= self.marked
        self.marked = self.marked - ids if unmark else self.marked | ids
        self.notify(f"{'Unmarked' if unmark else 'Marked'} {len(ids)} completed tasks")
        self.refresh_marks()

    def action_toggle_all(self) -> None:
        ids = {it.id for it in self.visible()}
        self.marked = self.marked - ids if ids <= self.marked else self.marked | ids
        self.refresh_marks()

    def action_cycle_sort(self) -> None:
        keys = list(SORTS)
        self.sort = keys[(keys.index(self.sort) + 1) % len(keys)]
        self.refresh_table()

    def action_regex(self, mode: str) -> None:
        def apply(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            pattern, mode = result
            if mode == "filter" and not pattern:
                self.action_clear_filter()
                return
            if not pattern:
                return
            try:
                rx = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                self.notify(f"Bad regex: {e}", severity="error")
                return
            if mode == "filter":
                self.filter = rx
                self.refresh_table()
            else:
                ids = {it.id for it in self.visible() if rx.search(self.match_text(it))}
                unmark = mode == "unmark"
                self.marked = self.marked - ids if unmark else self.marked | ids
                self.notify(f"{'Unmarked' if unmark else 'Marked'} {len(ids)} items")
                self.refresh_marks()

        current = self.filter.pattern if mode == "filter" and self.filter is not None else ""
        self.push_screen(RegexPrompt(mode, current), apply)

    def action_filter(self) -> None:
        self.action_regex("filter")

    def action_clear_filter(self) -> None:
        if self.filter is not None:
            self.filter = None
            self.refresh_table()

    def action_followup(self, ask: bool) -> None:
        if (cur := self.current_item()) is not None:
            self.followup(cur, ask)

    def archive_one(self, item: InboxItem) -> None:
        """Archive one item without asking (`x` in the details view, where it's in plain sight)."""
        self.busy("archiving 1…")
        self.run_worker(lambda: self._archive([item.id]), thread=True, exclusive=True, group="backend")

    def followup(self, item: InboxItem, ask: bool) -> None:
        """Create a task assigned to me, due today, titled "Follow up on <title>" with the item's
        link as its description (`f`), or ask for title and due date first (`F`); then archive the
        item unless told not to."""
        if not hasattr(self.backend, "create_task"):
            self.notify("This backend can't create tasks", severity="warning")
            return
        text = followup_name(item)

        def create(result: tuple[str, str, date, bool] | None) -> None:
            if result is None:
                return
            name, notes, due, archive = result
            self.busy("creating follow-up…")
            self.run_worker(lambda: self._followup(item, name, notes, due, archive), thread=True,
                            group="followup")

        if ask:
            self.push_screen(FollowupPrompt(item, text, item.url), create)
        else:
            create((text, item.url, date.today(), True))

    def _followup(self, item: InboxItem, name: str, notes: str, due: date,
                  archive: bool) -> None:  # worker thread
        try:
            url = self.backend.create_task(name, due, notes)
        except BaseException as e:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Creating the follow-up failed: {e}",
                                  severity="error", timeout=15)
            self.call_from_thread(self.update_status, self.visible())
            return
        when = "today" if due == date.today() else f"{due:%a %Y-%m-%d}"
        self.call_from_thread(self.notify, f"Follow-up due {when}: {url}", timeout=8)
        if archive:
            self._archive([item.id])
        else:
            self.call_from_thread(self.update_status, self.visible())

    def action_archive(self) -> None:
        if not self.marked:
            self.notify("Nothing marked", severity="warning")
            return

        def do(ok: bool | None) -> None:
            if not ok:
                return
            ids = sorted(self.marked)
            self.busy(f"archiving {len(ids)}…")
            self.run_worker(lambda: self._archive(ids), thread=True, exclusive=True, group="backend")

        shown_ids = {it.id for it in self.visible()}
        marked = sorted((it for it in self.items if it.id in self.marked), key=SORTS[self.sort])
        hidden = sum(1 for it in marked if it.id not in shown_ids)
        details = Text()
        for it in marked:
            details.append(f"{it.project}  ", style="dim")
            details.append(it.title)
            if it.id not in shown_ids:
                details.append("  (hidden by filter)", style="italic yellow")
            details.append("\n")
        details.rstrip()
        note = f" ({hidden} hidden by the filter)" if hidden else ""
        self.push_screen(Confirm(f"Archive these {len(marked)} items?{note}", details), do)

    def _archive(self, ids: list[str]) -> None:  # worker thread
        try:
            self.backend.archive(ids)
        except BaseException as e:  # noqa: BLE001
            self.call_from_thread(self.notify, f"Archive failed: {e}", severity="error", timeout=15)
            self.call_from_thread(self.refresh_table)
            return

        def done() -> None:
            self.notify(f"Archived {len(ids)} items (press r to re-sync with Asana)")
            self.marked -= set(ids)
            # The mutation was committed; drop locally instead of an expensive full reload.
            self.items = [it for it in self.items if it.id not in set(ids)]
            self.refresh_table()

        self.call_from_thread(done)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fake", action="store_true", help="use generated data, no Asana")
    ap.add_argument("--workspace", default=os.environ.get("ASANA_WORKSPACE"),
                    help="workspace gid (env ASANA_WORKSPACE; default: your only workspace)")
    ap.add_argument("-n", type=int, default=0, help="notifications to scan (0 = all)")
    ap.add_argument("--ignore-project", action="append",
                    default=[p for p in os.environ.get("ASANA_IGNORE_PROJECTS", "").split(",") if p],
                    help="don't group by this project if the task has others (repeatable; "
                         "env ASANA_IGNORE_PROJECTS, comma-separated)")
    args = ap.parse_args()
    if args.fake:
        backend: Backend = FakeBackend()
    else:
        try:
            backend = ApiBackend(args.workspace, args.n, set(args.ignore_project))
        except RuntimeError as e:  # ApiError: no token, bad token, several workspaces
            raise SystemExit(str(e)) from None
    InboxApp(backend).run()


if __name__ == "__main__":
    main()
