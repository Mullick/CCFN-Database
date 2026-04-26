# CCFN Database — Carlton County Jail Roster Tracker

Scrapes the Carlton County, MN jail roster PDF, stores structured inmate records in MySQL with full change history, extracts mugshot photos, and serves them via Nginx.

## Features

- **Per-booking records** — each incarceration is a separate row; returning inmates get a new entry
- **Change tracking** — every field change on an active record is logged to `inmate_history` with the roster print date as the source timestamp
- **Currently incarcerated flag** — automatically set/cleared based on roster presence with a 24-hour grace period for clerical errors
- **Roster print date** — extracted from the PDF header ("Printed on April 25, 2026") and stored on every record and history row
- **Duplicate-safe** — re-scraping the same PDF is idempotent
- **Flexible input** — accepts a live URL or a local historical PDF file

## Quick Start

1. Follow `docs/SETUP.md` for full server setup (MySQL, Python, Nginx)
2. Copy `.env.example` to `.env` and set your database password
3. Initialize the schema:
   ```bash
   source venv/bin/activate
   python scripts/db_init.py
   ```
4. Run the scraper:
   ```bash
   # Live URL
   python scripts/scraper.py --url https://jailroster.co.carlton.mn.us/CCJ_Jail_Roster.pdf

   # Local historical file
   python scripts/scraper.py --file /path/to/CCJ_Jail_Roster.pdf
   ```

## Project Structure

```
CCFN-Database/
├── scripts/
│   ├── scraper.py          # Main PDF scraper + DB logic
│   ├── db_init.py          # Schema creation
│   └── run.sh              # Cron-friendly wrapper
├── config/
│   └── nginx-jailroster.conf
├── docs/
│   └── SETUP.md
├── .env.example
├── .gitignore
├── requirements.txt
└── README.md
```

## Database Tables

### `inmates`
One row per booking. Key columns:

| Column | Description |
|--------|-------------|
| `id` | Auto-increment PK |
| `roster_id` | ID from the PDF |
| `full_name` / `last_name` / `first_name` / `middle_name` | Parsed name |
| `race`, `age` | Demographics |
| `arresting_agency`, `holding_agency` | Agency info |
| `book_datetime` | Booking date/time |
| `out_date` | Release date if listed |
| `hold_type` | BENCH WARRANT, PROBABLE CAUSE, etc. |
| `next_court_date` | Next court appearance |
| `bail_amount` | Bail in dollars |
| `charges` | JSON array of charge strings |
| `photo_filename` | Saved mugshot filename |
| `bonus` | Extra/unexpected data |
| `currently_incarcerated` | 1 = active, 0 = released |
| `last_seen_date` | Most recent roster date they appeared on |
| `release_confirmed_at` | When absence was first detected |
| `roster_print_date` | Date from PDF "Printed on ..." header |
| `source_file` | URL or filename scraped from |
| `booking_key` | SHA-256 dedup key (roster_id + name + book_datetime) |

### `inmate_history`
One row per field change on an active booking:

| Column | Description |
|--------|-------------|
| `inmate_id` | FK → inmates.id |
| `field_name` | Which column changed |
| `old_value` | Previous value |
| `new_value` | New value |
| `roster_print_date` | Source roster date for this change |
| `changed_at` | Timestamp of detection |

## Useful Queries

```sql
-- All currently incarcerated inmates
SELECT roster_id, full_name, hold_type, book_datetime
FROM inmates WHERE currently_incarcerated = 1
ORDER BY book_datetime DESC;

-- Full change history for one inmate
SELECT h.changed_at, h.field_name, h.old_value, h.new_value, h.roster_print_date
FROM inmate_history h
JOIN inmates i ON h.inmate_id = i.id
WHERE i.roster_id = '01008519'
ORDER BY h.changed_at;

-- All bookings for a person (multiple incarcerations)
SELECT id, book_datetime, out_date, hold_type, currently_incarcerated
FROM inmates WHERE full_name = 'Doe, Jane'
ORDER BY book_datetime;
```

## Automation

```
0 */6 * * * cd /opt/jailroster && ./scripts/run.sh >> /var/log/jailroster.log 2>&1
```

## Web Access

- `http://YOUR_SERVER_IP/photos/`
- `http://YOUR_SERVER_IP/reports/`
