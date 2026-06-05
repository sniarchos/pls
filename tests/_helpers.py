"""Shared test helpers (stdlib only). Not collected by unittest (name isn't test_*)."""
import json
import socket
import threading
import urllib.request
from http.server import ThreadingHTTPServer


def free_port():
    """Return a probably-free localhost port (bind to :0, read it, close)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_broker(app, capacity=1, budget=0.0, ttl=30.0):
    """Start the broker's HTTP handler on an ephemeral port in a daemon thread.

    Resets the module's shared state so each test is isolated. Returns (httpd, base_url).
    The caller must httpd.shutdown()/server_close() in tearDown.
    """
    app._tickets.clear()
    app.CAPACITY = capacity
    app.BUDGET = budget
    app.HEARTBEAT_TTL = ttl
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def post(url, path, payload=None):
    """POST JSON (or nothing, for heartbeat/release) and return the decoded response."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url + path, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def get(url, path):
    with urllib.request.urlopen(url + path, timeout=5) as r:
        return json.loads(r.read().decode())
