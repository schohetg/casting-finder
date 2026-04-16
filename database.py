"""
database.py - SQLite database operations for Casting Finder
"""
import sqlite3
import json
import os
from datetime import datetime

DB_PATH = os.environ.get('DB_PATH', 'castings.db')


def get_db():
    # timeout=30 → wait up to 30 s if another process holds a write lock
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass  # WAL already set, or read-only mount — not fatal
    return conn


def init_db():
    import time
    for attempt in range(5):
        try:
            with get_db() as conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS castings (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        description TEXT,
                        source_url TEXT UNIQUE NOT NULL,
                        source_site TEXT,
                        age_min INTEGER,
                        age_max INTEGER,
                        gender TEXT DEFAULT 'any',
                        country TEXT DEFAULT 'CH',
                        deadline TEXT,
                        pdf_urls TEXT DEFAULT '[]',
                        pdf_content TEXT,
                        status TEXT DEFAULT 'new',
                        found_date TEXT,
                        raw_content TEXT,
                        created_at TEXT DEFAULT (datetime('now')),
                        updated_at TEXT DEFAULT (datetime('now'))
                    );

                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    );

                    CREATE TABLE IF NOT EXISTS scan_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        started_at TEXT,
                        finished_at TEXT,
                        found INTEGER DEFAULT 0,
                        new_count INTEGER DEFAULT 0,
                        filtered INTEGER DEFAULT 0,
                        errors TEXT DEFAULT '[]',
                        status TEXT DEFAULT 'running'
                    );
                """)
                # Insert default settings if not exist
                defaults = {
                    'actor_name': 'Noam',
                    'actor_age': '14',
                    'actor_languages': '["German", "English", "Japanese", "Swiss German"]',
                    'enabled_countries': '["CH"]',
                    'enabled_sites': '["filmkidsplus.ch", "studentfilm.ch", "ronorp.net", "encast.pro", "swisscasting.ch", "streetcasting.ch", "451.ch", "casting-network.de", "castforward.de", "castingcallpro.com", "mandy.com"]',
                    'de_ecast_only': 'true',
                    'uk_ecast_only': 'true',
                    'scan_hour': '8',
                    'scan_minute': '0',
                }
                for key, value in defaults.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                        (key, value)
                    )
                conn.commit()
            return  # success — exit retry loop
        except sqlite3.OperationalError as e:
            if 'locked' in str(e) and attempt < 4:
                time.sleep(1 + attempt)  # back off and retry
            else:
                raise


def get_settings():
    with get_db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        settings = {row['key']: row['value'] for row in rows}
    # Parse JSON values
    for key in ['actor_languages', 'enabled_countries', 'enabled_sites']:
        if key in settings:
            try:
                settings[key] = json.loads(settings[key])
            except:
                pass
    settings['actor_age'] = int(settings.get('actor_age', 14))
    return settings


def update_settings(updates: dict):
    with get_db() as conn:
        for key, value in updates.items():
            if isinstance(value, (list, dict)):
                value = json.dumps(value)
            elif isinstance(value, bool):
                value = 'true' if value else 'false'
            else:
                value = str(value)
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )
        conn.commit()


def upsert_casting(casting: dict) -> bool:
    """Insert or update a casting. Returns True if it's new."""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id, status FROM castings WHERE id = ?", (casting['id'],)
        ).fetchone()

        if existing:
            # Update content but preserve user status
            conn.execute("""
                UPDATE castings SET
                    title = ?, description = ?, age_min = ?, age_max = ?,
                    gender = ?, deadline = ?, pdf_urls = ?, pdf_content = ?,
                    raw_content = ?, updated_at = datetime('now')
                WHERE id = ?
            """, (
                casting['title'],
                casting.get('description', ''),
                casting.get('age_min'),
                casting.get('age_max'),
                casting.get('gender', 'any'),
                casting.get('deadline'),
                json.dumps(casting.get('pdf_urls', [])),
                casting.get('pdf_content', ''),
                casting.get('description', ''),
                casting['id']
            ))
            conn.commit()
            return False  # Not new
        else:
            conn.execute("""
                INSERT INTO castings
                    (id, title, description, source_url, source_site,
                     age_min, age_max, gender, country, deadline,
                     pdf_urls, pdf_content, status, found_date, raw_content)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """, (
                casting['id'],
                casting['title'],
                casting.get('description', ''),
                casting['source_url'],
                casting.get('source_site', ''),
                casting.get('age_min'),
                casting.get('age_max'),
                casting.get('gender', 'any'),
                casting.get('country', 'CH'),
                casting.get('deadline'),
                json.dumps(casting.get('pdf_urls', [])),
                casting.get('pdf_content', ''),
                datetime.now().isoformat(),
                casting.get('description', ''),
            ))
            conn.commit()
            return True  # New casting


def get_castings(status_filter=None, country_filter=None, actor_age=None):
    """Get castings with optional filters applied dynamically."""
    with get_db() as conn:
        query = "SELECT * FROM castings WHERE 1=1"
        params = []

        # Never show 'not_interested'
        query += " AND status != 'not_interested'"

        if status_filter and status_filter != 'all':
            query += " AND status = ?"
            params.append(status_filter)

        if country_filter:
            query += " AND country = ?"
            params.append(country_filter)

        query += " ORDER BY CASE status WHEN 'new' THEN 0 WHEN 'applied' THEN 1 ELSE 2 END, found_date DESC"

        rows = conn.execute(query, params).fetchall()
        castings = []
        today = datetime.now().date().isoformat()

        for row in rows:
            c = dict(row)
            # Parse JSON fields
            try:
                c['pdf_urls'] = json.loads(c.get('pdf_urls', '[]'))
            except:
                c['pdf_urls'] = []

            # Dynamic filtering by age
            if actor_age:
                age_min = c.get('age_min')
                age_max = c.get('age_max')
                if age_min is not None and age_max is not None:
                    if actor_age < age_min or actor_age > age_max:
                        continue  # Skip age mismatch

            # Skip expired castings (but keep if deadline unknown)
            deadline = c.get('deadline')
            if deadline and deadline < today:
                continue

            castings.append(c)

        return castings


def get_casting(casting_id: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM castings WHERE id = ?", (casting_id,)).fetchone()
        if row:
            c = dict(row)
            try:
                c['pdf_urls'] = json.loads(c.get('pdf_urls', '[]'))
            except:
                c['pdf_urls'] = []
            return c
    return None


def update_casting_status(casting_id: str, status: str):
    valid = {'new', 'applied', 'not_interested', 'archived'}
    if status not in valid:
        raise ValueError(f"Invalid status: {status}")
    with get_db() as conn:
        conn.execute(
            "UPDATE castings SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, casting_id)
        )
        conn.commit()


def start_scan_log():
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO scan_log (started_at, status) VALUES (datetime('now'), 'running')",
        )
        conn.commit()
        return cur.lastrowid


def finish_scan_log(log_id, found, new_count, filtered, errors):
    with get_db() as conn:
        conn.execute("""
            UPDATE scan_log SET
                finished_at = datetime('now'),
                found = ?, new_count = ?, filtered = ?,
                errors = ?, status = 'done'
            WHERE id = ?
        """, (found, new_count, filtered, json.dumps(errors), log_id))
        conn.commit()


def get_last_scan():
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM scan_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_stats():
    with get_db() as conn:
        stats = {}
        stats['new'] = conn.execute(
            "SELECT COUNT(*) FROM castings WHERE status = 'new'"
        ).fetchone()[0]
        stats['applied'] = conn.execute(
            "SELECT COUNT(*) FROM castings WHERE status = 'applied'"
        ).fetchone()[0]
        stats['archived'] = conn.execute(
            "SELECT COUNT(*) FROM castings WHERE status = 'archived'"
        ).fetchone()[0]
        stats['not_interested'] = conn.execute(
            "SELECT COUNT(*) FROM castings WHERE status = 'not_interested'"
        ).fetchone()[0]
        stats['total'] = conn.execute(
            "SELECT COUNT(*) FROM castings"
        ).fetchone()[0]
    return stats
