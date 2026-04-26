#!/usr/bin/env bash
# =============================================================================
# cron_scrape.sh — Carlton County Jail Roster scraper cron wrapper
#
# Supports two modes set by SCRAPE_MODE in .env (or overridden here):
#   url  — fetch live PDF from SCRAPE_URL
#   file — parse a local PDF at SCRAPE_FILE
#
# Recommended crontab entry (every 6 hours):
#   0 */6 * * * /opt/jailroster/scripts/cron_scrape.sh >> /var/log/jailroster/cron.log 2>&1
#
# Setup:
#   sudo mkdir -p /var/log/jailroster
#   sudo chown $USER:$USER /var/log/jailroster
#   chmod +x /opt/jailroster/scripts/cron_scrape.sh
# =============================================================================

set -euo pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
VENV="$PROJECT_DIR/venv"
SCRAPER="$SCRIPT_DIR/scraper.py"
ENV_FILE="$PROJECT_DIR/.env"
LOG_DIR="/var/log/jailroster"
LOCK_FILE="/tmp/jailroster_scrape.lock"

# ── Load .env ─────────────────────────────────────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    echo "[$(date '+%F %T')] ERROR: .env not found at $ENV_FILE" >&2
    exit 1
fi

# ── Defaults (can be overridden in .env) ─────────────────────────────────────
SCRAPE_MODE="${SCRAPE_MODE:-url}"
SCRAPE_URL="${SCRAPE_URL:-https://jailroster.co.carlton.mn.us/CCJ_Jail_Roster.pdf}"
SCRAPE_FILE="${SCRAPE_FILE:-}"

# ── Logging ───────────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/scrape_$(date '+%Y-%m-%d').log"

log() {
    echo "[$(date '+%F %T')] $*" | tee -a "$LOG_FILE"
}

# ── Lock: prevent overlapping runs ───────────────────────────────────────────
if ! mkdir "$LOCK_FILE" 2>/dev/null; then
    log "WARN: Another scrape is already running (lock exists at $LOCK_FILE). Exiting."
    exit 0
fi
trap 'rm -rf "$LOCK_FILE"' EXIT

# ── Activate venv ─────────────────────────────────────────────────────────────
if [[ ! -f "$VENV/bin/activate" ]]; then
    log "ERROR: Virtual environment not found at $VENV"
    log "       Run: python3 -m venv $VENV && source $VENV/bin/activate && pip install -r $PROJECT_DIR/requirements.txt"
    exit 1
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# ── Run scraper ───────────────────────────────────────────────────────────────
log "===== Scrape run started (mode: $SCRAPE_MODE) ====="

case "$SCRAPE_MODE" in
    url)
        if [[ -z "$SCRAPE_URL" ]]; then
            log "ERROR: SCRAPE_MODE=url but SCRAPE_URL is not set in .env"
            exit 1
        fi
        log "Source: $SCRAPE_URL"
        python "$SCRAPER" --url "$SCRAPE_URL" 2>&1 | tee -a "$LOG_FILE"
        ;;
    file)
        if [[ -z "$SCRAPE_FILE" ]]; then
            log "ERROR: SCRAPE_MODE=file but SCRAPE_FILE is not set in .env"
            exit 1
        fi
        if [[ ! -f "$SCRAPE_FILE" ]]; then
            log "ERROR: File not found: $SCRAPE_FILE"
            exit 1
        fi
        log "Source: $SCRAPE_FILE"
        python "$SCRAPER" --file "$SCRAPE_FILE" 2>&1 | tee -a "$LOG_FILE"
        ;;
    *)
        log "ERROR: Unknown SCRAPE_MODE '$SCRAPE_MODE'. Must be 'url' or 'file'."
        exit 1
        ;;
esac

EXIT_CODE=${PIPESTATUS[0]}

if [[ $EXIT_CODE -eq 0 ]]; then
    log "===== Scrape run completed successfully ====="
else
    log "===== Scrape run FAILED (exit code $EXIT_CODE) ====="
fi

# ── Rotate logs older than 30 days ───────────────────────────────────────────
find "$LOG_DIR" -name "scrape_*.log" -mtime +30 -delete 2>/dev/null || true

exit $EXIT_CODE
