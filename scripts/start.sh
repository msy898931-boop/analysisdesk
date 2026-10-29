#!/usr/bin/env bash
# Start the server detached so it keeps running after this shell closes.
#
# Idempotent: running it twice will not start a second server. It also
# reinstalls dependencies if the container was recycled and wiped them, which
# is the one failure a restart loop inside the container cannot fix on its own.
set -u

cd "$(dirname "$0")/.."
mkdir -p .runtime

if [ -f .runtime/supervisor.pid ] && kill -0 "$(cat .runtime/supervisor.pid)" 2>/dev/null; then
    echo "already running (supervisor pid $(cat .runtime/supervisor.pid))"
    exit 0
fi

if ! python3 -c "import fastapi, httpx, pandas, uvicorn" 2>/dev/null; then
    echo "dependencies missing — installing from requirements.txt"
    pip install -q -r requirements.txt || {
        echo "install failed; see the output above" >&2
        exit 1
    }
fi

# setsid detaches into its own session so closing the terminal cannot SIGHUP it.
# The supervisor writes its own pid file once it holds the lock.
setsid nohup bash scripts/supervise.sh > .runtime/supervisor.out 2>&1 &

for _ in $(seq 1 20); do
    sleep 0.5
    if [ -f .runtime/supervisor.pid ] && kill -0 "$(cat .runtime/supervisor.pid)" 2>/dev/null; then
        echo "started (supervisor pid $(cat .runtime/supervisor.pid))"
        exit 0
    fi
done

echo "supervisor did not come up; see .runtime/supervisor.out" >&2
exit 1
