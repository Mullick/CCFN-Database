#!/usr/bin/env bash
# start_web.sh — Start the Flask web interface via gunicorn
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

source "$PROJECT_DIR/venv/bin/activate"
cd "$PROJECT_DIR"

exec gunicorn \
    --workers 2 \
    --bind 127.0.0.1:5000 \
    --access-logfile /var/log/jailroster/web_access.log \
    --error-logfile /var/log/jailroster/web_error.log \
    --daemon \
    --pid /tmp/jailroster_web.pid \
    web.app:app

echo "Web server started."
