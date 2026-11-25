"""Desktop app: runs the panel server in the background and shows panel.html in its own window.

    python quest_app.py [--engine auto|webview|chromium]

Window engines, tried in this order when --engine is auto:
  1. pywebview (pip install pywebview): a native window with the page embedded.
  2. Edge or Chrome in --app mode: a bare window without tabs or an address bar.
Closing the window stops a running quest cleanly (its final heartbeat is sent)
and shuts the server down. Python 3.10+.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import PureWindowsPath
from typing import Any, Callable, Mapping, Sequence

from quest_panel import PAGE, Panel, PanelServer

TITLE = "Панель квестов"
WINDOW_SIZE = (1180, 900)
MIN_SIZE = (420, 640)
HANDOFF_GRACE_S = 3.0     # a browser process that exits sooner than this handed its window to another process
IDLE_LIMIT_S = 150.0      # page polls every 1-4 s; background throttling can stretch that to ~60 s
FIRST_POLL_LIMIT_S = 60.0


class AppError(Exception):
    """The window could not be opened; the message says what to install or fix."""


# ----------------------------- pure functions -------------------------------

def browser_candidates(env: Mapping[str, str], platform: str) -> list[str]:
    """Chromium-based browsers to try, Edge first (it ships with Windows 11)."""
    if platform == "win32":
        roots = [env.get("PROGRAMFILES(X86)"), env.get("PROGRAMFILES"), env.get("LOCALAPPDATA")]
        rels = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
        return [str(PureWindowsPath(root) / rel) for rel in rels for root in roots if root]
    if platform == "darwin":
        return ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    return ["microsoft-edge", "google-chrome", "chromium", "chromium-browser"]


def find_browser(candidates: Sequence[str],
                 exists: Callable[[str], bool] = os.path.isfile,
                 which: Callable[[str], str | None] = shutil.which) -> str | None:
    for candidate in candidates:
        is_path = "/" in candidate or "\\" in candidate
        found = (candidate if exists(candidate) else None) if is_path else which(candidate)
        if found:
            return found
    return None


def app_command(exe: str, url: str, profile_dir: str) -> list[str]:
    # A private profile directory forces a separate browser process, so waiting on it
    # waits for this window instead of handing the window to an already running browser.
    return [exe, f"--app={url}", f"--user-data-dir={profile_dir}", "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}"]


def wait_for_window(proc: Any, panel: Panel, *,
                    clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep) -> None:
    started = clock()
    proc.wait()
    if clock() - started >= HANDOFF_GRACE_S:
        return
    # The launcher returned at once, so the window lives in another process. Watch the page instead.
    while True:
        idle = panel.idle_seconds()
        if idle is None and clock() - started > FIRST_POLL_LIMIT_S:
            return
        if idle is not None and idle > IDLE_LIMIT_S:
            return
        sleep(5.0)


# --------------------------------- engines ----------------------------------

def load_webview() -> Any:
    try:
        import webview
    except ImportError:
        return None
    return webview


def run_webview(webview: Any, url: str) -> None:
    webview.create_window(TITLE, url, width=WINDOW_SIZE[0], height=WINDOW_SIZE[1], min_size=MIN_SIZE)
    if sys.platform == "win32":
        webview.start(gui="edgechromium")  # without it pywebview may silently pick the old MSHTML engine
    else:
        webview.start()


def run_chromium(exe: str, url: str, panel: Panel) -> None:
    with tempfile.TemporaryDirectory(prefix="quest-panel-", ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen(app_command(exe, url, profile))
        try:
            wait_for_window(proc, panel)
        finally:
            if proc.poll() is None:
                proc.terminate()


def open_window(url: str, panel: Panel, engine: str = "auto", *,
                load: Callable[[], Any] = load_webview,
                find: Callable[[Sequence[str]], str | None] = find_browser,
                webview_runner: Callable[[Any, str], None] = run_webview,
                chromium_runner: Callable[[str, str, Panel], None] = run_chromium) -> None:
    if engine in ("auto", "webview"):
        webview = load()
        if webview is not None:
            try:
                webview_runner(webview, url)
                return
            except Exception as exc:
                if engine == "webview":
                    raise AppError(f"pywebview не смог открыть окно: {type(exc).__name__}: {exc}") from None
                print(f"pywebview не сработал ({type(exc).__name__}), пробую Edge или Chrome", file=sys.stderr)
        elif engine == "webview":
            raise AppError("pywebview не установлен. Установи: python -m pip install pywebview")
    browser = find(browser_candidates(os.environ, sys.platform))
    if browser is None:
        raise AppError("Не нашёл ни pywebview, ни Edge или Chrome. "
                       "Установи pywebview: python -m pip install pywebview")
    chromium_runner(browser, url, panel)


# ---------------------------------- entry -----------------------------------

def fatal(message: str) -> int:
    print(message, file=sys.stderr)
    if sys.platform == "win32":  # a windowed build has no console to read the message from
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, TITLE, 0x10)
        except Exception:
            pass
    return 1


def main(argv: list[str] | None = None,
         opener: Callable[[str, Panel, str], None] = open_window) -> int:
    parser = argparse.ArgumentParser(prog="quest_app", description="Desktop window for the quest panel")
    parser.add_argument("--engine", choices=("auto", "webview", "chromium"), default="auto",
                        help="window engine (default: pywebview, then Edge or Chrome)")
    args = parser.parse_args(argv)
    if not PAGE.is_file():
        return fatal("panel.html не найден рядом с программой")
    panel = Panel()
    try:
        server = PanelServer(("127.0.0.1", 0), panel, secrets.token_urlsafe(24))
    except OSError as exc:
        panel.close()
        return fatal(f"Не удалось запустить локальный сервер: {exc}")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/#k={server.key}"
    try:
        opener(url, panel, args.engine)
        return 0
    except AppError as exc:
        return fatal(str(exc))
    finally:
        panel.shutdown()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
