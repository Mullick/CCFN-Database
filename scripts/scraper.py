#!/usr/bin/env python3
"""
scraper.py — Carlton County Jail Roster PDF scraper.

Usage:
    python scraper.py --url https://jailroster.co.carlton.mn.us/CCJ_Jail_Roster.pdf
    python scraper.py --file /path/to/local/CCJ_Jail_Roster.pdf

The script:
  1. Downloads (or reads) the PDF.
  2. Extracts inmate records from the text layer.
  3. Extracts mugshot images embedded in the PDF.
  4. Saves mugshots to PHOTO_DIR as <id>_<roster_id>.jpg
  5. Inserts / skips-duplicate records in MySQL.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import traceback
from datetime import datetime
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

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def get_connection():
    return mysql.connector.connect(**DB_CFG)


def record_hash(roster_id: str, full_name: str, book_datetime: str) -> str:
    key = f"{roster_id}|{full_name}|{book_datetime}"
    return hashlib.sha256(key.encode()).hexdigest()


def parse_date(s: str):
    """Try several datetime formats; return datetime or None."""
    s = s.strip()
    for fmt in ("%m/%d/%y %H:%M", "%m/%d/%Y %H:%M", "%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_money(s: str):
    """'$2,000.00' → 2000.00 or None."""
    s = re.sub(r"[^\d.]", "", s)
    try:
        return float(s) if s else None
    except ValueError:
        return None


# ─────────────────────────────────────────────
# PDF Text Parsing
# ─────────────────────────────────────────────

# The roster repeats a footer line we can use as a record separator
FOOTER_RE = re.compile(
    r"\*\*\*Money can be deposited.*?JailATM\.com\*\*\*", re.IGNORECASE
)

# The page header line
HEADER_RE = re.compile(
    r"Page \d+ of \d+\s+Carlton Jail Website.*?\n", re.IGNORECASE
)

# Column-header line that appears at the top of each page
COL_HEADER_RE = re.compile(
    r"Mugshot\s+Demographics\s+Arresting Agency\s+Charges.*", re.IGNORECASE
)

# Roster ID: standalone number at the start of a block (5-8 digits, sometimes with leading zeros)
ROSTER_ID_RE = re.compile(r"^\s*(\d{5,9})\s*$", re.MULTILINE)

# Name line: "LAST, FIRST MIDDLE" in all-caps
NAME_RE = re.compile(r"^([A-Z][A-Z\-']+),\s+([A-Z][A-Z\s\-']+)$")

# Race / Age block — appears right after name
RACE_RE   = re.compile(r"^(American Indian or Alaska Native|White|Black or African American|Asian|Hispanic|Pacific Islander|Unknown|Other.*?)$", re.IGNORECASE)
AGE_RE    = re.compile(r"^\s*(\d{1,3})\s*$")

# "NAME:  RACE:  AGE:" label line — skip it
LABEL_RE  = re.compile(r"^NAME:\s*$|^RACE:\s*$|^AGE:\s*$")

# Booking datetime: "04/23/26 08:45"
BOOKDT_RE = re.compile(r"\b(\d{2}/\d{2}/\d{2,4}\s+\d{2}:\d{2})\b")

# Hold type keywords
HOLD_TYPES = [
    "BENCH WARRANT", "PROBABLE CAUSE", "SUPERVISION VIOLATION",
    "UNDER SENTENCE", "HOLD FOR ANOTHER AGENCY", "SENTENCED",
    "AWAITING TRIAL", "PRETRIAL",
]

# Next court date: standalone date after hold type
COURT_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{2,4}(?:\s+\d{2}:\d{2})?)\b")

# Bail amount
BAIL_RE = re.compile(r"\$[\d,]+\.?\d*")

# Charge line — statute code pattern
CHARGE_RE = re.compile(r"^\d+[A-Z]?\.\d+[\.\d\w\(\)]*\s*-\s*.+")


def extract_text_from_pdf(pdf_path: str) -> str:
    """Return full text of the PDF via pdfplumber."""
    with pdfplumber.open(pdf_path) as pdf:
        pages = []
        for page in pdf.pages:
            text = page.extract_text(layout=False)
            if text:
                pages.append(text)
    return "\n".join(pages)


def split_into_blocks(full_text: str) -> list[str]:
    """Split raw text into per-inmate blocks using the footer separator."""
    # Remove page headers / column headers
    text = HEADER_RE.sub("", full_text)
    text = COL_HEADER_RE.sub("", text)

    blocks = FOOTER_RE.split(text)
    return [b.strip() for b in blocks if b.strip()]


def parse_block(block: str) -> dict | None:
    """Parse a single inmate block into a dict. Returns None if unusable."""
    lines = [l.rstrip() for l in block.splitlines() if l.strip()]

    record = {
        "roster_id":        None,
        "full_name":        None,
        "last_name":        None,
        "first_name":       None,
        "middle_name":      None,
        "race":             None,
        "age":              None,
        "arresting_agency": None,
        "holding_agency":   None,
        "book_datetime":    None,
        "out_date":         None,
        "hold_type":        None,
        "next_court_date":  None,
        "bail_amount":      None,
        "charges":          [],
        "bonus":            [],
    }

    charges    = []
    bonus      = []
    agencies   = []
    dates_seen = []

    # ── Pass 1: roster ID and name ───────────────────────────────
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Skip label-only lines
        if re.match(r"^(NAME:|RACE:|AGE:|HELD FOR:)\s*$", line):
            i += 1
            continue

        # Roster ID
        if not record["roster_id"] and re.match(r"^\d{5,9}$", line):
            record["roster_id"] = line
            i += 1
            continue

        # Name
        if not record["full_name"]:
            m = NAME_RE.match(line)
            if m:
                record["last_name"]  = m.group(1).strip().title()
                rest = m.group(2).strip().split()
                record["first_name"] = rest[0].title() if rest else ""
                record["middle_name"]= " ".join(rest[1:]).title() if len(rest) > 1 else None
                record["full_name"]  = f"{record['last_name']}, {record['first_name']}"
                if record["middle_name"]:
                    record["full_name"] += f" {record['middle_name']}"
                i += 1
                continue

        # Race
        if not record["race"] and RACE_RE.match(line):
            record["race"] = line.strip()
            i += 1
            continue

        # Age — standalone integer
        if not record["age"] and re.match(r"^\d{1,3}$", line):
            record["age"] = int(line)
            i += 1
            continue

        # Booking datetime embedded in line
        bm = BOOKDT_RE.search(line)
        if bm and not record["book_datetime"]:
            record["book_datetime"] = parse_date(bm.group(1))
            # The rest of the line might be an agency
            remainder = BOOKDT_RE.sub("", line).strip()
            if remainder:
                agencies.append(remainder)
            i += 1
            continue

        # Charge line (statute pattern)
        if CHARGE_RE.match(line):
            charges.append(line.strip())
            i += 1
            continue

        # Hold type
        matched_hold = None
        for ht in HOLD_TYPES:
            if ht in line.upper():
                matched_hold = ht
                break
        if matched_hold:
            record["hold_type"] = matched_hold
            # Scan remaining part of line for court date / bail / out date
            remainder = line.upper().replace(matched_hold, "").strip()
            _parse_date_bail(remainder, record, dates_seen)
            i += 1
            continue

        # Standalone bail amount
        bm2 = BAIL_RE.search(line)
        if bm2 and not record["bail_amount"]:
            record["bail_amount"] = parse_money(bm2.group(0))
            i += 1
            continue

        # Agency lines — appear before booking datetime; collect them
        if line and not re.match(r"^[\d/: ]+$", line):
            agencies.append(line)

        i += 1

    # ── Agencies: first = arresting, second = holding ───────────
    # De-dup while preserving order
    seen_ag = []
    for ag in agencies:
        if ag not in seen_ag:
            seen_ag.append(ag)
    if seen_ag:
        record["arresting_agency"] = seen_ag[0] if len(seen_ag) >= 1 else None
        record["holding_agency"]   = seen_ag[1] if len(seen_ag) >= 2 else seen_ag[0]

    record["charges"] = charges

    if bonus:
        record["bonus"] = "; ".join(bonus)
    else:
        record["bonus"] = None

    if not record["roster_id"] or not record["full_name"]:
        return None

    return record


def _parse_date_bail(text: str, record: dict, dates_seen: list):
    """Extract trailing court date / out date / bail from a remainder string."""
    # Bail
    bm = BAIL_RE.search(text)
    if bm and not record["bail_amount"]:
        record["bail_amount"] = parse_money(bm.group(0))

    # Dates — first is court date, second might be out date
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

def extract_mugshots(pdf_path: str, records: list[dict]):
    """
    Extract embedded images from the PDF and match them to records by page order.
    Saves files as <db_id>_<roster_id>.jpg into PHOTO_DIR.
    Returns updated records with photo_filename set.
    """
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)

    # Collect all images from the PDF in page order
    images_by_page = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            page_imgs = page.images
            images_by_page.append((page_num, page_imgs))

    # Flatten to ordered list of image references
    ordered_images = []
    for page_num, page_imgs in images_by_page:
        # Sort images top-to-bottom on each page
        sorted_imgs = sorted(page_imgs, key=lambda img: img.get("top", 0))
        for img in sorted_imgs:
            ordered_images.append((page_num, img))

    if not ordered_images:
        print("  ⚠  No embedded images found in PDF — trying page-render fallback.")
        _extract_mugshots_render_fallback(pdf_path, records)
        return records

    for idx, record in enumerate(records):
        if idx < len(ordered_images):
            page_num, img_ref = ordered_images[idx]
            try:
                img_data = img_ref.get("stream")
                if img_data is None:
                    continue
                raw = img_data.get_data()
                # Try to open as image
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    tmp.write(raw)
                    tmp_path = tmp.name
                img = Image.open(tmp_path).convert("RGB")
                filename = f"{record['roster_id']}.jpg"
                out_path = PHOTO_DIR / filename
                img.save(out_path, "JPEG", quality=90)
                record["photo_filename"] = filename
                os.unlink(tmp_path)
                print(f"  📷  Saved photo: {filename}")
            except Exception as e:
                print(f"  ⚠  Could not save photo for {record['roster_id']}: {e}")
        else:
            print(f"  ℹ  No image available for record #{idx} ({record.get('roster_id')})")

    return records


def _extract_mugshots_render_fallback(pdf_path: str, records: list[dict]):
    """
    Fallback: render each page to an image, then crop the left mugshot region.
    The Carlton roster PDF places the mugshot in roughly the left 18% of each page.
    """
    try:
        pages = convert_from_path(pdf_path, dpi=150)
    except Exception as e:
        print(f"  ✗  Page render failed: {e}")
        return

    # Each page contains a variable number of records; we use text to figure out
    # how many are on each page then split accordingly.  For now, one crop per page.
    # Carlton's layout: ~2 records per page, mugshots stacked vertically.
    MUGSHOT_X_RATIO   = (0.0, 0.18)   # left 18% of page width
    MUGSHOT_Y_RATIOS  = [(0.0, 0.5), (0.5, 1.0)]  # top half / bottom half

    record_idx = 0
    for page_img in pages:
        w, h = page_img.size
        x0 = int(w * MUGSHOT_X_RATIO[0])
        x1 = int(w * MUGSHOT_X_RATIO[1])
        for y_ratio in MUGSHOT_Y_RATIOS:
            if record_idx >= len(records):
                break
            y0 = int(h * y_ratio[0])
            y1 = int(h * y_ratio[1])
            crop = page_img.crop((x0, y0, x1, y1))
            roster_id = records[record_idx].get("roster_id", f"unk_{record_idx}")
            filename = f"{roster_id}.jpg"
            out_path = PHOTO_DIR / filename
            crop.save(out_path, "JPEG", quality=85)
            records[record_idx]["photo_filename"] = filename
            print(f"  📷  Saved cropped photo: {filename}")
            record_idx += 1

    return records


# ─────────────────────────────────────────────
# Database Insert
# ─────────────────────────────────────────────

INSERT_SQL = """
INSERT INTO inmates
    (roster_id, full_name, last_name, first_name, middle_name,
     race, age, arresting_agency, holding_agency,
     book_datetime, out_date, hold_type, next_court_date,
     bail_amount, charges, photo_filename, bonus,
     source_file, record_hash)
VALUES
    (%(roster_id)s, %(full_name)s, %(last_name)s, %(first_name)s, %(middle_name)s,
     %(race)s, %(age)s, %(arresting_agency)s, %(holding_agency)s,
     %(book_datetime)s, %(out_date)s, %(hold_type)s, %(next_court_date)s,
     %(bail_amount)s, %(charges)s, %(photo_filename)s, %(bonus)s,
     %(source_file)s, %(record_hash)s)
ON DUPLICATE KEY UPDATE
    scraped_at = CURRENT_TIMESTAMP
"""


def save_records(records: list[dict], source_file: str):
    conn = get_connection()
    cur  = conn.cursor()
    inserted = skipped = 0

    for r in records:
        rh = record_hash(
            r.get("roster_id") or "",
            r.get("full_name") or "",
            str(r.get("book_datetime") or ""),
        )
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
            "source_file":      source_file,
            "record_hash":      rh,
        }
        try:
            cur.execute(INSERT_SQL, row)
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
        except mysql.connector.Error as e:
            print(f"  ✗  DB error for {r.get('roster_id')}: {e}")

    conn.commit()
    cur.close()
    conn.close()
    print(f"  ✅  Inserted {inserted} new records, {skipped} duplicates skipped.")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def fetch_pdf(url: str) -> str:
    """Download PDF to a temp file; return path."""
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
        if is_url:
            tmp_path = fetch_pdf(source)
            pdf_path = tmp_path
        else:
            pdf_path = source

        print(f"\n📄  Extracting text from: {pdf_path}")
        full_text = extract_text_from_pdf(pdf_path)
        blocks    = split_into_blocks(full_text)
        print(f"    Found {len(blocks)} potential record blocks.")

        records = []
        for blk in blocks:
            parsed = parse_block(blk)
            if parsed:
                records.append(parsed)
            else:
                print(f"  ⚠  Skipped unparseable block (first 80 chars): {blk[:80]!r}")

        print(f"\n🖼   Extracting mugshots ...")
        records = extract_mugshots(pdf_path, records)

        print(f"\n💾  Saving {len(records)} records to database ...")
        save_records(records, source)

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
