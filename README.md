# 🙏 pls - a Polite Little Semaphore

A tiny, dependency-free lease broker for **shared resources**: one, or any number of "cards"
(GPUs). Everything that wants a resource asks `pls` _before_ using it and releases when done;
`pls` keeps a list of tickets and decides who goes next. It **never touches the resource itself**
(it's pure bookkeeping) so it can't break your workloads and uses ~no resources of its own.

The flagship use case is **sharing GPUs across a small team** without the "hey, are you
using the GPU?" messages we all know and love. But the engine is _generic_: the resource can be
GPU VRAM, RAM, license seats, DB connections, API tokens, "credits"; anything with an additive cost.

```
# queue behind whoever's using the GPU
pls run --cost 42 -- docker run --gpus all my_container ...
# queue for one card in particular, and run on it
pls run --on maxq -- docker run --gpus device={device} my_container ...
# who's got what / who's waiting
pls status
#  ...and a live web page at :5555/
```

- **Stdlib only, Python ≥ 3.7.** No venv, no pip, no database, no Kubernetes.
- **Ticket + poll, never a long-held connection.** Nothing can "time out" on the broker.
- **Heartbeat + TTL.** A crashed job can't wedge the queue.
- **Fail-open.** If the broker is down, jobs run anyway (configurable) instead of blocking.
- **Survives restarts.** Live tickets are saved to disk and picked up again.

## How it works

It's a coat-check with a fixed number of hooks. You `acquire` -> get a numbered ticket
(instantly: _"you're #2"_). You poll `heartbeat` in a short local loop until it says `held`;
_then_ you use the resource. While working you keep heart-beating; when done you `release`. Stop
heart-beating (crash, Ctrl-C) and the broker reaps your ticket after `PLS_HEARTBEAT_TTL` seconds.

`PLS_CAPACITY` (default 1) is how many jobs may hold the resource at once. Set it >1 plus a
`PLS_BUDGET` to let several (small) jobs co-reside while their summed `--cost` fits the budget
(e.g. two small folds on a 48 GB GPU).

## Cards

With no card settings, pls guards one resource, as above. List cards in `.env` and it guards
each of them, from one to N, with one shared queue:

```ini
PLS_CARD_1_NAMES=maxq,blackwell
PLS_CARD_1_DEVICE=GPU-7c742dd2-98c6-60e3-e7a2-5f9fdaa28e52
PLS_CARD_1_LABEL=RTX PRO 6000 Blackwell Max-Q 96 GB
PLS_CARD_1_TAGS=sm_120
PLS_CARD_2_NAMES=ada
PLS_CARD_2_DEVICE=GPU-b893fc6e-1291-48a6-6789-e118daa7829b
PLS_CARD_2_LABEL=RTX 6000 Ada 48 GB
PLS_CARD_2_TAGS=sm_89
PLS_DEFAULT=ada
```

- **Names pick one card.** A card can have several (comma-separated, matched without regard to
  case); the first is the one pls shows. Its `DEVICE` UUID works as a name too.
- **Tags pick a group,** e.g. every card of one CUDA architecture.
- **`any` picks every card.**
- A ticket asks with `on`: `any`, names and/or tags, several at once meaning "any of these". A
  ticket that names nothing goes to the `PLS_DEFAULT` set (default: `any`).
- **Placement:** the queue is served by priority, then arrival, and each ticket gets the first
  card it can use that has room, in `<n>` order. Nothing is preempted. A ticket waiting for a busy
  card never holds up tickets that can use another one.
- Each card has its own `_CAPACITY` and `_BUDGET`, defaulting to `PLS_CAPACITY` and `PLS_BUDGET`.

pls refuses to start when a selector could mean two things: a name shared by two cards (UUIDs
included), a name that is also a tag, a card named or tagged `any`, a `PLS_DEFAULT` it doesn't
know, or a misspelt `PLS_CARD_*` setting. Asking for a card it doesn't know is an error the
client raises; only an unreachable broker makes the client run unguarded.

Address cards by UUID or name, never by index: `nvidia-smi` numbers GPUs in PCI order while
CUDA's default order is "fastest first", so `0` can mean two different cards.

## Restarts

Live tickets are written to `PLS_STATE_FILE`, or to `$STATE_DIRECTORY/tickets.json` under
systemd (the unit sets `StateDirectory=pls`, so `/var/lib/pls/tickets.json`). On start pls
reloads them, holders keep their cards, and every ticket gets a fresh TTL, so jobs that kept
heart-beating carry on as if nothing happened. Tickets saved before a reboot are discarded (pls
compares the kernel's boot ID): their jobs died with the machine.

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

The installer never overwrites an existing `/opt/pls/.env`, so add card settings to it by hand,
then `sudo systemctl restart pls`. Restart while `pls status` shows no lease if you change cards:
a saved ticket for a card or tag that no longer exists is dropped at start.

## Configuration

Everything is configured by environment variables, which can be set in a **`.env` file** at the
install root (`/opt/pls/.env`, or `$PLS_ENV_FILE`). The daemon, the CLI, and the embedded client
all read it on startup; **real environment variables override the file**. Copy `.env.example` to
`.env` and edit. Comments go on their own lines. Example (run the broker on a different port with
a custom UI label and unit):

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
| `PLS_CAPACITY`      | `1`               | concurrent holders (per card, unless the card sets its own)                                        |
| `PLS_BUDGET`        | `0`               | if >0, admit a job only while summed `--cost` <= budget (per card, likewise)                       |
| `PLS_HEARTBEAT_TTL` | `30`              | seconds of silence before a holder/waiter is reaped                                                |
| `PLS_LABEL`         | `shared resource` | headline shown on the status page (e.g. `GPU 0`, or `GPUs`)                                        |
| `PLS_UNIT`          | _(empty)_         | cost unit suffix shown in the UI (e.g. `GB`)                                                       |
| `PLS_STATE_FILE`    | _(see Restarts)_  | where live tickets are saved; unset and outside systemd, nothing is saved                          |
| `PLS_DEFAULT`       | `any`             | cards for tickets that name none (names and/or tags, comma-separated)                              |

**Cards** (`<n>` is a number and sets the placement order)

| var                     | default        | meaning                                                     |
| ----------------------- | -------------- | ----------------------------------------------------------- |
| `PLS_CARD_<n>_NAMES`    | _(required)_   | comma-separated names, case-insensitive; the first is shown |
| `PLS_CARD_<n>_DEVICE`   | _(none)_       | device UUID (`nvidia-smi -L`); doubles as a name            |
| `PLS_CARD_<n>_LABEL`    | the first name | shown in `pls status` and on the page                       |
| `PLS_CARD_<n>_TAGS`     | _(none)_       | comma-separated groups, e.g. `sm_120`                       |
| `PLS_CARD_<n>_CAPACITY` | `PLS_CAPACITY` | concurrent holders on this card                             |
| `PLS_CARD_<n>_BUDGET`   | `PLS_BUDGET`   | cost budget on this card                                    |

**Client**

`PLS_URL` defaults to `http://127.0.0.1:$PLS_PORT`; set it explicitly only for a
remote or containerized broker (e.g. `http://host.docker.internal:8555`). `PLS_REQUIRED=true`
makes the lease mandatory instead of failing open when the broker is down.

## CLI

```
pls run [--on CARD] [--cost N] [--prio low|normal|high] [--tool LABEL] [--user U] -- CMD...
pls status [--json]
pls watch [--interval S]
```

- `--on`: `any`, a card name or a tag. Repeat it or comma-separate to accept any of several
  (`--on maxq --on ada`, `--on sm_120,sm_89`). Without it, on a broker with several cards,
  `pls run` explains where the job would go (the broker's `PLS_DEFAULT`, who holds those cards,
  how many wait) and asks before queueing. Where nobody can answer (no terminal on stdin) it
  stops with the same explanation instead.
- `--yes`, `-y`: take the default cards without asking.
- `--cost`: the job's resource cost. Purely informational unless a budget
  is set; then it gates co-residency. Default `0`.
- `--prio`: serving order is `high`, then `normal`, then `low`. Orders the **waiting** queue (priority, then
  FIFO). Non-preemptive ie, it never bumps a job that's already holding. Default `normal`.
- `--tool`: the label shown on the board. Defaults to the **full command**.

When a card is granted, `pls run` says which one and hands it to the command:

- `{device}` anywhere in the command becomes the card's UUID, for
  `docker run --gpus device={device} ...`. A command containing `{device}` doesn't run unless a
  card with a UUID was granted (so never when failing open).
- The command's environment gains `CUDA_VISIBLE_DEVICES=<UUID>`, `CUDA_DEVICE_ORDER=PCI_BUS_ID`,
  `PLS_DEVICE=<UUID>` and `PLS_CARD=<name>`, so host processes see only their card.

`pls status` and `pls watch` show each card with its holders, then the queue with the cards
each ticket can use. Without cards the output is the single-resource view.

## Embedding in your app

The client is stdlib-only, so it drops into any process (even a container that mounts the install
read-only) with no new deps:

```python
import sys; sys.path.insert(0, "/opt/pls/client")
from pls import lease # async; or lease_sync for blocking code

async with lease("my_tool_name", cost=42, on="sm_120") as grant:
    ...  # use the GPU here: grant.device is its UUID
```

Both `lease` and `lease_sync` take `on` (a string or a list; omitted means the default set) and
yield a `Grant(ticket, card, label, device)`. They yield `None` when failing open, so fall back
to your usual device then. `card`, `label` and `device` are `None` on a broker without cards.
An unknown card or tag raises `pls.PlsError`. If the broker drops a lease that is still held
(say, after a long outage), the client prints a warning and the work carries on.

## Tests

```bash
bash scripts/test.sh
# or:
python3 -m unittest tests/test*.py
```

Covers the scheduling/admission/reaper logic, the HTTP endpoints, the sync + async client
(including fail-open and a waiting->granted handoff), and the CLI end-to-end, plus one, two and
three cards: config refusals, eligibility by name and tag, placement order, the default set,
restart reload and reboot discard, and `pls run`'s exports and `{device}`.

## HTTP API

```
POST /acquire   {user, tool, cost, priority, on} -> {id, state, position, ahead, card}
POST /heartbeat/<id>                             -> {id, state, position, ahead, card}
POST /release/<id>                               -> {ok}
GET  /status                                     -> {label, unit, capacity, budget, held[], waiting[], cards[], default[]}
GET  /                                           -> live HTML+JS status page
GET  /healthz                                    -> {ok}
```

- `on` is a string or a list of strings, each `any`, a name or a tag, comma-separated or not.
  An unknown one gets a `400` with `{"error": ...}`.
- `card` is `{id, label, device}` once the ticket is held on a card, else `null`.
- With cards, `held[]` entries gain `card` (its id), `waiting[]` entries gain `on` (what was
  asked) and `eligible` (card ids, in placement order), and `cards[]` lists every card with its
  names, tags, label, device, capacity and budget. `default` lists the card ids a ticket that names
  no card can use (`PLS_DEFAULT`). `capacity` is then the total over all cards.
  Without cards, `/status` has no `cards` or `default` key and its entries keep their original fields.
