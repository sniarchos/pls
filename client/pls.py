"""Stdlib-only client for pls (the Polite Little Semaphore lease broker).

Importable anywhere with zero third-party deps (including inside containers that mount the
install read-only) so the CLI and your app share one source of truth.

  lease_sync(...)   - sync context manager (used by the ``pls`` CLI and any blocking caller)
  lease(...)        - async context manager, a drop-in for an ``asyncio.Semaphore``

Both yield a ticket id, or ``None`` when running unguarded (see fail-open).

Fail-open: if the broker is unreachable the client retries for ``PLS_CONNECT_GRACE`` seconds and
then runs the work anyway (unguarded) with a warning, so a broker outage degrades gracefully
instead of blocking everyone. Set ``PLS_REQUIRED=true`` to make the lease mandatory instead.

Compatible with Python >= 3.7 (no third-party packages, no 3.8+/3.9+ stdlib calls).
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import urllib.request


def _load_env_file():
    """Populate os.environ from a .env file (KEY=VALUE lines; '#' comments; optional quotes;
    optional 'export '), WITHOUT overriding variables already set in the real environment.
    Looked up at $PLS_ENV_FILE, else <install root>/.env. A missing/unreadable file is ignored."""
    path = os.environ.get("PLS_ENV_FILE")
    if not path:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, ".env")
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
                    os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_env_file()

# PLS_PORT is the single knob: the server listens on it and the client targets it, so they can't
# drift. Set PLS_URL explicitly only for a remote/containerized broker (e.g. host.docker.internal).
_PORT = os.getenv("PLS_PORT", "5555")
PLS_URL = os.getenv("PLS_URL", f"http://127.0.0.1:{_PORT}")
_POLL = float(os.getenv("PLS_POLL", "2"))
_BEAT = float(os.getenv("PLS_BEAT", "10"))
_GRACE = float(os.getenv("PLS_CONNECT_GRACE", "15"))
_REQUIRED = os.getenv("PLS_REQUIRED", "false").lower() in ("1", "true", "yes")


def _call(method, path, payload=None, timeout=10):
    url = PLS_URL.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def _acquire(user, tool, cost, priority):
    return _call("POST", "/acquire",
                 {"user": user, "tool": tool, "cost": cost, "priority": priority})


def _acquire_resilient(user, tool, cost, priority):
    """Acquire a ticket, tolerating a broker that is briefly down. Returns the ticket, or
    ``None`` to signal "run unguarded" (only when the broker is optional)."""
    deadline = time.time() + _GRACE
    while True:
        try:
            return _acquire(user, tool, cost, priority)
        except Exception as exc:  # connection refused, DNS, timeout, ...
            if _REQUIRED:
                time.sleep(2)
                continue
            if time.time() > deadline:
                sys.stderr.write(
                    f"[pls] WARNING: broker unreachable ({exc}); running WITHOUT a lease\n"
                )
                return None
            time.sleep(2)


@contextlib.contextmanager
def lease_sync(tool="?", cost=0.0, priority="normal", user=None, on_wait=None):
    """Block until the resource is ours, keep the lease alive while the body runs, release on exit.

    ``on_wait(state)`` is called once per poll while queued (the CLI uses it to show the live
    queue position)."""
    user = user or os.getenv("USER") or "unknown"
    t = _acquire_resilient(user, tool, cost, priority)
    if t is None:
        yield None  # fail-open: run unguarded
        return

    tid = t["id"]
    while t.get("state") != "held":
        if on_wait:
            on_wait(t)
        time.sleep(_POLL)
        try:
            t = _call("POST", "/heartbeat/" + tid)
        except Exception:
            t = {"state": "waiting"}  # transient broker blip; keep waiting
        if t.get("state") in (None, "expired"):
            t = _acquire_resilient(user, tool, cost, priority)
            if t is None:
                yield None
                return
            tid = t["id"]

    stop = threading.Event()

    def _beat():
        while not stop.wait(_BEAT):
            try:
                _call("POST", "/heartbeat/" + tid)
            except Exception:
                pass

    threading.Thread(target=_beat, daemon=True).start()
    try:
        yield tid
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            _call("POST", "/release/" + tid)


@contextlib.asynccontextmanager
async def lease(tool="?", cost=0.0, priority="normal", user=None):
    """Async drop-in for a per-tool ``asyncio.Semaphore``. Each broker call is a brief offload
    to a worker thread; the waiting itself is native ``asyncio.sleep`` so the loop stays free."""
    import asyncio

    async def to_thread(fn, *a):  # asyncio.to_thread is 3.9+; this keeps us at 3.7+
        return await asyncio.get_running_loop().run_in_executor(None, fn, *a)

    user = user or os.getenv("USER") or "app"
    t = await to_thread(_acquire_resilient, user, tool, cost, priority)
    if t is None:
        yield None  # fail-open
        return

    tid = t["id"]
    while t.get("state") != "held":
        await asyncio.sleep(_POLL)
        try:
            t = await to_thread(_call, "POST", "/heartbeat/" + tid)
        except Exception:
            t = {"state": "waiting"}
        if t.get("state") in (None, "expired"):
            t = await to_thread(_acquire_resilient, user, tool, cost, priority)
            if t is None:
                yield None
                return
            tid = t["id"]

    async def _beat():
        try:
            while True:
                await asyncio.sleep(_BEAT)
                await to_thread(_call, "POST", "/heartbeat/" + tid)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    beat = asyncio.create_task(_beat())
    try:
        yield tid
    finally:
        beat.cancel()
        with contextlib.suppress(Exception):
            await to_thread(_call, "POST", "/release/" + tid)
