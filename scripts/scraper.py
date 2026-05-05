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
REPORTS_DIR     = Path(os.getenv("REPORTS_DIR",     "/var/www/jailroster/reports"))
PDF_ARCHIVE_DIR = Path(os.getenv("PDF_ARCHIVE_DIR", "/home/mullick/roster_archive"))

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

# ─────────────────────────────────────────────
# PDF Text Parsing — Regexes
# ─────────────────────────────────────────────

FOOTER_RE     = re.compile(r"\*\*\*Money can be deposited.*?JailATM\.com\*\*\*", re.IGNORECASE)
HEADER_RE     = re.compile(r"Page \d+ of \d+\s+Carlton Jail Website.*?\n", re.IGNORECASE)
COL_HEADER_RE = re.compile(r"Mugshot\s+Demographics\s+Arresting Agency\s+Charges.*", re.IGNORECASE)
PRINT_DATE_RE = re.compile(r"Printed on\s+(\w+ \d{1,2},\s*\d{4})", re.IGNORECASE)

# Name: "LAST, FIRST MIDDLE" all caps
NAME_RE = re.compile(r"^([A-Z][A-Z\-']+),\s+([A-Z][A-Z\s\-'\.]+)$")

# Age: "A G E : 21" — spaced letters, any amount of whitespace
AGE_RE = re.compile(r"A\s+G\s+E\s*:\s*(\d{1,3})", re.IGNORECASE)

# Datetime with time: "04/28/26 13:30"
DATETIME_RE = re.compile(r"\b(\d{2}/\d{2}/\d{2,4}\s+\d{2}:\d{2})\b")

# Date only (court date / out date): "08/18/26"
DATE_ONLY_RE = re.compile(r"\b(\d{2}/\d{2}/\d{2,4})\b")

# Bail: "$2,500.00" or "$0.00"
BAIL_RE = re.compile(r"\$[\d,]+\.?\d*")

# Statute: "152.025.2(a)(1)" — starts a charge line
# Matches at the start of the token, handles letters/parens/dots
STATUTE_RE = re.compile(r"\b\d{2,3}[A-Z]?\.\d+[\d\.A-Za-z\(\)]*\s*-\s*", re.IGNORECASE)

# Race values — longest first to prevent partial matches
RACE_VALUES = [
    "American Indian or Alaska Native",
    "Black or African American",
    "Pacific Islander",
    "Hispanic",
    "Unknown",
    "Asian",
    "White",
    "Other",
]
RACE_RE = re.compile(
    r"(" + "|".join(re.escape(r) for r in RACE_VALUES) + r")",
    re.IGNORECASE
)

HOLD_TYPES = [
    "BENCH WARRANT",
    "PROBABLE CAUSE",
    "SUPERVISION VIOLATION",
    "UNDER SENTENCE",
    "HOLD FOR ANOTHER AGENCY",
    "SENTENCED",
    "AWAITING TRIAL",
    "PRETRIAL",
]

# Labels to always skip
LABEL_RE = re.compile(r"^(NAME:|RACE:|AGE:|HELD FOR:)\s*$", re.IGNORECASE)


# ─────────────────────────────────────────────
# PDF Text Helpers
# ─────────────────────────────────────────────

def extract_roster_print_date(full_text: str):
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


# ─────────────────────────────────────────────
# Block Parser
# ─────────────────────────────────────────────

def parse_block(block: str) -> dict | None:
    """
    Parse one inmate block.

    Confirmed PDF structure (from diagnostic):
        ROSTER_ID
        A G E : NN
        AGENCY  HOLD_TYPE  COURT_DATE  [OUT_DATE]  [$BAIL]   <- one line
        RACE:                                                  <- label, skip
        DATE TIME  STATUTE - charge description               <- booking date prefix on charge
        [charge continuation lines]
        RACE_VALUE  (may be split across lines)
        HELD FOR:   (may be inline with race)
        HOLDING AGENCY
        NAME:
        LAST, FIRST MIDDLE
    """

    # ── Clean noise lines ─────────────────────────────────────────────────────
    raw_lines = []
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^(Carlton Jail Website|Page \d+ of \d+|Mugshot|Demographics)", line, re.IGNORECASE):
            continue
        raw_lines.append(line)

    if not raw_lines:
        return None

    blob = " ".join(raw_lines)

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

    # ── 1. ROSTER ID ──────────────────────────────────────────────────────────
    for line in raw_lines:
        if re.match(r"^\d{5,9}$", line):
            record["roster_id"] = line
            break

    # ── 2. NAME — last LAST, FIRST line in block ──────────────────────────────
    for line in reversed(raw_lines):
        m = NAME_RE.match(line)
        if m:
            record["last_name"]   = m.group(1).strip().title()
            rest = m.group(2).strip().split()
            record["first_name"]  = rest[0].title() if rest else ""
            record["middle_name"] = " ".join(rest[1:]).title() if len(rest) > 1 else None
            record["full_name"]   = f"{record['last_name']}, {record['first_name']}"
            if record["middle_name"]:
                record["full_name"] += f" {record['middle_name']}"
            break

    if not record["roster_id"] or not record["full_name"]:
        return None

    # ── 3. AGE ────────────────────────────────────────────────────────────────
    am = AGE_RE.search(blob)
    if am:
        record["age"] = int(am.group(1))

    # ── 4. RACE — search blob to catch split lines ────────────────────────────
    rm = RACE_RE.search(blob)
    if rm:
        record["race"] = rm.group(1).strip()

    # ── 5. HOLD TYPE LINE — "AGENCY  HOLD_TYPE  DATES  BAIL" ─────────────────
    # Agency = text before hold type keyword on that line
    # Court/out dates and bail = text after hold type keyword
    for line in raw_lines:
        matched_ht = None
        for ht in HOLD_TYPES:
            if ht in line.upper():
                matched_ht = ht
                break
        if not matched_ht:
            continue

        record["hold_type"] = matched_ht
        idx         = line.upper().find(matched_ht)
        agency_part = line[:idx].strip()
        after_part  = line[idx + len(matched_ht):].strip()

        if agency_part:
            record["arresting_agency"] = agency_part

        # Bail
        bm = BAIL_RE.search(after_part)
        if bm:
            record["bail_amount"] = parse_money(bm.group(0))
            after_part = BAIL_RE.sub("", after_part)

        # Court date / out date
        date_matches  = DATE_ONLY_RE.findall(after_part)
        dates_parsed  = [parse_date(d) for d in date_matches if parse_date(d)]
        if dates_parsed:
            record["next_court_date"] = dates_parsed[0]
        if len(dates_parsed) >= 2:
            record["out_date"] = dates_parsed[1].date()
        break

    # ── 6. HOLDING AGENCY — first AGENCY? line after HELD FOR: ───────────────
    held_for_idx = None
    for i, line in enumerate(raw_lines):
        if "HELD FOR:" in line.upper():
            held_for_idx = i
            break

    if held_for_idx is not None:
        for line in raw_lines[held_for_idx + 1:]:
            if LABEL_RE.match(line):
                continue
            if NAME_RE.match(line):
                break
            if RACE_RE.search(line):
                continue
            if AGE_RE.search(line):
                continue
            if STATUTE_RE.search(line):
                continue
            if DATETIME_RE.match(line):
                continue
            if re.match(r"^[\d/:.\s]+$", line):
                continue
            record["holding_agency"] = line
            break

    # ── 7. CHARGES — between RACE: label and HELD FOR: ───────────────────────
    # Charge lines appear after the RACE: label and before HELD FOR:.
    # First charge line often has "DATE TIME  STATUTE - desc" prefix.
    # Continuation lines (wrapped description) follow immediately after.
    race_label_idx = None
    for i, line in enumerate(raw_lines):
        if re.match(r"^RACE:\s*$", line, re.IGNORECASE):
            race_label_idx = i
            break

    if race_label_idx is None:
        # No RACE: label found — use hold type line as upper boundary
        for i, line in enumerate(raw_lines):
            if record["hold_type"] and record["hold_type"] in line.upper():
                race_label_idx = i
                break

    end_idx = held_for_idx if held_for_idx is not None else len(raw_lines)

    charges = []
    current_charge = None

    if race_label_idx is not None:
        for line in raw_lines[race_label_idx + 1: end_idx]:
            # Skip race value lines
            if RACE_RE.match(line):
                continue
            # Skip pure label lines
            if LABEL_RE.match(line):
                continue

            # Strip leading datetime prefix and capture booking date
            dt_m = re.match(r"^(\d{2}/\d{2}/\d{2,4}\s+\d{2}:\d{2})\s+", line)
            if dt_m:
                if not record["book_datetime"]:
                    record["book_datetime"] = parse_date(dt_m.group(1))
                line = line[dt_m.end():]

            # Does this line start a new charge?
            if STATUTE_RE.match(line):
                if current_charge:
                    charges.append(current_charge)
                current_charge = line
            elif re.match(r"^[A-Z]\d{3,}", line):
                # Non-standard code like X1140
                if current_charge:
                    charges.append(current_charge)
                current_charge = line
            elif current_charge is not None:
                # Continuation of previous charge description
                current_charge += " " + line
            elif line:
                current_charge = line

    if current_charge:
        charges.append(current_charge)

    record["charges"] = charges
    return record



# ─────────────────────────────────────────────
# Image Extraction
# ─────────────────────────────────────────────

def _try_decode_image(raw: bytes, img_ref: dict) -> Image.Image | None:
    """
    Try multiple strategies to decode raw PDF image bytes into a PIL Image.
    PDF images can be JPEG, PNG, JBIG2, CCITT Group 4, or indexed color —
    the raw stream bytes are not always a self-contained file.
    """
    import io

    # Strategy 1: raw bytes are already a valid image file (most common — JPEG)
    try:
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        pass

    # Strategy 2: write to temp file and let Pillow sniff the format
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        img = Image.open(tmp_path).convert("RGB")
        os.unlink(tmp_path)
        return img
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    # Strategy 3: CCITT Group 3/4 fax encoding — common in older PDFs
    # Reconstruct a valid TIFF wrapper so Pillow can decode it
    try:
        width  = int(img_ref.get("width",  img_ref.get("Width",  0)))
        height = int(img_ref.get("height", img_ref.get("Height", 0)))
        if width and height:
            import struct
            # Minimal TIFF header for CCITT Group 4
            def _tiff_header(w, h, data_len):
                # Little-endian TIFF with CCITT T6 (Group 4) compression
                ifd = [
                    (256, 3, 1, w),           # ImageWidth
                    (257, 3, 1, h),           # ImageLength
                    (258, 3, 1, 1),           # BitsPerSample = 1
                    (259, 3, 1, 4),           # Compression = CCITT Group 4
                    (262, 3, 1, 0),           # PhotometricInterpretation = WhiteIsZero
                    (278, 3, 1, h),           # RowsPerStrip
                    (279, 4, 1, data_len),    # StripByteCounts
                    (284, 3, 1, 1),           # PlanarConfiguration
                ]
                offset = 8 + 2 + len(ifd) * 12 + 4
                ifd.insert(6, (273, 4, 1, offset))  # StripOffsets
                ifd.sort(key=lambda x: x[0])
                hdr  = b"II" + struct.pack("<HI", 42, 8)
                hdr += struct.pack("<H", len(ifd))
                for tag, typ, cnt, val in ifd:
                    hdr += struct.pack("<HHII", tag, typ, cnt, val)
                hdr += struct.pack("<I", 0)
                return hdr

            import io
            hdr  = _tiff_header(width, height, len(raw))
            tiff = io.BytesIO(hdr + raw)
            return Image.open(tiff).convert("RGB")
    except Exception:
        pass

    # Strategy 4: treat as raw bitmap (last resort)
    try:
        width  = int(img_ref.get("width",  img_ref.get("Width",  0)))
        height = int(img_ref.get("height", img_ref.get("Height", 0)))
        mode   = img_ref.get("colorspace", "RGB")
        if isinstance(mode, list):
            mode = "RGB"
        if width and height:
            return Image.frombytes("RGB", (width, height), raw).convert("RGB")
    except Exception:
        pass

    return None


def _render_page_crop(pdf_path: str, page_num: int, img_ref: dict) -> Image.Image | None:
    """
    Render the specific page at high DPI and crop just the mugshot region
    defined by the image reference bounding box. Used as a last resort when
    all byte-decode strategies fail.
    """
    try:
        pages = convert_from_path(
            pdf_path, dpi=200,
            first_page=page_num + 1,
            last_page=page_num + 1,
        )
        if not pages:
            return None
        page_img = pages[0]
        pw, ph = page_img.size

        # img_ref coordinates are in PDF points (72 per inch), page size also in points
        # We need to scale to the rendered pixel dimensions
        with pdfplumber.open(pdf_path) as pdf:
            page = pdf.pages[page_num]
            pdf_w = float(page.width)
            pdf_h = float(page.height)

        scale_x = pw / pdf_w
        scale_y = ph / pdf_h

        x0 = int(float(img_ref.get("x0", 0))   * scale_x)
        y0 = int(float(img_ref.get("top", 0))   * scale_y)
        x1 = int(float(img_ref.get("x1", pw))   * scale_x)
        y1 = int(float(img_ref.get("bottom", ph)) * scale_y)

        # Clamp to page bounds
        x0, x1 = max(0, x0), min(pw, x1)
        y0, y1 = max(0, y0), min(ph, y1)

        if x1 > x0 and y1 > y0:
            return page_img.crop((x0, y0, x1, y1)).convert("RGB")
    except Exception as e:
        print(f"    ⚠  Page render crop failed: {e}")
    return None


def extract_mugshots(pdf_path: str, records: list) -> list:
    """
    Extract mugshots by finding each inmate's roster ID text on the page,
    then cropping the image region to its left.

    This is position-based — immune to index ordering mismatches between
    images and text blocks, which caused wrong photos being assigned.

    The Carlton County PDF layout places the mugshot in the leftmost ~18%
    of the page, vertically aligned with the inmate's record block.
    """
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)

    # Build a lookup: roster_id → record
    record_map = {r["roster_id"]: r for r in records if r.get("roster_id")}

    # Render all pages at 200 DPI
    try:
        pages_rendered = convert_from_path(pdf_path, dpi=200)
    except Exception as e:
        print(f"  ✗  Page render failed: {e}")
        return records

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            if page_num >= len(pages_rendered):
                break

            page_img = pages_rendered[page_num]
            pw, ph   = page_img.size
            pdf_w    = float(page.width)
            pdf_h    = float(page.height)
            scale_x  = pw / pdf_w
            scale_y  = ph / pdf_h

            # Find all roster ID text positions on this page
            # pdfplumber words give us x0, top, x1, bottom in PDF points
            words = page.extract_words()

            # Group words by their vertical position to find roster IDs
            # A roster ID is a standalone 5-9 digit number
            roster_positions = []
            for word in words:
                text = word["text"].strip()
                if re.match(r"^\d{5,9}$", text) and text in record_map:
                    roster_positions.append({
                        "roster_id": text,
                        "top":       float(word["top"]),
                        "bottom":    float(word["bottom"]),
                    })

            if not roster_positions:
                continue

            # Sort by vertical position top to bottom
            roster_positions.sort(key=lambda x: x["top"])

            # For each roster ID, determine the vertical crop region:
            # from its top to the next roster ID's top (or page bottom)
            for i, rp in enumerate(roster_positions):
                roster_id = rp["roster_id"]
                y_top_pdf = rp["top"]

                if i + 1 < len(roster_positions):
                    y_bot_pdf = roster_positions[i + 1]["top"]
                else:
                    y_bot_pdf = pdf_h

                # Mugshot is in the left ~18% of the page
                x0 = 0
                x1 = int(pw * 0.18)
                y0 = max(0, int(y_top_pdf * scale_y) - 10)  # small buffer
                y1 = min(ph, int(y_bot_pdf * scale_y))

                if x1 <= x0 or y1 <= y0:
                    continue

                crop = page_img.crop((x0, y0, x1, y1)).convert("RGB")

                # Skip if the crop is mostly blank (no photo for this record)
                import numpy as np
                arr = np.array(crop)
                if arr.mean() > 245:  # nearly all white — no photo
                    print(f"  ℹ  No photo region found for {roster_id}")
                    continue

                filename = f"{roster_id}.jpg"
                crop.save(PHOTO_DIR / filename, "JPEG", quality=90)
                record_map[roster_id]["photo_filename"] = filename
                print(f"  📷  Saved photo: {filename} (position-based)")

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
# PDF Archiving
# ─────────────────────────────────────────────

def archive_pdf(pdf_path: str, roster_print_date, reason: str = ""):
    """
    Copy the source PDF to PDF_ARCHIVE_DIR with a timestamped filename.
    Called only when changes are detected (or on first-ever run).

    Filename format: CCJ_Roster_<print_date>_scraped_<timestamp>[_<reason>].pdf
    Example:         CCJ_Roster_2026-04-30_scraped_2026-04-30_18-45-00_new_records.pdf
    """
    try:
        PDF_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

        print_str   = roster_print_date.strftime("%Y-%m-%d") if roster_print_date else "unknown"
        scrape_str  = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
        reason_slug = f"_{reason.replace(' ', '_')}" if reason else ""
        filename    = f"CCJ_Roster_{print_str}_scraped_{scrape_str}{reason_slug}.pdf"
        dest        = PDF_ARCHIVE_DIR / filename

        import shutil
        shutil.copy2(pdf_path, dest)
        # Ensure mullick owns the file (safe to call even if already correct)
        os.chmod(dest, 0o644)
        print(f"  📁  Archived PDF → {dest}")
        return str(dest)

    except Exception as e:
        print(f"  ⚠  Could not archive PDF: {e}")
        return None


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

    return {
        "inserted": inserted,
        "updated":  updated,
        "released": released,
        "flagged":  flagged,
        "changes":  total_changes,
    }


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
        stats = save_records(records, source, roster_print_date)

        # Archive the PDF if anything changed or this is the first run
        something_changed = (
            stats["inserted"] > 0
            or stats["updated"] > 0
            or stats["released"] > 0
        )
        if something_changed:
            parts = []
            if stats["inserted"] > 0:
                parts.append(f"{stats['inserted']}_new")
            if stats["updated"] > 0:
                parts.append(f"{stats['updated']}_updated")
            if stats["released"] > 0:
                parts.append(f"{stats['released']}_released")
            reason = "_".join(parts)
            archive_pdf(pdf_path, roster_print_date, reason)
        else:
            print("  ℹ  No changes detected — PDF not archived.")

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
