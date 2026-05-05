#!/usr/bin/env python3
"""
diagnose.py — Dump raw text blocks and tag each line with what the parser
would identify it as.

Usage:
    python scripts/diagnose.py --file "/path/to/CCJ_Jail_Roster.pdf"
    python scripts/diagnose.py --file "/path/to/CCJ_Jail_Roster.pdf" --records 5
"""

import argparse
import re
import pdfplumber

FOOTER_RE     = re.compile(r"\*\*\*Money can be deposited.*?JailATM\.com\*\*\*", re.IGNORECASE)
HEADER_RE     = re.compile(r"Page \d+ of \d+\s+Carlton Jail Website.*?\n", re.IGNORECASE)
COL_HEADER_RE = re.compile(r"Mugshot\s+Demographics\s+Arresting Agency\s+Charges.*", re.IGNORECASE)

NAME_RE     = re.compile(r"^([A-Z][A-Z\-']+),\s+([A-Z][A-Z\s\-'\.]+)$")
AGE_RE      = re.compile(r"A\s+G\s+E\s*:\s*(\d{1,3})", re.IGNORECASE)
DATETIME_RE = re.compile(r"^\d{2}/\d{2}/\d{2,4}\s+\d{2}:\d{2}$")
BAIL_RE     = re.compile(r"\$[\d,]+\.?\d*")
LABEL_RE    = re.compile(r"^(NAME:|RACE:|AGE:|HELD FOR:)\s*$", re.IGNORECASE)
STATUTE_RE  = re.compile(r"\b\d{2,3}[A-Za-z]?\.\d+|[A-Z]\d{3,}", re.IGNORECASE)

RACE_VALUES = [
    "American Indian or Alaska Native", "Black or African American",
    "Pacific Islander", "Hispanic", "Unknown", "Asian", "White", "Other",
]
RACE_RE = re.compile(r"(" + "|".join(re.escape(r) for r in RACE_VALUES) + r")", re.IGNORECASE)

HOLD_TYPES = [
    "BENCH WARRANT", "PROBABLE CAUSE", "SUPERVISION VIOLATION",
    "UNDER SENTENCE", "HOLD FOR ANOTHER AGENCY", "SENTENCED",
    "AWAITING TRIAL", "PRETRIAL",
]


def tag_line(line: str) -> str:
    if not line.strip():
        return "EMPTY"
    if re.match(r"^\d{5,9}$", line):
        return "ROSTER_ID"
    if NAME_RE.match(line):
        return "NAME"
    if LABEL_RE.match(line):
        return "LABEL"
    if AGE_RE.search(line):
        return "AGE"
    if RACE_RE.search(line):
        return "RACE"
    if DATETIME_RE.match(line):
        return "BOOK_DATETIME"
    if any(ht in line.upper() for ht in HOLD_TYPES):
        return "HOLD_TYPE_LINE"
    if BAIL_RE.search(line) and not STATUTE_RE.search(line):
        return "BAIL"
    if STATUTE_RE.search(line):
        return "CHARGE"
    if re.match(r"^[\d/:.\s]+$", line):
        return "DATE/NUM"
    if re.match(r"^(Carlton Jail Website|Page \d+|Mugshot|Demographics)", line, re.IGNORECASE):
        return "NOISE"
    return "AGENCY?"


def extract_text(pdf_path: str) -> str:
    with pdfplumber.open(pdf_path) as pdf:
        pages = []
        for page in pdf.pages:
            text = page.extract_text(layout=False)
            if text:
                pages.append(text)
    return "\n".join(pages)


def split_blocks(full_text: str) -> list:
    text = HEADER_RE.sub("", full_text)
    text = COL_HEADER_RE.sub("", text)
    blocks = FOOTER_RE.split(text)
    return [b.strip() for b in blocks if b.strip()]


def main():
    parser = argparse.ArgumentParser(description="Diagnose raw PDF text extraction")
    parser.add_argument("--file", required=True, help="Path to roster PDF")
    parser.add_argument("--records", type=int, default=3, help="Number of records to show (default 3)")
    args = parser.parse_args()

    print(f"Reading: {args.file}\n")
    full_text = extract_text(args.file)
    blocks    = split_blocks(full_text)

    print(f"Total blocks found: {len(blocks)}\n")
    print("=" * 75)

    for i, block in enumerate(blocks[:args.records]):
        print(f"\n{'=' * 75}")
        print(f"BLOCK {i + 1}")
        print(f"{'=' * 75}")
        for j, line in enumerate(block.splitlines()):
            stripped = line.strip()
            tag = tag_line(stripped) if stripped else "EMPTY"
            print(f"  [{j:02d}] {tag:18s}  {line!r}")

    print(f"\n{'=' * 75}")
    print("Done.")


if __name__ == "__main__":
    main()
