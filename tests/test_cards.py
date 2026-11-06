"""Cards: config, eligibility, placement, status, persistence, and the client and CLI on top."""
import asyncio
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, os.path.join(ROOT, "server"), os.path.join(ROOT, "client")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _helpers import start_broker, post, get, free_port  # noqa: E402  (first: see _helpers)
import app  # noqa: E402
import pls as client  # noqa: E402

BIN = os.path.join(ROOT, "bin", "pls")
MAXQ = "GPU-7c742dd2-98c6-60e3-e7a2-5f9fdaa28e52"
ADA = "GPU-b893fc6e-1291-48a6-6789-e118daa7829b"
B2 = "GPU-00000000-0000-0000-0000-000000000003"

ONE = {"PLS_CARD_1_NAMES": "ada", "PLS_CARD_1_DEVICE": ADA, "PLS_CARD_1_TAGS": "sm_89"}
TWO = {
    "PLS_CARD_1_NAMES": "maxq, blackwell", "PLS_CARD_1_DEVICE": MAXQ,
    "PLS_CARD_1_LABEL": "RTX PRO 6000 Blackwell Max-Q", "PLS_CARD_1_TAGS": "sm_120",
    "PLS_CARD_2_NAMES": "ada", "PLS_CARD_2_DEVICE": ADA,
    "PLS_CARD_2_LABEL": "RTX 6000 Ada", "PLS_CARD_2_TAGS": "sm_89",
    "PLS_DEFAULT": "ada",
}
# Card 10 sorts after card 2: <n> is a number, not a string.
THREE = dict(TWO, PLS_CARD_10_NAMES="b2", PLS_CARD_10_DEVICE=B2, PLS_CARD_10_TAGS="sm_120")


def _reset():
    app._tickets.clear()
    app._configure({})
    app.CAPACITY, app.BUDGET = 1, 0.0


def _picks(on):
    return app._eligible(app._tokens(on))


class ConfigTests(unittest.TestCase):
    def tearDown(self):
        _reset()

    def test_no_cards_is_one_implicit_resource(self):
        app._configure({})
        self.assertFalse(app._has_cards())
        self.assertEqual(_picks(None), [None])
        self.assertEqual(_picks("any"), [None])
        with self.assertRaisesRegex(ValueError, "unknown card or tag 'ada'"):
            _picks("ada")

    def test_cards_follow_their_number(self):
        app._configure(THREE)
        self.assertEqual([c["id"] for c in app.CARDS], ["maxq", "ada", "b2"])
        self.assertEqual(_picks("any"), ["maxq", "ada", "b2"])

    def test_first_name_is_the_id_and_default_label(self):
        app._configure(THREE)
        b2 = app.CARDS[2]
        self.assertEqual((b2["id"], b2["label"], b2["device"]), ("b2", "b2", B2))
        self.assertEqual(app.CARDS[0]["names"], ["maxq", "blackwell"])

    def test_capacity_and_budget_default_to_the_globals(self):
        app._configure(dict(TWO, PLS_CARD_1_CAPACITY="2", PLS_CARD_1_BUDGET="96"))
        app.CAPACITY, app.BUDGET = 1, 48.0
        self.assertEqual(app._limits(app.CARDS[0]), (2, 96.0))
        self.assertEqual(app._limits(app.CARDS[1]), (1, 48.0))

    def test_names_and_tags_pick_cards_case_insensitively(self):
        app._configure(THREE)
        for on in ("maxq", "MaxQ", "BLACKWELL", MAXQ, MAXQ.lower()):
            self.assertEqual(_picks(on), ["maxq"], on)
        self.assertEqual(_picks("SM_120"), ["maxq", "b2"])
        self.assertEqual(_picks("sm_89"), ["ada"])

    def test_selectors_combine_in_config_order(self):
        app._configure(THREE)
        self.assertEqual(_picks("ada,maxq"), ["maxq", "ada"])
        self.assertEqual(_picks(["b2", "ada"]), ["ada", "b2"])
        self.assertEqual(_picks(["sm_89", "sm_120"]), ["maxq", "ada", "b2"])
        self.assertEqual(_picks(["any", "ada"]), ["maxq", "ada", "b2"])

    def test_default_set(self):
        app._configure(TWO)
        self.assertEqual(app.DEFAULT, ["ada"])
        self.assertEqual(_picks(None), ["ada"])
        self.assertEqual(_picks([]), ["ada"])
        app._configure({k: v for k, v in TWO.items() if k != "PLS_DEFAULT"})
        self.assertEqual(app.DEFAULT, ["maxq", "ada"])

    def test_refuses_ambiguous_config(self):
        bad = {
            "share the name": {"PLS_CARD_1_NAMES": "a,x", "PLS_CARD_2_NAMES": "b,X"},
            "same device": {"PLS_CARD_1_NAMES": "a", "PLS_CARD_1_DEVICE": ADA,
                            "PLS_CARD_2_NAMES": "b", "PLS_CARD_2_DEVICE": ADA.lower()},
            "also a tag": {"PLS_CARD_1_NAMES": "a", "PLS_CARD_2_NAMES": "b", "PLS_CARD_2_TAGS": "A"},
            "own tag": {"PLS_CARD_1_NAMES": "a", "PLS_CARD_1_TAGS": "a"},
            "named any": {"PLS_CARD_1_NAMES": "Any"},
            "tagged any": {"PLS_CARD_1_NAMES": "a", "PLS_CARD_1_TAGS": "any"},
            "unknown default": dict(TWO, PLS_DEFAULT="nope"),
            "default without cards": {"PLS_DEFAULT": "ada"},
            "typo": dict(TWO, PLS_CARD_1_NAME="maxq"),
            "no names": {"PLS_CARD_1_DEVICE": ADA},
            "bad number": dict(TWO, PLS_CARD_1_CAPACITY="two"),
        }
        for why, env in bad.items():
            with self.subTest(why), self.assertRaises(ValueError):
                app._configure(env)

    def test_server_refuses_to_start(self):
        env = dict(os.environ, PLS_ENV_FILE=os.devnull, PLS_PORT=str(free_port()),
                   PLS_CARD_1_NAMES="a", PLS_CARD_2_NAMES="A")
        p = subprocess.run([sys.executable, os.path.join(ROOT, "server", "app.py")], env=env,
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 1)
        self.assertIn("refusing to start: cards a and A share the name 'A'", p.stderr)

    def test_state_file_location(self):
        app._configure({"STATE_DIRECTORY": "/var/lib/pls"})
        self.assertEqual(app.STATE_FILE, "/var/lib/pls/tickets.json")
        app._configure({"STATE_DIRECTORY": "/var/lib/pls", "PLS_STATE_FILE": "/tmp/t.json"})
        self.assertEqual(app.STATE_FILE, "/tmp/t.json")
        app._configure({})
        self.assertIsNone(app.STATE_FILE)


class BrokerCase(unittest.TestCase):
    ENV = TWO

    def setUp(self):
        self.httpd, self.url = start_broker(app)
        app._configure(self.ENV)
        self.addCleanup(_reset)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def acquire(self, on=None, priority="normal", cost=0, tool="t"):
        body = {"user": "u", "tool": tool, "cost": cost, "priority": priority}
        if on is not None:
            body["on"] = on
        return post(self.url, "/acquire", body)

    def release(self, ticket):
        post(self.url, "/release/" + ticket["id"])

    def card_of(self, ticket):
        return (post(self.url, "/heartbeat/" + ticket["id"])["card"] or {}).get("id")


class PlacementTests(BrokerCase):
    def test_any_fills_cards_in_config_order(self):
        a, b, c = self.acquire("any"), self.acquire("any"), self.acquire("any")
        self.assertEqual((a["card"]["id"], b["card"]["id"]), ("maxq", "ada"))
        self.assertEqual((c["state"], c["card"]), ("waiting", None))

    def test_no_card_named_means_the_default_set_only(self):
        first, second = self.acquire(), self.acquire()
        self.assertEqual(first["card"]["id"], "ada")
        self.assertEqual(second["state"], "waiting")  # the Max-Q is free but not in the default set
        self.assertEqual(get(self.url, "/status")["waiting"][0]["eligible"], ["ada"])
        self.release(first)
        self.assertEqual(self.card_of(second), "ada")

    def test_every_name_and_tag_reaches_the_card(self):
        for on in ("maxq", "Blackwell", "sm_120", MAXQ):
            t = self.acquire(on)
            self.assertEqual(t["card"]["id"], "maxq", on)
            self.release(t)

    def test_waiting_for_a_busy_card_blocks_nobody_else(self):
        self.acquire("maxq")
        stuck = self.acquire("maxq", priority="high")
        other = self.acquire("any", priority="low")
        self.assertEqual(stuck["state"], "waiting")
        self.assertEqual(other["card"]["id"], "ada")

    def test_priority_then_fifo_across_cards(self):
        on_maxq, on_ada = self.acquire("maxq"), self.acquire("ada")
        w_ada = self.acquire("ada")
        w_any = self.acquire("any", priority="high")
        w_maxq = self.acquire("maxq")
        self.release(on_ada)
        self.assertEqual(self.card_of(w_any), "ada")       # high priority first, wherever it fits
        self.release(on_maxq)
        self.assertEqual(self.card_of(w_maxq), "maxq")     # w_ada can't use the Max-Q
        self.assertEqual(post(self.url, "/heartbeat/" + w_ada["id"])["state"], "waiting")

    def test_budget_is_per_card(self):
        app._configure(dict(TWO, PLS_CARD_1_CAPACITY="3", PLS_CARD_1_BUDGET="96",
                            PLS_CARD_2_CAPACITY="3"))
        app.BUDGET = 48.0
        self.assertEqual(self.acquire("any", cost=60)["card"]["id"], "maxq")
        self.assertEqual(self.acquire("any", cost=40)["card"]["id"], "ada")   # 100 > 96
        self.assertEqual(self.acquire("any", cost=30)["card"]["id"], "maxq")  # 90 <= 96
        self.assertEqual(self.acquire("any", cost=10)["state"], "waiting")    # 100 and 50

    def test_a_cost_over_every_eligible_budget_is_refused(self):
        app._configure(dict(TWO, PLS_CARD_1_BUDGET="96", PLS_CARD_2_BUDGET="48"))
        for on, cost in (("ada", 60), ("any", 100)):
            with self.subTest(on=on), self.assertRaises(urllib.error.HTTPError) as cm:
                self.acquire(on, cost=cost)
            self.assertEqual(cm.exception.code, 400)
            self.assertIn("over the budget of every card it can use",
                          json.loads(cm.exception.read().decode())["error"])
        self.assertEqual(self.acquire("any", cost=60)["card"]["id"], "maxq")
        app._configure(dict(TWO, PLS_CARD_1_BUDGET="96"))  # the Ada has no budget: anything fits
        self.assertEqual(self.acquire("ada", cost=500)["card"]["id"], "ada")

    def test_grant_and_heartbeat_name_the_card(self):
        held = self.acquire("blackwell")
        expect = {"id": "maxq", "label": "RTX PRO 6000 Blackwell Max-Q", "device": MAXQ}
        self.assertEqual(held["card"], expect)
        self.assertEqual(post(self.url, "/heartbeat/" + held["id"])["card"], expect)
        waiting = self.acquire("maxq")
        self.assertIsNone(waiting["card"])
        self.release(held)
        self.assertEqual(post(self.url, "/heartbeat/" + waiting["id"])["card"], expect)

    def test_unknown_selector_is_refused_with_a_message(self):
        for on, msg in (("nope", "unknown card or tag 'nope'"), (5, "'on' must be")):
            with self.subTest(on=on), self.assertRaises(urllib.error.HTTPError) as cm:
                self.acquire(on)
            self.assertEqual(cm.exception.code, 400)
            self.assertIn(msg, json.loads(cm.exception.read().decode())["error"])
        self.assertEqual(get(self.url, "/status")["waiting"], [])

    def test_status_lists_cards_and_where_tickets_go(self):
        self.acquire("maxq", tool="big", cost=60)
        self.acquire("Blackwell,ada", tool="next")
        self.acquire("ada", tool="later")
        s = get(self.url, "/status")
        self.assertEqual(s["capacity"], 2)
        self.assertEqual([c["id"] for c in s["cards"]], ["maxq", "ada"])
        self.assertEqual(s["cards"][0], {
            "id": "maxq", "names": ["maxq", "blackwell"], "tags": ["sm_120"],
            "label": "RTX PRO 6000 Blackwell Max-Q", "device": MAXQ, "capacity": 1, "budget": 0.0})
        self.assertEqual([(h["tool"], h["card"]) for h in s["held"]], [("big", "maxq"), ("next", "ada")])
        self.assertEqual(s["waiting"][0]["on"], ["ada"])
        self.assertEqual(s["waiting"][0]["eligible"], ["ada"])
        self.assertEqual(s["default"], ["ada"])


class ThreeCardTests(BrokerCase):
    ENV = THREE

    def test_tag_spreads_over_its_group_only(self):
        a, b, c = self.acquire("sm_120"), self.acquire("sm_120"), self.acquire("sm_120")
        self.assertEqual((a["card"]["id"], b["card"]["id"]), ("maxq", "b2"))
        self.assertEqual(c["state"], "waiting")
        self.assertEqual(self.acquire()["card"]["id"], "ada")


class SingleCardTests(BrokerCase):
    ENV = ONE

    def test_one_card_behaves_like_today(self):
        a, b = self.acquire(), self.acquire("any")
        self.assertEqual((a["state"], a["card"]["id"]), ("held", "ada"))
        self.assertEqual((b["state"], b["position"]), ("waiting", 1))
        self.release(a)
        self.assertEqual(self.card_of(b), "ada")
        s = get(self.url, "/status")
        self.assertEqual(set(s) - {"cards", "default"}, {"label", "unit", "capacity", "budget", "held", "waiting"})
        self.assertEqual(s["capacity"], 1)

    def test_no_cards_keeps_todays_payloads(self):
        app._configure({})
        a, b = self.acquire(), self.acquire("ANY")
        self.assertIsNone(a["card"])
        s = get(self.url, "/status")
        self.assertNotIn("cards", s)
        self.assertNotIn("default", s)
        self.assertEqual(set(s["held"][0]), {"id", "user", "tool", "cost", "held_s"})
        self.assertEqual(set(s["waiting"][0]), {"id", "user", "tool", "priority", "waited_s"})
        with self.assertRaises(urllib.error.HTTPError):
            self.acquire("ada")


class PersistenceTests(BrokerCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "tickets.json")
        app._configure(dict(TWO, PLS_STATE_FILE=self.path))

    def restart(self, env=None):
        app._tickets.clear()
        app._configure(dict(env or TWO, PLS_STATE_FILE=self.path))
        with mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            app._load()
        return err.getvalue()

    def test_restart_reloads_live_tickets_with_a_fresh_ttl(self):
        held, waiting = self.acquire("maxq", tool="A"), self.acquire("maxq", tool="B")
        done = self.acquire("ada")
        self.release(done)
        before = {t["id"]: t["created_at"] for t in app._tickets.values()}
        with open(self.path) as fh:
            self.assertEqual(len(json.load(fh)["tickets"]), 2)

        app.HEARTBEAT_TTL = 1.0
        self.addCleanup(setattr, app, "HEARTBEAT_TTL", 30.0)
        time.sleep(1.1)  # older than the TTL by now, but a reload starts the clock again
        self.restart()
        app._reap()
        self.assertEqual(set(app._tickets), {held["id"], waiting["id"]})
        self.assertEqual({t["id"]: t["created_at"] for t in app._tickets.values()},
                         {k: v for k, v in before.items() if k != done["id"]})
        self.assertEqual(post(self.url, "/heartbeat/" + held["id"])["card"]["id"], "maxq")
        self.assertEqual(post(self.url, "/heartbeat/" + waiting["id"])["state"], "waiting")
        self.release(held)
        self.assertEqual(self.card_of(waiting), "maxq")

    def test_reboot_discards_saved_tickets(self):
        self.acquire("maxq")
        with mock.patch.object(app, "BOOT_ID", "another-boot"):
            self.restart()
        self.assertEqual(app._tickets, {})

    def test_tickets_for_cards_that_are_gone_are_dropped(self):
        self.acquire("maxq")
        kept = self.acquire("ada")
        err = self.restart(ONE)
        self.assertEqual(list(app._tickets), [kept["id"]])
        self.assertIn("card maxq is no longer configured", err)

    def test_a_holder_keeps_its_card_under_any_name_or_its_uuid(self):
        renamed = {
            "alias removed": dict(TWO, PLS_CARD_1_NAMES="maxq"),
            "names reordered": dict(TWO, PLS_CARD_1_NAMES="blackwell, maxq"),
            "renamed, same UUID": dict(TWO, PLS_CARD_1_NAMES="mq", PLS_CARD_1_TAGS="b"),
        }
        for why, env in renamed.items():
            with self.subTest(why):
                self.restart(TWO)
                app._tickets.clear()
                held = self.acquire("blackwell", tool="running")
                waiter = self.acquire("sm_120", tool="next")
                err = self.restart(env)
                t = app._tickets[held["id"]]
                self.assertEqual((t["state"], t["eligible"], t["card"]), ("held", [t["card"]], app.CARDS[0]["id"]))
                self.assertEqual(t["device"], MAXQ)
                self.assertEqual(app._tickets.get(waiter["id"], {}).get("state", "dropped"),
                                 "dropped" if why == "renamed, same UUID" else "waiting", err)

    def test_a_holder_saved_without_a_uuid_keeps_its_card_by_id(self):
        no_uuids = {k: v for k, v in TWO.items() if not k.endswith("_DEVICE")}
        cases = {  # (config when granted, config after the restart, the card it should keep)
            "names reordered": (no_uuids, dict(no_uuids, PLS_CARD_1_NAMES="blackwell, maxq"), "blackwell"),
            "alias removed": (TWO, dict(TWO, PLS_CARD_1_NAMES="maxq"), "maxq"),
            "first name removed": (TWO, dict(TWO, PLS_CARD_1_NAMES="blackwell"), None),
        }
        for why, (before, after, kept) in cases.items():
            with self.subTest(why):
                self.restart(before)
                app._tickets.clear()
                held = self.acquire("blackwell", tool="running")
                with open(self.path) as fh:
                    saved = json.load(fh)
                for t in saved["tickets"]:
                    del t["device"]  # the format a broker that saved no UUIDs wrote
                with open(self.path, "w") as fh:
                    json.dump(saved, fh)
                self.restart(after)
                self.assertEqual((app._tickets.get(held["id"]) or {}).get("card"), kept)

    def test_a_malformed_state_file_drops_only_what_is_malformed(self):
        good = self.acquire("ada", tool="good")
        with open(self.path) as fh:
            saved = json.load(fh)
        bad = [
            "oops", ["a", "list"], None,
            {k: v for k, v in saved["tickets"][0].items() if k != "id"},
            {k: v for k, v in saved["tickets"][0].items() if k != "created_at"},
            dict(saved["tickets"][0], id="nan", cost=float("nan")),
            dict(saved["tickets"][0], id="state", state="weird"),
        ]
        with open(self.path, "w") as fh:
            json.dump(dict(saved, tickets=bad + saved["tickets"]), fh)
        err = self.restart(TWO)
        self.assertEqual(list(app._tickets), [good["id"]])
        self.assertEqual(err.count("dropping saved ticket"), len(bad))
        for body in ("[]", '"x"', json.dumps({"boot_id": app.BOOT_ID, "tickets": {"a": 1}})):
            with self.subTest(body=body):
                with open(self.path, "w") as fh:
                    fh.write(body)
                self.assertIn("not a saved ticket list", self.restart(TWO))
                self.assertEqual(app._tickets, {})

    def test_the_broker_starts_on_a_malformed_state_file(self):
        with open(self.path, "w") as fh:
            fh.write(json.dumps({"boot_id": app.BOOT_ID, "tickets": ["oops"]}))
        env = dict(os.environ, PLS_PORT=str(free_port()), PLS_HOST="127.0.0.1",
                   PLS_STATE_FILE=self.path, **TWO)
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "server", "app.py")], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(100):
                try:
                    if get(f"http://127.0.0.1:{env['PLS_PORT']}", "/healthz") == {"ok": True}:
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("the broker never answered")
        finally:
            p.terminate()
            out, err = p.communicate(timeout=5)
        self.assertIn("dropping saved ticket None", err)
        self.assertIn("reloaded 0 ticket(s)", out)

    def test_release_is_saved(self):
        self.release(self.acquire("maxq"))
        with open(self.path) as fh:
            self.assertEqual(json.load(fh)["tickets"], [])


class ClientTests(BrokerCase):
    def setUp(self):
        super().setUp()
        saved = (client.PLS_URL, client._POLL, client._BEAT)

        def restore():
            client.PLS_URL, client._POLL, client._BEAT = saved
        self.addCleanup(restore)
        client.PLS_URL, client._POLL, client._BEAT = self.url, 0.05, 0.05

    def test_lease_sync_yields_the_grant(self):
        with client.lease_sync(tool="x", on="blackwell") as g:
            self.assertEqual((g.card, g.label, g.device),
                             ("maxq", "RTX PRO 6000 Blackwell Max-Q", MAXQ))
            self.assertEqual(get(self.url, "/status")["held"][0]["id"], g.ticket)

    def test_async_lease_goes_to_the_default_set(self):
        async def go():
            async with client.lease(tool="ax") as g:
                return g
        self.assertEqual(asyncio.run(go()).card, "ada")

    def test_lease_names_the_card_after_waiting(self):
        first = self.acquire("maxq")
        got = {}

        def worker():
            with client.lease_sync(tool="B", on=["sm_120"]) as g:
                got["card"] = g.card

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        time.sleep(0.3)
        self.assertEqual(got, {})
        self.release(first)
        th.join(timeout=5)
        self.assertEqual(got, {"card": "maxq"})

    def test_unknown_card_raises(self):
        with self.assertRaisesRegex(client.PlsError, "unknown card or tag 'nope'"):
            with client.lease_sync(on="nope"):
                self.fail("ran without a lease")

        async def go():
            async with client.lease(on="nope"):
                self.fail("ran without a lease")
        with self.assertRaises(client.PlsError):
            asyncio.run(go())

    def test_async_heartbeat_survives_a_failed_call(self):
        real, beats = client._call, []

        def flaky(method, path, *a, **kw):
            if path.startswith("/heartbeat/"):
                beats.append(path)
                if len(beats) == 1:
                    raise OSError("broker blip")
            return real(method, path, *a, **kw)

        async def go():
            async with client.lease(tool="ax", on="ada"):
                await asyncio.sleep(0.5)
        with mock.patch.object(client, "_call", flaky):
            asyncio.run(go())
        self.assertGreaterEqual(len(beats), 3)

    def test_both_clients_warn_when_a_held_lease_expires(self):
        async def run_async():
            async with client.lease(tool="ax", on="ada") as g:
                post(self.url, "/release/" + g.ticket)
                await asyncio.sleep(0.4)

        def run_sync():
            with client.lease_sync(tool="sx", on="ada") as g:
                post(self.url, "/release/" + g.ticket)
                time.sleep(0.4)

        for name, run in (("sync", run_sync), ("async", lambda: asyncio.run(run_async()))):
            with self.subTest(name), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
                run()
            self.assertEqual(err.getvalue().count("expired while held"), 1, name)


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("pls_cli", BIN)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class CliTests(BrokerCase):
    SHOW = 'echo "$CUDA_VISIBLE_DEVICES|$CUDA_DEVICE_ORDER|$PLS_DEVICE|$PLS_CARD|$1"'

    def setUp(self):
        super().setUp()
        self.env = dict(os.environ, PLS_URL=self.url, USER="clitester", PLS_POLL="0.05")
        for k in ("CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "PLS_DEVICE", "PLS_CARD"):
            self.env.pop(k, None)

    def pls(self, *args):
        return subprocess.run([sys.executable, BIN, *args], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=20)

    def pls_on_a_terminal(self, answer, *args):
        primary, secondary = os.openpty()
        try:
            p = subprocess.Popen([sys.executable, BIN, *args], env=self.env, stdin=secondary,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            os.write(primary, answer.encode())
            out, err = p.communicate(timeout=20)
        finally:
            os.close(secondary)
            os.close(primary)
        return p.returncode, out, err

    def nothing_queued(self):
        s = get(self.url, "/status")
        return not s["held"] and not s["waiting"]

    def test_run_exports_the_card_and_fills_device(self):
        p = self.pls("run", "--on", "maxq", "--", "sh", "-c", self.SHOW, "sh", "--gpus=device={device}")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), f"{MAXQ}|PCI_BUS_ID|{MAXQ}|maxq|--gpus=device={MAXQ}")
        self.assertIn("lease granted on maxq (RTX PRO 6000 Blackwell Max-Q)", p.stderr)

    def test_run_without_on_refuses_when_nobody_can_answer(self):
        self.acquire("ada", tool="ftljob")
        p = self.pls("run", "--", "echo", "ran")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stdout, "")
        self.assertIn("would queue for the default card:", p.stderr)
        self.assertIn("RTX 6000 Ada [ada]: held by u (ftljob), 0 waiting", p.stderr)
        self.assertIn("--on any, a name (maxq, blackwell, ada) or a tag (sm_120, sm_89)", p.stderr)
        self.assertIn("pass --on, or --yes for the default", p.stderr)
        self.assertEqual(len(get(self.url, "/status")["held"]), 1)  # only the ftljob lease

    def test_yes_takes_the_default_set_without_asking(self):
        for flag in ("--yes", "-y"):
            p = self.pls("run", flag, "--", "sh", "-c", 'echo "$PLS_CARD"')
            self.assertEqual((p.returncode, p.stdout.strip()), (0, "ada"), flag)
            self.assertNotIn("default", p.stderr)

    @unittest.skipUnless(hasattr(os, "openpty"), "needs a pseudo-terminal")
    def test_run_without_on_asks_on_a_terminal(self):
        code, out, err = self.pls_on_a_terminal("y\n", "run", "--", "sh", "-c", 'echo "$PLS_CARD"')
        self.assertEqual((code, out.strip()), (0, "ada"))
        self.assertIn("Queue for the default? [y/N]", err)
        for answer in ("n\n", "\n"):
            code, out, err = self.pls_on_a_terminal(answer, "run", "--", "echo", "ran")
            self.assertEqual((code, out), (1, ""), repr(answer))
            self.assertIn("Not started.", err)
            self.assertTrue(self.nothing_queued())

    def test_closed_stdin_is_not_an_answer(self):
        p = subprocess.run(["sh", "-c", 'exec 0<&-; exec "$0" "$1" run -- echo ran', sys.executable, BIN],
                           env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual((p.returncode, p.stdout), (2, ""))
        self.assertIn("Not started.", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertTrue(self.nothing_queued())

    def test_ctrl_c_while_the_question_reads_the_board(self):
        quiet = mock.patch.object(self.httpd, "handle_error")  # its reply finds pls gone
        quiet.start()
        self.addCleanup(quiet.stop)
        with app._lock:  # /status waits on the lock
            p = subprocess.Popen([sys.executable, BIN, "run", "--", "echo", "ran"], env=self.env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
            time.sleep(1.0)
            p.send_signal(signal.SIGINT)
            out, err = p.communicate(timeout=10)
        time.sleep(0.3)
        self.assertEqual((p.returncode, out), (130, ""))
        self.assertIn("Not started.", err)
        self.assertNotIn("Traceback", err)

    @unittest.skipUnless(hasattr(os, "openpty"), "needs a pseudo-terminal")
    def test_ctrl_c_at_the_question(self):
        primary, secondary = os.openpty()
        try:
            p = subprocess.Popen([sys.executable, BIN, "run", "--", "echo", "ran"], env=self.env,
                                 stdin=secondary, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            seen = b""
            while b"[y/N]" not in seen:
                ready, _, _ = select.select([p.stderr], [], [], 10)
                self.assertTrue(ready, seen)
                seen += os.read(p.stderr.fileno(), 4096)
            p.send_signal(signal.SIGINT)
            out, err = p.communicate(timeout=10)
        finally:
            os.close(secondary)
            os.close(primary)
        self.assertEqual((p.returncode, out), (130, b""))
        self.assertIn(b"Not started.", err)
        self.assertNotIn(b"Traceback", err)
        self.assertTrue(self.nothing_queued())

    def test_a_cost_no_card_can_take_is_an_error(self):
        app._configure(dict(TWO, PLS_CARD_2_BUDGET="48"))
        p = self.pls("run", "--on", "ada", "--cost", "60", "--", "echo", "ran")
        self.assertEqual((p.returncode, p.stdout), (2, ""))
        self.assertIn("error: cost 60 is over the budget of every card it can use (48)", p.stderr)

    def test_on_never_asks(self):
        p = self.pls("run", "--on", "ada", "--", "sh", "-c", 'echo "$PLS_CARD"')
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "ada"))
        self.assertNotIn("default", p.stderr)

    def test_one_card_never_asks(self):
        app._configure(ONE)
        p = self.pls("run", "--", "sh", "-c", 'echo "$PLS_CARD"')
        self.assertEqual((p.returncode, p.stdout.strip()), (0, "ada"))
        self.assertNotIn("default", p.stderr)

    def test_on_is_repeatable_and_comma_separated(self):
        self.acquire("maxq")
        for on in (["--on", "maxq", "--on", "sm_89"], ["--on", "MAXQ,sm_89"]):
            p = self.pls("run", *on, "--", "sh", "-c", 'echo "$PLS_CARD"')
            self.assertEqual(p.stdout.strip(), "ada", on)

    def test_unknown_card_is_an_error(self):
        p = self.pls("run", "--on", "nope", "--", "echo", "ran")
        self.assertEqual(p.returncode, 2)
        self.assertIn("error: unknown card or tag 'nope'", p.stderr)
        self.assertEqual(p.stdout, "")

    def test_device_placeholder_needs_a_card(self):
        app._configure({})
        p = self.pls("run", "--", "echo", "{device}")
        self.assertEqual(p.returncode, 2)
        self.assertIn("{device} but no card was granted", p.stderr)
        self.assertEqual(p.stdout, "")
        p = self.pls("run", "--", "sh", "-c", 'echo "${PLS_CARD-unset}|${CUDA_DEVICE_ORDER-unset}"')
        self.assertEqual(p.stdout.strip(), "unset|unset")

    def test_status_shows_each_card(self):
        self.addCleanup(setattr, app, "UNIT", app.UNIT)
        app.UNIT = "GB"
        self.acquire("maxq", tool="bigjob", cost=60)
        self.acquire("sm_120", tool="nextjob")
        self.env["PATH"] = ""  # no nvidia-smi, so no live usage to vary the output
        out = self.pls("status").stdout.splitlines()
        self.assertTrue(out[0].startswith("pls · "), out[0])
        self.assertEqual(out[2], "● RTX PRO 6000 Blackwell Max-Q   maxq, blackwell · sm_120")
        self.assertTrue(out[3].startswith("    u            bigjob  "), out[3])
        self.assertTrue(out[3].endswith("  ~60 GB"), out[3])
        self.assertEqual(out[4:8], ["○ RTX 6000 Ada   ada · sm_89 · default", "    free", "", "queue"])
        self.assertTrue(out[8].startswith("   1  u            nextjob  normal"), out[8])
        self.assertTrue(out[8].endswith("  for maxq"), out[8])

    def test_status_shows_full_job_names_and_lines_up_live_usage(self):
        cli = _load_cli()
        name = "bench maxq full-fused-fp32-msa complex rep1 seed0 with a long tail"
        status = {"label": "GPUs", "unit": "GB", "default": ["ada"], "waiting": [],
                  "held": [{"id": "1", "user": "alice", "tool": name, "cost": 0, "held_s": 5, "card": "maxq"}],
                  "cards": [{"id": "maxq", "label": "Max-Q", "names": ["maxq"], "tags": ["sm_120"],
                             "capacity": 1, "budget": 0, "device": MAXQ},
                            {"id": "ada", "label": "Ada", "names": ["ada"], "tags": [],
                             "capacity": 1, "budget": 0, "device": ADA}]}
        out = cli._render_status(status, usage={MAXQ: "6.4/96 GB   46%", ADA: "0.5/48 GB    0%"})
        self.assertEqual(out[2], "● Max-Q   maxq · sm_120   6.4/96 GB   46%")
        self.assertEqual(out[3], f"    alice        {name}      5s")
        self.assertEqual(out[4], "○ Ada   ada · default     0.5/48 GB    0%")

    def test_status_without_cards_shows_the_one_resource(self):
        cli = _load_cli()
        status = {"label": "GPU 0", "unit": "GB", "capacity": 1, "budget": 0.0,
                  "held": [{"id": "1", "user": "alice", "tool": "jobA", "cost": 20.0, "held_s": 75}],
                  "waiting": [{"id": "2", "user": "bob", "tool": "jobB", "priority": "high", "waited_s": 3}]}
        self.assertEqual(cli._render_status(status)[2:], [
            "● GPU 0",
            "    alice        jobA   1m15s  ~20 GB",
            "",
            "queue",
            "   1  bob          jobB  high        3s",
        ])

    def test_status_is_coloured_only_when_asked(self):
        cli = _load_cli()
        status = {"label": "GPU 0", "capacity": 1, "held": [], "waiting": []}
        self.assertNotIn("\x1b[", "\n".join(cli._render_status(status)))
        self.assertIn("\x1b[32m", "\n".join(cli._render_status(status, color=True)))


if __name__ == "__main__":
    unittest.main()
