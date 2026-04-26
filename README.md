# Carlton County Jail Roster Scraper

Scrapes the Carlton County, MN jail roster PDF, stores structured inmate records in MySQL, extracts mugshot photos, and serves them via Nginx.

## Features

- Parses all fields from the roster PDF: name, race, age, agencies, booking date, hold type, court date, bail amount, charges
- Extracts and saves mugshot photos named by roster ID
- `bonus` field captures any unexpected data found in a record
- Duplicate-safe: re-scraping the same PDF won't create duplicate records
- Accepts a **live URL** or a **local historical PDF file** as input
- Nginx serves photos and reports over HTTP

## Quick Start

1. Follow `docs/SETUP.md` for full server setup (MySQL, Python, Nginx)
2. Copy `.env.example` to `.env` and set your database password
3. Initialize the database schema:
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
jailroster/
├── scripts/
│   ├── scraper.py          # Main PDF scraper
│   ├── db_init.py          # Database schema setup
│   └── run.sh              # Cron-friendly wrapper
├── config/
│   └── nginx-jailroster.conf
├── docs/
│   └── SETUP.md            # Full server setup guide
├── .env.example            # Environment variable template
├── .gitignore
├── requirements.txt
└── README.md
```

## Database Schema

| Column | Description |
|--------|-------------|
| `id` | Auto-increment primary key |
| `roster_id` | ID printed on the PDF roster |
| `full_name` / `last_name` / `first_name` / `middle_name` | Parsed name fields |
| `race` | Race as listed in the PDF |
| `age` | Age at time of booking |
| `arresting_agency` | Agency that made the arrest |
| `holding_agency` | Agency currently holding the inmate |
| `book_datetime` | Booking date and time |
| `out_date` | Release date (if listed) |
| `hold_type` | e.g. BENCH WARRANT, PROBABLE CAUSE |
| `next_court_date` | Next scheduled court appearance |
| `bail_amount` | Bail in dollars |
| `charges` | JSON array of charge strings |
| `photo_filename` | Filename of saved mugshot |
| `bonus` | Any unexpected/extra data found in the record |
| `source_file` | URL or filename the record was scraped from |
| `scraped_at` | Timestamp of when the record was inserted |
| `record_hash` | SHA-256 deduplication key |

## Automated Scraping

Add to crontab to run every 6 hours:
```
0 */6 * * * cd /opt/jailroster && ./scripts/run.sh >> /var/log/jailroster.log 2>&1
```

## Web Access

After Nginx setup, photos and reports are available at:
- `http://YOUR_SERVER_IP/photos/`
- `http://YOUR_SERVER_IP/reports/`

## Requirements

- Ubuntu 24.04
- Python 3.12+
- MySQL 8.0+
- Nginx
- `poppler-utils` system package (for pdf2image)
