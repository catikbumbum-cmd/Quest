"""Tests for the quest runners with a fake API and a fake clock. All payloads are
SYNTHETIC: shapes follow my recollection of the API, nothing was captured live."""
import threading
import unittest
from datetime import datetime, timedelta, timezone

import discord_quests as dq

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
QID = "1234567890123456789"
APP = "111111111111111111"


def make_quest(**over):
    base = dict(quest_id=QID, name="q", application_id=APP, tasks={"WATCH_VIDEO": 20},
                enrolled_at=NOW - timedelta(hours=1), completed=False, claimed=False,
                progress={}, expires_at=None)
    base.update(over)
    return dq.Quest(**base)


class FakeApi:
    def __init__(self, replies=(), clock=None):
        self.calls = []
        self.replies = list(replies)
        self.clock = clock

    def request(self, method, path, body=None):
        elapsed = self.clock.elapsed() if self.clock else None
        self.calls.append((method, path, body, elapsed))
        return self.replies.pop(0) if self.replies else {}

    def bodies(self, suffix):
        return [c[2] for c in self.calls if c[1].endswith(suffix)]


class FakeClock:
    def __init__(self, start):
        self.start = start
        self.t = start

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += timedelta(seconds=seconds)

    def elapsed(self):
        return (self.t - self.start).total_seconds()


DONE = {"completed_at": "2026-11-22T12:00:00Z"}
CFG = dq.Config(video_step_pause_s=0.0, heartbeat_interval_s=0.0)


class VideoTests(unittest.TestCase):
    def test_reports_steps_until_completed(self):
        api = FakeApi([{}, {}, DONE])
        ok = dq.run_video(api, CFG, make_quest(), "WATCH_VIDEO", 20, now=lambda: NOW,
                          sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual([b["timestamp"] for b in api.bodies("/video-progress")], [7.0, 14.0, 20.0])

    def test_unconfirmed_when_server_never_completes(self):
        api = FakeApi()
        ok = dq.run_video(api, CFG, make_quest(), "WATCH_VIDEO", 20, now=lambda: NOW,
                          sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertEqual(len(api.calls), 3)

    def test_never_runs_ahead_of_the_clock(self):
        clock = FakeClock(NOW)
        api = FakeApi([{}, {}, DONE], clock=clock)
        quest = make_quest(enrolled_at=NOW)
        cfg = dq.Config()
        ok = dq.run_video(api, cfg, quest, "WATCH_VIDEO", 20, now=clock.now, sleep=clock.sleep)
        self.assertTrue(ok)
        for _, _, body, elapsed in api.calls:
            self.assertLessEqual(body["timestamp"], elapsed + cfg.video_max_ahead_s)

    def test_requires_enrollment_time(self):
        with self.assertRaises(dq.Gated):
            dq.run_video(FakeApi(), CFG, make_quest(enrolled_at=None), "WATCH_VIDEO", 20)

    def test_stop_before_start_sends_nothing(self):
        stop = threading.Event()
        stop.set()
        api = FakeApi()
        with self.assertRaises(dq.Stopped):
            dq.run_video(api, CFG, make_quest(), "WATCH_VIDEO", 20, now=lambda: NOW, stop=stop)
        self.assertEqual(api.calls, [])

    def test_stop_during_run(self):
        stop = threading.Event()
        api = FakeApi([{}, {}, DONE])
        with self.assertRaises(dq.Stopped):
            dq.run_video(api, CFG, make_quest(), "WATCH_VIDEO", 20, now=lambda: NOW,
                         stop=stop, report=lambda done, target: stop.set())
        self.assertEqual(len(api.calls), 1)


def progress(value):
    return {"progress": {"PLAY_ON_DESKTOP": {"value": value}}}


class HeartbeatTests(unittest.TestCase):
    quest = make_quest(tasks={"PLAY_ON_DESKTOP": 900})

    def test_runs_until_server_reports_target_then_sends_terminal(self):
        api = FakeApi([progress(60), progress(900)])
        ok = dq.run_heartbeat(api, CFG, self.quest, "PLAY_ON_DESKTOP", 900, None, None, "g.exe",
                              sleep=lambda s: None)
        self.assertTrue(ok)
        bodies = api.bodies("/heartbeat")
        self.assertEqual([b["terminal"] for b in bodies], [False, False, True])
        self.assertEqual(bodies[0], {"application_id": APP, "executable_path": "g.exe", "terminal": False})

    def test_stop_still_sends_terminal_heartbeat(self):
        stop = threading.Event()
        api = FakeApi([progress(60), progress(120)])
        with self.assertRaises(dq.Stopped):
            dq.run_heartbeat(api, CFG, self.quest, "PLAY_ON_DESKTOP", 900, None, None, "g.exe",
                             stop=stop, report=lambda done, target: stop.set())
        bodies = api.bodies("/heartbeat")
        self.assertEqual([b["terminal"] for b in bodies], [False, True])

    def test_failed_body_sends_nothing(self):
        api = FakeApi()
        with self.assertRaises(dq.Gated):
            dq.run_heartbeat(api, CFG, self.quest, "PLAY_ON_DESKTOP", 900, None, None, None)
        self.assertEqual(api.calls, [])


class DriveTests(unittest.TestCase):
    def test_gates_before_enrolling(self):
        api = FakeApi()
        no_app = make_quest(tasks={"PLAY_ON_DESKTOP": 900}, application_id=None, enrolled_at=None)
        with self.assertRaises(dq.Gated):
            dq.drive_quest(api, CFG, no_app, dq.RunOptions(exe_path="g.exe"))
        unsupported = make_quest(tasks={"ACHIEVEMENT_IN_ACTIVITY": 4}, enrolled_at=None)
        with self.assertRaises(dq.Gated):
            dq.drive_quest(api, CFG, unsupported, dq.RunOptions())
        self.assertEqual(api.calls, [])

    def test_enrolls_refreshes_then_runs(self):
        enrolled = (dq.utcnow() - timedelta(hours=1)).isoformat()
        listing = {"quests": [{
            "id": QID,
            "config": {"messages": {"quest_name": "q"},
                       "task_config": {"tasks": {"WATCH_VIDEO": {"target": 14}}}},
            "user_status": {"enrolled_at": enrolled},
        }]}
        api = FakeApi([{}, listing, {}, DONE])
        quest = make_quest(tasks={"WATCH_VIDEO": 14}, enrolled_at=None)
        self.assertTrue(dq.drive_quest(api, CFG, quest, dq.RunOptions()))
        self.assertEqual([(c[0], c[1].rsplit("/", 1)[-1]) for c in api.calls],
                         [("POST", "enroll"), ("GET", "@me"),
                          ("POST", "video-progress"), ("POST", "video-progress")])


if __name__ == "__main__":
    unittest.main()
