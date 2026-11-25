"""Discord Quests runner: finds your pending quests and drives them to completion.

Python 3.10+, standard library only (no pip needed).

Environment (read once, never written to disk, never printed):
    DISCORD_TOKEN             value of the Authorization request header
    DISCORD_SUPER_PROPERTIES  value of the X-Super-Properties request header
    DISCORD_USER_AGENT        value of the User-Agent request header

How to get the three values: open discord.com/app in a browser, press F12,
open the Network tab, click any request to /api/..., and copy those three
request headers from it.

Usage:
    python discord_quests.py list
    python discord_quests.py run [--quest-id ID] [--channel-id ID] [--exe-path PATH]
                                 [--play-body-file FILE] [--dry-run]
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import logging
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

log = logging.getLogger("quests")

SNOWFLAKE = re.compile(r"^\d{15,25}$")
MAX_TARGET_S = 6 * 3600
MAX_BODY_BYTES = 2_000_000
MAX_QUESTS = 200
VIDEO_TASKS = ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE")
# Order in which a quest's task types are tried when it lists several.
TASK_PRIORITY = ("WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE", "PLAY_ACTIVITY", "PLAY_ON_DESKTOP")


# ---------------------------------------------------------------------------
# BUILD-DEPENDENT VALUES -- the single place they live.
# Provenance of every value in this class: recalled from publicly observed
# client behaviour, NOT taken from an artifact supplied by the user.
# Status of every value: UNVERIFIED. Confirm each one against a live request
# (DevTools -> Network -> filter "quests") before trusting a run.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    api_base: str = "https://discord.com/api/v9"
    path_list: str = "/quests/@me"
    path_enroll: str = "/quests/{quest_id}/enroll"
    path_video: str = "/quests/{quest_id}/video-progress"
    path_heartbeat: str = "/quests/{quest_id}/heartbeat"
    enroll_body: dict[str, Any] = field(
        default_factory=lambda: {"location": 11, "is_targeted": False, "metadata_raw": None}
    )
    activity_stream_key: str = "call:{channel_id}:1"
    path_apps: str = "/applications/public?application_ids={application_id}"
    # The three entries below come from a real PLAY_ON_DESKTOP heartbeat the user
    # captured (fields: application_id, executable_path, terminal). That makes the
    # body shape and the key name "terminal" VERIFIED for PLAY_ON_DESKTOP only;
    # for PLAY_ACTIVITY the key name is carried over and still UNVERIFIED.
    terminal_key: str = "terminal"
    play_template: dict[str, Any] = field(
        default_factory=lambda: {"application_id": "{application_id}",
                                 "executable_path": "{executable_path}"}
    )
    # Pacing thresholds the server is believed to enforce (also UNVERIFIED).
    video_speed_s: float = 7.0
    video_max_ahead_s: float = 10.0
    video_step_pause_s: float = 1.0
    heartbeat_interval_s: float = 60.0
    timeout_s: float = 20.0
    max_retries_429: int = 3


class ApiError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


class Gated(Exception):
    """A feature is blocked by a missing or unverified input; the message says which."""


class Stopped(Exception):
    """The caller asked the run to stop; heartbeat runners still send their final heartbeat."""


@dataclass(frozen=True)
class RunOptions:
    channel_id: str | None = None
    exe_path: str | None = None
    play_template: dict[str, Any] | None = None


@dataclass(frozen=True)
class Quest:
    quest_id: str
    name: str
    application_id: str | None
    tasks: dict[str, int]
    enrolled_at: datetime | None
    completed: bool
    claimed: bool
    progress: dict[str, float]
    expires_at: datetime | None


@dataclass(frozen=True, repr=False)
class Credentials:
    token: str
    super_properties: str
    user_agent: str

    def __repr__(self) -> str:
        return "Credentials(<redacted>)"


# ----------------------------- pure functions -------------------------------

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$")


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    match = _TS.match(value)
    if match is None:
        return None
    base, frac, tz = match.groups()
    frac = (frac or "0")[:6].ljust(6, "0")
    tz = "+00:00" if tz in (None, "Z") else tz
    try:
        return datetime.fromisoformat(f"{base}.{frac}{tz}")
    except ValueError:
        return None


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_quest(raw: Any) -> Quest | None:
    quest_id = _dict(raw).get("id")
    if not isinstance(quest_id, str) or not SNOWFLAKE.match(quest_id):
        return None
    config = _dict(raw.get("config"))
    task_cfg = _dict(config.get("task_config") or config.get("task_config_v2"))
    tasks: dict[str, int] = {}
    for task_type, task in _dict(task_cfg.get("tasks")).items():
        target = _dict(task).get("target")
        if _is_num(target) and 0 < target <= MAX_TARGET_S:
            tasks[str(task_type)] = math.ceil(target)
    name = _dict(config.get("messages")).get("quest_name")
    app_id = _dict(config.get("application")).get("id")
    status = _dict(raw.get("user_status"))
    progress: dict[str, float] = {}
    for key, entry in _dict(status.get("progress")).items():
        value = _dict(entry).get("value")
        if _is_num(value) and value >= 0:
            progress[str(key)] = float(value)
    return Quest(
        quest_id=quest_id,
        name=name if isinstance(name, str) and name else quest_id,
        application_id=app_id if isinstance(app_id, str) and SNOWFLAKE.match(app_id) else None,
        tasks=tasks,
        enrolled_at=parse_ts(status.get("enrolled_at")),
        completed=bool(status.get("completed_at")),
        claimed=bool(status.get("claimed_at")),
        progress=progress,
        expires_at=parse_ts(config.get("expires_at")),
    )


def quests_from_payload(payload: Any) -> list[Quest]:
    items = payload.get("quests") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("unexpected quest list shape")
    parsed = (parse_quest(item) for item in items[:MAX_QUESTS])
    return [quest for quest in parsed if quest is not None]


def skip_reason(quest: Quest, now: datetime) -> str | None:
    if quest.completed:
        return "already completed"
    if not quest.tasks:
        return "no parseable tasks"
    if quest.expires_at is not None and quest.expires_at <= now:
        return f"expired {quest.expires_at.isoformat(timespec='seconds')}"
    return None


def select_pending(quests: list[Quest], now: datetime) -> list[Quest]:
    return [q for q in quests if skip_reason(q, now) is None]


def pick_task(quest: Quest) -> tuple[str, int] | None:
    for task_type in TASK_PRIORITY:
        if task_type in quest.tasks:
            return task_type, quest.tasks[task_type]
    return None


def plan_video_step(done_s: float, target_s: float, elapsed_s: float,
                    speed_s: float, max_ahead_s: float) -> float | None:
    """Next timestamp to report, or None when finished or when pacing says wait."""
    if done_s >= target_s:
        return None
    needed = min(speed_s, target_s - done_s)
    headroom = (elapsed_s + max_ahead_s) - done_s
    if headroom < needed:
        return None
    return min(float(target_s), done_s + speed_s)


def completed_in(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return False
    return bool(resp.get("completed_at")) or bool(_dict(resp.get("user_status")).get("completed_at"))


def progress_in(resp: Any, task_type: str) -> float | None:
    if not isinstance(resp, dict):
        return None
    status = _dict(resp.get("user_status")) or resp
    value = _dict(_dict(status.get("progress")).get(task_type)).get("value")
    return float(value) if _is_num(value) and value >= 0 else None


def _fill(node: Any, values: Mapping[str, str]) -> Any:
    if isinstance(node, str):
        for key, val in values.items():
            node = node.replace("{" + key + "}", val)
        return node
    if isinstance(node, dict):
        return {k: _fill(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_fill(v, values) for v in node]
    return node


def build_heartbeat_body(cfg: Config, task_type: str, application_id: str | None,
                         executable_path: str | None, channel_id: str | None,
                         play_template: dict[str, Any] | None,
                         terminated: bool) -> dict[str, Any]:
    if task_type == "PLAY_ACTIVITY":
        if channel_id is None or not SNOWFLAKE.match(channel_id):
            raise Gated("PLAY_ACTIVITY needs --channel-id (a channel id your account can access)")
        key = cfg.activity_stream_key.replace("{channel_id}", channel_id)
        return {"stream_key": key, cfg.terminal_key: terminated}
    if task_type == "PLAY_ON_DESKTOP":
        if application_id is None:
            raise Gated("quest carries no application id for PLAY_ON_DESKTOP")
        values = {"application_id": application_id}
        if executable_path is not None:
            values["executable_path"] = executable_path
        body = _fill(cfg.play_template if play_template is None else play_template, values)
        left = re.search(r"\{(application_id|executable_path)\}", json.dumps(body))
        if left is not None:
            raise Gated(f"{left.group(0)} is unresolved: the executable lookup found nothing, so pass "
                        "--exe-path with the executable_path your real client sent for this game")
        body[cfg.terminal_key] = terminated
        return body
    raise Gated(f"{task_type} has no implementation")


def plausible_path(value: str) -> bool:
    return 0 < len(value) <= 260 and all(32 <= ord(c) != 127 for c in value)


def pick_executable(payload: Any, application_id: str) -> str | None:
    if not isinstance(payload, list):
        return None
    for app in payload[:50]:
        if not isinstance(app, dict) or app.get("id") != application_id:
            continue
        executables = app.get("executables")
        for exe in (executables[:50] if isinstance(executables, list) else []):
            name = _dict(exe).get("name")
            if _dict(exe).get("os") != "win32" or _dict(exe).get("is_launcher") or not isinstance(name, str):
                continue
            name = name.lstrip(">").strip()
            if plausible_path(name):
                return name
    return None


def parse_retry_after(header: str | None, body: bytes) -> float:
    candidates: list[Any] = [header]
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            candidates.append(data.get("retry_after"))
    except (ValueError, TypeError):
        pass
    for candidate in candidates:
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return min(max(value, 0.5), 120.0)
    return 5.0


def validate_super_properties(value: str) -> bool:
    try:
        data = json.loads(base64.b64decode(value, validate=True))
    except (binascii.Error, ValueError):
        return False
    return isinstance(data, dict) and bool(data)


def valid_header_value(value: str) -> bool:
    return 0 < len(value) <= 8192 and all(32 <= ord(c) != 127 for c in value)


def load_credentials(env: Mapping[str, str]) -> Credentials:
    token = env.get("DISCORD_TOKEN", "").strip().strip('"')
    sprops = env.get("DISCORD_SUPER_PROPERTIES", "").strip()
    agent = env.get("DISCORD_USER_AGENT", "").strip()
    missing = [name for name, val in (("DISCORD_TOKEN", token),
                                      ("DISCORD_SUPER_PROPERTIES", sprops),
                                      ("DISCORD_USER_AGENT", agent)) if not val]
    if missing:
        raise Gated(f"unset: {', '.join(missing)}. Open discord.com/app, F12 -> Network, click any "
                    "/api request, copy the Authorization, X-Super-Properties and User-Agent headers.")
    if not (valid_header_value(token) and " " not in token):
        raise Gated("DISCORD_TOKEN is not a plausible header value")
    if not (valid_header_value(sprops) and validate_super_properties(sprops)):
        raise Gated("DISCORD_SUPER_PROPERTIES is not base64-encoded JSON")
    if not valid_header_value(agent):
        raise Gated("DISCORD_USER_AGENT is not a plausible header value")
    return Credentials(token, sprops, agent)


# --------------------------------- HTTP -------------------------------------

class Api:
    def __init__(self, creds: Credentials, cfg: Config) -> None:
        self._cfg = cfg
        self._headers = {
            "Authorization": creds.token,
            "Content-Type": "application/json",
            "User-Agent": creds.user_agent,
            "X-Super-Properties": creds.super_properties,
            "Accept": "*/*",
        }

    def request(self, method: str, path: str, body: Any = None) -> Any:
        url = self._cfg.api_base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        for attempt in range(self._cfg.max_retries_429 + 1):
            req = urllib.request.Request(url, data=data, method=method, headers=self._headers)
            try:
                with urllib.request.urlopen(req, timeout=self._cfg.timeout_s) as resp:
                    raw = resp.read(MAX_BODY_BYTES)
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                raw = exc.read(MAX_BODY_BYTES)
                if exc.code == 429 and attempt < self._cfg.max_retries_429:
                    wait = parse_retry_after(exc.headers.get("Retry-After"), raw)
                    log.warning("rate limited, waiting %.1fs", wait)
                    time.sleep(wait)
                    continue
                raise ApiError(exc.code, raw.decode("utf-8", "replace")[:500]) from None
            except OSError as exc:
                raise ApiError(0, f"network error: {exc}") from None
            except ValueError:
                raise ApiError(0, "response was not valid JSON") from None
        raise ApiError(429, "rate limit retries exhausted")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fetch_quests(api: Api, cfg: Config) -> list[Quest]:
    return quests_from_payload(api.request("GET", cfg.path_list))


def fetch_executable(api: Api, cfg: Config, application_id: str) -> str | None:
    path = cfg.path_apps.format(application_id=application_id)
    try:
        return pick_executable(api.request("GET", path), application_id)
    except ApiError as exc:
        log.warning("executable lookup failed: %s", exc)
        return None


def refresh(api: Api, cfg: Config, quest_id: str) -> Quest:
    for quest in fetch_quests(api, cfg):
        if quest.quest_id == quest_id:
            return quest
    raise ApiError(0, "quest disappeared from the list")


# -------------------------------- runners -----------------------------------

def pause(seconds: float, stop: threading.Event | None,
          sleep: Callable[[float], None] = time.sleep) -> None:
    if stop is None:
        sleep(seconds)
    elif stop.wait(seconds):
        raise Stopped()


def run_video(api: Api, cfg: Config, quest: Quest, task_type: str, target: int,
              now: Callable[[], datetime] = utcnow,
              sleep: Callable[[float], None] = time.sleep,
              stop: threading.Event | None = None,
              report: Callable[[float, int], None] | None = None) -> bool:
    if quest.enrolled_at is None:
        raise Gated("enrollment time unknown, cannot pace video progress")
    path = cfg.path_video.format(quest_id=quest.quest_id)
    done = min(quest.progress.get(task_type, 0.0), target - 1.0)
    deadline = time.monotonic() + target * 2 + 120
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            raise Stopped()
        elapsed = (now() - quest.enrolled_at).total_seconds()
        stamp = plan_video_step(done, target, elapsed, cfg.video_speed_s, cfg.video_max_ahead_s)
        if stamp is None:
            if done >= target:
                return False
            pause(1.0, stop, sleep)
            continue
        resp = api.request("POST", path, {"timestamp": stamp})
        done = stamp
        log.info("%s: reported %.1fs / %ds", quest.name, done, target)
        if report is not None:
            report(done, target)
        if completed_in(resp):
            return True
        pause(cfg.video_step_pause_s, stop, sleep)
    return False


def run_heartbeat(api: Api, cfg: Config, quest: Quest, task_type: str, target: int,
                  channel_id: str | None, play_template: dict[str, Any] | None,
                  exe_path: str | None,
                  sleep: Callable[[float], None] = time.sleep,
                  stop: threading.Event | None = None,
                  report: Callable[[float, int], None] | None = None) -> bool:
    path = cfg.path_heartbeat.format(quest_id=quest.quest_id)
    live = build_heartbeat_body(cfg, task_type, quest.application_id, exe_path, channel_id,
                                play_template, False)
    last = quest.progress.get(task_type, 0.0)
    deadline = time.monotonic() + max(target - last, 0.0) * 1.5 + 300
    started = False
    try:
        while time.monotonic() < deadline:
            if stop is not None and stop.is_set():
                raise Stopped()
            resp = api.request("POST", path, live)
            started = True
            if completed_in(resp):
                return True
            value = progress_in(resp, task_type)
            if value is not None:
                last = value
                log.info("%s: server progress %.0f / %ds", quest.name, last, target)
                if report is not None:
                    report(last, target)
                if last >= target:
                    return True
            pause(cfg.heartbeat_interval_s, stop, sleep)
        return False
    finally:
        if started:
            stop = build_heartbeat_body(cfg, task_type, quest.application_id, exe_path, channel_id,
                                        play_template, True)
            try:
                api.request("POST", path, stop)
            except ApiError:
                pass


def drive_quest(api: Api, cfg: Config, quest: Quest, opts: RunOptions,
                stop: threading.Event | None = None,
                report: Callable[[float, int], None] | None = None) -> bool:
    picked = pick_task(quest)
    if picked is None:
        raise Gated(f"task types {sorted(quest.tasks)} are not supported")
    task_type, target = picked
    exe_path = None
    if task_type not in VIDEO_TASKS:
        exe_path = opts.exe_path
        if task_type == "PLAY_ON_DESKTOP" and exe_path is None and quest.application_id:
            exe_path = fetch_executable(api, cfg, quest.application_id)
        # Preflight: fail before enrolling if the body cannot be built.
        build_heartbeat_body(cfg, task_type, quest.application_id, exe_path,
                             opts.channel_id, opts.play_template, False)
    if quest.enrolled_at is None:
        api.request("POST", cfg.path_enroll.format(quest_id=quest.quest_id), cfg.enroll_body)
        quest = refresh(api, cfg, quest.quest_id)
    if task_type in VIDEO_TASKS:
        return run_video(api, cfg, quest, task_type, target, stop=stop, report=report)
    return run_heartbeat(api, cfg, quest, task_type, target, opts.channel_id,
                         opts.play_template, exe_path, stop=stop, report=report)


# ---------------------------------- CLI -------------------------------------

def load_play_template(path: str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        with open(path, "rb") as handle:
            raw = handle.read(65_537)
        if len(raw) > 65_536:
            raise Gated("--play-body-file is larger than 64 KiB")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise Gated(f"cannot read --play-body-file: {exc}") from None
    if not isinstance(data, dict):
        raise Gated("--play-body-file must contain a JSON object")
    return data


def quest_state(quest: Quest) -> str:
    if quest.claimed:
        return "claimed"
    if quest.completed:
        return "completed"
    return "enrolled" if quest.enrolled_at else "new"


def cmd_list(api: Api, cfg: Config) -> int:
    quests = fetch_quests(api, cfg)
    for quest in quests:
        tasks = ", ".join(f"{t}={s}s" for t, s in quest.tasks.items()) or "unparsed"
        until = quest.expires_at.isoformat(timespec="seconds") if quest.expires_at else "unknown"
        print(f"{quest.quest_id}  [{quest_state(quest):9}]  {quest.name}  ({tasks})  until {until}")
    if not quests:
        print("no quests returned")
    return 0


def cmd_run(api: Api, cfg: Config, args: argparse.Namespace) -> int:
    opts = RunOptions(channel_id=args.channel_id, exe_path=args.exe_path,
                      play_template=load_play_template(args.play_body_file))
    quests = fetch_quests(api, cfg)
    now = utcnow()
    scope = [q for q in quests if not args.quest_id or q.quest_id == args.quest_id]
    if args.quest_id and not scope:
        log.warning("quest id %s is not in your quest list", args.quest_id)
        return 1
    pending = select_pending(scope, now)
    if not pending:
        for quest in scope[:10]:
            log.info("%s: filtered out, %s", quest.name, skip_reason(quest, now))
        log.info("nothing pending (%d filtered out; local clock reads %s UTC)",
                 len(scope), now.isoformat(timespec="seconds"))
        return 0
    failures = 0
    for quest in pending:
        picked = pick_task(quest)
        if picked is None:
            log.warning("%s: skipped, task types %s are not supported", quest.name, sorted(quest.tasks))
            failures += 1
            continue
        task_type, target = picked
        if args.dry_run:
            log.info("%s: would run %s (%ds)", quest.name, task_type, target)
            continue
        try:
            ok = drive_quest(api, cfg, quest, opts)
        except (ApiError, Gated) as exc:
            log.error("%s: %s", quest.name, exc)
            failures += 1
            continue
        log.info("%s: %s", quest.name, "completed" if ok else "not confirmed, check the Quests page")
        failures += 0 if ok else 1
        time.sleep(2.0)
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="discord_quests", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="show quests and their state")
    run = sub.add_parser("run", help="drive pending quests to completion")
    run.add_argument("--quest-id", help="only this quest")
    run.add_argument("--channel-id", help="channel id for PLAY_ACTIVITY quests")
    run.add_argument("--exe-path", help="executable_path to send for PLAY_ON_DESKTOP quests "
                                        "(use with --quest-id; default: looked up per game)")
    run.add_argument("--play-body-file", help="JSON body replacing the built-in PLAY_ON_DESKTOP one "
                                              "(placeholders {application_id}, {executable_path})")
    run.add_argument("--dry-run", action="store_true", help="show the plan, send nothing")
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    args = build_parser().parse_args(argv)
    cfg = Config()
    try:
        for name in ("quest_id", "channel_id"):
            value = getattr(args, name, None)
            if value is not None and not SNOWFLAKE.match(value):
                raise Gated(f"--{name.replace('_', '-')} is not a valid id")
        exe = getattr(args, "exe_path", None)
        if exe is not None and not plausible_path(exe):
            raise Gated("--exe-path is not a plausible path")
        api = Api(load_credentials(os.environ if env is None else env), cfg)
        return cmd_list(api, cfg) if args.command == "list" else cmd_run(api, cfg, args)
    except Gated as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"api error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
