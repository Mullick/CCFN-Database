# Carlton County Jail Roster System — Setup Guide

## 1. System Update & Core Dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv python3-dev \
    build-essential libssl-dev libffi-dev \
    nginx curl wget git unzip \
    libpoppler-cpp-dev pkg-config \
    poppler-utils
```

## 2. Install MySQL Server (Secure)

```bash
sudo apt install -y mysql-server

# Run the secure installation wizard
sudo mysql_secure_installation
# Recommended answers:
#   VALIDATE PASSWORD COMPONENT: Y, strength: 2 (strong)
#   Remove anonymous users: Y
#   Disallow root login remotely: Y
#   Remove test database: Y
#   Reload privilege tables: Y
```

## 3. Create the Database & Application User

```bash
sudo mysql -u root -p << 'EOF'
CREATE DATABASE jail_roster CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER 'jailapp'@'localhost' IDENTIFIED BY 'CHANGE_THIS_STRONG_PASSWORD';
GRANT ALL PRIVILEGES ON jail_roster.* TO 'jailapp'@'localhost';
FLUSH PRIVILEGES;
EOF
```

> **⚠️ Change `CHANGE_THIS_STRONG_PASSWORD` to something strong before running.**

## 4. Create Directory Structure

```bash
sudo mkdir -p /var/www/jailroster/photos
sudo mkdir -p /var/www/jailroster/reports
sudo mkdir -p /opt/jailroster

# Set ownership — www-data for nginx-served dirs, your user for app
sudo chown -R www-data:www-data /var/www/jailroster
sudo chmod -R 755 /var/www/jailroster

# App directory owned by your user
sudo chown -R $USER:$USER /opt/jailroster
```

## 5. Python Virtual Environment & Packages

```bash
cd /opt/jailroster
python3 -m venv venv
source venv/bin/activate

pip install \
    mysql-connector-python \
    pdfplumber \
    requests \
    Pillow \
    pdf2image \
    python-dotenv
```

## 6. Environment Config File

Create `/opt/jailroster/.env`:

```ini
DB_HOST=localhost
DB_PORT=3306
DB_NAME=jail_roster
DB_USER=jailapp
DB_PASS=CHANGE_THIS_STRONG_PASSWORD

PHOTO_DIR=/var/www/jailroster/photos
REPORTS_DIR=/var/www/jailroster/reports
```

## 7. Deploy the Application Scripts

Copy the provided scripts to `/opt/jailroster/`:
- `db_init.py`      — creates the database schema
- `scraper.py`      — scrapes PDF and populates the DB
- `run.sh`          — convenience wrapper

```bash
# Make scripts executable
chmod +x /opt/jailroster/run.sh
```

## 8. Initialize the Database

```bash
cd /opt/jailroster
source venv/bin/activate
python db_init.py
```

## 9. Configure Nginx

```bash
sudo cp /opt/jailroster/nginx-jailroster.conf /etc/nginx/sites-available/jailroster
sudo ln -s /etc/nginx/sites-available/jailroster /etc/nginx/sites-enabled/jailroster
sudo nginx -t
sudo systemctl reload nginx
```

## 10. Run the Scraper

```bash
cd /opt/jailroster
source venv/bin/activate

# From URL (live):
python scraper.py --url https://jailroster.co.carlton.mn.us/CCJ_Jail_Roster.pdf

# From a local PDF file:
python scraper.py --file /path/to/local/CCJ_Jail_Roster.pdf
```

## 11. Automate with Cron (Optional)

```bash
crontab -e
# Add — runs every 6 hours:
0 */6 * * * cd /opt/jailroster && ./run.sh >> /var/log/jailroster.log 2>&1
```

---

## Directory Summary

| Path | Purpose |
|------|---------|
| `/opt/jailroster/` | Application scripts & venv |
| `/var/www/jailroster/photos/` | Mugshot images (served by nginx) |
| `/var/www/jailroster/reports/` | Report files (served by nginx) |

## Access

- Photos: `http://YOUR_SERVER_IP/photos/`
- Reports: `http://YOUR_SERVER_IP/reports/`
