"""Client / session log: who connected, how often, how long, what latency.

The guest path never waits on the database: it calls `session_start` /
`session_end` / `flush_live`, which only append (sql, params) to a bounded
queue. One writer thread commits the queue once a second in ONE transaction
(a burst of 1000 connects is one commit, not 1000). If the database stalls
and the queue hits QUEUE_MAX, new writes are dropped and counted (`dropped`,
shown on the Clients page): the show keeps running, the log has a gap.

What we can learn about a phone over plain HTTP + WebSocket: its IP (as seen
by this server; a NAT router hides the real one), the User-Agent (parsed into
device / OS / browser here), and what the guest page reports in its hello
(screen, pixel ratio, language, timezone, touch points, cores, memory,
connection type). MAC addresses are not visible to a web server.
"""
import json
import logging
import re
import threading
import time
from collections import deque

from . import db, settings

log = logging.getLogger('phonelight.tracker')

QUEUE_MAX = 20000
FLUSH_EVERY = 1.0
LIVE_FLUSH_EVERY = 30.0      # live sessions' running totals, so a crash loses little
PURGE_EVERY = 3600.0

_ID = re.compile(r'^[A-Za-z0-9_-]{8,64}$')

_queue = deque()
_qlock = threading.Lock()
dropped = 0


def valid_id(value):
    return isinstance(value, str) and bool(_ID.match(value))


def _clip(value, n=200):
    return None if value is None else str(value)[:n]


# ── User-Agent ─────────────────────────────────────────────────────────────
def parse_ua(ua):
    """-> (device, os, browser). Good enough for a log; not a fingerprint."""
    ua = ua or ''
    os_name = device = None
    if m := re.search(r'iPhone OS (\d+)[_\d]*', ua):
        os_name, device = f'iOS {m.group(1)}', 'iPhone'
    elif m := re.search(r'iPad.*OS (\d+)[_\d]*', ua):
        os_name, device = f'iPadOS {m.group(1)}', 'iPad'
    elif m := re.search(r'Android ([\d.]+)(?:;\s*([^;)]+))?', ua):
        os_name = f'Android {m.group(1)}'
        model = (m.group(2) or '').split(' Build/')[0].strip()
        device = model if model and model != 'K' else 'Android'
    elif m := re.search(r'Windows NT ([\d.]+)', ua):
        os_name, device = {'10.0': 'Windows 10/11'}.get(m.group(1), f'Windows NT {m.group(1)}'), 'PC'
    elif 'Mac OS X' in ua:
        os_name, device = 'macOS', 'Mac'
    elif 'CrOS' in ua:
        os_name, device = 'ChromeOS', 'Chromebook'
    elif 'Linux' in ua:
        os_name, device = 'Linux', 'PC'
    for pattern, name in ((r'SamsungBrowser/(\d+)', 'Samsung Internet'), (r'EdgA?/(\d+)', 'Edge'),
                          (r'OPR/(\d+)', 'Opera'), (r'FxiOS/(\d+)', 'Firefox'), (r'CriOS/(\d+)', 'Chrome'),
                          (r'Firefox/(\d+)', 'Firefox'), (r'Chrome/(\d+)', 'Chrome'),
                          (r'Version/(\d+)[\d.]* .*Safari', 'Safari')):
        if m := re.search(pattern, ua):
            browser = f'{name} {m.group(1)}'
            break
    else:
        browser = 'In-app browser' if re.search(r'FBAN|FBAV|Instagram|Line/|TikTok', ua) else None
    return device, os_name, browser


def describe(user_agent, hello):
    """-> the info dict kept on the guest + logged."""
    device, os_name, browser = parse_ua(user_agent)
    sw, sh, dpr = hello.get('sw'), hello.get('sh'), hello.get('dpr')
    screen = f'{sw}x{sh}@{dpr}' if isinstance(sw, int) and isinstance(sh, int) else None
    details = {k: hello[k] for k in ('touch', 'cores', 'mem', 'conn', 'plat', 'standalone')
               if isinstance(hello.get(k), (int, float, str, bool))}
    return dict(user_agent=_clip(user_agent, 400), device=_clip(device, 60), os=_clip(os_name, 40),
                browser=_clip(browser, 40), screen=_clip(screen, 30), language=_clip(hello.get('lang'), 35),
                timezone=_clip(hello.get('tz'), 64),
                details=json.dumps({k: _clip(v, 60) if isinstance(v, str) else v for k, v in details.items()}))


# ── queue ──────────────────────────────────────────────────────────────────
def _put(statements):
    global dropped
    with _qlock:
        if len(_queue) + len(statements) > QUEUE_MAX:
            dropped += len(statements)
            return
        _queue.extend(statements)


def session_start(guest):
    i, stamp = guest.info, db.iso(guest.connected_at)
    _put([
        ('INSERT INTO clients (id, first_seen, last_seen, visits, last_ip, last_fixture, user_agent, device, '
         'os, browser, screen, language, timezone, details) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
         'ON CONFLICT (id) DO UPDATE SET last_seen = excluded.last_seen, visits = clients.visits + 1, '
         'last_ip = excluded.last_ip, last_fixture = excluded.last_fixture, user_agent = excluded.user_agent, '
         'device = excluded.device, os = excluded.os, browser = excluded.browser, screen = excluded.screen, '
         'language = excluded.language, timezone = excluded.timezone, details = excluded.details',
         (guest.client_id, stamp, stamp, guest.ip, guest.fixture + 1, i['user_agent'], i['device'], i['os'],
          i['browser'], i['screen'], i['language'], i['timezone'], i['details'])),
        ('INSERT INTO sessions (id, client_id, ip, fixture, connected_at) VALUES (?, ?, ?, ?, ?)',
         (guest.sid, guest.client_id, guest.ip, guest.fixture + 1, stamp)),
    ])


def _session_totals(guest, now):
    avg = guest.latency_avg()
    return (now - guest.connected_at, avg, guest.lat_min, guest.lat_max, guest.lat_n)


def flush_live(guests, now=None):
    """Running totals of live sessions (every LIVE_FLUSH_EVERY)."""
    now = now or time.time()
    stamp = db.iso(now)
    statements = []
    for g in guests:
        seconds, avg, lo, hi, n = _session_totals(g, now)
        statements.append(('UPDATE sessions SET seconds = ?, fixture = ?, latency_avg = ?, latency_min = ?, '
                           'latency_max = ?, latency_samples = ? WHERE id = ?',
                           (seconds, g.fixture + 1, avg, lo, hi, n, g.sid)))
        statements.append(('UPDATE clients SET last_seen = ?, last_fixture = ? WHERE id = ?',
                           (stamp, g.fixture + 1, g.client_id)))
    _put(statements)


def session_end(guest, reason, now=None):
    now = now or time.time()
    seconds, avg, lo, hi, n = _session_totals(guest, now)
    stamp = db.iso(now)
    _put([
        ('UPDATE sessions SET disconnected_at = ?, seconds = ?, fixture = ?, latency_avg = ?, latency_min = ?, '
         'latency_max = ?, latency_samples = ?, close_reason = ? WHERE id = ?',
         (stamp, seconds, guest.fixture + 1, avg, lo, hi, n, _clip(reason, 60), guest.sid)),
        ('UPDATE clients SET last_seen = ?, total_seconds = total_seconds + ?, latency_sum = latency_sum + ?, '
         'latency_count = latency_count + ? WHERE id = ?',
         (stamp, seconds, guest.lat_sum, n, guest.client_id)),
    ])


def close_orphans():
    """Sessions left open by a crash / restart: close them at their last
    flushed length and add that to their clients' totals. Runs once at start,
    before any guest connects."""
    rows = db.query('SELECT id, client_id, seconds, latency_avg, latency_samples FROM sessions '
                    'WHERE disconnected_at IS NULL')
    stamp = db.now()
    statements = []
    for r in rows:
        statements.append(('UPDATE sessions SET disconnected_at = ?, close_reason = ? WHERE id = ?',
                           (stamp, 'server restart', r['id'])))
        statements.append(('UPDATE clients SET total_seconds = total_seconds + ?, latency_sum = latency_sum + ?, '
                           'latency_count = latency_count + ? WHERE id = ?',
                           (r['seconds'], (r['latency_avg'] or 0) * r['latency_samples'], r['latency_samples'],
                            r['client_id'])))
    db.run_batch(statements)
    return len(rows)


def purge(now=None):
    days = settings.current()['retention_days']
    if not days:
        return 0
    cutoff = db.iso((now or time.time()) - days * 86400)
    old = db.query('SELECT COUNT(*) AS n FROM clients WHERE last_seen < ?', (cutoff,))[0]['n']
    db.run_batch([('DELETE FROM sessions WHERE connected_at < ? AND disconnected_at IS NOT NULL', (cutoff,)),
                  ('DELETE FROM clients WHERE last_seen < ? AND NOT EXISTS '
                   '(SELECT 1 FROM sessions s WHERE s.client_id = clients.id AND s.disconnected_at IS NULL)',
                   (cutoff,))])
    if old:
        log.info('purged %d clients not seen in %d days', old, days)
    return old


def clear_all():
    db.run_batch([('DELETE FROM sessions WHERE disconnected_at IS NOT NULL', ()),
                  ('DELETE FROM clients WHERE NOT EXISTS '
                   '(SELECT 1 FROM sessions s WHERE s.client_id = clients.id)', ())])


def client_rows(limit, offset, q=''):
    where, params = '', []
    if q:
        like = f'%{q[:60]}%'
        where = ('WHERE id LIKE ? OR last_ip LIKE ? OR device LIKE ? OR os LIKE ? OR browser LIKE ?')
        params = [like] * 5
    return db.query(f'SELECT id, first_seen, last_seen, visits, total_seconds, last_ip, last_fixture, device, os, '
                    f'browser, screen, language, timezone, details, user_agent, '
                    f'CASE WHEN latency_count > 0 THEN latency_sum / latency_count END AS latency_avg '
                    f'FROM clients {where} ORDER BY last_seen DESC LIMIT ? OFFSET ?', (*params, limit, offset))


def session_rows(client_id, limit=200):
    return db.query('SELECT id, ip, fixture, connected_at, disconnected_at, seconds, latency_avg, latency_min, '
                    'latency_max, latency_samples, close_reason FROM sessions WHERE client_id = ? '
                    'ORDER BY connected_at DESC LIMIT ?', (client_id, limit))


def summary():
    row = db.query('SELECT COUNT(*) AS clients, COALESCE(SUM(visits), 0) AS sessions, '
                   'COALESCE(SUM(total_seconds), 0) AS seconds, '
                   'CASE WHEN SUM(latency_count) > 0 THEN SUM(latency_sum) / SUM(latency_count) END AS latency_avg '
                   'FROM clients')[0]
    return row


def queued():
    return len(_queue)


def flush():
    with _qlock:
        batch = list(_queue)
        _queue.clear()
    if batch:
        db.run_batch(batch)


class Writer:
    def __init__(self, live_guests):
        self.live_guests = live_guests          # fn() -> [Guest]
        self._stop = False

    def start(self):
        threading.Thread(target=self._run, name='tracker', daemon=True).start()

    def stop(self):
        self._stop = True

    def _run(self):
        last_live = last_purge = time.monotonic()
        while not self._stop:
            time.sleep(FLUSH_EVERY)
            now = time.monotonic()
            try:
                if now - last_live >= LIVE_FLUSH_EVERY:
                    last_live = now
                    flush_live(self.live_guests())
                if now - last_purge >= PURGE_EVERY:
                    last_purge = now
                    purge()
                flush()
            except Exception:
                log.exception('client log write failed (those rows are lost)')
