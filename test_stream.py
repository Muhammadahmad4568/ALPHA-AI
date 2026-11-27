"""
tests/test_stream.py - verifies the dashboard server and the SSE event broker.

    python -m unittest tests.test_stream -v

Runs against the REAL uvicorn when it is installed, otherwise against tests/asgi_host.py (stdlib). Every test targets
one way the page could end up stuck on "Reconnecting - Alpha's backend isn't answering".
"""
import json
import time
import unittest

import requests

from tests.fixtures import SSEClient, Server


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.srv = Server(heartbeat=0.3)
        self.addCleanup(self.srv.stop)
        self.clients = []

    def open(self, **kw) -> SSEClient:
        c = SSEClient(self.srv.url, token=kw.pop("token", self.srv.token), **kw)
        self.clients.append(c)
        self.addCleanup(c.close)
        return c

    # ------------------------------------------------------------------ startup
    def test_startup_endpoints_answer(self):
        r = requests.get(self.srv.url + "api/ping", timeout=3).json()
        self.assertTrue(r["ok"]); self.assertEqual(r["app"], "alpha-ai"); self.assertIn("server_id", r)
        page = requests.get(self.srv.url, timeout=3)
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'content="{self.srv.token}"', page.text)            # token injected
        self.assertNotIn("__ALPHA_TOKEN__", page.text)
        for asset, ctype in (("static/app.js", "javascript"), ("static/style.css", "css")):
            a = requests.get(self.srv.url + asset, timeout=3)
            self.assertEqual(a.status_code, 200); self.assertIn(ctype, a.headers["content-type"])
        self.assertEqual(requests.get(self.srv.url + "static/../main.py", timeout=3).status_code, 404)
        self.assertEqual(requests.get(self.srv.url + "nope", timeout=3).status_code, 404)
        self.assertIn("no-store", page.headers["cache-control"]); self.assertEqual(page.headers["x-frame-options"], "DENY")

    def test_first_event_is_hello_with_padding_and_retry(self):
        c = self.open()
        hello = c.wait_for(lambda e: e["type"] == "hello", 3)
        self.assertIsNotNone(hello, "no hello snapshot arrived")
        self.assertEqual(c.status, 200)
        self.assertTrue(c.raw[0].startswith(":") and len(c.raw[0]) > 1000, "buffer-defeating padding comment missing")
        self.assertIn("retry: 1500", c.raw)
        self.assertEqual(hello["unicode"], "اردو ✓")                      # UTF-8 intact over the wire
        self.assertIn("server_id", hello)

    def test_verify_stream_healthy_and_unhealthy(self):
        from ui.server import verify_stream

        self.assertEqual(verify_stream(self.srv.url, self.srv.token), [])
        bad = verify_stream(self.srv.url, "wrong-token")
        self.assertTrue(any("/events answered 401" in p for p in bad) or any("token" in p for p in bad), bad)
        self.assertTrue(verify_stream("http://127.0.0.1:1/", "x")[0].startswith("/api/ping unreachable"))

    # --------------------------------------------------------------- heartbeat
    def test_heartbeat_is_a_data_event_javascript_can_see(self):
        c = self.open()
        self.assertIsNotNone(c.wait_for(lambda e: e["type"] == "ping", 3), "no data heartbeat while idle")
        first = [e for e in c.events if e["type"] == "ping"]
        time.sleep(0.8)
        self.assertGreater(len([e for e in c.events if e["type"] == "ping"]), len(first) - 1 + 1)

    def test_events_relayed_in_order_to_every_client(self):
        a, b = self.open(), self.open()
        for c in (a, b):
            self.assertIsNotNone(c.wait_for(lambda e: e["type"] == "hello", 3))
        for i in range(25):
            self.srv.bus.publish({"type": "message", "role": "user", "text": f"m{i}"})
        for c in (a, b):
            self.assertIsNotNone(c.wait_for(lambda e: e.get("text") == "m24", 3))
            texts = [e["text"] for e in c.events if e["type"] == "message"]
            self.assertEqual(texts, [f"m{i}" for i in range(25)])
        self.assertEqual(self.srv.clients(), 2)

    def test_unserialisable_event_does_not_kill_the_stream(self):
        c = self.open(); c.wait_for(lambda e: e["type"] == "hello", 3)
        self.srv.bus.publish({"type": "log", "msg": object()})              # default=str handles it
        self.srv.bus.publish({"type": "message", "role": "user", "text": "after"})
        self.assertIsNotNone(c.wait_for(lambda e: e.get("text") == "after", 3))

    # ------------------------------------------------------ disconnect / leaks
    def test_disconnect_releases_subscription_no_leak_over_many_cycles(self):
        for i in range(30):
            c = SSEClient(self.srv.url, token=self.srv.token)
            self.assertIsNotNone(c.wait_for(lambda e: e["type"] == "hello", 3), f"cycle {i}")
            c.close()
        self.assertTrue(self.srv.wait_clients(0, 6), f"leaked subscriptions: {self.srv.clients()}")

    def test_server_keeps_serving_after_abrupt_client_death(self):
        c = self.open(); c.wait_for(lambda e: e["type"] == "hello", 3); c.close()
        self.srv.bus.publish({"type": "message", "role": "user", "text": "to nobody"})   # write to a dead client
        d = self.open()
        self.assertIsNotNone(d.wait_for(lambda e: e["type"] == "hello", 3))
        self.assertTrue(self.srv.wait_clients(1, 4))

    def test_stream_limit_returns_503_not_a_hang(self):
        self.srv.stop(); self.srv = Server(heartbeat=0.3, max_streams=2); self.addCleanup(self.srv.stop)
        a, b = self.open(), self.open()
        for c in (a, b):
            c.wait_for(lambda e: e["type"] == "hello", 3)
        r = requests.get(self.srv.url + "events", params={"t": self.srv.token}, timeout=3)
        self.assertEqual(r.status_code, 503); self.assertEqual(r.headers.get("retry-after"), "3")

    # ----------------------------------------------- the snapshot cannot kill it
    def test_failing_snapshot_still_delivers_a_degraded_hello(self):
        self.srv.alpha.mode = "raise"
        c = self.open(); hello = c.wait_for(lambda e: e["type"] == "hello", 3)
        self.assertIsNotNone(hello, "stream died because the snapshot raised")
        self.assertTrue(hello["degraded"]); self.assertIn("snapshot exploded", hello["error"])
        self.srv.alpha.mode = "ok"
        self.srv.bus.publish({"type": "state", "state": "thinking"})
        self.assertIsNotNone(c.wait_for(lambda e: e.get("state") == "thinking", 3), "live events stopped after a degraded hello")

    def test_hanging_snapshot_times_out_instead_of_hanging_the_stream(self):
        self.srv.stop(); self.srv = Server(heartbeat=0.3, snapshot_timeout=0.5); self.addCleanup(self.srv.stop)
        self.srv.app.snapshot_timeout = 0.5
        self.srv.alpha.mode = "hang"
        t0 = time.time(); c = self.open(); hello = c.wait_for(lambda e: e["type"] == "hello", 4)
        self.assertIsNotNone(hello); self.assertTrue(hello["degraded"]); self.assertLess(time.time() - t0, 3)
        self.assertEqual(requests.get(self.srv.url + "api/ping", timeout=2).status_code, 200)   # server still responsive

    def test_repeated_hung_snapshots_do_not_exhaust_threads(self):
        """Regression: each reconnect used to park another executor thread in a hung snapshot()."""
        self.srv.stop(); self.srv = Server(heartbeat=0.3, snapshot_timeout=0.3); self.addCleanup(self.srv.stop)
        self.srv.alpha.mode, self.srv.alpha.hang_for = "hang", 1.5
        t0 = time.time(); degraded = 0
        for _ in range(40):
            c = SSEClient(self.srv.url, token=self.srv.token)
            h = c.wait_for(lambda e: e["type"] == "hello", 3)
            self.assertIsNotNone(h, "a connection got no hello at all while snapshot() was hung")
            degraded += bool(h.get("degraded")); c.close()
        elapsed = time.time() - t0
        self.assertEqual(self.srv.alpha.max_inflight, 1, "more than one snapshot() ran at once")
        self.assertLessEqual(self.srv.alpha.snapshot_calls, int(elapsed / 1.5) + 2)      # one per hang period, not per client
        self.assertGreater(degraded, 20, "clients were not served degraded hellos while the snapshot was hung")
        self.assertEqual(requests.get(self.srv.url + "api/ping", timeout=2).status_code, 200)
        self.srv.alpha.mode = "ok"; time.sleep(1.7)                       # the last hung call finishes; service recovers
        c = self.open(); h = c.wait_for(lambda e: e["type"] == "hello", 3)
        self.assertFalse(h.get("degraded", False), "did not recover after the hung snapshot finished")

    # ------------------------------------------------------------- shutdown
    def test_shutdown_with_open_streams_ends_them_cleanly_and_fast(self):
        cs = [self.open() for _ in range(4)]
        for c in cs:
            c.wait_for(lambda e: e["type"] == "hello", 3)
        t0 = time.time(); self.srv.stop(); took = time.time() - t0
        for c in cs:
            self.assertTrue(c.wait_ended(3), "a stream stayed open after server shutdown")
        self.assertLess(took, 5, f"shutdown took {took:.1f}s with open streams")

    def test_restart_on_same_port_new_clients_connect(self):
        port = self.srv.port; c = self.open(); c.wait_for(lambda e: e["type"] == "hello", 3)
        self.srv.stop(); c.wait_ended(3)
        self.srv = Server(heartbeat=0.3, port=port); self.addCleanup(self.srv.stop)
        self.assertEqual(self.srv.port, port)
        d = SSEClient(self.srv.url, token=self.srv.token); self.addCleanup(d.close)
        self.assertIsNotNone(d.wait_for(lambda e: e["type"] == "hello", 3))

    # --------------------------------------------------------------- security
    def test_token_required_everywhere(self):
        u = self.srv.url
        self.assertEqual(requests.get(u + "events", timeout=3).status_code, 401)
        self.assertEqual(requests.get(u + "events", params={"t": "nope"}, timeout=3).status_code, 401)
        self.assertEqual(requests.get(u + "api/snapshot", timeout=3).status_code, 401)
        self.assertEqual(requests.post(u + "api/control", json={"type": "command", "text": "x"}, timeout=3).status_code, 401)
        ok = requests.get(u + "api/snapshot", headers={"X-Alpha-Token": self.srv.token}, timeout=3)
        self.assertEqual(ok.status_code, 200); self.assertEqual(ok.json()["state"], "listening")
        self.assertEqual(requests.get(u + "api/snapshot", headers={"Authorization": "Bearer " + self.srv.token}, timeout=3).status_code, 200)

    def test_host_header_and_cross_site_post_are_rejected(self):
        u, h = self.srv.url, {"X-Alpha-Token": self.srv.token}
        self.assertEqual(requests.get(u + "api/ping", headers={"Host": "evil.example.com"}, timeout=3).status_code, 403)
        for host in ("localhost:80", "0.0.0.0:80", "[::1]:80", "127.0.0.1:80"):     # every local spelling is fine
            self.assertEqual(requests.get(u + "api/ping", headers={"Host": host}, timeout=3).status_code, 200, host)
        r = requests.post(u + "api/control", json={"type": "command", "text": "x"}, timeout=3,
                          headers={**h, "Origin": "https://evil.example.com"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.srv.alpha.calls, [], "a cross-site POST reached the assistant")

    def test_control_messages_reach_the_assistant(self):
        h = {"X-Alpha-Token": self.srv.token, "Origin": self.srv.url.rstrip("/")}
        for msg in ({"type": "command", "text": "open notepad"}, {"type": "quick", "id": "lock"}, {"type": "set_gate_db", "value": -42},
                    {"type": "set_muted", "value": True}, {"type": "confirm", "answer": True}, {"type": "reset_counters"}):
            self.assertEqual(requests.post(self.srv.url + "api/control", json=msg, headers=h, timeout=3).json(), {"ok": True})
        self.assertEqual([c[0] for c in self.srv.alpha.calls],
                         ["submit_text", "submit_quick", "set_gate_db", "set_muted", "answer_confirm", "reset_counters"])
        self.assertEqual(requests.post(self.srv.url + "api/control", data=b"not json", headers=h, timeout=3).status_code, 400)
        self.assertEqual(requests.post(self.srv.url + "api/control", data=b"x" * 5000, headers=h, timeout=3).status_code, 413)
        self.assertEqual(requests.post(self.srv.url + "api/control", json=[1, 2], headers=h, timeout=3).status_code, 400)


if __name__ == "__main__":
    unittest.main()
