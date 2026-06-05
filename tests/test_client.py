"""Client tests: lease_sync, async lease, fail-open, and waiting->granted handoff."""
import asyncio
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, os.path.join(ROOT, "server"), os.path.join(ROOT, "client")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import app  # noqa: E402
import pls as client  # noqa: E402  (client/pls.py)
from _helpers import start_broker, post, get, free_port  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
