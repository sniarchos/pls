#!/usr/bin/env bash
# Spin up pls locally (no sudo, no /opt) and fire a few fake jobs at it so you can watch the
# queue form and drain. Each "job" is just `sleep` standing in for real work. Capacity is 1,
# so jobs run one at a time. The flagship resource here is a GPU (label/unit set below).
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PLS_PORT:-5556}"   # an unlikely-to-be-busy port for the throwaway demo broker
export PLS_URL="http://127.0.0.1:${PORT}"
export PLS_POLL=1                 # poll fast so the demo is snappy
export PLS_LABEL="${PLS_LABEL:-GPU 0}"
export PLS_UNIT="${PLS_UNIT:-GB}"

echo "==> starting pls on :${PORT}  (label=${PLS_LABEL})"
PLS_PORT="$PORT" python3 "$HERE/server/app.py" &
BROKER=$!
trap 'kill $BROKER 2>/dev/null || true' EXIT
sleep 1

echo "==> launching jobs (capacity 1, so they run one at a time):"
echo "    - bizon: a terminal 'docker run' with NO --tool -> label defaults to the full command"
echo "    - alice/bob: normal priority   carol: high (jumps the queue)"

# A real terminal user would type the docker command directly; here `bash -c 'sleep 8 # ...'`
# stands in for it (the '#' makes bash just sleep and ignore the rest). With no --tool the
# whole command becomes the label -- long enough to show the web view's truncate + click-to-expand.
USER=bizon  "$HERE/bin/pls" run --cost 24 --prio low -- \
    bash -c 'sleep 8 # docker run --rm --gpus all -v /data/run9qpy:/workdir -e SEED=0 ghcr.io/qvq/boltzgen:latest /app/src/boltzgen/main.py /workdir/9qpy_prod/config/design.yaml' &
sleep 0.4   # let it grab the resource first so it shows up as the IN USE holder
USER=alice  "$HERE/bin/pls" run --tool jobA --cost 20 --prio normal      -- sleep 6 &
USER=bob    "$HERE/bin/pls" run --tool jobB --cost 20 --prio normal      -- sleep 6 &
USER=carol  "$HERE/bin/pls" run --tool jobC --cost 20 --prio high -- sleep 6 &

sleep 2
echo
echo "==> pls status  (bizon's full docker command is HELD; high-priority carol is ahead in the queue):"
"$HERE/bin/pls" status
echo
echo "==> open http://127.0.0.1:${PORT}/ in a browser to watch it live."
echo "==> waiting for all jobs to drain..."
wait $(jobs -p | grep -v "$BROKER") 2>/dev/null || true
echo
echo "==> final status (should be free):"
"$HERE/bin/pls" status
