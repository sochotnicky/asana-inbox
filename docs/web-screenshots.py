#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["playwright>=1.45"]
# ///
"""Regenerate the README screenshots of the web UI (PNG) from the fake backend: ./docs/web-screenshots.py

The first run downloads Playwright's headless Chromium (into ~/.cache/ms-playwright).
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

DOCS = Path(__file__).resolve().parent
APP = DOCS.parent / "asana_inbox_web.py"
VIEWPORT = {"width": 1400, "height": 820}
# Inline SVG so the login shot shows a logo without fetching anything.
LOGO = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
        "<circle cx='16' cy='16' r='14' fill='%23F06A6A'/><path d='M9 17l5 5 9-11' stroke='white'"
        " stroke-width='3' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>")
NO_ANIMATIONS = "*, *::before, *::after { transition: none !important; animation: none !important; caret-color: transparent !important; }"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def server(*args: str, env: dict[str, str] | None = None):
    port = free_port()
    proc = subprocess.Popen([str(APP), *args, "--port", str(port)],
                            env={**os.environ, "XDG_CONFIG_HOME": "/nonexistent", **(env or {})},
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    url = f"http://localhost:{port}/"  # the app refuses Host headers other than localhost
    try:
        for _ in range(300):
            if proc.poll() is not None:
                raise SystemExit(f"server exited:\n{proc.stderr.read()}")
            try:
                urllib.request.urlopen(url, timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise SystemExit(f"server didn't answer on {url}")
        yield url
    finally:
        proc.terminate()
        proc.wait(10)


def open_page(browser, url: str, theme: str) -> Page:
    ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=1, color_scheme=theme,
                              reduced_motion="reduce", timezone_id="UTC", locale="en-US",
                              bypass_csp=True)  # CSP forbids eval, which wait_for_function needs
    # The page follows the system theme unless one was picked; pick it so the shots don't depend on it.
    ctx.add_init_script(f'localStorage.setItem("asana-inbox.theme", "{theme}");')
    page = ctx.new_page()
    page.goto(url)
    page.add_style_tag(content=NO_ANIMATIONS)
    return page


def shot(page: Page, name: str) -> None:
    page.mouse.move(0, VIEWPORT["height"] - 1)  # no hover highlight
    page.wait_for_timeout(200)
    page.screenshot(path=DOCS / f"{name}.png")
    print(f"docs/{name}.png")


def keys(page: Page, *ks: str) -> None:
    for k in ks:
        page.keyboard.press(k)


def inbox_shots(browser, url: str) -> None:
    page = open_page(browser, url, "light")
    page.wait_for_selector("#rows tr")
    # Fake details fill in over a few seconds; wait until no Project cell is still pending.
    page.wait_for_function("!document.querySelector('#rows td.project.pending')", timeout=60_000)
    page.wait_for_function("!/…/.test(document.getElementById('status').textContent)")

    # 1. Default view: grouped by project, a few rows marked.
    keys(page, "ArrowDown", " ", " ", "ArrowDown", "ArrowDown", " ")
    shot(page, "web-main")

    # 2. Filter: only my tasks, all of them marked with `v`.
    keys(page, "l")
    page.keyboard.type("mine")
    keys(page, "Enter")
    page.wait_for_function("document.getElementById('status').textContent.includes('filter: /mine/')")
    keys(page, "v")
    shot(page, "web-filter")
    keys(page, "v", "Escape")
    page.wait_for_function("!document.getElementById('status').textContent.includes('filter:')")

    # 3. Details (Enter) on a task with several projects, a due date and comments.
    row = page.evaluate("""() => {
        const it = S.rows.find((it) => it.projects.length >= 3 && it.due && it.description && it.assignee
                                       && it.events.filter((e) => e.text).length >= 2);
        return S.rows.indexOf(it);
    }""")
    keys(page, "Home", *["ArrowDown"] * row, "Enter")
    page.wait_for_selector("#ov-detail:not([hidden])")
    shot(page, "web-details")
    keys(page, "Escape")

    # 4. Archive confirmation: also mark a whole project with `p`.
    keys(page, "End", "p", "a")
    page.wait_for_selector("#ov-confirm:not([hidden])")
    shot(page, "web-archive")
    keys(page, "Escape")
    page.context.close()

    # 5. Dark mode, same view as 1.
    page = open_page(browser, url, "dark")
    page.wait_for_function("document.querySelectorAll('#rows tr').length && !document.querySelector('#rows td.project.pending')")
    keys(page, "ArrowDown", " ", " ", "ArrowDown", "ArrowDown", " ")
    shot(page, "web-dark")
    page.context.close()


def login_shot(browser, url: str) -> None:
    page = open_page(browser, url, "light")
    page.wait_for_selector("#login:not([hidden])")
    shot(page, "web-login")
    page.context.close()


def main() -> None:
    # Matches this env's Playwright version; a no-op once downloaded.
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium-headless-shell"], check=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        with server("--fake") as url:
            inbox_shots(browser, url)
        with server("--logo-url", LOGO, env={"ASANA_CLIENT_ID": "1", "ASANA_CLIENT_SECRET": "x"}) as url:
            login_shot(browser, url)
        browser.close()


if __name__ == "__main__":
    main()
