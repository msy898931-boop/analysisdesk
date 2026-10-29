#!/usr/bin/env bash
# Report whether the server is actually up, and whether the AI path is live.
set -u

cd "$(dirname "$0")/.."
PORT="${PORT:-12000}"

if [ -f .runtime/supervisor.pid ] && kill -0 "$(cat .runtime/supervisor.pid)" 2>/dev/null; then
    echo "supervisor : running (pid $(cat .runtime/supervisor.pid))"
else
    echo "supervisor : not running"
fi

echo -n "health     : "
curl -s --max-time 10 "http://127.0.0.1:${PORT}/api/health" || echo "unreachable"
echo
