"""CLI tests: helpers (_fmt) and end-to-end `pls run` through the real entrypoint."""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, os.path.join(ROOT, "server"), os.path.join(ROOT, "client")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import app  # noqa: E402
from _helpers import start_broker, get  # noqa: E402

BIN = os.path.join(ROOT, "bin", "pls")


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("pls_cli", BIN)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class CliHelperTests(unittest.TestCase):
    def setUp(self):
        self.cli = _load_cli()

    def test_fmt(self):
        self.assertEqual(self.cli._fmt(0), "0s")
        self.assertEqual(self.cli._fmt(59), "59s")
        self.assertEqual(self.cli._fmt(60), "1m00s")
        self.assertEqual(self.cli._fmt(3661), "1h01m")


class CliRunTests(unittest.TestCase):
    def setUp(self):
        self.httpd, self.url = start_broker(app, capacity=1)
        self.env = {**os.environ, "PLS_URL": self.url, "USER": "clitester"}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _run_bg(self, *args):
        return subprocess.Popen([sys.executable, BIN, *args], env=self.env)

    def test_run_tool_label(self):
        p = self._run_bg("run", "--cost", "7", "--tool", "aliastest", "--", "sleep", "0.8")
        time.sleep(0.35)
        held = get(self.url, "/status")["held"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["tool"], "aliastest")
        self.assertEqual(held[0]["user"], "clitester")
        self.assertEqual(p.wait(timeout=5), 0)
        self.assertEqual(get(self.url, "/status")["held"], [])  # released when CMD exits

    def test_default_tool_is_the_command(self):
        p = self._run_bg("run", "--", "sleep", "0.8")
        time.sleep(0.35)
        held = get(self.url, "/status")["held"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["tool"], "sleep 0.8")  # default label = full command
        p.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
