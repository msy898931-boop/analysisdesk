#!/usr/bin/env bash
# Keep the AnalysisDesk server up.
#
# This sandbox has no init system (PID 1 is the agent, systemd is offline, no
# cron, no supervisor), so the restart loop lives here. flock guarantees only
# one supervisor exists no matter how many times this is launched.
#
# It restarts the server if it exits for ANY reason. It cannot survive the
# container itself being recycled — nothing inside the container can. See
# scripts/start.sh for how to come back from that.
set -u

cd "$(dirname "$0")/.."
mkdir -p .runtime

exec 9>.runtime/supervisor.lock
if ! flock -n 9; then
    echo "a supervisor is already running" >&2
    exit 0
fi

echo $$ > .runtime/supervisor.pid
echo "[supervisor] started, pid $$" >> .runtime/server.log

while true; do
    python3 -m uvicorn app.main:app \
        --host "${HOST:-0.0.0.0}" --port "${PORT:-12000}" \
        >> .runtime/server.log 2>&1
    code=$?
    echo "[supervisor] server exited with code $code; restarting in 3s" >> .runtime/server.log
    sleep 3
done
