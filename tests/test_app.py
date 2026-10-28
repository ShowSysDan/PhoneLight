"""End to end: the real server (python -m server) in a subprocess, real sACN
over UDP, a real WebSocket guest, the admin API, the client log."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from sacn_sender import packet
from simple_websocket import Client

from server import tracker

REPO = Path(__file__).resolve().parent.parent


def free_port(kind=socket.SOCK_STREAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('srv')
    port, sacn_port = free_port(), free_port(socket.SOCK_DGRAM)
    env_file = tmp / 'test.env'
    env_file.write_text(f'DB_PATH={tmp / "t.db"}\nAPP_PORT={port}\nSACN_BIND=127.0.0.1\nSACN_PORT={sacn_port}\n')
    env = dict(os.environ, PHONELIGHT_ENV_FILE=str(env_file))
    env.pop('DB_PATH', None)
    proc = subprocess.Popen([sys.executable, '-m', 'server'], cwd=REPO, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = f'http://127.0.0.1:{port}'
    for _ in range(100):
        try:
            urllib.request.urlopen(base + '/healthz', timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError(proc.stdout.read().decode())
    yield {'base': base, 'sacn': ('127.0.0.1', sacn_port), 'db': tmp / 't.db', 'env': env_file}
    proc.terminate()
    proc.wait(10)


def get(server, path):
    with urllib.request.urlopen(server['base'] + path, timeout=5) as r:
        return r.status, r.read()


def post(server, path, body):
    req = urllib.request.Request(server['base'] + path, json.dumps(body).encode(),
                                 {'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def send_sacn(server, data, seq):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(packet(b'T' * 16, 'test', 100, seq, 1, data), server['sacn'])
    s.close()


def receive_color(ws, want, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        msg = ws.receive(timeout=0.5)
        if isinstance(msg, (bytes, bytearray)) and bytes(msg[:3]) == want and len(msg) == 7:
            return True
        if isinstance(msg, str) and msg.startswith('p'):
            ws.send(msg)
    return False


def test_pages(server):
    status, body = get(server, '/')
    assert status == 200 and b'/ws' in body and len(body) < 8000
    for path in ('/admin', '/admin/sacn', '/admin/clients', '/admin/settings', '/static/keepawake.mp4'):
        assert get(server, path)[0] == 200


def test_sacn_to_phone(server):
    assert post(server, '/admin/api/settings', {'fixtures': 1, 'bit_depth': 8, 'start_address': 1})[0] == 200
    ws = Client.connect(server['base'].replace('http', 'ws') + '/ws',
                        headers={'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) '
                                               'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'})
    try:
        ws.send(json.dumps({'id': 'integration-test-1', 'sw': 390, 'sh': 844, 'dpr': 3, 'lang': 'en-US', 'tz': 'UTC'}))
        assert ws.receive(timeout=3) == 'f1'
        data = bytearray(512)
        data[0:3] = b'\x10\x20\x30'
        send_sacn(server, data, 1)
        assert receive_color(ws, b'\x10\x20\x30')
        data[0:3] = b'\xff\x00\x80'
        send_sacn(server, data, 2)
        assert receive_color(ws, b'\xff\x00\x80')
        # override beats sACN, and back
        post(server, '/admin/api/mode', {'mode': 'color', 'color': '#00ff00'})
        assert receive_color(ws, b'\x00\xff\x00')
        post(server, '/admin/api/mode', {'mode': 'sacn'})
        assert receive_color(ws, b'\xff\x00\x80')
        assert post(server, '/admin/api/mode', {'mode': 'color', 'color': 'nope'})[0] == 400
        ws.send('s1000.0')                                  # clock sync round trip
        for _ in range(20):
            msg = ws.receive(timeout=1)
            if isinstance(msg, str) and msg.startswith('s'):
                assert msg.startswith('s1000.0,') and len(msg.split(',')) == 3
                break
        else:
            raise AssertionError('no sync reply')
    finally:
        ws.close()
    time.sleep(1.5)                               # tracker writes once a second
    status, body = get(server, '/admin/api/clients?q=integration')
    rows = json.loads(body)['rows']
    assert rows and rows[0]['visits'] == 1 and rows[0]['os'] == 'iOS 17' and rows[0]['browser'] == 'Safari 17'
    status, body = get(server, '/admin/api/clients/integration-test-1/sessions')
    sess = json.loads(body)['rows'][0]
    assert sess['disconnected_at'] and sess['close_reason']


def test_settings_validation(server):
    status, body = post(server, '/admin/api/settings', {'fixtures': 200})
    assert status == 400 and 'Fixtures' in body['error']


def test_parse_ua():
    assert tracker.parse_ua('Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) '
                            'SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36') == \
        ('SM-S918B', 'Android 14', 'Samsung Internet 25')
    assert tracker.parse_ua('Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) '
                            'Chrome/130.0.0.0 Mobile Safari/537.36') == ('Android', 'Android 10', 'Chrome 130')
    assert tracker.parse_ua('') == (None, None, None)


def test_tracker_totals_and_orphans(env):
    from server import db
    from server.hub import Guest
    g = Guest('sess1', 'clientabc1', '10.1.1.1', tracker.describe('ua', {'sw': 1, 'sh': 2}))
    g.connected_at = time.time() - 60
    g.lat_n, g.lat_sum = 2, 100.0
    tracker.session_start(g)
    tracker.session_end(g, 'closed by guest')
    g2 = Guest('sess2', 'clientabc1', '10.1.1.1', g.info)
    tracker.session_start(g2)
    tracker.flush_live([g2])
    tracker.flush()
    row = db.query('SELECT * FROM clients')[0]
    assert row['visits'] == 2 and 59 < row['total_seconds'] < 62 and row['latency_count'] == 2
    assert tracker.close_orphans() == 1
    assert db.query("SELECT close_reason FROM sessions WHERE id = 'sess2'")[0]['close_reason'] == 'server restart'
    assert tracker.purge(now=time.time() + 400 * 86400) == 1
    assert db.query('SELECT COUNT(*) AS n FROM sessions')[0]['n'] == 0
