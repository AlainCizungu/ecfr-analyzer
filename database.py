"""
database.py — SQLite schema, connection, and helpers.
"""
import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "ecfr.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS agencies (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    short_name  TEXT,
    cfr_refs    TEXT    -- JSON: [{title, chapter}, ...]
);

CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agency_id    TEXT    NOT NULL,
    date         TEXT    NOT NULL,
    word_count   INTEGER NOT NULL DEFAULT 0,
    checksum     TEXT,
    title_nums   TEXT,               -- JSON: [int, ...]
    UNIQUE (agency_id, date),
    FOREIGN KEY (agency_id) REFERENCES agencies (id)
);

-- Per-title snapshots: used to detect changes before fetching history
CREATE TABLE IF NOT EXISTS title_snapshots (
    title_num  INTEGER NOT NULL,
    date       TEXT    NOT NULL,
    word_count INTEGER NOT NULL DEFAULT 0,
    checksum   TEXT,
    PRIMARY KEY (title_num, date)
);

CREATE TABLE IF NOT EXISTS title_versions (
    title_num  INTEGER NOT NULL,
    date       TEXT    NOT NULL,
    PRIMARY KEY (title_num, date)
);

CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def set_meta(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()


def get_meta(key: str) -> str | None:
    conn = get_conn()
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def get_title_checksum(title_num: int, snap_date: str) -> str | None:
    """Return the previously stored checksum for a title/date, or None if first run."""
    conn = get_conn()
    row = conn.execute(
        "SELECT checksum FROM title_snapshots WHERE title_num=? AND date=?",
        (title_num, snap_date),
    ).fetchone()
    conn.close()
    return row["checksum"] if row else None


def upsert_title_snapshot(title_num: int, snap_date: str,
                           wc: int, checksum: str) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO title_snapshots (title_num, date, word_count, checksum)
           VALUES (?,?,?,?)
           ON CONFLICT(title_num, date) DO UPDATE SET
               word_count=excluded.word_count, checksum=excluded.checksum""",
        (title_num, snap_date, wc, checksum),
    )
    conn.commit()
    conn.close()
