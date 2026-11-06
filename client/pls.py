"""Stdlib-only client for pls (the Polite Little Semaphore lease broker).

Importable anywhere with zero third-party deps (including inside containers that mount the
install read-only) so the CLI and your app share one source of truth.

  lease_sync(...)   - sync context manager (used by the ``pls`` CLI and any blocking caller)
  lease(...)        - async context manager, a drop-in for an ``asyncio.Semaphore``

Both take ``on`` (``"any"``, card names and/or tags; omitted means the broker's default set) and
yield a ``Grant(ticket, card, label, device)``, or ``None`` when running unguarded. ``card``,
``label`` and ``device`` are ``None`` when the broker has no cards configured.

Fail-open: if the broker is unreachable the client retries for ``PLS_CONNECT_GRACE`` seconds and
then runs the work anyway (unguarded) with a warning, so a broker outage degrades gracefully
instead of blocking everyone. Set ``PLS_REQUIRED=true`` to make the lease mandatory instead. A
broker that refuses the request (an unknown card or tag) raises ``PlsError``.

Compatible with Python >= 3.7 (no third-party packages, no 3.8+/3.9+ stdlib calls).
"""
import collections
import contextlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request


def _settings():
    """The settings: a .env file (KEY=VALUE lines; '#' comments; optional quotes; optional
    'export ') under the real environment, which wins. The file is $PLS_ENV_FILE, else
    <install root>/.env; a missing or unreadable one is ignored. os.environ is left alone, so
    the file's settings never reach the processes this one starts."""
    path = os.environ.get("PLS_ENV_FILE")
    if not path:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, ".env")
    found = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, sep, val = line.partition("=")
                if sep:
                    found[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return {**found, **os.environ}


_SETTINGS = _settings()
# PLS_PORT is the single knob: the server listens on it and the client targets it, so they can't
# drift. Set PLS_URL explicitly only for a remote/containerized broker (e.g. host.docker.internal).
_PORT = _SETTINGS.get("PLS_PORT", "5555")
PLS_URL = _SETTINGS.get("PLS_URL", f"http://127.0.0.1:{_PORT}")
_POLL = float(_SETTINGS.get("PLS_POLL", "2"))
_BEAT = float(_SETTINGS.get("PLS_BEAT", "10"))
_GRACE = float(_SETTINGS.get("PLS_CONNECT_GRACE", "15"))
_REQUIRED = _SETTINGS.get("PLS_REQUIRED", "false").lower() in ("1", "true", "yes")

Grant = collections.namedtuple("Grant", "ticket card label device")


class PlsError(Exception):
    """The broker refused the request, e.g. it knows no card or tag by the name asked for."""


def _call(method, path, payload=None, timeout=10):
    url = PLS_URL.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def _acquire(user, tool, cost, priority, on):
    on = [on] if isinstance(on, str) else list(on or [])
    try:
        return _call("POST", "/acquire", {"user": user, "tool": tool, "cost": cost,
                                          "priority": priority, "on": on})
    except urllib.error.HTTPError as exc:
        if not 400 <= exc.code < 500:
            raise
        try:
            msg = json.loads(exc.read().decode()).get("error")
        except Exception:
            msg = None
        raise PlsError(msg or f"broker refused the lease (HTTP {exc.code})") from None


def _acquire_resilient(user, tool, cost, priority, on, cancel=None):
    """Acquire a ticket, tolerating a broker that is briefly down. Returns the ticket, or
    ``None`` to signal "run unguarded" (only when the broker is optional) or that ``cancel``, a
    threading.Event, was set."""
    cancel = cancel or threading.Event()
    deadline = time.monotonic_ns() + int(_GRACE * 1_000_000_000)
    while not cancel.is_set():
        try:
            return _acquire(user, tool, cost, priority, on)
        except PlsError:
            raise
        except Exception as exc:  # connection refused, DNS, timeout, ...
            if not _REQUIRED and time.monotonic_ns() > deadline:
                sys.stderr.write(
                    f"[pls] WARNING: broker unreachable ({exc}); running WITHOUT a lease\n"
                )
                return None
            cancel.wait(2)
    return None


def _poll(t, args):
    """One heartbeat while queued: the ticket's new state, a fresh ticket if the broker lost
    this one, or ``None`` to run unguarded."""
    try:
        nxt = _call("POST", "/heartbeat/" + t["id"])
    except Exception:
        return t  # transient broker blip; keep waiting
    if nxt.get("state") in (None, "expired"):
        return _acquire_resilient(*args)
    return nxt


def _beat_once(tid, done):
    """One heartbeat while holding; False once the broker has dropped the lease. ``done`` is set
    before our own release, which this beat may have raced."""
    try:
        state = _call("POST", "/heartbeat/" + tid).get("state")
    except Exception:
        return True  # a missed beat is harmless; the broker allows PLS_HEARTBEAT_TTL of silence
    if state == "expired":
        if not done.is_set():
            sys.stderr.write(f"[pls] WARNING: lease {tid} expired while held; "
                             "the work carries on without one\n")
        return False
    return True


def _release(tid):
    with contextlib.suppress(Exception):
        _call("POST", "/release/" + tid)


def _grant(t):
    card = t.get("card") or {}
    return Grant(t["id"], card.get("id"), card.get("label"), card.get("device"))


@contextlib.contextmanager
def lease_sync(tool="?", cost=0.0, priority="normal", user=None, on_wait=None, on=None):
    """Block until a card is ours, keep the lease alive while the body runs, release on exit.

    ``on_wait(state)`` is called once per poll while queued (the CLI uses it to show the live
    queue position)."""
    args = (user or os.getenv("USER") or "unknown", tool, cost, priority, on)
    t = _acquire_resilient(*args)
    try:
        while t is not None and t.get("state") != "held":
            if on_wait:
                on_wait(t)
            time.sleep(_POLL)
            t = _poll(t, args)
    except BaseException:
        if t is not None:
            _release(t["id"])
        raise
    if t is None:
        yield None  # fail-open: run unguarded
        return

    grant = _grant(t)
    stop = threading.Event()

    def _beat():
        while not stop.wait(_BEAT) and _beat_once(grant.ticket, stop):
            pass

    threading.Thread(target=_beat, daemon=True).start()
    try:
        yield grant
    finally:
        stop.set()
        _release(grant.ticket)


@contextlib.asynccontextmanager
async def lease(tool="?", cost=0.0, priority="normal", user=None, on=None):
    """Async drop-in for a per-tool ``asyncio.Semaphore``. Each broker call is a brief offload
    to a worker thread; the waiting itself is native ``asyncio.sleep`` so the loop stays free."""
    import asyncio

    loop = asyncio.get_running_loop()
    call = None

    async def to_thread(fn, *a):  # asyncio.to_thread is 3.9+; this keeps us at 3.7+
        return await loop.run_in_executor(None, fn, *a)

    async def step(fn, *a):
        # Shielded, so a cancelled wait can still learn which ticket the call in flight left.
        nonlocal call
        call = loop.run_in_executor(None, fn, *a)
        return await asyncio.shield(call)

    cancel = threading.Event()
    args = (user or os.getenv("USER") or "app", tool, cost, priority, on, cancel)
    t = None
    try:
        t = await step(_acquire_resilient, *args)
        while t is not None and t.get("state") != "held":
            await asyncio.sleep(_POLL)
            t = await step(_poll, t, args)
    except BaseException:
        cancel.set()  # a worker still retrying an unreachable broker stops
        with contextlib.suppress(BaseException):
            await asyncio.wait({call}, timeout=10)  # a broker call times out after 10 s anyway
            t = call.result()
        if t is not None:
            await to_thread(_release, t["id"])
        raise
    if t is None:
        yield None  # fail-open
        return

    grant = _grant(t)
    stop = threading.Event()

    async def _beat():
        while True:
            await asyncio.sleep(_BEAT)
            if not await to_thread(_beat_once, grant.ticket, stop):
                return

    beat = asyncio.create_task(_beat())
    try:
        yield grant
    finally:
        stop.set()
        beat.cancel()
        await to_thread(_release, grant.ticket)
