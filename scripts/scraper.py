#!/usr/bin/env python3
"""
scraper.py — Carlton County Jail Roster PDF scraper.

Usage:
    python scraper.py --url https://jailroster.co.carlton.mn.us/CCJ_Jail_Roster.pdf
    python scraper.py --file /path/to/local/CCJ_Jail_Roster.pdf

Behaviour:
  - Extracts the roster print date from the PDF header ("Printed on April 25, 2026").
  - Each booking is treated as a unique record keyed by SHA-256(roster_id|name|book_datetime).
  - If a booking already exists, any changed fields are written to inmate_history and
    the live row is updated.
  - Inmates absent from the current roster for more than 24 hours are marked
    currently_incarcerated = 0.
  - A person returning after a genuine absence gets a brand-new row (new booking_key)
    because their book_datetime will differ.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import traceback
from datetime import datetime, date, timedelta
from pathlib import Path

import mysql.connector
import pdfplumber
import requests
from dotenv import load_dotenv
from pdf2image import convert_from_path
from PIL import Image

load_dotenv()

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
PHOTO_DIR   = Path(os.getenv("PHOTO_DIR",   "/var/www/jailroster/photos"))
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "/var/www/jailroster/reports"))

DB_CFG = dict(
    host     = os.getenv("DB_HOST", "localhost"),
    port     = int(os.getenv("DB_PORT", 3306)),
    database = os.getenv("DB_NAME", "jail_roster"),
    user     = os.getenv("DB_USER", "jailapp"),
    password = os.getenv("DB_PASS", ""),
)

# Fields we track for changes (must match column names in inmates table)
TRACKED_FIELDS = [
    "hold_type", "next_court_date", "out_date", "bail_amount",
    "charges", "arresting_agency", "holding_agency", "bonus",
    "currently_incarcerated",
]

# Grace period: if an inmate has been gone less than this, don't close out the booking
RELEASE_GRACE_HOURS = 24

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def get_connection():
    return mysql.connector.connect(**DB_CFG)


def booking_key(roster_id: str, full_name: str, book_datetime) -> str:
    ts = str(book_datetime) if book_datetime else ""
    key = f"{roster_id}|{full_name}|{ts}"
    return hashlib.sha256(key.encode()).hexdigest()


def parse_date(s: str):
    s = s.strip()
    for fmt in ("%m/%d/%y %H:%M", "%m/%d/%Y %H:%M", "%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_money(s: str):
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s) if s else None
    except ValueError:
        return None


def coerce_str(val) -> str:
    """Normalize a value to a comparable string for change-detection."""
    if val is None:
        return ""
    if isinstance(val, (list, dict)):
        return json.dumps(val, sort_keys=True)
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(val, date):
        return val.strftime("%Y-%m-%d")
    return str(val).strip()


# ─────────────────────────────────────────────
# PDF Text Parsing
# ─────────────────────────────────────────────

FOOTER_RE     = re.compile(r"\*\*\*Money can be deposited.*?JailATM\.com\*\*\*", re.IGNORECASE)
HEADER_RE     = re.compile(r"Page \d+ of \d+\s+Carlton Jail Website.*?\n", re.IGNORECASE)
COL_HEADER_RE = re.compile(r"Mugshot\s+Demographics\s+Arresting Agency\s+Charges.*", re.IGNORECASE)
ROSTER_ID_RE  = re.compile(r"^\s*(\d{5,9})\s*$", re.MULTILINE)
NAME_RE       = re.compile(r"^([A-Z][A-Z\-']+),\s+([A-Z][A-Z\s\-']+)$")
RACE_RE       = re.compile(
    r"^(American Indian or Alaska Native|White|Black or African American|"
    r"Asian|Hispanic|Pacific Islander|Unknown|Other.*)$", re.IGNORECASE
)
BOOKDT_RE     = re.compile(r"\b(\d{2}/\d{2}/\d{2,4}\s+\d{2}:\d{2})\b")
COURT_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{2,4}(?:\s+\d{2}:\d{2})?)\b")
BAIL_RE       = re.compile(r"\$[\d,]+\.?\d*")
CHARGE_RE     = re.compile(r"^\d+[A-Z]?\.\d+[\.\d\w\(\)]*\s*-\s*.+")
PRINT_DATE_RE = re.compile(r"Printed on\s+(\w+ \d{1,2},\s*\d{4})", re.IGNORECASE)

HOLD_TYPES = [
    "BENCH WARRANT", "PROBABLE CAUSE", "SUPERVISION VIOLATION",
    "UNDER SENTENCE", "HOLD FOR ANOTHER AGENCY", "SENTENCED",
    "AWAITING TRIAL", "PRETRIAL",
]


def extract_roster_print_date(full_text: str) -> date | None:
    """Parse 'Printed on April 25, 2026' from the PDF text."""
    m = PRINT_DATE_RE.search(full_text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).strip(), "%B %d, %Y").date()
    except ValueError:
        return None


def extract_text_from_pdf(pdf_path: str) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        pages = []
        for page in pdf.pages:
            text = page.extract_text(layout=False)
            if text:
                pages.append(text)
    return "\n".join(pages)


def split_into_blocks(full_text: str) -> list:
    text = HEADER_RE.sub("", full_text)
    text = COL_HEADER_RE.sub("", text)
    blocks = FOOTER_RE.split(text)
    return [b.strip() for b in blocks if b.strip()]


def parse_block(block: str) -> dict | None:
    lines = [l.rstrip() for l in block.splitlines() if l.strip()]

    record = {
        "roster_id": None, "full_name": None,
        "last_name": None, "first_name": None, "middle_name": None,
        "race": None, "age": None,
        "arresting_agency": None, "holding_agency": None,
        "book_datetime": None, "out_date": None,
        "hold_type": None, "next_court_date": None,
        "bail_amount": None, "charges": [],
        "bonus": None,
    }

    charges  = []
    agencies = []
    dates_seen = []

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if re.match(r"^(NAME:|RACE:|AGE:|HELD FOR:)\s*$", line):
            i += 1
            continue

        if not record["roster_id"] and re.match(r"^\d{5,9}$", line):
            record["roster_id"] = line
            i += 1
            continue

        if not record["full_name"]:
            m = NAME_RE.match(line)
            if m:
                record["last_name"]   = m.group(1).strip().title()
                rest = m.group(2).strip().split()
                record["first_name"]  = rest[0].title() if rest else ""
                record["middle_name"] = " ".join(rest[1:]).title() if len(rest) > 1 else None
                record["full_name"]   = f"{record['last_name']}, {record['first_name']}"
                if record["middle_name"]:
                    record["full_name"] += f" {record['middle_name']}"
                i += 1
                continue

        if not record["race"] and RACE_RE.match(line):
            record["race"] = line.strip()
            i += 1
            continue

        if not record["age"] and re.match(r"^\d{1,3}$", line):
            record["age"] = int(line)
            i += 1
            continue

        bm = BOOKDT_RE.search(line)
        if bm and not record["book_datetime"]:
            record["book_datetime"] = parse_date(bm.group(1))
            remainder = BOOKDT_RE.sub("", line).strip()
            if remainder:
                agencies.append(remainder)
            i += 1
            continue

        if CHARGE_RE.match(line):
            charges.append(line.strip())
            i += 1
            continue

        matched_hold = None
        for ht in HOLD_TYPES:
            if ht in line.upper():
                matched_hold = ht
                break
        if matched_hold:
            record["hold_type"] = matched_hold
            remainder = line.upper().replace(matched_hold, "").strip()
            _parse_date_bail(remainder, record, dates_seen)
            i += 1
            continue

        bm2 = BAIL_RE.search(line)
        if bm2 and not record["bail_amount"]:
            record["bail_amount"] = parse_money(bm2.group(0))
            i += 1
            continue

        if line and not re.match(r"^[\d/: ]+$", line):
            agencies.append(line)

        i += 1

    seen_ag = list(dict.fromkeys(agencies))
    record["arresting_agency"] = seen_ag[0] if len(seen_ag) >= 1 else None
    record["holding_agency"]   = seen_ag[1] if len(seen_ag) >= 2 else seen_ag[0] if seen_ag else None
    record["charges"] = charges

    if not record["roster_id"] or not record["full_name"]:
        return None
    return record


def _parse_date_bail(text: str, record: dict, dates_seen: list):
    bm = BAIL_RE.search(text)
    if bm and not record["bail_amount"]:
        record["bail_amount"] = parse_money(bm.group(0))
    for m in COURT_DATE_RE.finditer(text):
        dt = parse_date(m.group(1))
        if dt and dt not in dates_seen:
            dates_seen.append(dt)
            if not record["next_court_date"]:
                record["next_court_date"] = dt
            elif not record["out_date"]:
                record["out_date"] = dt.date()


# ─────────────────────────────────────────────
# Image Extraction
# ─────────────────────────────────────────────

def extract_mugshots(pdf_path: str, records: list) -> list:
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)

    images_by_page = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            sorted_imgs = sorted(page.images, key=lambda img: img.get("top", 0))
            images_by_page.extend((page_num, img) for img in sorted_imgs)

    if not images_by_page:
        print("  ⚠  No embedded images found — using page-render fallback.")
        _extract_mugshots_render_fallback(pdf_path, records)
        return records

    for idx, record in enumerate(records):
        if idx >= len(images_by_page):
            break
        _, img_ref = images_by_page[idx]
        try:
            raw = img_ref.get("stream").get_data()
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp.write(raw)
                tmp_path = tmp.name
            img = Image.open(tmp_path).convert("RGB")
            filename = f"{record['roster_id']}.jpg"
            img.save(PHOTO_DIR / filename, "JPEG", quality=90)
            record["photo_filename"] = filename
            os.unlink(tmp_path)
            print(f"  📷  Saved photo: {filename}")
        except Exception as e:
            print(f"  ⚠  Photo error for {record['roster_id']}: {e}")

    return records


def _extract_mugshots_render_fallback(pdf_path: str, records: list):
    try:
        pages = convert_from_path(pdf_path, dpi=150)
    except Exception as e:
        print(f"  ✗  Page render failed: {e}")
        return

    MUGSHOT_X = (0.0, 0.18)
    Y_BANDS   = [(0.0, 0.5), (0.5, 1.0)]

    record_idx = 0
    for page_img in pages:
        w, h = page_img.size
        x0, x1 = int(w * MUGSHOT_X[0]), int(w * MUGSHOT_X[1])
        for y0r, y1r in Y_BANDS:
            if record_idx >= len(records):
                break
            crop = page_img.crop((x0, int(h * y0r), x1, int(h * y1r)))
            roster_id = records[record_idx].get("roster_id", f"unk_{record_idx}")
            filename  = f"{roster_id}.jpg"
            crop.save(PHOTO_DIR / filename, "JPEG", quality=85)
            records[record_idx]["photo_filename"] = filename
            print(f"  📷  Saved cropped photo: {filename}")
            record_idx += 1


# ─────────────────────────────────────────────
# Database Logic
# ─────────────────────────────────────────────

INSERT_SQL = """
INSERT INTO inmates
    (roster_id, full_name, last_name, first_name, middle_name,
     race, age, arresting_agency, holding_agency,
     book_datetime, out_date, hold_type, next_court_date,
     bail_amount, charges, photo_filename, bonus,
     currently_incarcerated, last_seen_date,
     source_file, roster_print_date, booking_key)
VALUES
    (%(roster_id)s, %(full_name)s, %(last_name)s, %(first_name)s, %(middle_name)s,
     %(race)s, %(age)s, %(arresting_agency)s, %(holding_agency)s,
     %(book_datetime)s, %(out_date)s, %(hold_type)s, %(next_court_date)s,
     %(bail_amount)s, %(charges)s, %(photo_filename)s, %(bonus)s,
     1, %(last_seen_date)s,
     %(source_file)s, %(roster_print_date)s, %(booking_key)s)
"""

UPDATE_SQL = """
UPDATE inmates SET
    hold_type              = %(hold_type)s,
    next_court_date        = %(next_court_date)s,
    out_date               = %(out_date)s,
    bail_amount            = %(bail_amount)s,
    charges                = %(charges)s,
    arresting_agency       = %(arresting_agency)s,
    holding_agency         = %(holding_agency)s,
    bonus                  = %(bonus)s,
    currently_incarcerated = 1,
    last_seen_date         = %(last_seen_date)s,
    release_confirmed_at   = NULL,
    source_file            = %(source_file)s,
    roster_print_date      = %(roster_print_date)s
WHERE id = %(id)s
"""

HISTORY_SQL = """
INSERT INTO inmate_history
    (inmate_id, roster_id, full_name,
     field_name, old_value, new_value,
     source_file, roster_print_date)
VALUES
    (%(inmate_id)s, %(roster_id)s, %(full_name)s,
     %(field_name)s, %(old_value)s, %(new_value)s,
     %(source_file)s, %(roster_print_date)s)
"""


def fetch_existing(cur, bkey: str) -> dict | None:
    cur.execute("SELECT * FROM inmates WHERE booking_key = %s", (bkey,))
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def write_history(cur, existing: dict, updated: dict, source_file: str, roster_print_date):
    """Compare existing vs updated for tracked fields; insert history rows for changes."""
    changes = 0
    for field in TRACKED_FIELDS:
        old_val = coerce_str(existing.get(field))
        new_val = coerce_str(updated.get(field))
        if old_val != new_val:
            cur.execute(HISTORY_SQL, {
                "inmate_id":        existing["id"],
                "roster_id":        existing["roster_id"],
                "full_name":        existing["full_name"],
                "field_name":       field,
                "old_value":        old_val or None,
                "new_value":        new_val or None,
                "source_file":      source_file,
                "roster_print_date": roster_print_date,
            })
            changes += 1
    return changes


def mark_releases(cur, seen_booking_keys: set, roster_print_date, source_file: str):
    """
    Any booking that is currently_incarcerated=1 but was NOT seen in this roster
    gets flagged. If it was already flagged more than RELEASE_GRACE_HOURS ago,
    we close it out (currently_incarcerated = 0).
    """
    # Find all currently-active bookings not seen in this run
    placeholders = ",".join(["%s"] * len(seen_booking_keys)) if seen_booking_keys else "''"
    query = f"""
        SELECT id, roster_id, full_name, booking_key, release_confirmed_at
        FROM inmates
        WHERE currently_incarcerated = 1
        {"AND booking_key NOT IN (" + placeholders + ")" if seen_booking_keys else ""}
    """
    cur.execute(query, tuple(seen_booking_keys) if seen_booking_keys else ())
    absent = cur.fetchall()
    cols   = [d[0] for d in cur.description]

    released = flagged = 0
    now = datetime.utcnow()

    for row in absent:
        rec = dict(zip(cols, row))
        rca = rec["release_confirmed_at"]

        if rca is None:
            # First time we've noticed them gone — set the flag, don't release yet
            cur.execute(
                "UPDATE inmates SET release_confirmed_at = %s WHERE id = %s",
                (now, rec["id"])
            )
            flagged += 1
        elif (now - rca) >= timedelta(hours=RELEASE_GRACE_HOURS):
            # Gone for longer than the grace period — close out the booking
            cur.execute(
                """UPDATE inmates SET
                       currently_incarcerated = 0,
                       roster_print_date = %s,
                       source_file = %s
                   WHERE id = %s""",
                (roster_print_date, source_file, rec["id"])
            )
            # Log the status change to history
            cur.execute(HISTORY_SQL, {
                "inmate_id":         rec["id"],
                "roster_id":         rec["roster_id"],
                "full_name":         rec["full_name"],
                "field_name":        "currently_incarcerated",
                "old_value":         "1",
                "new_value":         "0",
                "source_file":       source_file,
                "roster_print_date": roster_print_date,
            })
            released += 1

    return flagged, released


def save_records(records: list, source_file: str, roster_print_date):
    conn = get_connection()
    cur  = conn.cursor()

    inserted = updated = skipped = total_changes = 0
    seen_booking_keys = set()

    for r in records:
        bkey = booking_key(
            r.get("roster_id") or "",
            r.get("full_name")  or "",
            r.get("book_datetime"),
        )
        seen_booking_keys.add(bkey)

        existing = fetch_existing(cur, bkey)

        row = {
            "roster_id":        r.get("roster_id"),
            "full_name":        r.get("full_name"),
            "last_name":        r.get("last_name"),
            "first_name":       r.get("first_name"),
            "middle_name":      r.get("middle_name"),
            "race":             r.get("race"),
            "age":              r.get("age"),
            "arresting_agency": r.get("arresting_agency"),
            "holding_agency":   r.get("holding_agency"),
            "book_datetime":    r.get("book_datetime"),
            "out_date":         r.get("out_date"),
            "hold_type":        r.get("hold_type"),
            "next_court_date":  r.get("next_court_date"),
            "bail_amount":      r.get("bail_amount"),
            "charges":          json.dumps(r.get("charges") or []),
            "photo_filename":   r.get("photo_filename"),
            "bonus":            r.get("bonus"),
            "last_seen_date":   roster_print_date,
            "source_file":      source_file,
            "roster_print_date": roster_print_date,
            "booking_key":      bkey,
        }

        try:
            if existing is None:
                cur.execute(INSERT_SQL, row)
                inserted += 1
            else:
                # Check for changes before updating
                n_changes = write_history(cur, existing, row, source_file, roster_print_date)
                total_changes += n_changes
                if n_changes > 0:
                    row["id"] = existing["id"]
                    cur.execute(UPDATE_SQL, row)
                    updated += 1
                else:
                    # No field changes — just refresh last_seen and incarcerated flag
                    cur.execute(
                        """UPDATE inmates SET
                               currently_incarcerated = 1,
                               last_seen_date = %s,
                               release_confirmed_at = NULL
                           WHERE id = %s""",
                        (roster_print_date, existing["id"])
                    )
                    skipped += 1

        except mysql.connector.Error as e:
            print(f"  ✗  DB error for {r.get('roster_id')}: {e}")

    # Mark inmates not seen in this roster
    flagged, released = mark_releases(cur, seen_booking_keys, roster_print_date, source_file)

    conn.commit()
    cur.close()
    conn.close()

    print(f"  ✅  Inserted: {inserted}  |  Updated: {updated} ({total_changes} field changes)"
          f"  |  Unchanged: {skipped}  |  Newly absent: {flagged}  |  Released: {released}")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def fetch_pdf(url: str) -> str:
    print(f"  ⬇  Downloading {url} ...")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.write(resp.content)
    tmp.close()
    print(f"  ✔  Downloaded {len(resp.content):,} bytes → {tmp.name}")
    return tmp.name


def run(source: str, is_url: bool):
    tmp_path = None
    try:
        pdf_path = fetch_pdf(source) if is_url else source
        tmp_path = pdf_path if is_url else None

        print(f"\n📄  Extracting text from: {pdf_path}")
        full_text = extract_text_from_pdf(pdf_path)

        roster_print_date = extract_roster_print_date(full_text)
        if roster_print_date:
            print(f"  📅  Roster print date: {roster_print_date}")
        else:
            print("  ⚠  Could not parse roster print date — using today.")
            roster_print_date = date.today()

        blocks  = split_into_blocks(full_text)
        print(f"    Found {len(blocks)} potential record blocks.")

        records = []
        for blk in blocks:
            parsed = parse_block(blk)
            if parsed:
                records.append(parsed)
            else:
                print(f"  ⚠  Skipped block: {blk[:80]!r}")

        print(f"\n🖼   Extracting mugshots ...")
        records = extract_mugshots(pdf_path, records)

        print(f"\n💾  Saving {len(records)} records ...")
        save_records(records, source, roster_print_date)

        print(f"\n✅  Done. Processed {len(records)} inmates from {source}.")

    except Exception as e:
        print(f"\n✗  Fatal error: {e}")
        traceback.print_exc()
        sys.exit(1)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def main():
    parser = argparse.ArgumentParser(description="Carlton County Jail Roster Scraper")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url",  help="URL of the jail roster PDF")
    group.add_argument("--file", help="Local path to a jail roster PDF")
    args = parser.parse_args()

    if args.url:
        run(args.url, is_url=True)
    else:
        if not os.path.isfile(args.file):
            print(f"✗  File not found: {args.file}")
            sys.exit(1)
        run(args.file, is_url=False)


if __name__ == "__main__":
    main()
