"""Database migrations: numbered SQL files, applied once, in order.

    python -m server.migrate            apply pending migrations
    python -m server.migrate --status   list applied / pending, change nothing

start.sh runs this before every service start, so an update is just
`git pull && sudo systemctl restart phonelight`.

Rules (see CLAUDE.md):
- One file per change: server/migrations/NNNN_short_name.sql. Never edit or
  renumber a file once it is pushed; add a new one.
- Each file runs in ONE transaction; if any statement fails the whole file
  rolls back and the service does not start (fail loud).
- Applied versions are recorded in schema_migrations with a checksum; an
  applied file that changed afterwards stops the start.
"""
import hashlib
import re
import sys
from pathlib import Path

from . import db

MIGRATIONS_DIR = Path(__file__).resolve().parent / 'migrations'
_FILE = re.compile(r'^(\d{4})_([a-z0-9_]+)\.sql$')


def discover():
    """[(version, name, path, sha256)] sorted by version."""
    found = {}
    for path in sorted(MIGRATIONS_DIR.glob('*.sql')):
        m = _FILE.match(path.name)
        if not m:
            raise SystemExit(f'migrate: bad migration file name {path.name} '
                             f'(expected NNNN_name.sql)')
        version = int(m.group(1))
        if version in found:
            raise SystemExit(f'migrate: duplicate migration number {version:04d}')
        found[version] = (version, m.group(2), path, hashlib.sha256(path.read_bytes()).hexdigest())
    return [found[v] for v in sorted(found)]


def _applied(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations ('
                 'version INTEGER PRIMARY KEY, name TEXT NOT NULL, '
                 'checksum TEXT NOT NULL, applied_at TEXT NOT NULL)')
    return {r['version']: r for r in conn.execute('SELECT * FROM schema_migrations')}


def migrate(status_only=False, out=print):
    conn = db.connect()
    try:
        applied = _applied(conn)
        pending = []
        for version, name, path, sha in discover():
            row = applied.get(version)
            if row is None:
                pending.append((version, name, path, sha))
            elif row['checksum'] != sha:
                raise SystemExit(f'migrate: {path.name} changed after it was applied; '
                                 f'add a new migration instead of editing it')
        if status_only:
            for v in sorted(applied):
                out(f'applied  {v:04d}_{applied[v]["name"]}')
            for version, name, _, _ in pending:
                out(f'pending  {version:04d}_{name}')
            return len(pending)
        for version, name, path, sha in pending:
            # executescript() commits first, so wrap the file ourselves.
            sql = path.read_text(encoding='utf-8')
            try:
                conn.executescript('BEGIN;\n' + sql + '\n;'
                                   f"INSERT INTO schema_migrations VALUES ({version}, '{name}', "
                                   f"'{sha}', '{db.now()}');\nCOMMIT;")
            except Exception as exc:
                if conn.in_transaction:
                    conn.execute('ROLLBACK')
                raise SystemExit(f'migrate: {path.name} failed: {exc}')
            out(f'migrate: applied {version:04d}_{name}')
        return len(pending)
    finally:
        conn.close()


if __name__ == '__main__':
    migrate(status_only='--status' in sys.argv[1:])
