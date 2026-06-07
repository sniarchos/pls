#!/usr/bin/env python3
"""pls: a Polite Little Semaphore (a tiny single-resource lease broker).

A dependency-free HTTP service that arbitrates access to ONE shared, fungible resource (a
GPU, a slab of RAM, a pool of license seats, anything). Clients ask here *before* using the
resource and release when done; the broker just keeps a list of tickets in memory and decides
who goes next. It never touches the resource itself.

The resource is generic. "cost" is whatever additive quantity a job consumes (GPU VRAM in GB,
RAM, seats, credits...); set ``PLS_BUDGET`` to cap the running total, or leave it 0 to gate purely
on ``PLS_CAPACITY`` (a plain N-slot lock). GPU sharing is the flagship use case (see the README).

Design notes
------------
* **Stdlib only, Python >= 3.7.** No venv, no pip. It's the always-on arbiter, so it has the
  fewest possible moving parts.
* **Ticket + poll, never a long-held connection.** ``/acquire`` answers instantly ("you are #2");
  clients poll ``/heartbeat`` in a local loop until granted. Nothing the broker holds open, so
  no proxy/gateway timeout can ever fire on it.
* **Heartbeat + TTL.** A holder or waiter that goes silent for ``PLS_HEARTBEAT_TTL`` seconds is
  reaped, so a crashed job can never wedge the queue.

Endpoints
---------
  POST /acquire            {user, tool, cost, priority} -> {id, state, position}
  POST /heartbeat/<id>                                  -> {id, state, position}
  POST /release/<id>                                    -> {ok}
  GET  /status                                          -> {label, unit, capacity, budget, held, waiting}
  GET  /                                                -> live HTML+JS status page
  GET  /healthz                                         -> {ok}
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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

# ---- config (all env-overridable; see .env / .env.example) ------------------
HOST = os.getenv("PLS_HOST", "0.0.0.0")
PORT = int(os.getenv("PLS_PORT", "5555"))
CAPACITY = int(os.getenv("PLS_CAPACITY", "1"))               # max concurrent holders
BUDGET = float(os.getenv("PLS_BUDGET", "0"))                 # 0 = ignore cost, gate on CAPACITY only
HEARTBEAT_TTL = float(os.getenv("PLS_HEARTBEAT_TTL", "30"))  # silence -> reaped
DONE_RETAIN = float(os.getenv("PLS_DONE_RETAIN", "600"))     # keep finished tickets this long
_NS = 1_000_000_000
_HEARTBEAT_TTL_NS = int(HEARTBEAT_TTL * _NS)
_DONE_RETAIN_NS = int(DONE_RETAIN * _NS)
LABEL = os.getenv("PLS_LABEL", "shared resource")            # headline shown on the status page
UNIT = os.getenv("PLS_UNIT", "")                             # cost unit suffix, e.g. "GB"
PRIORITIES = {"high": 0, "normal": 10, "low": 20}            # lower = served first

# ---- in-memory state --------------------------------------------------------
_lock = threading.RLock()
_tickets = {}  # id -> ticket dict


def _now():
    return time.monotonic_ns()


def _held():
    return [t for t in _tickets.values() if t["state"] == "held"]


def _waiting():
    return sorted(
        (t for t in _tickets.values() if t["state"] == "waiting"),
        key=lambda t: (t["priority"], t["created_at"]),  # priority, then FIFO
    )


def _can_admit(t):
    held = _held()
    if len(held) >= CAPACITY:
        return False
    if BUDGET > 0:
        used = sum(x["cost"] for x in held)
        if used + t["cost"] > BUDGET:
            return False
    return True


def _schedule():
    """Grant waiting tickets in priority/FIFO order while there's room. Held tickets are never
    preempted (priority only decides who goes *next*)."""
    for t in _waiting():
        if _can_admit(t):
            t["state"] = "held"
            t["granted_at"] = _now()
            t["last_seen"] = _now()


def _reap():
    now = _now()
    for t in list(_tickets.values()):
        if t["state"] in ("waiting", "held") and now - t["last_seen"] > _HEARTBEAT_TTL_NS:
            t["state"] = "done"
            t["reaped"] = True
            t["ended_at"] = now
    _schedule()
    for tid in [tid for tid, t in _tickets.items()
                if t["state"] == "done" and now - t.get("ended_at", now) > _DONE_RETAIN_NS]:
        del _tickets[tid]


def _reaper_loop():
    while True:
        time.sleep(5)
        with _lock:
            _reap()


def _state_of(t):
    pos = _waiting().index(t) + 1 if t["state"] == "waiting" else None
    return {"id": t["id"], "state": t["state"], "position": pos,
            "ahead": (pos - 1) if pos else 0}


def _status_payload():
    now = _now()
    return {
        "label": LABEL,
        "unit": UNIT,
        "capacity": CAPACITY,
        "budget": BUDGET,
        "held": [
            {"id": t["id"], "user": t["user"], "tool": t["tool"], "cost": t["cost"],
             "held_s": (now - t["granted_at"]) // _NS if t.get("granted_at") else 0}
            for t in _held()
        ],
        "waiting": [
            {"id": t["id"], "user": t["user"], "tool": t["tool"],
             "priority": t["priority_name"], "waited_s": (now - t["created_at"]) // _NS}
            for t in _waiting()
        ],
    }


# ---- HTTP -------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "pls/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_a):  # keep the journal quiet
        pass

    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode() or "{}")
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/status":
            with _lock:
                self._send(200, _status_payload())
        elif self.path in ("/", "/index.html"):
            self._send(200, _PAGE.encode(), ctype="text/html; charset=utf-8")
        elif self.path == "/healthz":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        parts = self.path.strip("/").split("/")
        if self.path == "/acquire":
            body = self._read_json()
            prio_name = str(body.get("priority") or "normal")
            if prio_name not in PRIORITIES:
                prio_name = "normal"
            with _lock:
                t = {
                    "id": uuid.uuid4().hex[:12],
                    "user": str(body.get("user") or "unknown")[:40],
                    "tool": str(body.get("tool") or "?")[:1000],
                    "cost": float(body.get("cost") or 0.0),
                    "priority": PRIORITIES.get(prio_name, 10),
                    "priority_name": prio_name,
                    "state": "waiting",
                    "created_at": _now(),
                    "granted_at": None,
                    "last_seen": _now(),
                }
                _tickets[t["id"]] = t
                _schedule()
                self._send(200, _state_of(t))
        elif len(parts) == 2 and parts[0] == "heartbeat":
            with _lock:
                t = _tickets.get(parts[1])
                if t and t["state"] in ("waiting", "held"):
                    t["last_seen"] = _now()
                    _schedule()
                    self._send(200, _state_of(t))
                else:
                    self._send(200, {"id": parts[1], "state": "expired", "position": None})
        elif len(parts) == 2 and parts[0] == "release":
            with _lock:
                t = _tickets.get(parts[1])
                if t:
                    t["state"] = "done"
                    t["ended_at"] = _now()
                    t["last_seen"] = _now()
                    _schedule()
                self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})


# Dependency-free status page: plain HTML + vanilla JS that polls /status every 2s and
# re-renders in place (no reloads, no frameworks, no external fonts/CDNs). Light/dark adaptive.
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🙏</text></svg>">
<title>pls</title>
<style>
  :root{
    --bg:#f6f7f9; --panel:#fff; --text:#1b1f24; --muted:#6b7280; --faint:#9aa1ab;
    --border:#e6e8ec; --head:#f3f4f6; --row:#fbfbfc;
    --green:#22c55e; --red:#ef4444;
    --high-bg:#e0edff; --high-fg:#1d4ed8; --norm-bg:#eef0f3; --norm-fg:#4b5563;
    --low-bg:#f1f2f4; --low-fg:#98a0ab;
    --shadow:0 1px 2px rgba(0,0,0,.04), 0 1px 3px rgba(0,0,0,.06);
  }
  @media (prefers-color-scheme:dark){
    :root{
      --bg:#0e1014; --panel:#161a20; --text:#e7e9ec; --muted:#9aa3af; --faint:#6b7280;
      --border:#262b33; --head:#1b2027; --row:#191d24;
      --green:#34d399; --red:#f87171;
    --high-bg:#1a2a4d; --high-fg:#9cc0ff; --norm-bg:#222730; --norm-fg:#aab2bd;
    --low-bg:#1c2128; --low-fg:#6b7280;
      --shadow:0 1px 2px rgba(0,0,0,.3);
    }
  }
  *{box-sizing:border-box}
  body{margin:0; background:var(--bg); color:var(--text);
       font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
       -webkit-font-smoothing:antialiased;}
  .wrap{max-width:760px; margin:0 auto; padding:2.2rem 1.4rem 4rem;}
  header{display:flex; align-items:baseline; justify-content:space-between; gap:1rem; flex-wrap:wrap;}
  h1{font-size:1.25rem; font-weight:650; margin:0; display:flex; align-items:center; gap:.55rem;
     letter-spacing:.02em;}
  .dot{width:.6rem; height:.6rem; border-radius:50%; background:var(--green); animation:pulse 2s infinite;}
  .dot.stale{background:var(--red); animation:none;}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(52,211,153,.45)}
                   70%{box-shadow:0 0 0 .5rem rgba(52,211,153,0)}
                   100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}
  .sub{color:var(--muted); margin:.35rem 0 1.7rem; font-size:.9rem;}
  .badge{display:inline-block; border:1px solid var(--border); border-radius:999px;
         padding:.05rem .55rem; font-size:.78rem; color:var(--muted); background:var(--panel);}
  .ts{color:var(--faint); font-size:.82rem; font-variant-numeric:tabular-nums;}
  h2{font-size:.72rem; text-transform:uppercase; letter-spacing:.07em; color:var(--faint);
     margin:1.7rem 0 .6rem; font-weight:600;}
  .card{background:var(--panel); border:1px solid var(--border); border-radius:12px;
        box-shadow:var(--shadow); overflow:hidden;}
  table{width:100%; border-collapse:collapse;}
  th{font-size:.7rem; text-transform:uppercase; letter-spacing:.04em; color:var(--faint);
     text-align:left; font-weight:600; padding:.55rem .9rem; background:var(--head);
     border-bottom:1px solid var(--border);}
  td{padding:.62rem .9rem; border-bottom:1px solid var(--border);}
  tr:last-child td{border-bottom:none;}
  tbody tr:nth-child(even){background:var(--row);}
  .u{font-weight:600;}
  .tool{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:.85rem;
        word-break:break-all;}
  .num{font-variant-numeric:tabular-nums; color:var(--muted);}
  .pos{display:inline-flex; align-items:center; justify-content:center; min-width:1.45rem;
       height:1.45rem; border-radius:7px; background:var(--head); color:var(--muted);
       font-size:.8rem; font-weight:600; font-variant-numeric:tabular-nums;}
  .pill{display:inline-block; border-radius:999px; padding:.1rem .55rem; font-size:.74rem; font-weight:600;}
    .pill.high{background:var(--high-bg); color:var(--high-fg);}
  .pill.normal{background:var(--norm-bg); color:var(--norm-fg);}
    .pill.low{background:var(--low-bg); color:var(--low-fg);}
  .empty{padding:1.3rem .9rem; color:var(--faint); font-style:italic; text-align:center;}
  .free{display:flex; align-items:center; gap:.6rem; padding:1.15rem .9rem; color:var(--muted);}
  .free .dot{animation:none;}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1><span id="dot" class="dot"></span>pls</h1>
    <span class="ts">updated <span id="ts">—</span></span>
  </header>
  <div class="sub" id="sub"></div>

  <h2>In use</h2>
  <div class="card" id="held-card"></div>

  <h2>Queue</h2>
  <div class="card" id="wait-card"></div>
</div>

<script>
const fmt = s => {
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60), x = s % 60;
  if (m < 60) return m + "m " + String(x).padStart(2, "0") + "s";
  return Math.floor(m / 60) + "h " + String(m % 60).padStart(2, "0") + "m";
};
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt != null) e.textContent = txt;
  return e;
};
const tdTool = name => {
  const td = el("td");
  td.append(el("span", "tool", name));
  return td;
};

function renderHeld(card, held, unit) {
  card.replaceChildren();
  if (!held.length) {
    const d = el("div", "free");
    d.append(el("span", "dot"), el("span", null, "free"));
    card.append(d);
    return;
  }
  const t = el("table");
  t.innerHTML = "<thead><tr><th>User</th><th>Tool</th><th>Held for</th><th>Cost</th></tr></thead>";
  const tb = el("tbody");
  for (const h of held) {
    const tr = el("tr");
    tr.append(el("td", "u", h.user), tdTool(h.tool), el("td", "num", fmt(h.held_s)),
              el("td", "num", h.cost ? (h.cost + (unit ? " " + unit : "")) : "—"));
    tb.append(tr);
  }
  t.append(tb); card.append(t);
}

function renderWait(card, waiting) {
  card.replaceChildren();
  if (!waiting.length) { card.append(el("div", "empty", "nobody waiting")); return; }
  const t = el("table");
  t.innerHTML = "<thead><tr><th>#</th><th>User</th><th>Tool</th><th>Priority</th><th>Waited</th></tr></thead>";
  const tb = el("tbody");
  waiting.forEach((w, i) => {
    const tr = el("tr");
    const tdPos = el("td"); tdPos.append(el("span", "pos", String(i + 1)));
    const tdPri = el("td"); tdPri.append(el("span", "pill " + w.priority, w.priority));
    tr.append(tdPos, el("td", "u", w.user), tdTool(w.tool), tdPri, el("td", "num", fmt(w.waited_s)));
    tb.append(tr);
  });
  t.append(tb); card.append(t);
}

async function tick() {
  const dot = document.getElementById("dot");
  try {
    const s = await (await fetch("/status", { cache: "no-store" })).json();
    dot.classList.remove("stale");
    let sub = '<span class="badge">' + (s.label || "resource") + '</span>' +
              ' <span class="badge">capacity ' + s.capacity + '</span>';
    if (s.budget > 0) sub += ' <span class="badge">budget ' + s.budget + (s.unit ? ' ' + s.unit : '') + '</span>';
    document.getElementById("sub").innerHTML = sub;
    renderHeld(document.getElementById("held-card"), s.held, s.unit);
    renderWait(document.getElementById("wait-card"), s.waiting);
    document.getElementById("ts").textContent = new Date().toLocaleTimeString();
  } catch (e) {
    dot.classList.add("stale");
    document.getElementById("sub").textContent = "broker unreachable...";
  }
}
tick();
setInterval(tick, 2000);
</script>
</body>
</html>"""


def main():
    threading.Thread(target=_reaper_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(
        f"pls listening on {HOST}:{PORT} (capacity={CAPACITY}, budget={BUDGET}, ttl={HEARTBEAT_TTL}s, label={LABEL!r})",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
