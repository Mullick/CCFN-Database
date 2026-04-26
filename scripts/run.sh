#!/usr/bin/env bash
# run.sh — Activate venv and run scraper against live URL
set -e

cd "$(dirname "$0")"
source venv/bin/activate

python scraper.py --url https://jailroster.co.carlton.mn.us/CCJ_Jail_Roster.pdf
