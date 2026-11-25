"""Tests for the panel: pure helpers, then a real HTTP server on an ephemeral port with a
fake Discord API behind it. Payloads are SYNTHETIC."""
import base64
import http.client
import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

import discord_quests as dq
import quest_panel as qp

NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
QID = "1234567890123456789"
QID2 = "1234567890123456790"
SECRET = "tok.en.secret"


def make_quest(qid=QID, **over):
    base = dict(quest_id=qid, name="Sample", application_id="111111111111111111",
                tasks={"WATCH_VIDEO": 14}, enrolled_at=NOW - timedelta(hours=1), completed=False,
                claimed=False, progress={}, expires_at=None)
    base.update(over)
    return dq.Quest(**base)


class FakeApi:
    def __init__(self, creds):
        self.creds = creds
        self.calls = []
        enrolled = (dq.utcnow() - timedelta(hours=1)).isoformat()
        self.payload = {"quests": [
            {"id": QID, "config": {"messages": {"quest_name": "Video quest"},
                                   "task_config": {"tasks": {"WATCH_VIDEO": {"target": 14}}}},
             "user_status": {"enrolled_at": enrolled}},
            {"id": QID2, "config": {"messages": {"quest_name": "Unsupported"},
                                    "task_config": {"tasks": {"ACHIEVEMENT_IN_ACTIVITY": {"target": 4}}}},
             "user_status": {}},
        ]}

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/quests/@me"):
            return self.payload
        if path.endswith("/video-progress") and body["timestamp"] >= 14:
            return {"completed_at": "2026-11-22T12:00:00Z"}
        return {}


class HelperTests(unittest.TestCase):
    def test_quest_view(self):
        view = qp.quest_view(make_quest(progress={"WATCH_VIDEO": 99.0}), None, NOW)
        self.assertEqual((view["task"], view["target"], view["done"]), ("WATCH_VIDEO", 14, 14.0))
        self.assertTrue(view["selectable"])
        gone = qp.quest_view(make_quest(expires_at=NOW - timedelta(days=1)), None, NOW)
        self.assertFalse(gone["selectable"])
        self.assertTrue(gone["skip_reason"].startswith("expired"))
        odd = qp.quest_view(make_quest(tasks={"ACHIEVEMENT_IN_ACTIVITY": 4}), None, NOW)
        self.assertFalse(odd["supported"])

    def test_parse_run_request(self):
        quests = [make_quest(), make_quest(QID2, tasks={"ACHIEVEMENT_IN_ACTIVITY": 4})]
        req = qp.parse_run_request({"quest_ids": [QID, QID], "dry_run": True}, quests, NOW)
        self.assertEqual((req.quest_ids, req.dry_run), ((QID,), True))
        bad = [{}, {"quest_ids": []}, {"quest_ids": ["abc"]}, {"quest_ids": [QID2]},
               {"quest_ids": ["999999999999999999"]}, {"quest_ids": [QID], "channel_id": "x"},
               {"quest_ids": [QID], "dry_run": "yes"}, {"quest_ids": [QID], "exe_path": "bad\npath"},
               {"quest_ids": [QID], "channel_id": 5}]
        for data in bad:
            with self.assertRaises(qp.PanelError, msg=repr(data)):
                qp.parse_run_request(data, quests, NOW)

    def test_exe_path_needs_a_single_quest(self):
        quests = [make_quest(), make_quest(QID2)]
        with self.assertRaises(qp.PanelError):
            qp.parse_run_request({"quest_ids": [QID, QID2], "exe_path": "a.exe"}, quests, NOW)
        req = qp.parse_run_request({"quest_ids": [QID], "exe_path": " a.exe "}, quests, NOW)
        self.assertEqual(req.exe_path, "a.exe")

    def test_credential_source(self):
        env = {"DISCORD_TOKEN": "x"}
        self.assertIs(qp.credential_source({"use_env": True}, env), env)
        self.assertEqual(qp.credential_source({"token": "t"}, env)["DISCORD_TOKEN"], "t")
        with self.assertRaises(qp.PanelError):
            qp.credential_source({"token": 5}, env)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fakes = []

        def factory(creds):
            fake = FakeApi(creds)
            cls.fakes.append(fake)
            return fake

        cfg = dq.Config(video_step_pause_s=0.0)
        cls.panel = qp.Panel(cfg=cfg, api_factory=factory, env={}, pause_s=0.0)
        cls.key = "test-key"
        cls.server = qp.PanelServer(("127.0.0.1", 0), cls.panel, cls.key)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        sprops = base64.b64encode(json.dumps({"os": "x"}).encode()).decode()
        cls.creds = {"token": SECRET, "super_properties": sprops, "user_agent": "UA/1.0"}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.panel.close()

    def call(self, method, path, body=None, host=None, key=True, ctype="application/json", origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if key:
            headers["X-Panel-Key"] = self.key
        if ctype:
            headers["Content-Type"] = ctype
        if origin:
            headers["Origin"] = origin
        data = None if body is None else json.dumps(body)
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, resp.headers, raw

    def test_page_gets_a_fresh_nonce_and_csp(self):
        status, headers, raw = self.call("GET", "/", key=False, ctype=None)
        text = raw.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertNotIn(qp.NONCE_TOKEN, text)
        nonce = headers["Content-Security-Policy"].split("'nonce-")[1].split("'")[0]
        self.assertIn(f'nonce="{nonce}"', text)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

    def test_foreign_host_origin_and_missing_key_are_rejected(self):
        self.assertEqual(self.call("GET", "/", host="evil.example", key=False, ctype=None)[0], 403)
        self.assertEqual(self.call("GET", "/api/state", key=False, ctype=None)[0], 403)
        self.assertEqual(self.call("POST", "/api/stop", {}, origin="http://evil.example")[0], 403)
        self.assertEqual(self.call("POST", "/api/stop", {}, key=False)[0], 403)

    def test_bad_requests(self):
        self.assertEqual(self.call("POST", "/api/stop", {}, ctype="text/plain")[0], 415)
        self.assertEqual(self.call("POST", "/api/nothing", {})[0], 404)
        self.assertEqual(self.call("GET", "/elsewhere", key=False, ctype=None)[0], 404)
        status, _, _ = self.call("POST", "/api/run", {"quest_ids": []})
        self.assertEqual(status, 409)  # not connected yet

    def test_full_flow_connect_dry_run_run_disconnect(self):
        status, _, raw = self.call("POST", "/api/connect", {"token": "x"})
        self.assertEqual(status, 400)  # missing fields are gated, not sent anywhere
        status, _, raw = self.call("POST", "/api/connect", self.creds)
        self.assertEqual(status, 200)
        state = json.loads(self.call("GET", "/api/state?since=0", ctype=None)[2])
        self.assertTrue(state["connected"])
        self.assertEqual([q["selectable"] for q in state["quests"]], [True, False])
        self.assertNotIn(SECRET, json.dumps(state))

        self.assertEqual(self.call("POST", "/api/run", {"quest_ids": [QID2]})[0], 400)
        self.assertEqual(self.call("POST", "/api/run", {"quest_ids": [QID], "dry_run": True})[0], 200)
        state = self.wait_idle()
        self.assertEqual(state["quests"][0]["run"]["status"], "planned")
        posts = [c for fake in self.fakes for c in fake.calls if c[0] == "POST"]
        self.assertEqual(posts, [])

        self.assertEqual(self.call("POST", "/api/run", {"quest_ids": [QID]})[0], 200)
        state = self.wait_idle()
        self.assertEqual(state["quests"][0]["run"]["status"], "completed")
        self.assertEqual(state["quests"][0]["run"]["done"], 14.0)
        self.assertTrue(any("completed" in e["msg"] for e in state["log"] + self.panel.state(0)["log"]))

        self.assertEqual(self.call("POST", "/api/disconnect", {})[0], 200)
        self.assertFalse(json.loads(self.call("GET", "/api/state", ctype=None)[2])["connected"])

    def wait_idle(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = json.loads(self.call("GET", "/api/state?since=0", ctype=None)[2])
            if not state["running"]:
                return state
            time.sleep(0.05)
        self.fail("run did not finish")


if __name__ == "__main__":
    unittest.main()
