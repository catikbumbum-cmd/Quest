"""Local control panel for discord_quests.py. Python 3.10+, standard library only.

    python quest_panel.py [--port 8765] [--no-browser]

Serves panel.html on 127.0.0.1 only. The credentials the page sends stay in this
process's memory: never written to disk, never sent back to the page. The page
URL printed at startup carries a random access key after the "#"; API calls
without it are rejected, and so are requests whose Host or Origin is not local.
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import secrets
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

import discord_quests as dq

def resource_dir() -> Path:
    """Folder holding panel.html: next to this file, or the unpack folder of a PyInstaller build."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


PAGE = resource_dir() / "panel.html"
NONCE_TOKEN = "__NONCE__"
MAX_REQUEST_BYTES = 65_536
MAX_LOG_ENTRIES = 400
MAX_SELECTED = 60


class PanelError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class RunRequest:
    quest_ids: tuple[str, ...]
    channel_id: str | None
    exe_path: str | None
    dry_run: bool


# ----------------------------- pure functions -------------------------------

def quest_view(quest: dq.Quest, run: Mapping[str, Any] | None, now: datetime) -> dict[str, Any]:
    picked = dq.pick_task(quest)
    task, target = picked if picked is not None else (None, 0)
    done = min(quest.progress.get(task, 0.0), float(target)) if task is not None else 0.0
    reason = dq.skip_reason(quest, now)
    return {
        "id": quest.quest_id,
        "name": quest.name,
        "tasks": dict(quest.tasks),
        "task": task,
        "target": target,
        "done": done,
        "state": dq.quest_state(quest),
        "supported": picked is not None,
        "selectable": picked is not None and reason is None,
        "skip_reason": reason,
        "expires_at": quest.expires_at.isoformat(timespec="seconds") if quest.expires_at else None,
        "run": dict(run) if run else None,
    }


def _opt_str(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PanelError(400, f"Поле {key} должно быть строкой")
    return value.strip() or None


def parse_run_request(data: Mapping[str, Any], quests: list[dq.Quest], now: datetime) -> RunRequest:
    ids = data.get("quest_ids")
    if not isinstance(ids, list) or not ids:
        raise PanelError(400, "Не выбран ни один квест")
    if len(ids) > MAX_SELECTED:
        raise PanelError(400, f"За один запуск можно выбрать не больше {MAX_SELECTED} квестов")
    by_id = {quest.quest_id: quest for quest in quests}
    chosen: list[str] = []
    for quest_id in ids:
        if not isinstance(quest_id, str) or not dq.SNOWFLAKE.match(quest_id):
            raise PanelError(400, "Некорректный id квеста")
        quest = by_id.get(quest_id)
        if quest is None:
            raise PanelError(400, "Квеста нет в твоём списке, нажми «Обновить»")
        if dq.pick_task(quest) is None or dq.skip_reason(quest, now) is not None:
            raise PanelError(400, f"Квест «{quest.name}» сейчас запустить нельзя")
        if quest_id not in chosen:
            chosen.append(quest_id)
    channel = _opt_str(data, "channel_id")
    if channel is not None and not dq.SNOWFLAKE.match(channel):
        raise PanelError(400, "ID канала должен состоять из 15–25 цифр")
    exe = _opt_str(data, "exe_path")
    if exe is not None:
        if not dq.plausible_path(exe):
            raise PanelError(400, "executable_path выглядит неправдоподобно")
        if len(chosen) > 1:
            raise PanelError(400, "executable_path относится к одному квесту: выбери один")
    dry_run = data.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise PanelError(400, "Поле dry_run должно быть true или false")
    return RunRequest(tuple(chosen), channel, exe, dry_run)


def credential_source(data: Mapping[str, Any], env: Mapping[str, str]) -> Mapping[str, str]:
    if data.get("use_env") is True:
        return env
    fields = {
        "DISCORD_TOKEN": data.get("token", ""),
        "DISCORD_SUPER_PROPERTIES": data.get("super_properties", ""),
        "DISCORD_USER_AGENT": data.get("user_agent", ""),
    }
    if not all(isinstance(value, str) for value in fields.values()):
        raise PanelError(400, "Поля подключения должны быть строками")
    return fields


# --------------------------------- state ------------------------------------

class _LogHandler(logging.Handler):
    def __init__(self, panel: "Panel") -> None:
        super().__init__(logging.INFO)
        self._panel = panel

    def emit(self, record: logging.LogRecord) -> None:
        self._panel.add_log(record.levelname.lower(), record.getMessage())


class Panel:
    def __init__(self, cfg: dq.Config | None = None,
                 api_factory: Callable[[dq.Credentials], Any] | None = None,
                 env: Mapping[str, str] | None = None,
                 pause_s: float = 2.0) -> None:
        self.cfg = cfg or dq.Config()
        self._factory = api_factory or (lambda creds: dq.Api(creds, self.cfg))
        self._env = os.environ if env is None else env
        self.pause_s = pause_s
        self._lock = threading.RLock()
        self._api: Any = None
        self._quests: list[dq.Quest] = []
        self._statuses: dict[str, dict[str, Any]] = {}
        self._running = False
        self._stop = threading.Event()
        self._entries: list[dict[str, Any]] = []
        self._seq = 0
        self._thread: threading.Thread | None = None
        self._last_poll: float | None = None
        self._closing = False
        try:
            dq.load_credentials(self._env)
            self.env_ready = True
        except dq.Gated:
            self.env_ready = False
        self._handler = _LogHandler(self)
        dq.log.addHandler(self._handler)
        dq.log.setLevel(logging.INFO)

    def close(self) -> None:
        self._stop.set()
        dq.log.removeHandler(self._handler)

    def shutdown(self, timeout: float = 15.0) -> None:
        """Stop a running quest, wait for its final heartbeat to go out, then detach logging."""
        self._closing = True
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self.close()

    def idle_seconds(self, clock: Callable[[], float] = time.monotonic) -> float | None:
        """Seconds since the page last polled the state, or None if it never has."""
        with self._lock:
            return None if self._last_poll is None else clock() - self._last_poll

    # log -------------------------------------------------------------------
    def add_log(self, level: str, message: str) -> None:
        with self._lock:
            self._seq += 1
            self._entries.append({"n": self._seq, "t": datetime.now().strftime("%H:%M:%S"),
                                  "level": level, "msg": message})
            del self._entries[:-MAX_LOG_ENTRIES]

    # reads -----------------------------------------------------------------
    def state(self, since: int) -> dict[str, Any]:
        with self._lock:
            self._last_poll = time.monotonic()
            now = dq.utcnow()
            return {
                "connected": self._api is not None,
                "env_ready": self.env_ready,
                "running": self._running,
                "quests": [quest_view(q, self._statuses.get(q.quest_id), now) for q in self._quests],
                "log": [entry for entry in self._entries if entry["n"] > since],
                "log_last": self._seq,
            }

    # actions ---------------------------------------------------------------
    def connect(self, data: Mapping[str, Any]) -> dict[str, Any]:
        source = credential_source(data, self._env)
        try:
            creds = dq.load_credentials(source)
            api = self._factory(creds)
            quests = dq.fetch_quests(api, self.cfg)
        except dq.Gated as exc:
            raise PanelError(400, str(exc)) from None
        except dq.ApiError as exc:
            raise PanelError(502, f"Discord ответил: {exc}") from None
        except ValueError:
            raise PanelError(502, "Discord вернул список квестов в неожиданном формате") from None
        with self._lock:
            if self._running:
                raise PanelError(409, "Сначала останови текущий запуск")
            self._api, self._quests, self._statuses = api, quests, {}
        dq.log.info("connected, %d quests in your list", len(quests))
        return {"ok": True}

    def disconnect(self, data: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._running:
                raise PanelError(409, "Сначала останови текущий запуск")
            self._api, self._quests, self._statuses = None, [], {}
        dq.log.info("disconnected, credentials dropped from memory")
        return {"ok": True}

    def refresh(self, data: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            api = self._api
        if api is None:
            raise PanelError(409, "Сначала подключись")
        try:
            quests = dq.fetch_quests(api, self.cfg)
        except (dq.ApiError, ValueError) as exc:
            raise PanelError(502, f"Не удалось обновить список: {exc}") from None
        with self._lock:
            self._quests = quests
        return {"ok": True}

    def start_run(self, data: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._api is None:
                raise PanelError(409, "Сначала подключись")
            if self._running:
                raise PanelError(409, "Запуск уже идёт")
            req = parse_run_request(data, self._quests, dq.utcnow())
            by_id = {q.quest_id: q for q in self._quests}
            chosen = {qid: by_id[qid] for qid in req.quest_ids}
            self._statuses = {}
            for qid, quest in chosen.items():
                picked = dq.pick_task(quest)
                task, target = picked if picked is not None else (None, 0)
                self._statuses[qid] = {"status": "queued", "message": "", "target": target,
                                       "done": quest.progress.get(task, 0.0) if task else 0.0}
            self._stop.clear()
            self._running = True
            api = self._api
        worker = threading.Thread(target=self._worker, args=(api, req, chosen), daemon=True)
        self._thread = worker
        worker.start()
        return {"ok": True}

    def stop(self, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._stop.set()
        return {"ok": True}

    # worker ----------------------------------------------------------------
    def _set(self, qid: str, status: str, *, done: float | None = None,
             target: int | None = None, message: str = "") -> None:
        with self._lock:
            current = self._statuses.get(qid, {})
            self._statuses[qid] = {
                "status": status,
                "message": message,
                "done": float(current.get("done", 0.0) if done is None else done),
                "target": int(current.get("target", 0) if target is None else target),
            }

    def _worker(self, api: Any, req: RunRequest, chosen: dict[str, dq.Quest]) -> None:
        opts = dq.RunOptions(channel_id=req.channel_id, exe_path=req.exe_path)
        last = len(req.quest_ids) - 1
        try:
            for index, qid in enumerate(req.quest_ids):
                if self._stop.is_set():
                    for rest in req.quest_ids[index:]:
                        self._set(rest, "stopped", message="Остановлено до запуска")
                    break
                self._run_one(api, chosen[qid], req, opts)
                if index < last and not req.dry_run:
                    self._stop.wait(self.pause_s)
        finally:
            with self._lock:
                self._running = False
            if not self._closing:
                self._reload(api)

    def _run_one(self, api: Any, quest: dq.Quest, req: RunRequest, opts: dq.RunOptions) -> None:
        qid = quest.quest_id
        picked = dq.pick_task(quest)
        if picked is None:
            self._set(qid, "blocked", message="Тип задания не поддерживается")
            return
        task_type, target = picked
        if req.dry_run:
            dq.log.info("%s: would run %s (%ds)", quest.name, task_type, target)
            self._set(qid, "planned", target=target, message=f"План: {task_type}, {target} с")
            return
        self._set(qid, "running", target=target)

        def report(done: float, total: int) -> None:
            self._set(qid, "running", done=done, target=total)

        try:
            ok = dq.drive_quest(api, self.cfg, quest, opts, stop=self._stop, report=report)
        except dq.Stopped:
            dq.log.info("%s: stopped", quest.name)
            self._set(qid, "stopped", message="Остановлено")
            return
        except dq.Gated as exc:
            dq.log.error("%s: %s", quest.name, exc)
            self._set(qid, "blocked", message=str(exc))
            return
        except dq.ApiError as exc:
            dq.log.error("%s: %s", quest.name, exc)
            self._set(qid, "error", message=str(exc))
            return
        except Exception as exc:  # keep the worker alive, never leave a row stuck on "running"
            dq.log.error("%s: unexpected %s", quest.name, type(exc).__name__)
            self._set(qid, "error", message=f"Внутренняя ошибка: {type(exc).__name__}")
            return
        dq.log.info("%s: %s", quest.name, "completed" if ok else "not confirmed, check the Quests page")
        self._set(qid, "completed" if ok else "unconfirmed", done=target if ok else None)

    def _reload(self, api: Any) -> None:
        try:
            quests = dq.fetch_quests(api, self.cfg)
        except (dq.ApiError, ValueError) as exc:
            dq.log.warning("could not reload the quest list: %s", exc)
            return
        with self._lock:
            if self._api is api:
                self._quests = quests


# --------------------------------- server -----------------------------------

class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let another process share the port

    def __init__(self, address: tuple[str, int], panel: Panel, key: str) -> None:
        super().__init__(address, Handler)
        self.panel = panel
        self.key = key


class Handler(BaseHTTPRequestHandler):
    server: PanelServer
    server_version = "QuestPanel"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _hosts(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _check(self, needs_key: bool) -> None:
        hosts = self._hosts()
        if (self.headers.get("Host") or "").lower() not in hosts:
            raise PanelError(403, "bad host")
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in {f"http://{host}" for host in hosts}:
            raise PanelError(403, "bad origin")
        if needs_key:
            given = (self.headers.get("X-Panel-Key") or "").encode("utf-8")
            if not hmac.compare_digest(given, self.server.key.encode("utf-8")):
                raise PanelError(403, "bad key")

    def _send(self, status: int, body: bytes, content_type: str, csp: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if csp:
            self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _fail(self, exc: Exception) -> None:
        if isinstance(exc, PanelError):
            self._json(exc.status, {"error": exc.message})
        else:
            print(f"panel: unexpected {type(exc).__name__}", file=sys.stderr)
            self._json(500, {"error": "Внутренняя ошибка панели"})

    def do_GET(self) -> None:
        try:
            url = urlsplit(self.path)
            if url.path == "/":
                self._check(False)
                try:
                    page = PAGE.read_text(encoding="utf-8")
                except OSError:
                    raise PanelError(500, "panel.html не найден рядом с quest_panel.py") from None
                nonce = secrets.token_hex(16)
                csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
                self._send(200, page.replace(NONCE_TOKEN, nonce).encode("utf-8"),
                           "text/html; charset=utf-8", csp)
            elif url.path == "/api/state":
                self._check(True)
                try:
                    since = max(0, int(parse_qs(url.query).get("since", ["0"])[0]))
                except ValueError:
                    since = 0
                self._json(200, self.server.panel.state(since))
            else:
                raise PanelError(404, "not found")
        except Exception as exc:
            self._fail(exc)

    def do_POST(self) -> None:
        try:
            self._check(True)
            panel = self.server.panel
            routes = {
                "/api/connect": panel.connect, "/api/disconnect": panel.disconnect,
                "/api/refresh": panel.refresh, "/api/run": panel.start_run, "/api/stop": panel.stop,
            }
            action = routes.get(urlsplit(self.path).path)
            if action is None:
                raise PanelError(404, "not found")
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                raise PanelError(415, "нужен Content-Type: application/json")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise PanelError(411, "нужен Content-Length") from None
            if not 0 <= length <= MAX_REQUEST_BYTES:
                raise PanelError(413, "слишком большой запрос")
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise PanelError(400, "тело запроса не является JSON") from None
            if not isinstance(data, dict):
                raise PanelError(400, "ожидался JSON-объект")
            self._json(200, action(data))
        except Exception as exc:
            self._fail(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quest_panel", description="Local panel for discord_quests.py")
    parser.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = parser.parse_args(argv)
    if not PAGE.is_file():
        print(f"panel.html not found next to {Path(__file__).name}", file=sys.stderr)
        return 2
    key = secrets.token_urlsafe(24)
    panel = Panel()
    try:
        server = PanelServer(("127.0.0.1", args.port), panel, key)
    except OSError as exc:
        print(f"cannot listen on 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/#k={key}"
    print(f"Panel is running. Open this address (it contains the access key):\n  {url}\nCtrl+C stops it.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
    finally:
        panel.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
