#!/usr/bin/env python3
"""
app.py — Carlton County Jail Roster web interface.
Serves two pages: /query and /current

Run:
    source venv/bin/activate
    python web/app.py

Or with gunicorn:
    gunicorn -w 2 -b 0.0.0.0:5000 web.app:app
"""

import json
import os
from datetime import datetime

import mysql.connector
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for

load_dotenv()

app = Flask(__name__, template_folder="templates", static_folder="static")

DB_CFG = dict(
    host     = os.getenv("DB_HOST", "localhost"),
    port     = int(os.getenv("DB_PORT", 3306)),
    database = os.getenv("DB_NAME", "jail_roster"),
    user     = os.getenv("DB_USER", "jailapp"),
    password = os.getenv("DB_PASS", ""),
)

PHOTO_URL_BASE = os.getenv("PHOTO_URL_BASE", "/photos")


def get_db():
    conn = mysql.connector.connect(**DB_CFG)
    conn.row_factory = None
    return conn


def query_db(sql, params=None):
    conn = get_db()
    cur  = conn.cursor(dictionary=True)
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def parse_charges(charges_val):
    if not charges_val:
        return []
    if isinstance(charges_val, list):
        return charges_val
    try:
        return json.loads(charges_val)
    except Exception:
        return [str(charges_val)]


def photo_url(filename):
    if not filename:
        return None
    return f"{PHOTO_URL_BASE}/{filename}"


@app.context_processor
def inject_helpers():
    return dict(photo_url=photo_url, parse_charges=parse_charges)


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("current"))


@app.route("/current")
def current():
    inmates = query_db("""
        SELECT
            id, roster_id, full_name, race, age,
            hold_type, book_datetime, next_court_date,
            bail_amount, photo_filename,
            (SELECT COUNT(*) FROM inmates i2
             WHERE i2.full_name = inmates.full_name) AS record_count,
            (SELECT GROUP_CONCAT(i3.photo_filename ORDER BY i3.book_datetime SEPARATOR ',')
             FROM inmates i3
             WHERE i3.full_name = inmates.full_name
               AND i3.photo_filename IS NOT NULL) AS all_photos
        FROM inmates
        WHERE currently_incarcerated = 1
        ORDER BY full_name ASC
    """)
    return render_template("current.html", inmates=inmates)


@app.route("/query")
def query():
    mode    = request.args.get("mode", "")
    results = []
    search  = ""
    name_records = None  # grouped by person for name search

    if mode == "name":
        search = request.args.get("q", "").strip()
        if search:
            results = query_db("""
                SELECT * FROM inmates
                WHERE full_name LIKE %s
                   OR CONCAT(first_name, ' ', last_name) LIKE %s
                ORDER BY book_datetime DESC
            """, (f"%{search}%", f"%{search}%"))

    elif mode == "crime":
        search = request.args.get("q", "").strip()
        if search:
            results = query_db("""
                SELECT * FROM inmates
                WHERE JSON_SEARCH(LOWER(charges), 'one', %s) IS NOT NULL
                   OR charges LIKE %s
                ORDER BY book_datetime DESC
            """, (f"%{search.lower()}%", f"%{search}%"))

    elif mode == "date":
        search = request.args.get("q", "").strip()
        if search:
            results = query_db("""
                SELECT * FROM inmates
                WHERE DATE(book_datetime) = %s
                ORDER BY book_datetime DESC
            """, (search,))

    return render_template("query.html",
                           mode=mode,
                           search=search,
                           results=results)


@app.route("/inmate/<full_name>")
def inmate_profile(full_name):
    records = query_db("""
        SELECT * FROM inmates
        WHERE full_name = %s
        ORDER BY book_datetime DESC
    """, (full_name,))

    if not records:
        return render_template("404.html"), 404

    history = query_db("""
        SELECT h.*, i.full_name
        FROM inmate_history h
        JOIN inmates i ON h.inmate_id = i.id
        WHERE i.full_name = %s
        ORDER BY h.changed_at DESC
        LIMIT 100
    """, (full_name,))

    return render_template("inmate.html",
                           records=records,
                           history=history,
                           full_name=full_name)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
