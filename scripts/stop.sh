#!/usr/bin/env bash
# Stop the server and its supervisor.
#
# Order matters: kill the supervisor first, otherwise it sees the server exit
# and immediately restarts it.
set -u

cd "$(dirname "$0")/.."

if [ -f .runtime/supervisor.pid ]; then
    pid=$(cat .runtime/supervisor.pid)
    kill "$pid" 2>/dev/null && echo "stopped supervisor $pid" || echo "supervisor $pid was not running"
    rm -f .runtime/supervisor.pid
fi

pkill -f "uvicorn app.main:app" 2>/dev/null && echo "stopped server" || echo "server was not running"
