#!/usr/bin/env python3
"""
cleanup.py — Fix malformed records in the inmates table.

Fixes applied in order:
  1. Age landed in arresting_agency as "AGE : 35" — extract age, shift agencies up
  2. arresting_agency is a bare page number — shift agencies up
  3. Charges sitting in holding_agency — parse and move to charges column

Run with --dry-run first to preview changes without writing anything.

Usage:
    python scripts/cleanup.py --dry-run
    python scripts/cleanup.py
"""

import argparse
import json
import os
import re
import sys
from dotenv import load_dotenv
import mysql.connector

load_dotenv()

DB_CFG = dict(
    host     = os.getenv("DB_HOST", "localhost"),
    port     = int(os.getenv("DB_PORT", 3306)),
    database = os.getenv("DB_NAME", "jail_roster"),
    user     = os.getenv("DB_USER", "jailapp"),
    password = os.getenv("DB_PASS", ""),
)

# Matches "AGE : 35", "AGE: 35", "AGE :35" etc.
AGE_LABEL_RE = re.compile(r"^AGE\s*:\s*(\d{1,3})$", re.IGNORECASE)

# A bare page number / roster ID fragment — digits only, no letters
NUMERIC_RE = re.compile(r"^\d+$")

# Statute code pattern — same as scraper
STATUTE_RE = re.compile(r"^\d{2,3}[A-Z]?\.\d+", re.IGNORECASE)


def get_connection():
    return mysql.connector.connect(**DB_CFG)


# ─────────────────────────────────────────────
# Fix 1 & 2: Age / agency column misalignment
# ─────────────────────────────────────────────

def fix_age_and_agencies(cur, dry_run: bool) -> int:
    """
    Cases handled:
      A) arresting_agency = "AGE : 35"
         → age = 35, arresting_agency = holding_agency, holding_agency = NULL
         (holding_agency was the real arresting agency in this layout)

      B) arresting_agency = bare number (page number slipped in)
         → arresting_agency = holding_agency, holding_agency = NULL
    """
    cur.execute("""
        SELECT id, roster_id, full_name, age,
               arresting_agency, holding_agency
        FROM inmates
        WHERE arresting_agency IS NOT NULL
    """)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    fixed = 0

    for row in rows:
        rec = dict(zip(cols, row))
        arr = (rec["arresting_agency"] or "").strip()
        hld = (rec["holding_agency"]  or "").strip()

        new_age = rec["age"]
        new_arr = arr
        new_hld = hld

        # Case A: "AGE : 35" in arresting_agency
        age_match = AGE_LABEL_RE.match(arr)
        if age_match:
            new_age = int(age_match.group(1))
            new_arr = hld if hld else None
            new_hld = None

        # Case B: bare number in arresting_agency (page number / roster fragment)
        elif NUMERIC_RE.match(arr):
            new_arr = hld if hld else None
            new_hld = None

        # Nothing to fix
        if new_age == rec["age"] and new_arr == arr and new_hld == hld:
            continue

        fixed += 1
        print(f"  [{rec['id']}] {rec['full_name']}")
        if new_age != rec["age"]:
            print(f"        age:               {rec['age']!r} → {new_age!r}")
        if new_arr != arr:
            print(f"        arresting_agency:  {arr!r} → {new_arr!r}")
        if new_hld != hld:
            print(f"        holding_agency:    {hld!r} → {new_hld!r}")

        if not dry_run:
            cur.execute("""
                UPDATE inmates
                SET age = %s,
                    arresting_agency = %s,
                    holding_agency   = %s
                WHERE id = %s
            """, (new_age, new_arr, new_hld, rec["id"]))

    return fixed


# ─────────────────────────────────────────────
# Fix 3: Charges sitting in holding_agency
# ─────────────────────────────────────────────

def fix_charges_in_holding_agency(cur, dry_run: bool) -> int:
    """
    If holding_agency looks like a statute line (starts with a statute code),
    parse all statute-like content out of it, merge with existing charges,
    and clear holding_agency.

    Some records may have MULTIPLE charges crammed into holding_agency as
    newline- or semicolon-separated text — we split and handle all of them.
    """
    cur.execute("""
        SELECT id, roster_id, full_name,
               charges, holding_agency, arresting_agency
        FROM inmates
        WHERE holding_agency IS NOT NULL
    """)
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    fixed = 0

    for row in rows:
        rec = dict(zip(cols, row))
        hld = (rec["holding_agency"] or "").strip()

        if not hld:
            continue

        # Split on newlines or semicolons to handle multi-charge blobs
        candidate_lines = [l.strip() for l in re.split(r"[\n;]", hld) if l.strip()]

        charge_lines  = []
        agency_lines  = []

        for line in candidate_lines:
            if STATUTE_RE.match(line):
                charge_lines.append(line)
            else:
                agency_lines.append(line)

        # Nothing statute-like found — leave this record alone
        if not charge_lines:
            continue

        # Merge with existing charges (avoid duplicates)
        try:
            existing_charges = json.loads(rec["charges"]) if rec["charges"] else []
        except (json.JSONDecodeError, TypeError):
            existing_charges = []

        merged_charges = existing_charges[:]
        for c in charge_lines:
            if c not in merged_charges:
                merged_charges.append(c)

        new_hld = "; ".join(agency_lines) if agency_lines else None

        fixed += 1
        print(f"  [{rec['id']}] {rec['full_name']}")
        print(f"        charges (added):   {charge_lines}")
        print(f"        holding_agency:    {hld!r} → {new_hld!r}")

        if not dry_run:
            cur.execute("""
                UPDATE inmates
                SET charges         = %s,
                    holding_agency  = %s
                WHERE id = %s
            """, (json.dumps(merged_charges), new_hld, rec["id"]))

    return fixed


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Clean up malformed inmates records")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview changes without writing to the database"
    )
    args = parser.parse_args()

    if args.dry_run:
        print("🔍  DRY RUN — no changes will be written.\n")
    else:
        print("⚠️   LIVE RUN — changes will be committed to the database.\n")

    conn = get_connection()
    cur  = conn.cursor()

    print("=" * 60)
    print("Fix 1 & 2: Age / agency misalignment")
    print("=" * 60)
    age_fixed = fix_age_and_agencies(cur, args.dry_run)
    print(f"\n  → {age_fixed} records {'would be' if args.dry_run else ''} fixed.\n")

    print("=" * 60)
    print("Fix 3: Charges sitting in holding_agency")
    print("=" * 60)
    charge_fixed = fix_charges_in_holding_agency(cur, args.dry_run)
    print(f"\n  → {charge_fixed} records {'would be' if args.dry_run else ''} fixed.\n")

    if not args.dry_run:
        conn.commit()
        print("✅  All fixes committed.")
    else:
        print("🔍  Dry run complete — rerun without --dry-run to apply.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
