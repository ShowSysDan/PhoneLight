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

from flask import Flask, abort, jsonify, render_template, request
from flask_sock import ConnectionClosed, Sock
from . import config, migrate, settings, tracker
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
        self.engine = Engine(self.hub.publish)
        self.receiver = Receiver(lambda: settings.current()['universe'], self.engine.on_frame,
                                 bind=config.get('SACN_BIND', '0.0.0.0'),
                                 port=config.get_int('SACN_PORT', 5568))
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
        headers), and ADMIN_ALLOW is checked against the real peer (proxy.py)."""
        if request.path.startswith(('/admin', '/ws/admin')):
            if request.environ.get('phonelight.via_proxy'):
                abort(403)
            if not _admin_allowed(request.environ.get('phonelight.peer', request.remote_addr)):
                abort(403)

    # ── guest ──────────────────────────────────────────────────────────────
    with app.app_context():
        guest_page = render_template('guest.html').encode()

    @app.get('/')
    def guest():
        return guest_page, 200, {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-cache'}

    @app.get('/healthz')
    def healthz():
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
        info = tracker.describe(request.headers.get('User-Agent'), hello)
        raw = getattr(ws.sock, '_sock', None)           # gevent: the non-blocking OS socket
        guest = Guest(uuid.uuid4().hex, client_id, request.remote_addr, info,
                      raw=raw if raw is not None and raw.gettimeout() == 0.0 else None, closer=ws.sock.close)
        preferred = hello.get('fixture')
        rt.hub.join(guest, preferred - 1 if isinstance(preferred, int) else None)
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
                           sacn={k: snap[k] for k in ('sources', 'packets', 'ignored', 'error', 'listen',
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

