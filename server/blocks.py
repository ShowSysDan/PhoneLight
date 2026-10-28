"""Blocked devices: refused when they connect, by client id or by IP.

Stored in the `blocks` table and cached in memory: the guest path only ever
checks the cached sets (`is_blocked`), never the database. Changed from the
admin Clients page through `add()` / `remove()`. A client id is what the
guest page keeps in localStorage + a cookie, so a guest who clears their
browser data comes back as a new client; an IP block catches them, but
also everyone else behind the same address (a venue Wi-Fi's NAT).
"""
import ipaddress
import threading

from . import db, tracker

KINDS = ('client', 'ip')
MAX_BLOCKS = 5000

_lock = threading.Lock()
_sets = {k: set() for k in KINDS}
_hits = {}                       # (kind, value) -> refused connects since start (pruned on remove)


def _norm(kind, value):
    value = str(value or '').strip()
    if kind == 'client':
        if not tracker.valid_id(value):
            raise ValueError('not a client id')
        return value
    if kind == 'ip':
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            raise ValueError('not an IP address')
    raise ValueError(f'kind must be one of {", ".join(KINDS)}')


def load():
    sets = {k: set() for k in KINDS}
    for row in db.query('SELECT kind, value FROM blocks'):
        if row['kind'] in sets:
            sets[row['kind']].add(row['value'])
    with _lock:
        _sets.update(sets)


def is_blocked(client_id, ip):
    """On connect: the matching block, (kind, value), or None. Counts the refusal."""
    for key in (('client', client_id), ('ip', ip)):
        if key[1] in _sets[key[0]]:
            _hits[key] = _hits.get(key, 0) + 1
            return key
    return None


def add(kind, value, note=''):
    value = _norm(kind, value)
    if sum(len(s) for s in _sets.values()) >= MAX_BLOCKS and value not in _sets[kind]:
        raise ValueError(f'at most {MAX_BLOCKS} blocks')
    db.run_batch([('INSERT INTO blocks (kind, value, note, created_at) VALUES (?, ?, ?, ?) '
                   'ON CONFLICT (kind, value) DO UPDATE SET note = excluded.note',
                   (kind, value, str(note or '')[:200], db.now()))])
    with _lock:
        _sets[kind].add(value)
    return kind, value


def remove(kind, value):
    value = _norm(kind, value)
    db.run_batch([('DELETE FROM blocks WHERE kind = ? AND value = ?', (kind, value))])
    with _lock:
        _sets[kind].discard(value)
        _hits.pop((kind, value), None)


def rows():
    out = db.query('SELECT kind, value, note, created_at FROM blocks ORDER BY created_at DESC')
    for r in out:
        r['refused'] = _hits.get((r['kind'], r['value']), 0)
    return out
