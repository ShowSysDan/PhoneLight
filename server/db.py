"""SQLite access (for now: PostgreSQL once PhoneLight is past the trial).

One file at DB_PATH (default data/phonelight.db), WAL mode. Every call opens
its own short connection (`connect()`); nothing holds one open across a
request. The guest WebSocket path never touches the database directly: its
writes go through tracker.py's batch queue so a burst of connects can never
stall the colour fan-out.

Kept PostgreSQL-friendly on purpose so the move is a driver swap plus
migrations, not a rewrite:
- timestamps are ISO-8601 UTC text (`now()`), -> TIMESTAMPTZ later;
- ids are app-made text (uuid4 hex), no AUTOINCREMENT / lastrowid;
- upserts are `INSERT ... ON CONFLICT (...) DO UPDATE` (same syntax in PG);
- no SQLite-only functions in the SQL (date maths happens in Python).
Placeholders are `?` here; they become `%s` with psycopg2.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config


def path():
    p = Path(config.get('DB_PATH', 'data/phonelight.db'))
    return p if p.is_absolute() else config.REPO_DIR / p


def connect():
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def iso(ts):
    """Unix seconds -> the same ISO text as now()."""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec='milliseconds')


def query(sql, params=()):
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def run_batch(statements):
    """[(sql, params)] in ONE transaction (all or nothing)."""
    if not statements:
        return
    conn = connect()
    try:
        conn.execute('BEGIN')
        for sql, params in statements:
            conn.execute(sql, params)
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()
