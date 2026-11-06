"""CLI tests: helpers (_fmt) and end-to-end `pls run` through the real entrypoint."""
import importlib.machinery
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, os.path.join(ROOT, "server"), os.path.join(ROOT, "client")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import start_broker, get, post, free_port  # noqa: E402  (first: see _helpers)
import app  # noqa: E402

BIN = os.path.join(ROOT, "bin", "pls")
# Makes the pty on stdin the controlling terminal, as a login shell would, then runs argv[1:].
CTTY = ("import fcntl, os, sys, termios; os.setsid(); fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
        "os.execvp(sys.argv[1], sys.argv[1:])")
# The same, but runs argv[1:] in a process group of its own: a background job of that terminal.
CTTY_BG = ("import fcntl, os, subprocess, sys, termios; os.setsid(); fcntl.ioctl(0, termios.TIOCSCTTY, 0); "
           "sys.exit(subprocess.call(sys.argv[1:], preexec_fn=lambda: os.setpgid(0, 0)))")
# Prints each signal it gets, takes 0.6 s to stop after the first, and exits 10 + how many it got.
COUNTER = ("import signal, sys, time\n"
           "got = []\n"
           "def note(signum, _frame):\n"
           "    got.append(signum); print('got', signum, flush=True)\n"
           "for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP): signal.signal(s, note)\n"
           "print('up', flush=True)\n"
           "end = time.monotonic() + 1.5\n"
           "while time.monotonic() < end and not got: time.sleep(0.05)\n"
           "time.sleep(0.6 if got else 0)\n"
           "sys.exit(10 + len(got))")


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

    def test_label_hides_secret_values(self):
        cmd = ["docker", "run", "-e", "HF_TOKEN=abc", "--env=API=x y", "-e", "PASSTHRU",
               "--hf-token", "s3cr3t", "--api-key=k", "--Password", "a b", "--use-key", "--gpus",
               "all", "img", "KEY=positional"]
        self.assertEqual(self.cli._label(cmd),
                         "docker run -e HF_TOKEN=*** --env=API=*** -e PASSTHRU --hf-token *** "
                         "--api-key=*** --Password *** --use-key --gpus all img KEY=positional")
        self.assertEqual(self.cli._label(["sh", "-c", "echo $X"]), "sh -c 'echo $X'")

    def test_status_strips_control_characters(self):
        status = {"label": "GPU 0", "capacity": 1,
                  "held": [{"user": "a\x1b[2J", "tool": "job\x1b]0;owned\x07\nnext\x9b", "cost": 0, "held_s": 1}],
                  "waiting": [{"user": "b\x07", "tool": "w\r", "priority": "low", "waited_s": 1}]}
        out = "\n".join(self.cli._render_status(status))
        self.assertNotRegex(out, "[\x00-\x09\x0b-\x1f\x7f-\x9f]")
        self.assertIn("job]0;ownednext", out)

    def test_usage_loses_only_the_card_it_cannot_read(self):
        out = "GPU-a, 6400, 97887, 46\nGPU-b, 500, 49140, [N/A]\n"
        with mock.patch.object(self.cli.subprocess, "run", return_value=mock.Mock(stdout=out)):
            self.assertEqual(self.cli._usage([{"device": "GPU-a"}, {"device": "GPU-b"}]),
                             {"GPU-a": "6.2/96 GB   46%"})


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

    def _pls(self, *args, env=None):
        return subprocess.run([sys.executable, BIN, *args], env=env or self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)

    def _start(self, *args):
        return subprocess.Popen([sys.executable, BIN, *args], env=dict(self.env, PLS_POLL="0.05"),
                                start_new_session=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def _state(self, tool):
        s = get(self.url, "/status")
        return next((k for k in ("held", "waiting") for t in s[k] if t["tool"] == tool), None)

    def _until(self, tool, state):
        for _ in range(200):
            if self._state(tool) == state:
                return
            time.sleep(0.05)
        self.fail(f"{tool} never {state}")

    def _queued_behind_job(self, *child, tool="job"):
        p = self._start("run", "--tool", tool, "--", sys.executable, "-c", *child)
        self._until(tool, "held")
        nxt = post(self.url, "/acquire", {"user": "b", "tool": "next"})
        self.assertEqual(nxt["state"], "waiting")
        return p, nxt

    def test_ctrl_c_at_the_terminal_reaches_the_command_once(self):
        primary, secondary = os.openpty()
        try:
            p = subprocess.Popen([sys.executable, "-c", CTTY, sys.executable, BIN, "run", "--tool", "job",
                                  "--", sys.executable, "-c", COUNTER], env=dict(self.env, PLS_POLL="0.05"),
                                 stdin=secondary, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self._until("job", "held")
            nxt = post(self.url, "/acquire", {"user": "b", "tool": "next"})
            time.sleep(0.5)
            os.write(primary, b"\x03")  # Ctrl-C: the terminal signals its whole foreground group
            time.sleep(0.3)
            self.assertEqual(self._state("job"), "held")  # still shutting down, still its card
            out, err = p.communicate(timeout=10)
        finally:
            os.close(secondary)
            os.close(primary)
        self.assertEqual((p.returncode, out.split()), (11, ["up", "got", "2"]), err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(post(self.url, "/heartbeat/" + nxt["id"])["state"], "held")

    def test_sigint_and_sigterm_to_pls_alone_are_passed_on(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(sig.name):
                p, nxt = self._queued_behind_job(COUNTER, tool=f"job-{sig.name}")
                time.sleep(0.3)
                p.send_signal(sig)  # kill, a supervisor, a script: no terminal involved
                time.sleep(0.3)
                self.assertEqual(self._state(f"job-{sig.name}"), "held")
                out, err = p.communicate(timeout=10)
                self.assertEqual((p.returncode, out.split()), (11, ["up", "got", str(int(sig))]), err)
                self.assertEqual(post(self.url, "/heartbeat/" + nxt["id"])["state"], "held")
                post(self.url, "/release/" + nxt["id"])

    @unittest.skipUnless(hasattr(os, "openpty"), "needs a pseudo-terminal")
    def test_sigint_to_pls_in_the_background_of_a_terminal_is_passed_on(self):
        primary, secondary = os.openpty()
        try:
            p = subprocess.Popen([sys.executable, "-c", CTTY_BG, sys.executable, BIN, "run", "--tool", "bg",
                                  "--", sys.executable, "-c", COUNTER], env=dict(self.env, PLS_POLL="0.05"),
                                 stdin=secondary, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self._until("bg", "held")
            time.sleep(0.5)
            with open(f"/proc/{p.pid}/task/{p.pid}/children") as fh:
                pls = int(fh.read().split()[0])
            os.kill(pls, signal.SIGINT)
            out, err = p.communicate(timeout=10)
        finally:
            os.close(secondary)
            os.close(primary)
        self.assertEqual((p.returncode, out.split()), (11, ["up", "got", str(int(signal.SIGINT))]), err)

    def test_a_hangup_is_not_passed_on_but_reaches_the_group_once(self):
        p, nxt = self._queued_behind_job(COUNTER, tool="alone")
        time.sleep(0.3)
        p.send_signal(signal.SIGHUP)  # pls alone: the command is left to finish
        out, err = p.communicate(timeout=10)
        self.assertEqual((p.returncode, out.split()), (10, ["up"]), err)
        post(self.url, "/release/" + nxt["id"])
        p, nxt = self._queued_behind_job(COUNTER, tool="group")
        time.sleep(0.3)
        os.killpg(p.pid, signal.SIGHUP)  # a terminal hangup or tmux: the whole group
        out, err = p.communicate(timeout=10)
        self.assertEqual((p.returncode, out.split()), (11, ["up", "got", str(int(signal.SIGHUP))]), err)

    def test_signals_pls_was_started_ignoring_stay_ignored(self):
        p = subprocess.Popen(["sh", "-c", 'trap "" HUP INT; exec "$@"', "sh", sys.executable, BIN, "run",
                              "--tool", "nohup", "--", "sh", "-c", "sleep 1; echo survived"],
                             env=self.env, start_new_session=True, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        self._until("nohup", "held")
        os.killpg(p.pid, signal.SIGHUP)
        os.killpg(p.pid, signal.SIGINT)
        out, err = p.communicate(timeout=10)
        self.assertEqual((p.returncode, out.strip()), (0, "survived"), err)

    def test_a_signal_before_the_command_starts_reaches_it(self):
        script = ("import importlib.machinery, importlib.util, os, signal, subprocess, sys\n"
                  "loader = importlib.machinery.SourceFileLoader('cli', sys.argv[1])\n"
                  "cli = importlib.util.module_from_spec(importlib.util.spec_from_loader('cli', loader))\n"
                  "loader.exec_module(cli)\n"
                  "popen = subprocess.Popen\n"
                  "def late(*a, **kw):\n"
                  "    os.kill(os.getpid(), signal.SIGTERM)  # after the handlers, before the command\n"
                  "    return popen(*a, **kw)\n"
                  "subprocess.Popen = late\n"
                  "sys.exit(cli._run(['sleep', '5'], None))")
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, "-c", script, BIN], env=self.env, capture_output=True,
                           text=True, timeout=20)
        self.assertEqual(p.returncode, 128 + signal.SIGTERM, p.stderr)
        self.assertLess(time.monotonic() - t0, 3)

    def test_exit_code_of_a_signalled_command(self):
        p = self._pls("run", "--", "sh", "-c", "kill -9 $$")
        self.assertEqual(p.returncode, 137)

    def test_ctrl_c_sigterm_or_sighup_while_queued_leave_the_queue(self):
        holder = post(self.url, "/acquire", {"user": "a", "tool": "holder"})
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(sig.name):
                p = self._start("run", "--tool", "impatient", "--", "true")
                self._until("impatient", "waiting")
                p.send_signal(sig)
                out, err = p.communicate(timeout=10)
                self.assertEqual(p.returncode, 128 + sig)
                self.assertIn("[pls] Not started.", err)
                self.assertNotIn("Traceback", err)
                self.assertIsNone(self._state("impatient"))
        post(self.url, "/release/" + holder["id"])

    def test_the_board_shows_the_redacted_command(self):
        p = self._start("run", "--", "sh", "-c", "sleep 1", "-e", "HF_TOKEN=abc123", "--api-key", "sk-xyz")
        for _ in range(100):
            held = get(self.url, "/status")["held"]
            if held:
                break
            time.sleep(0.05)
        self.assertEqual(held[0]["tool"], "sh -c 'sleep 1' -e HF_TOKEN=*** --api-key ***")
        p.communicate(timeout=10)

    def test_cost_must_be_a_finite_number_of_zero_or_more(self):
        for cost in ("nan", "inf", "-1", "abc"):
            with self.subTest(cost=cost):
                p = self._pls("run", "--cost", cost, "--", "true")
                self.assertEqual(p.returncode, 2)
                self.assertIn("is not a finite number >= 0", p.stderr)
        self.assertEqual(get(self.url, "/status")["held"], [])

    def test_watch_needs_a_terminal_and_a_positive_interval(self):
        for interval in ("0", "-1", "nan", "inf"):
            p = self._pls("watch", "--interval", interval)
            self.assertEqual(p.returncode, 2, interval)
            self.assertIn("--interval must be a number of seconds > 0", p.stderr)
        p = self._pls("watch")
        self.assertEqual((p.returncode, p.stdout), (2, ""))
        self.assertIn("use `pls status`", p.stderr)

    def test_dotenv_settings_stay_out_of_the_command(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
            fh.write(f"PLS_URL={self.url}\nPLS_LABEL=from file\n")
        self.addCleanup(os.unlink, fh.name)
        env = {k: v for k, v in self.env.items() if k != "PLS_URL"}
        env["PLS_ENV_FILE"] = fh.name
        show = 'echo "${PLS_URL-unset} ${PLS_LABEL-unset}"'
        p = self._pls("run", "--tool", "envcheck", "--", "sh", "-c", show, env=env)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "unset unset"), p.stderr)
        self.assertNotIn("WITHOUT a lease", p.stderr)  # the file's PLS_URL found the broker
        with open(fh.name, "w") as out:
            out.write(f"PLS_URL=http://127.0.0.1:{free_port()}\n")
        p = self._pls("run", "--", "true", env=dict(env, PLS_URL=self.url))
        self.assertEqual(p.returncode, 0)
        self.assertNotIn("WITHOUT a lease", p.stderr)  # the real environment wins


if __name__ == "__main__":
    unittest.main()
