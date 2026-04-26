#!/usr/bin/env python3
"""
db_init.py — Initialize the jail roster MySQL database schema.
Run once before first scrape.
"""

import mysql.connector
import os
from dotenv import load_dotenv

load_dotenv()

DDL = """
CREATE TABLE IF NOT EXISTS inmates (
    id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,

    -- Roster identifier (printed on the PDF, e.g. "01008519")
    roster_id       VARCHAR(20)  NOT NULL,

    -- Demographics
    full_name       VARCHAR(120) NOT NULL,
    last_name       VARCHAR(60),
    first_name      VARCHAR(60),
    middle_name     VARCHAR(60),
    race            VARCHAR(80),
    age             SMALLINT UNSIGNED,

    -- Booking / holding info
    arresting_agency  VARCHAR(120),
    holding_agency    VARCHAR(120),
    book_datetime     DATETIME,
    out_date          DATE,
    hold_type         VARCHAR(120),   -- "BENCH WARRANT", "PROBABLE CAUSE", etc.
    next_court_date   DATETIME,
    bail_amount       DECIMAL(12,2),

    -- Charges (stored as JSON array of strings)
    charges           JSON,

    -- Photo
    photo_filename    VARCHAR(200),

    -- Catch-all for anything extra parsed from a record
    bonus             TEXT,

    -- Housekeeping
    source_file       VARCHAR(500),   -- URL or filename this record came from
    scraped_at        DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    record_hash       VARCHAR(64)     UNIQUE,  -- SHA-256 of key fields; prevents duplicates

    INDEX idx_roster_id  (roster_id),
    INDEX idx_full_name  (full_name),
    INDEX idx_book_dt    (book_datetime),
    INDEX idx_race       (race)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


def get_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 3306)),
        database=os.getenv("DB_NAME", "jail_roster"),
        user=os.getenv("DB_USER", "jailapp"),
        password=os.getenv("DB_PASS", ""),
    )


def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(DDL)
    conn.commit()
    cur.close()
    conn.close()
    print("✅  Schema ready.")


if __name__ == "__main__":
    init_db()
