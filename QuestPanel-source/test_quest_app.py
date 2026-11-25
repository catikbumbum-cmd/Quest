"""Tests for the window shell: pure helpers, engine selection with fake engines, close detection with a
fake process and clock, and the shutdown path that must still send the final heartbeat."""
import base64
import contextlib
import http.client
import io
import json
import sys
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock
from urllib.parse import urlsplit

import discord_quests as dq
import quest_app as qa
import quest_panel as qp

APP = "363445589247131668"
QID = "1234567890123456789"


class HelperTests(unittest.TestCase):
    def test_browser_candidates(self):
        env = {"PROGRAMFILES(X86)": r"C:\Program Files (x86)", "PROGRAMFILES": r"C:\Program Files",
               "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
        win = qa.browser_candidates(env, "win32")
        self.assertEqual(win[0], r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
        self.assertEqual(len(win), 6)
        self.assertTrue(all("msedge" in p for p in win[:3]) and all("chrome" in p for p in win[3:]))
        self.assertEqual(qa.browser_candidates({}, "win32"), [])
        self.assertIn("Edge", qa.browser_candidates({}, "darwin")[0])
        self.assertEqual(qa.browser_candidates({}, "linux")[0], "microsoft-edge")

    def test_find_browser(self):
        paths = [r"C:\a\msedge.exe", r"C:\b\chrome.exe"]
        self.assertEqual(qa.find_browser(paths, exists=lambda p: p.endswith("chrome.exe")), r"C:\b\chrome.exe")
        self.assertIsNone(qa.find_browser(paths, exists=lambda p: False))
        self.assertEqual(qa.find_browser(["x", "chromium"], which=lambda n: "/usr/bin/chromium" if n == "chromium" else None),
                         "/usr/bin/chromium")

    def test_app_command(self):
        cmd = qa.app_command("edge.exe", "http://127.0.0.1:1/#k=abc", "/tmp/p")
        self.assertEqual(cmd[0], "edge.exe")
        self.assertIn("--app=http://127.0.0.1:1/#k=abc", cmd)
        self.assertIn("--user-data-dir=/tmp/p", cmd)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def run_open(self, engine, *, webview, browser, webview_error=None):
        def webview_runner(module, url):
            self.calls.append(("webview", url))
            if webview_error:
                raise webview_error

        qa.open_window("http://u/", None, engine,
                       load=lambda: webview, find=lambda cands: browser,
                       webview_runner=webview_runner,
                       chromium_runner=lambda exe, url, panel: self.calls.append(("chromium", exe)))

    def test_auto_prefers_pywebview(self):
        self.run_open("auto", webview=object(), browser="edge.exe")
        self.assertEqual(self.calls, [("webview", "http://u/")])

    def test_auto_falls_back_to_browser(self):
        self.run_open("auto", webview=None, browser="edge.exe")
        self.assertEqual(self.calls, [("chromium", "edge.exe")])
        self.calls.clear()
        self.run_open("auto", webview=object(), browser="edge.exe", webview_error=RuntimeError("boom"))
        self.assertEqual(self.calls, [("webview", "http://u/"), ("chromium", "edge.exe")])

    def test_forced_engines_do_not_fall_back(self):
        with self.assertRaises(qa.AppError):
            self.run_open("webview", webview=None, browser="edge.exe")
        with self.assertRaises(qa.AppError):
            self.run_open("webview", webview=object(), browser="edge.exe", webview_error=RuntimeError("boom"))
        self.calls.clear()
        self.run_open("chromium", webview=object(), browser="edge.exe")
        self.assertEqual(self.calls, [("chromium", "edge.exe")])

    def test_nothing_available(self):
        with self.assertRaises(qa.AppError) as ctx:
            self.run_open("auto", webview=None, browser=None)
        self.assertIn("pywebview", str(ctx.exception))


class FakeProc:
    def __init__(self, clock, lifetime):
        self.clock, self.lifetime = clock, lifetime

    def wait(self):
        self.clock.t += self.lifetime


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FakePanel:
    def __init__(self, clock, polls_until):
        self.clock, self.polls_until = clock, polls_until

    def idle_seconds(self):
        if self.polls_until is None:
            return None
        return max(0.0, self.clock.t - self.polls_until)


class WaitTests(unittest.TestCase):
    def test_long_lived_process_means_window_closed_on_exit(self):
        clock = FakeClock()
        qa.wait_for_window(FakeProc(clock, 600), FakePanel(clock, None), clock=clock, sleep=clock.sleep)
        self.assertEqual(clock.t, 600)

    def test_instant_return_falls_back_to_page_polling(self):
        clock = FakeClock()
        qa.wait_for_window(FakeProc(clock, 0.5), FakePanel(clock, 40.0), clock=clock, sleep=clock.sleep)
        self.assertGreater(clock.t, 40.0 + qa.IDLE_LIMIT_S)

    def test_page_that_never_polls_is_given_up_on(self):
        clock = FakeClock()
        qa.wait_for_window(FakeProc(clock, 0.5), FakePanel(clock, None), clock=clock, sleep=clock.sleep)
        self.assertGreater(clock.t, qa.FIRST_POLL_LIMIT_S)
        self.assertLess(clock.t, qa.FIRST_POLL_LIMIT_S + 10)


class MainTests(unittest.TestCase):
    def test_server_is_up_while_window_is_open_and_gone_after(self):
        seen = {}

        def opener(url, panel, engine):
            parts = urlsplit(url)
            seen["fragment"] = parts.fragment
            conn = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=5)
            conn.request("GET", "/")
            seen["status"] = conn.getresponse().status
            conn.close()
            seen["port"] = parts.port

        self.assertEqual(qa.main([], opener=opener), 0)
        self.assertEqual(seen["status"], 200)
        self.assertTrue(seen["fragment"].startswith("k="))
        with self.assertRaises(OSError):
            http.client.HTTPConnection("127.0.0.1", seen["port"], timeout=2).request("GET", "/")

    def test_window_error_is_reported_and_returns_failure_code(self):
        reported = []

        def opener(url, panel, engine):
            raise qa.AppError("нет движка")

        self.assertEqual(qa.main([], opener=opener, report=lambda msg: reported.append(msg) or 1), 1)
        self.assertEqual(reported, ["нет движка"])


class FatalTests(unittest.TestCase):
    """fatal() must never open a blocking message box outside the packaged exe (it hung CI on Windows)."""

    def box_calls(self, frozen):
        fake_ctypes = mock.MagicMock()
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(sys, "frozen", frozen, create=True), \
                mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(qa.fatal("boom"), 1)
        return fake_ctypes.windll.user32.MessageBoxW.call_count

    def test_no_message_box_when_run_from_source(self):
        self.assertEqual(self.box_calls(frozen=False), 0)

    def test_message_box_in_the_packaged_exe(self):
        self.assertEqual(self.box_calls(frozen=True), 1)


class HeartbeatApi:
    def __init__(self, creds):
        self.calls = []
        enrolled = (dq.utcnow() - timedelta(hours=1)).isoformat()
        self.payload = {"quests": [{
            "id": QID,
            "config": {"messages": {"quest_name": "Game"}, "application": {"id": APP},
                       "task_config": {"tasks": {"PLAY_ON_DESKTOP": {"target": 900}}}},
            "user_status": {"enrolled_at": enrolled},
        }]}

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/quests/@me"):
            return self.payload
        if method == "GET":
            return [{"id": APP, "executables": [{"os": "win32", "name": ">g.exe"}]}]
        return {"progress": {"PLAY_ON_DESKTOP": {"value": 60}}}


class ShutdownTests(unittest.TestCase):
    def test_shutdown_during_a_run_sends_the_final_heartbeat(self):
        apis = []

        def factory(creds):
            apis.append(HeartbeatApi(creds))
            return apis[0]

        panel = qp.Panel(api_factory=factory, env={}, pause_s=0.0)  # default 60 s heartbeat interval
        sprops = base64.b64encode(json.dumps({"os": "x"}).encode()).decode()
        panel.connect({"token": "t", "super_properties": sprops, "user_agent": "UA"})
        panel.start_run({"quest_ids": [QID]})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not [c for c in apis[0].calls if c[1].endswith("/heartbeat")]:
            time.sleep(0.02)
        started = time.monotonic()
        panel.shutdown(timeout=5)
        self.assertLess(time.monotonic() - started, 4)
        beats = [c[2] for c in apis[0].calls if c[1].endswith("/heartbeat")]
        self.assertEqual([b["terminal"] for b in beats], [False, True])
        self.assertEqual(beats[0]["executable_path"], "g.exe")
        self.assertFalse(panel.state(0)["running"])

    def test_idle_seconds(self):
        panel = qp.Panel(api_factory=HeartbeatApi, env={})
        self.assertIsNone(panel.idle_seconds())
        panel.state(0)
        self.assertLess(panel.idle_seconds(), 1.0)
        self.assertGreater(panel.idle_seconds(clock=lambda: time.monotonic() + 100), 99)
        panel.close()


if __name__ == "__main__":
    unittest.main()