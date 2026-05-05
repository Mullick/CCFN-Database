#!/usr/bin/env bash
# stop_web.sh — Stop the gunicorn web server
PID_FILE="/tmp/jailroster_web.pid"
if [ -f "$PID_FILE" ]; then
    kill "$(cat $PID_FILE)" && echo "Web server stopped."
    rm -f "$PID_FILE"
else
    echo "No PID file found — server may not be running."
fi
