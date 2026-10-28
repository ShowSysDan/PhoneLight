"""PhoneLight web app: the guest page, its WebSocket, and the admin pages.

ONE process (gunicorn, one gevent worker; see start.sh): the sACN receiver,
the engine and every guest socket must share memory, so this app never runs
with more than one worker. gevent makes each thread below a greenlet, so a
thousand open sockets cost a few KB each, not an OS thread each.

    sACN (UDP 5568) -> sacn.Receiver -> engine.Engine -> hub.Hub -> /ws senders
                                                               \\-> tracker (SQLite, batched)
"""
import ipaddress
import json
import logging
import threading
import time
import uuid
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, render_template, request
from flask_sock import ConnectionClosed, Sock
from . import blocks, config, migrate, proxy, settings, tracker
from .engine import Engine
from .hub import Guest, Hub
from .proxy import ProxyHeaders
from .sacn import Receiver

log = logging.getLogger('phonelight')

ADMIN_FEED_HZ = 10
HELLO_TIMEOUT = 10


class Runtime:
    """The live show: everything that is not a request."""

    def __init__(self):
        v = settings.current()
        self.hub = Hub(v['fixtures'], v['cells'], v['cell_columns'])
        self.hub.delay_ms = v['sync_delay']
        self.hub.max_per_ip = max(0, config.get_int('MAX_PER_IP', 0))
        self.hub.per_ip_exempt = proxy.networks(config.get('MAX_PER_IP_EXEMPT', ''))
        self.engine = Engine(self.hub.publish)
        self.receiver = Receiver(lambda: settings.current()['universe'], self.engine.on_frame,
                                 bind=config.get('SACN_BIND', '0.0.0.0'),
                                 port=config.get_int('SACN_PORT', 5568),
                                 allow=_sacn_allow())
        self.writer = tracker.Writer(lambda: list(self.hub.guests.values()))
        settings.on_change(self._settings_changed)

    def _settings_changed(self, old, new):
        self.hub.delay_ms = new['sync_delay']
        if new['fixtures'] != old['fixtures']:
            self.hub.resize(new['fixtures'])
        if (new['cells'], new['cell_columns']) != (old['cells'], old['cell_columns']):
            self.hub.set_layout(new['cells'], new['cell_columns'])
        if new['universe'] != old['universe']:
            self.receiver.universe_changed()
        self.engine.recompute()

    def start(self):
        orphans = tracker.close_orphans()
        if orphans:
            log.info('closed %d sessions left open by the last run', orphans)
        self.receiver.start()
        self.engine.start()
        self.hub.start()
        self.writer.start()


def _sacn_allow():
    """SACN_ALLOW: IPs / networks the console sends from; unset = every sender."""
    text = config.get('SACN_ALLOW', '')
    if not text:
        return None
    nets = proxy.networks(text)
    log.info('sACN accepted only from %s', ', '.join(map(str, nets)) or 'nobody (SACN_ALLOW has no valid entry)')
    return nets


_FORWARDED = ('HTTP_X_FORWARDED_FOR', 'HTTP_FORWARDED', 'HTTP_X_REAL_IP', 'HTTP_X_FORWARDED_HOST')


def _admin_host_ok(host):
    """The admin answers only to an IP address, localhost or a name in ADMIN_HOSTS:
    a DNS-rebinding page (evil.example resolving to this server) can't use the
    crew's browser to reach it."""
    name = (host or '').strip().lower()
    if name.startswith('['):
        name = name[1:name.find(']')] if ']' in name else name
    elif name.count(':') == 1:
        name = name.split(':', 1)[0]
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    allowed = {h.strip().lower() for h in config.get('ADMIN_HOSTS', '').split(',') if h.strip()}
    return name == 'localhost' or name in allowed


def _same_origin(origin, host):
    """A browser's Origin header names the page that sent the request."""
    try:
        return urlsplit(origin).netloc.lower() == (host or '').lower()
    except ValueError:
        return False


def _admin_allowed(ip):
    """ADMIN_ALLOW: comma-separated IPs / CIDRs that may open /admin. Empty = anyone."""
    allow = config.get('ADMIN_ALLOW', '')
    if not allow:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for part in allow.split(','):
        try:
            if addr in ipaddress.ip_network(part.strip(), strict=False):
                return True
        except ValueError:
            log.warning('ADMIN_ALLOW: %r is not an IP or network', part)
    return False


def create_app(start=True):
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    migrate.migrate(out=log.info)
    settings.load()
    blocks.load()

    app = Flask(__name__)
    app.config['SOCK_SERVER_OPTIONS'] = {'max_message_size': 4096}
    app.wsgi_app = ProxyHeaders(app.wsgi_app, config.get('TRUST_PROXY', ''))
    sock = Sock(app)
    rt = Runtime()
    app.extensions['phonelight'] = rt
    version = config.version()

    @app.context_processor
    def _globals():
        return {'version': version}

    @app.before_request
    def _guard_admin():
        """The admin is building-network only: never through the gateway (even with forged
        headers), and ADMIN_ALLOW is checked against the real peer (proxy.py). There is no
        sign-in yet, so it also refuses what a web page in a crew member's browser could
        send: another site's Origin (cross-site POSTs, WebSocket hijacking; browsers send
        Origin on both) and a Host that is not this server's address (DNS rebinding)."""
        if request.path.startswith(('/admin', '/ws/admin')):
            env = request.environ
            if env.get('phonelight.via_proxy') or any(env.get(h) for h in _FORWARDED):
                abort(403)          # through a proxy, even when TRUST_PROXY is not set
            if not _admin_allowed(env.get('phonelight.peer', request.remote_addr)):
                abort(403)
            if not _admin_host_ok(request.host):
                abort(403, description='Open the admin by the server\'s IP address, or add this name '
                                       'to ADMIN_HOSTS in .env.')
            origin = request.headers.get('Origin')
            if origin and not _same_origin(origin, request.host):
                abort(403)

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('Referrer-Policy', 'same-origin')
        if request.path.startswith('/admin'):
            resp.headers.setdefault('X-Frame-Options', 'DENY')
            resp.headers.setdefault('Content-Security-Policy', "frame-ancestors 'none'")
            resp.headers.setdefault('Cache-Control', 'no-store')
        return resp

    # ── guest ──────────────────────────────────────────────────────────────
    with app.app_context():
        guest_page = render_template('guest.html').encode()

    @app.get('/')
    def guest():
        return guest_page, 200, {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-cache'}

    @app.get('/healthz')
    def healthz():
        if request.environ.get('phonelight.via_proxy'):
            return jsonify(ok=True)             # the internet doesn't need the version or the audience size
        return jsonify(ok=True, version=version, guests=len(rt.hub.guests))

    @sock.route('/ws')
    def guest_ws(ws):
        try:
            hello = json.loads(ws.receive(timeout=HELLO_TIMEOUT) or '{}')
        except (ValueError, TypeError):
            hello = {}
        except ConnectionClosed:
            return
        if not isinstance(hello, dict):
            hello = {}
        client_id = hello['id'] if tracker.valid_id(hello.get('id')) else uuid.uuid4().hex
        if blocks.is_blocked(client_id, request.remote_addr):
            try:
                ws.send('x')                # blocked: the page stops reconnecting
            except ConnectionClosed:
                pass
            ws.close()
            return
        info = tracker.describe(request.headers.get('User-Agent'), hello)
        raw = getattr(ws.sock, '_sock', None)           # gevent: the non-blocking OS socket
        guest = Guest(uuid.uuid4().hex, client_id, request.remote_addr, info,
                      raw=raw if raw is not None and raw.gettimeout() == 0.0 else None, closer=ws.sock.close)
        preferred = hello.get('fixture')
        if rt.hub.join(guest, preferred - 1 if isinstance(preferred, int) else None) is None:
            ws.close()                  # MAX_PER_IP: this address holds enough sockets already
            return
        tracker.session_start(guest)
        threading.Thread(target=_guest_reader, args=(ws, guest), daemon=True).start()
        reason = 'closed by guest'
        try:
            reason = rt.hub.serve(guest, ws.sock.sendall, ws.send)
        except (ConnectionClosed, OSError):
            reason = guest.why or 'connection lost'
        finally:
            rt.hub.leave(guest)
            tracker.session_end(guest, reason)
            ws.close()

    def _guest_reader(ws, guest):
        try:
            while guest.alive:
                msg = ws.receive()
                if isinstance(msg, str):
                    if msg[:1] == 'p':
                        guest.pong(msg)
                    elif msg[:1] == 's':
                        rt.hub.sync_request(guest, msg)
        except ConnectionClosed:
            pass
        finally:
            guest.alive = False
            guest.wake.set()

    # ── admin pages ────────────────────────────────────────────────────────
    @app.get('/admin')
    def admin_live():
        return render_template('live.html', page='live')

    @app.get('/admin/sacn')
    def admin_sacn():
        return render_template('sacn.html', page='sacn')

    @app.get('/admin/clients')
    def admin_clients():
        return render_template('clients.html', page='clients')

    @app.get('/admin/settings')
    def admin_settings():
        return render_template('settings.html', page='settings', defs=settings.DEFS,
                               values=settings.current())

    # ── admin API ──────────────────────────────────────────────────────────
    @app.post('/admin/api/mode')
    def api_mode():
        """{mode, color?: '#rrggbb', fixture?: n, cell?: n, pattern?, rate?} -> the Live page's
        output and test tools (all made inside the engine; nothing is sent as sACN)."""
        body = request.get_json(silent=True) or {}
        color = None
        if body.get('color'):
            try:
                color = bytes.fromhex(str(body['color']).lstrip('#'))
            except ValueError:
                color = b''
            if len(color) != 3:
                return jsonify(error='colour must be #rrggbb'), 400
        try:
            fixture = int(body['fixture']) if body.get('fixture') is not None else None
            cell = int(body['cell']) if body.get('cell') is not None else None
            rate = float(body['rate']) if body.get('rate') is not None else None
            rt.engine.set_mode(body.get('mode', 'sacn'), color, fixture, body.get('pattern'), rate, cell)
        except (TypeError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        if not body.get('quiet'):
            log.info('mode -> %s %s', rt.engine.mode,
                     {k: body[k] for k in ('color', 'fixture', 'cell', 'pattern', 'rate') if k in body})
        return jsonify(rt.engine.snapshot())

    @app.post('/admin/api/rebalance')
    def api_rebalance():
        rt.hub.rebalance()
        return jsonify(counts=rt.hub.counts())

    @app.post('/admin/api/settings')
    def api_settings():
        try:
            values = settings.save(request.get_json(silent=True) or {})
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        log.info('settings saved: %s', values)
        return jsonify(values)

    @app.get('/admin/api/clients')
    def api_clients():
        limit = min(request.args.get('limit', 200, type=int), 1000)
        offset = max(request.args.get('offset', 0, type=int), 0)
        rows = tracker.client_rows(limit, offset, request.args.get('q', ''))
        return jsonify(rows=rows, summary=tracker.summary(), dropped=tracker.dropped,
                       queued=tracker.queued())

    @app.post('/admin/api/clients/kick')
    def api_kick():
        """{sid} -> disconnect that phone (it reconnects by itself: a reset, not a ban)."""
        sid = (request.get_json(silent=True) or {}).get('sid')
        n = rt.hub.kick(lambda g: g.sid == sid)
        log.info('kicked %s (%d)', sid, n)
        return jsonify(kicked=n)

    @app.get('/admin/api/blocks')
    def api_blocks():
        return jsonify(rows=blocks.rows())

    @app.post('/admin/api/blocks')
    def api_block():
        """{kind: 'client' | 'ip', value, note?} -> refused from now on; connected matches are closed."""
        body = request.get_json(silent=True) or {}
        try:
            value = body.get('value')
            if body.get('kind') == 'client':
                value = blocks.resolve_client(value, {g.client_id for g in list(rt.hub.guests.values())})
            kind, value = blocks.add(body.get('kind'), value, body.get('note'))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        attr = 'client_id' if kind == 'client' else 'ip'
        n = rt.hub.kick(lambda g: getattr(g, attr) == value, bye='x', why='blocked by admin')
        log.info('blocked %s %s (%d connected closed)', kind, value, n)
        return jsonify(kind=kind, value=value, kicked=n)

    @app.post('/admin/api/blocks/remove')
    def api_unblock():
        body = request.get_json(silent=True) or {}
        try:
            blocks.remove(body.get('kind'), body.get('value'))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        log.info('unblocked %s %s', body.get('kind'), body.get('value'))
        return jsonify(ok=True)

    @app.get('/admin/api/clients/<client_id>/sessions')
    def api_client_sessions(client_id):
        return jsonify(rows=tracker.session_rows(client_id))

    @app.post('/admin/api/clients/clear')
    def api_clients_clear():
        tracker.flush()
        tracker.clear_all()
        log.info('client log cleared')
        return jsonify(ok=True)

    @sock.route('/ws/admin')
    def admin_ws(ws):
        """~10 Hz JSON for the admin pages. The page says which extras it
        wants ({"views": ["levels", "guests"]}); guests (the big list) goes at 1 Hz."""
        views = set()
        last_guests = 0.0
        try:
            while True:
                msg = ws.receive(timeout=1 / ADMIN_FEED_HZ)
                if msg:
                    try:
                        views = set(json.loads(msg).get('views', []))
                    except (ValueError, AttributeError):
                        pass
                snap = rt.receiver.snapshot()
                out = dict(engine=rt.engine.snapshot(), counts=rt.hub.counts(), connected=len(rt.hub.guests),
                           settings=settings.current(),
                           sacn={k: snap[k] for k in ('sources', 'packets', 'ignored', 'refused', 'error', 'listen',
                                                       'other_universes', 'stale', 'timing')})
                if 'levels' in views:
                    out['levels'] = snap['levels']
                now = time.monotonic()
                if 'guests' in views and now - last_guests >= 1:
                    last_guests = now
                    out['guests'] = rt.hub.snapshot()
                ws.send(json.dumps(out, separators=(',', ':')))
        except ConnectionClosed:
            pass

    if start:
        rt.start()
    return app

