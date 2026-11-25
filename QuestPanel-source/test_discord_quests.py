"""Unit tests for the pure functions. All data below is SYNTHETIC: its shape follows
my recollection of the quest payload, it was not captured from Discord."""
import base64
import json
import unittest
from datetime import datetime, timezone

import discord_quests as dq

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
QID = "1234567890123456789"
APP = "111111111111111111"

SAMPLE = {
    "quests": [{
        "id": QID,
        "config": {
            "expires_at": "2099-01-01T00:00:00.000000+00:00",
            "messages": {"quest_name": "Sample"},
            "application": {"id": APP},
            "task_config": {"tasks": {"WATCH_VIDEO": {"target": 30}}},
        },
        "user_status": {
            "enrolled_at": "2026-11-22T10:00:00.5Z",
            "progress": {"WATCH_VIDEO": {"value": 7}},
        },
    }]
}


def make_quest(**over):
    base = dict(quest_id=QID, name="q", application_id=APP, tasks={"WATCH_VIDEO": 30},
                enrolled_at=None, completed=False, claimed=False, progress={}, expires_at=None)
    base.update(over)
    return dq.Quest(**base)


class ParsingTests(unittest.TestCase):
    def test_sample_parses(self):
        (quest,) = dq.quests_from_payload(SAMPLE)
        self.assertEqual(quest.tasks, {"WATCH_VIDEO": 30})
        self.assertEqual(quest.progress, {"WATCH_VIDEO": 7.0})
        self.assertEqual(quest.application_id, APP)
        self.assertIsNotNone(quest.enrolled_at)
        self.assertFalse(quest.completed)

    def test_bad_ids_and_targets_are_rejected(self):
        self.assertIsNone(dq.parse_quest({"id": "abc"}))
        self.assertIsNone(dq.parse_quest("nope"))
        for bad in (-1, 0, float("nan"), float("inf"), True, 10 ** 9, "30"):
            raw = {"id": QID, "config": {"task_config": {"tasks": {"WATCH_VIDEO": {"target": bad}}}}}
            self.assertEqual(dq.parse_quest(raw).tasks, {}, msg=repr(bad))

    def test_payload_shapes(self):
        self.assertEqual(len(dq.quests_from_payload(SAMPLE["quests"])), 1)
        with self.assertRaises(ValueError):
            dq.quests_from_payload({"quests": "x"})

    def test_timestamps(self):
        self.assertEqual(dq.parse_ts("2026-11-22T10:00:00Z").hour, 10)
        self.assertEqual(dq.parse_ts("2026-11-22T10:00:00.123+00:00").microsecond, 123000)
        self.assertIsNone(dq.parse_ts("yesterday"))
        self.assertIsNone(dq.parse_ts(None))

    def test_select_pending_and_pick(self):
        live = make_quest()
        done = make_quest(completed=True)
        expired = make_quest(expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        empty = make_quest(tasks={})
        self.assertEqual(dq.select_pending([live, done, expired, empty], NOW), [live])
        both = make_quest(tasks={"PLAY_ON_DESKTOP": 900, "WATCH_VIDEO": 30})
        self.assertEqual(dq.pick_task(both), ("WATCH_VIDEO", 30))
        self.assertIsNone(dq.pick_task(make_quest(tasks={"STREAM_ON_DESKTOP": 900})))

    def test_skip_reasons(self):
        self.assertIsNone(dq.skip_reason(make_quest(), NOW))
        self.assertEqual(dq.skip_reason(make_quest(completed=True), NOW), "already completed")
        self.assertEqual(dq.skip_reason(make_quest(tasks={}), NOW), "no parseable tasks")
        old = make_quest(expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(dq.skip_reason(old, NOW).startswith("expired 2020-01-01"))


class PacingTests(unittest.TestCase):
    def test_video_steps(self):
        self.assertEqual(dq.plan_video_step(0, 30, 0, 7, 10), 7)
        self.assertIsNone(dq.plan_video_step(7, 30, 0, 7, 10))
        self.assertEqual(dq.plan_video_step(7, 30, 4, 7, 10), 14)
        self.assertEqual(dq.plan_video_step(28, 30, 100, 7, 10), 30)
        self.assertIsNone(dq.plan_video_step(30, 30, 100, 7, 10))

    def test_response_helpers(self):
        self.assertTrue(dq.completed_in({"completed_at": "2026-11-22T10:00:00Z"}))
        self.assertTrue(dq.completed_in({"user_status": {"completed_at": "x"}}))
        self.assertFalse(dq.completed_in({}))
        self.assertFalse(dq.completed_in(None))
        self.assertEqual(dq.progress_in({"progress": {"A": {"value": 3}}}, "A"), 3.0)
        self.assertEqual(dq.progress_in({"user_status": {"progress": {"A": {"value": 4}}}}, "A"), 4.0)
        self.assertIsNone(dq.progress_in({"progress": {"A": {"value": -1}}}, "A"))

    def test_retry_after(self):
        self.assertEqual(dq.parse_retry_after("3", b"{}"), 3.0)
        self.assertEqual(dq.parse_retry_after(None, b'{"retry_after": 0.01}'), 0.5)
        self.assertEqual(dq.parse_retry_after("9999", b""), 120.0)
        self.assertEqual(dq.parse_retry_after("x", b"garbage"), 5.0)


class BodyTests(unittest.TestCase):
    cfg = dq.Config()

    # Copied from the user's own capture of a real PLAY_ON_DESKTOP heartbeat.
    CAPTURED_APP = "363445589247131668"
    CAPTURED_EXE = "version-2c6fd5e09aaf4cfa/robloxplayerbeta.exe"

    def test_activity_body(self):
        body = dq.build_heartbeat_body(self.cfg, "PLAY_ACTIVITY", None, None,
                                       "222222222222222222", None, False)
        self.assertEqual(body, {"stream_key": "call:222222222222222222:1", "terminal": False})
        with self.assertRaises(dq.Gated):
            dq.build_heartbeat_body(self.cfg, "PLAY_ACTIVITY", None, None, None, None, False)

    def test_desktop_body_matches_captured_shape(self):
        body = dq.build_heartbeat_body(self.cfg, "PLAY_ON_DESKTOP", self.CAPTURED_APP,
                                       self.CAPTURED_EXE, None, None, False)
        self.assertEqual(body, {"application_id": self.CAPTURED_APP,
                                "executable_path": self.CAPTURED_EXE, "terminal": False})
        stop = dq.build_heartbeat_body(self.cfg, "PLAY_ON_DESKTOP", self.CAPTURED_APP,
                                       self.CAPTURED_EXE, None, None, True)
        self.assertTrue(stop["terminal"])
        self.assertEqual(self.cfg.play_template["executable_path"], "{executable_path}")

    def test_desktop_body_is_gated_on_missing_inputs(self):
        with self.assertRaises(dq.Gated):
            dq.build_heartbeat_body(self.cfg, "PLAY_ON_DESKTOP", APP, None, None, None, False)
        with self.assertRaises(dq.Gated):
            dq.build_heartbeat_body(self.cfg, "PLAY_ON_DESKTOP", None, self.CAPTURED_EXE,
                                    None, None, False)

    def test_custom_template_replaces_builtin(self):
        body = dq.build_heartbeat_body(self.cfg, "PLAY_ON_DESKTOP", APP, None, None,
                                       {"application_id": "{application_id}", "n": [1]}, True)
        self.assertEqual(body, {"application_id": APP, "n": [1], "terminal": True})

    def test_unknown_task_is_gated(self):
        with self.assertRaises(dq.Gated):
            dq.build_heartbeat_body(self.cfg, "STREAM_ON_DESKTOP", APP, None, None, None, False)


class ExecutableTests(unittest.TestCase):
    PAYLOAD = [{"id": APP, "executables": [
        {"os": "linux", "name": "game"},
        {"os": "win32", "name": "launcher.exe", "is_launcher": True},
        {"os": "win32", "name": ">game.exe", "is_launcher": False},
    ]}]

    def test_pick_executable(self):
        self.assertEqual(dq.pick_executable(self.PAYLOAD, APP), "game.exe")
        self.assertIsNone(dq.pick_executable(self.PAYLOAD, "999999999999999999"))
        self.assertIsNone(dq.pick_executable({"unexpected": 1}, APP))
        self.assertIsNone(dq.pick_executable([{"id": APP, "executables": "x"}], APP))

    def test_plausible_path(self):
        self.assertTrue(dq.plausible_path("a/b.exe"))
        self.assertFalse(dq.plausible_path(""))
        self.assertFalse(dq.plausible_path("x" * 261))
        self.assertFalse(dq.plausible_path("bad\npath"))


class CredentialTests(unittest.TestCase):
    def good_env(self):
        sprops = base64.b64encode(json.dumps({"os": "x"}).encode()).decode()
        return {"DISCORD_TOKEN": "tok.en.value", "DISCORD_SUPER_PROPERTIES": sprops,
                "DISCORD_USER_AGENT": "UA/1.0"}

    def test_missing_is_gated(self):
        with self.assertRaises(dq.Gated) as ctx:
            dq.load_credentials({})
        self.assertIn("DISCORD_TOKEN", str(ctx.exception))

    def test_valid_and_redacted(self):
        creds = dq.load_credentials(self.good_env())
        self.assertEqual(creds.token, "tok.en.value")
        self.assertNotIn("tok.en", repr(creds))

    def test_bad_values(self):
        env = self.good_env()
        env["DISCORD_SUPER_PROPERTIES"] = "not base64 json"
        with self.assertRaises(dq.Gated):
            dq.load_credentials(env)
        env = self.good_env()
        env["DISCORD_USER_AGENT"] = "bad\r\nInjected: 1"
        with self.assertRaises(dq.Gated):
            dq.load_credentials(env)


if __name__ == "__main__":
    unittest.main()
