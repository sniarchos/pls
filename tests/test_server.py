"""Broker tests: scheduling/admission/reaper logic + the HTTP endpoints."""
import json
import os
import socket
import sys
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, os.path.join(ROOT, "server"), os.path.join(ROOT, "client")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import start_broker, post, get  # noqa: E402  (first: see _helpers)
import app  # noqa: E402


def _add(state="waiting", priority="normal", created_at=None, cost=0.0,
         last_seen=None, granted_at=None, ended_at=None):
    now = time.monotonic_ns()
    t = {
        "id": uuid.uuid4().hex[:12], "user": "u", "tool": "t", "cost": cost,
        "priority": app.PRIORITIES[priority], "priority_name": priority, "state": state,
        "on": [], "eligible": [None], "card": None,
        "created_at": created_at if created_at is not None else now,
        "granted_at": granted_at,
        "last_seen": last_seen if last_seen is not None else now,
    }
    if ended_at is not None:
        t["ended_at"] = ended_at
    app._tickets[t["id"]] = t
    return t


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        app._tickets.clear()
        app.CAPACITY, app.BUDGET = 1, 0.0
        app.HEARTBEAT_TTL, app.DONE_RETAIN = 30.0, 600.0

    def test_capacity_one_serializes(self):
        _add()
        _add()
        app._schedule()
        self.assertEqual(len(app._held()), 1)
        self.assertEqual(len(app._waiting()), 1)

    def test_priority_then_fifo(self):
        app.CAPACITY = 0  # admit nothing, just inspect ordering
        _add(priority="normal", created_at=100)
        _add(priority="low", created_at=50)
        _add(priority="high", created_at=200)
        _add(priority="normal", created_at=80)
        order = [(t["priority_name"], t["created_at"]) for t in app._waiting()]
        self.assertEqual(
            order, [("high", 200), ("normal", 80), ("normal", 100), ("low", 50)]
        )

    def test_budget_admission_is_additive(self):
        app.CAPACITY, app.BUDGET = 5, 40.0
        _add(cost=24, created_at=1)
        _add(cost=20, created_at=2)
        _add(cost=10, created_at=3)
        app._schedule()
        held_costs = sorted(t["cost"] for t in app._held())
        self.assertEqual(held_costs, [10, 24])             # 24 then 10 fit (34 <= 40); 20 skipped
        self.assertEqual([t["cost"] for t in app._waiting()], [20])

    def test_priority_is_not_preemptive(self):
        held = _add(state="held", granted_at=time.monotonic_ns())
        _add(priority="high")
        app._schedule()
        self.assertEqual(app._tickets[held["id"]]["state"], "held")  # holder keeps the resource
        self.assertEqual(len(app._waiting()), 1)                     # high priority still waits

    def test_done_holder_frees_slot(self):
        held = _add(state="held", granted_at=time.monotonic_ns())
        waiter = _add()
        app._tickets[held["id"]]["state"] = "done"   # mimic a release
        app._schedule()
        self.assertEqual(app._tickets[waiter["id"]]["state"], "held")

    def test_reaper_expires_stale_holder_and_promotes(self):
        stale = _add(state="held", granted_at=time.monotonic_ns(), last_seen=time.monotonic_ns() - 31_000_000_000)
        waiter = _add()
        app._reap()
        self.assertEqual(app._tickets[stale["id"]]["state"], "done")
        self.assertTrue(app._tickets[stale["id"]].get("reaped"))
        self.assertEqual(app._tickets[waiter["id"]]["state"], "held")

    def test_reaper_gc_old_done_tickets(self):
        gone = _add(state="done", ended_at=time.monotonic_ns() - 601_000_000_000)
        app._reap()
        self.assertNotIn(gone["id"], app._tickets)

    def test_a_grant_keeps_the_waiters_ttl(self):
        # Live waiters beat while they wait, so a grant must not revive one that stopped.
        held = _add(state="held", granted_at=time.monotonic_ns())
        silent = time.monotonic_ns() - 29_000_000_000
        dead, nxt = _add(created_at=1, last_seen=silent), _add(created_at=2)
        held["state"] = "done"
        app._schedule()
        self.assertEqual((dead["state"], dead["last_seen"]), ("held", silent))
        dead["last_seen"] -= 2_000_000_000
        app._reap()
        self.assertEqual((dead["state"], nxt["state"]), ("done", "held"))

    def test_state_of_reports_queue_position(self):
        app.CAPACITY = 0
        _add(created_at=1)
        second = _add(created_at=2)
        st = app._state_of(second)
        self.assertEqual(st["position"], 2)
        self.assertEqual(st["ahead"], 1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.httpd, self.url = start_broker(app, capacity=1)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_healthz(self):
        self.assertEqual(get(self.url, "/healthz"), {"ok": True})

    def test_acquire_held_then_waiting(self):
        a = post(self.url, "/acquire", {"user": "a", "tool": "A", "cost": 0, "priority": "normal"})
        b = post(self.url, "/acquire", {"user": "b", "tool": "B", "cost": 0, "priority": "normal"})
        self.assertEqual(a["state"], "held")
        self.assertEqual(b["state"], "waiting")
        self.assertEqual(b["position"], 1)

    def test_status_shape(self):
        post(self.url, "/acquire", {"user": "a", "tool": "A", "cost": 7, "priority": "normal"})
        s = get(self.url, "/status")
        self.assertEqual(set(s), {"label", "unit", "capacity", "budget", "held", "waiting"})
        self.assertEqual(len(s["held"]), 1)
        self.assertEqual(s["held"][0]["cost"], 7.0)

    def test_release_reschedules_next(self):
        a = post(self.url, "/acquire", {"user": "a", "tool": "A", "cost": 0, "priority": "normal"})
        b = post(self.url, "/acquire", {"user": "b", "tool": "B", "cost": 0, "priority": "normal"})
        post(self.url, "/release/" + a["id"])
        held = get(self.url, "/status")["held"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["id"], b["id"])

    def test_heartbeat_unknown_ticket_is_expired(self):
        self.assertEqual(post(self.url, "/heartbeat/deadbeef")["state"], "expired")

    def test_bad_costs_are_refused(self):
        app.BUDGET = 48.0
        self.addCleanup(setattr, app, "BUDGET", 0.0)
        for cost in ("nan", "inf", "-Infinity", -1, "abc", [1], 60, 10 ** 400):
            with self.subTest(cost=cost), self.assertRaises(urllib.error.HTTPError) as cm:
                post(self.url, "/acquire", {"user": "a", "tool": "A", "cost": cost})
            self.assertEqual(cm.exception.code, 400)
            self.assertIn("cost", json.loads(cm.exception.read().decode())["error"])
        self.assertEqual(post(self.url, "/acquire", {"user": "a", "tool": "A", "cost": 48})["state"], "held")
        with urllib.request.urlopen(self.url + "/status", timeout=5) as r:
            json.loads(r.read().decode(), parse_constant=self.fail)  # strict JSON, as a browser parses it

    def test_a_body_that_is_not_a_json_object_gets_a_400(self):
        head = "POST /acquire HTTP/1.1\r\nHost: x\r\n"
        bad = {
            "list": head + "Content-Length: 2\r\n\r\n[]",
            "string": head + "Content-Length: 3\r\n\r\n\"x\"",
            "null": head + "Content-Length: 4\r\n\r\nnull",
            "broken JSON": head + "Content-Length: 9\r\n\r\n{\"user\": ",
            "deep nesting": head + "Content-Length: 60000\r\n\r\n" + "[" * 60000,
            "truncated": head + "Content-Length: 200\r\n\r\n{\"user\": \"real\"",
            "bad length": head + "Content-Length: x\r\n\r\n{}",
            "negative length": head + "Content-Length: -1\r\n\r\n{}",
            "huge length": head + "Content-Length: 99999999\r\n\r\n{}",
        }
        for why, request in bad.items():
            with self.subTest(why), socket.create_connection(self.httpd.server_address, timeout=5) as c:
                c.sendall(request.encode())
                c.shutdown(socket.SHUT_WR)  # the sender is done, whatever its Content-Length said
                reply = b""
                while chunk := c.recv(4096):
                    reply += chunk
                self.assertTrue(reply.startswith(b"HTTP/1.1 400"), reply[:200])
        self.assertEqual(get(self.url, "/status")["held"] + get(self.url, "/status")["waiting"], [])
        self.assertEqual(post(self.url, "/acquire")["state"], "held")  # an empty body still asks with defaults

    def test_a_client_that_stops_reading_freezes_nothing(self):
        now = time.monotonic_ns()
        with app._lock:
            app.CAPACITY = 0
            for i in range(20000):
                tid = f"x{i:011d}"
                app._tickets[tid] = {"id": tid, "user": "u", "tool": "t" * 1000, "cost": 0.0,
                                     "priority": 10, "priority_name": "normal", "on": [],
                                     "eligible": [None], "card": None, "device": None,
                                     "state": "waiting", "created_at": now + i, "granted_at": None,
                                     "last_seen": now}
        self.addCleanup(app._tickets.clear)
        stuck = socket.create_connection(self.httpd.server_address)
        self.addCleanup(stuck.close)
        stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        stuck.sendall(b"GET /status HTTP/1.1\r\nHost: x\r\n\r\n")
        time.sleep(1.0)  # its handler is blocked writing a body far larger than the socket buffers
        self.assertEqual(post(self.url, "/heartbeat/x00000000001")["state"], "waiting")
        self.assertTrue(app._lock.acquire(timeout=1))
        app._lock.release()

    def test_idle_connections_are_closed(self):
        self.assertEqual(app.Handler.timeout, 15)
        with mock.patch.object(app.Handler, "timeout", 0.3), \
                socket.create_connection(self.httpd.server_address, timeout=5) as idle:
            time.sleep(0.6)
            self.assertEqual(idle.recv(1), b"")


if __name__ == "__main__":
    unittest.main()
