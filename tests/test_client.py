"""Client tests: lease_sync, async lease, fail-open, and waiting->granted handoff."""
import asyncio
import io
import os
import sys
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, os.path.join(ROOT, "server"), os.path.join(ROOT, "client")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import start_broker, post, get, free_port  # noqa: E402  (first: see _helpers)
import app  # noqa: E402
import pls as client  # noqa: E402  (client/pls.py)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.httpd, self.url = start_broker(app, capacity=1)
        self._saved = (client.PLS_URL, client._POLL, client._BEAT,
                       client._GRACE, client._REQUIRED)
        client.PLS_URL = self.url
        client._POLL = 0.05      # poll fast so waiting tests are quick
        client._BEAT = 0.1

    def tearDown(self):
        (client.PLS_URL, client._POLL, client._BEAT,
         client._GRACE, client._REQUIRED) = self._saved
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_lease_sync_acquires_and_releases(self):
        with client.lease_sync(tool="x", cost=3) as tid:
            self.assertIsNotNone(tid)
            held = get(self.url, "/status")["held"]
            self.assertEqual(len(held), 1)
            self.assertEqual(held[0]["cost"], 3.0)
        self.assertEqual(get(self.url, "/status")["held"], [])  # released on exit

    def test_lease_sync_fails_open_when_broker_down(self):
        client.PLS_URL = f"http://127.0.0.1:{free_port()}"  # nothing listening
        client._GRACE = 0          # give up immediately -> run unguarded
        client._REQUIRED = False
        ran = []
        with client.lease_sync(tool="x") as tid:
            self.assertIsNone(tid)  # no lease, but the body still runs
            ran.append(True)
        self.assertEqual(ran, [True])

    def test_async_lease_acquires_and_releases(self):
        async def go():
            async with client.lease(tool="ax", cost=2) as tid:
                self.assertIsNotNone(tid)
                self.assertEqual(len(get(self.url, "/status")["held"]), 1)
        asyncio.run(go())
        self.assertEqual(get(self.url, "/status")["held"], [])

    def test_waiting_job_is_granted_after_release(self):
        first = post(self.url, "/acquire",
                     {"user": "a", "tool": "A", "cost": 0, "priority": "normal"})
        self.assertEqual(first["state"], "held")

        granted = threading.Event()
        result = {}

        def worker():
            with client.lease_sync(tool="B", cost=0) as tid:
                result["tid"] = tid
                granted.set()

        threading.Thread(target=worker, daemon=True).start()
        time.sleep(0.4)
        self.assertFalse(granted.is_set())                          # B is still queued
        self.assertEqual(len(get(self.url, "/status")["waiting"]), 1)

        post(self.url, "/release/" + first["id"])                   # free the slot
        self.assertTrue(granted.wait(timeout=5))                    # B proceeds
        self.assertIsNotNone(result["tid"])

    def _queue_behind(self, tool):
        return post(self.url, "/acquire", {"user": "a", "tool": tool, "cost": 0, "priority": "normal"})

    def _waiting_tools(self):
        return [w["tool"] for w in get(self.url, "/status")["waiting"]]

    def test_ctrl_c_while_queued_leaves_the_queue(self):
        self._queue_behind("holder")

        def interrupt(_state):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            with client.lease_sync(tool="impatient", on_wait=interrupt):
                self.fail("granted")
        self.assertEqual(self._waiting_tools(), [])

    def test_a_cancelled_async_waiter_leaves_the_queue(self):
        holder = self._queue_behind("holder")

        async def go():
            async def want():
                async with client.lease(tool="cancelled"):
                    self.fail("granted")
            task = asyncio.ensure_future(want())
            await asyncio.sleep(0.3)
            self.assertEqual(self._waiting_tools(), ["cancelled"])
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(go())
        self.assertEqual(self._waiting_tools(), [])
        post(self.url, "/release/" + holder["id"])
        self.assertEqual(get(self.url, "/status")["held"], [])

    def test_a_cancel_during_a_poll_still_releases(self):
        holder = self._queue_behind("holder")
        real, polling = client._poll, threading.Event()

        def slow_poll(t, args):
            polling.set()
            post(self.url, "/release/" + holder["id"])  # so this very poll comes back held
            time.sleep(0.3)
            return real(t, args)

        async def go():
            async def want():
                async with client.lease(tool="cancelled"):
                    self.fail("granted")
            task = asyncio.ensure_future(want())
            while not polling.is_set():
                await asyncio.sleep(0.01)
            task.cancel()  # while the poll is in flight
            with self.assertRaises(asyncio.CancelledError):
                await task
        with mock.patch.object(client, "_poll", slow_poll):
            asyncio.run(go())
        self.assertEqual(self._waiting_tools(), [])
        self.assertEqual(get(self.url, "/status")["held"], [])

    def test_a_cancel_stops_retrying_an_unreachable_broker(self):
        client._REQUIRED = True  # would retry forever
        tries = []

        def down(*_args):
            tries.append(time.monotonic())
            raise OSError("connection refused")

        async def go():
            async def want():
                async with client.lease(tool="ghost"):
                    self.fail("granted")
            task = asyncio.ensure_future(want())
            await asyncio.sleep(0.3)
            t0 = time.monotonic()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return time.monotonic() - t0

        took = []
        with mock.patch.object(client, "_acquire", down):
            run = threading.Thread(target=lambda: took.append(asyncio.run(go())), daemon=True)
            run.start()
            run.join(timeout=5)  # asyncio.run waits for its worker threads
            self.assertFalse(run.is_alive())
            seen = len(tries)
            time.sleep(2.5)
        self.assertLess(took[0], 1)
        self.assertEqual(len(tries), seen)

    def test_both_clients_beat_while_they_wait(self):
        holder = self._queue_behind("holder")
        app.HEARTBEAT_TTL = 0.5  # shorter than a waiter's stay: only its own beats keep it queued
        self.addCleanup(setattr, app, "HEARTBEAT_TTL", 30.0)
        got = []

        def sync_waiter():
            with client.lease_sync(tool="sync"):
                got.append("sync")

        async def async_waiter():
            async with client.lease(tool="async"):
                got.append("async")

        threads = [threading.Thread(target=sync_waiter, daemon=True),
                   threading.Thread(target=lambda: asyncio.run(async_waiter()), daemon=True)]
        for th in threads:
            th.start()
        time.sleep(1.5)
        with app._lock:
            app._tickets[holder["id"]]["last_seen"] = time.monotonic_ns()  # stands in for its beats
            app._reap()
            self.assertEqual(sorted(t["tool"] for t in app._waiting()), ["async", "sync"])
        post(self.url, "/release/" + holder["id"])
        for th in threads:
            th.join(timeout=5)
        self.assertEqual(sorted(got), ["async", "sync"])

    def test_no_warning_when_our_own_release_wins_the_race(self):
        t = self._queue_behind("held")
        post(self.url, "/release/" + t["id"])
        done = threading.Event()
        done.set()
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertFalse(client._beat_once(t["id"], done))
        self.assertEqual(err.getvalue(), "")

    def test_tests_never_read_a_dotenv_or_target_the_live_broker(self):
        self.assertEqual(os.environ["PLS_ENV_FILE"], os.devnull)
        self.assertNotIn(":8555", self._saved[0])
        self.assertNotEqual(app.PORT, 8555)


if __name__ == "__main__":
    unittest.main()
