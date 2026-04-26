-- reset_db.sql
-- Run as MySQL root to wipe and recreate the jail_roster database.
--
-- Usage:
--   sudo mysql -u root -p < scripts/reset_db.sql
--
-- WARNING: This permanently deletes all data.

DROP DATABASE IF EXISTS jail_roster;
CREATE DATABASE jail_roster CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Re-grant privileges to the app user (idempotent)
CREATE USER IF NOT EXISTS 'jailapp'@'localhost' IDENTIFIED BY 'CHANGE_THIS_STRONG_PASSWORD';
GRANT ALL PRIVILEGES ON jail_roster.* TO 'jailapp'@'localhost';
FLUSH PRIVILEGES;

SELECT 'Database jail_roster recreated successfully.' AS status;
