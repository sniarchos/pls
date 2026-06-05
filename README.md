# pls - a Polite Little Semaphore

A tiny, dependency-free lease broker for **one shared resource**. Everything that wants the
resource asks `pls` _before_ using it and releases when done; `pls` keeps a list of tickets in
memory and decides who goes next. It **never touches the resource itself** (it's pure
bookkeeping) so it can't break your workloads and uses ~no resources of its own.

The flagship use case is **sharing a single GPU across a small team** without the "hey, are you
using the GPU?" messages we all know and love. But the engine is _generic_: the resource can be
GPU VRAM, RAM, license seats, DB connections, API tokens, "credits"; anything with an additive cost.

```
# queue behind whoever's using the GPU
pls run --cost 42 -- docker run --gpus all my_container ...
# who's got it / who's waiting
pls status
#  ...and a live web page at :5555/
```

- **Stdlib only, Python ≥ 3.7.** No venv, no pip, no database, no Kubernetes.
- **Ticket + poll, never a long-held connection.** Nothing can "time out" on the broker.
- **Heartbeat + TTL.** A crashed job can't wedge the queue.
- **Fail-open.** If the broker is down, jobs run anyway (configurable) instead of blocking.

## How it works

It's a coat-check with a fixed number of hooks. You `acquire` -> get a numbered ticket
(instantly: _"you're #2"_). You poll `heartbeat` in a short local loop until it says `held`;
_then_ you use the resource. While working you keep heart-beating; when done you `release`. Stop
heart-beating (crash, Ctrl-C) and the broker reaps your ticket after `PLS_HEARTBEAT_TTL` seconds.

`PLS_CAPACITY` (default 1) is how many jobs may hold the resource at once. Set it >1 plus a
`PLS_BUDGET` to let several (small) jobs co-reside while their summed `--cost` fits the budget
(e.g. two small folds on a 48 GB GPU).

## Layout

```
server/app.py            the broker (HTTP service), stdlib only
client/pls.py            shared client: lease (async) + lease_sync (sync)
bin/pls                  the CLI: pls run / pls status / pls watch
deploy/pls.service       systemd unit (for the /opt install)
scripts/install.sh       install + enable the daemon (sudo, prompts for dest)
scripts/demo.sh          local demo: watch a few jobs queue (no sudo)
```

## Try it locally (no sudo, no /opt)

```bash
# starts a throwaway broker, fires jobs, watch them queue + drain
bash scripts/demo.sh
# (it prints a http://127.0.0.1:<port>/ link for the live page)
```

Run the broker by hand (defaults to port 5555):

```bash
# listens on 0.0.0.0:5555
PLS_LABEL="GPU 0" PLS_UNIT=GB python3 server/app.py
bin/pls status
# talks to http://127.0.0.1:5555
bin/pls run --cost 12 -- sleep 30
```

## Install as a daemon

```bash
sudo bash scripts/install.sh
# -> prompts for install dir (default /opt/pls), /usr/local/bin/pls, and the systemd daemon (enabled)
pls status
```

## Configuration

Everything is configured by environment variables, which can be set in a **`.env` file** at the
install root (`/opt/pls/.env`, or `$PLS_ENV_FILE`). The daemon, the CLI, and the embedded client
all read it on startup; **real environment variables override the file**. Copy `.env.example` to
`.env` and edit. Example (run the broker on a different port with a custom UI label and unit):

```ini
# .env
PLS_PORT=8555
PLS_LABEL=GPU 0
PLS_UNIT=GB
```

**Server**

| var                 | default           | meaning                                                                                            |
| ------------------- | ----------------- | -------------------------------------------------------------------------------------------------- |
| `PLS_HOST`          | `0.0.0.0`         | bind address (for docker enthusiasts: 0.0.0.0 lets containers reach it via `host.docker.internal`) |
| `PLS_PORT`          | `5555`            | listen port (also the client's default target)                                                     |
| `PLS_CAPACITY`      | `1`               | concurrent holders                                                                                 |
| `PLS_BUDGET`        | `0`               | if >0, admit a job only while summed `--cost` <= budget                                            |
| `PLS_HEARTBEAT_TTL` | `30`              | seconds of silence before a holder/waiter is reaped                                                |
| `PLS_LABEL`         | `shared resource` | headline shown on the status page (e.g. `GPU 0`)                                                   |
| `PLS_UNIT`          | _(empty)_         | cost unit suffix shown in the UI (e.g. `GB`)                                                       |

**Client**

`PLS_URL` defaults to `http://127.0.0.1:$PLS_PORT`; set it explicitly only for a
remote or containerized broker (e.g. `http://host.docker.internal:8555`). `PLS_REQUIRED=true`
makes the lease mandatory instead of failing open when the broker is down.

## CLI

```
pls run [--cost N] [--prio low|normal|high] [--tool LABEL] [--user U] -- CMD...
pls status [--json]
pls watch [--interval S]
```

- `--cost`: the job's resource cost. Purely informational unless a `PLS_BUDGET`
  is set; then it gates co-residency. Default `0`.
- `--prio`: serving order is `high`, then `normal`, then `low`. Orders the **waiting** queue (priority, then
  FIFO). Non-preemptive ie, it never bumps a job that's already holding. Default `normal`.
- `--tool`: the label shown on the board. Defaults to the **full command** (truncated with `...`
  in the web view; click to expand).

## Embedding in your app

The client is stdlib-only, so it drops into any process (even a container that mounts the install
read-only) with no new deps:

```python
import sys; sys.path.insert(0, "/opt/pls/client")
from pls import lease # async; or lease_sync for blocking code

async with lease("my_tool_name", cost=42):
    ...  # use the GPU here
```

## Tests

```bash
bash scripts/test.sh
# or:
python3 -m unittest tests/test*.py
```

Covers the scheduling/admission/reaper logic, the HTTP endpoints, the sync + async client
(including fail-open and a waiting->granted handoff), and the CLI end-to-end.

## HTTP API

```
POST /acquire   {user, tool, cost, priority} -> {id, state, position}
POST /heartbeat/<id>                         -> {id, state, position}
POST /release/<id>                           -> {ok}
GET  /status                                 -> {label, unit, capacity, budget, held[], waiting[]}
GET  /                                       -> live HTML+JS status page
GET  /healthz                                -> {ok}
```
