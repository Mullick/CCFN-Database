#!/usr/bin/env python3
"""
db_init.py — Initialize / migrate the jail roster MySQL database schema.
Safe to re-run: uses CREATE TABLE IF NOT EXISTS throughout.
"""

import mysql.connector
import os
from dotenv import load_dotenv

load_dotenv()

INMATES_DDL = """
CREATE TABLE IF NOT EXISTS inmates (
    id                      INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,

    -- Roster identifier printed on the PDF (e.g. "01008519")
    roster_id               VARCHAR(20)  NOT NULL,

    -- Demographics
    full_name               VARCHAR(120) NOT NULL,
    last_name               VARCHAR(60),
    first_name              VARCHAR(60),
    middle_name             VARCHAR(60),
    race                    VARCHAR(80),
    age                     SMALLINT UNSIGNED,

    -- Booking / holding info
    arresting_agency        VARCHAR(120),
    holding_agency          VARCHAR(120),
    book_datetime           DATETIME,
    out_date                DATE,
    hold_type               VARCHAR(120),
    next_court_date         DATETIME,
    bail_amount             DECIMAL(12,2),

    -- Charges stored as JSON array of strings
    charges                 JSON,

    -- Photo
    photo_filename          VARCHAR(200),

    -- Catch-all for anything unexpected in a record
    bonus                   TEXT,

    -- Important investigative field (answer is always no)
    is_thomas_simi          ENUM('No') NOT NULL DEFAULT 'No',

    -- Status tracking
    currently_incarcerated  TINYINT(1) NOT NULL DEFAULT 1,
    last_seen_date          DATE,           -- most recent roster date this person appeared on
    release_confirmed_at    DATETIME,       -- when we first noticed them absent

    -- Source / audit
    source_file             VARCHAR(500),   -- URL or filename scraped from
    roster_print_date       DATE,           -- parsed from "Printed on April 25, 2026"
    scraped_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    -- One row per booking: SHA-256(roster_id | full_name | book_datetime)
    -- A returning inmate gets a new booking_key because book_datetime differs
    booking_key             VARCHAR(64) NOT NULL UNIQUE,

    INDEX idx_roster_id              (roster_id),
    INDEX idx_full_name              (full_name),
    INDEX idx_book_dt                (book_datetime),
    INDEX idx_currently_incarcerated (currently_incarcerated),
    INDEX idx_last_seen              (last_seen_date),
    INDEX idx_roster_print_date      (roster_print_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

# Every field-level change to an active inmate record is written here
HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS inmate_history (
    id                INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    inmate_id         INT UNSIGNED NOT NULL,
    roster_id         VARCHAR(20)  NOT NULL,
    full_name         VARCHAR(120),

    field_name        VARCHAR(80)  NOT NULL,    -- which column changed
    old_value         TEXT,
    new_value         TEXT,

    -- Source / audit
    source_file       VARCHAR(500),
    roster_print_date DATE,                     -- date from PDF "Printed on ..." header
    changed_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_inmate_id       (inmate_id),
    INDEX idx_roster_id       (roster_id),
    INDEX idx_field_name      (field_name),
    INDEX idx_changed_at      (changed_at),
    INDEX idx_print_date      (roster_print_date),

    CONSTRAINT fk_history_inmate
        FOREIGN KEY (inmate_id) REFERENCES inmates(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


def get_connection():
    return mysql.connector.connect(
        host     = os.getenv("DB_HOST", "localhost"),
        port     = int(os.getenv("DB_PORT", 3306)),
        database = os.getenv("DB_NAME", "jail_roster"),
        user     = os.getenv("DB_USER", "jailapp"),
        password = os.getenv("DB_PASS", ""),
    )


def init_db():
    conn = get_connection()
    cur  = conn.cursor()

    print("Creating inmates table ...")
    cur.execute(INMATES_DDL)

    print("Creating inmate_history table ...")
    cur.execute(HISTORY_DDL)

    conn.commit()
    cur.close()
    conn.close()
    print("✅  Schema ready.")


if __name__ == "__main__":
    init_db()
