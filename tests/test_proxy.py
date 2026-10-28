"""TRUST_PROXY / ADMIN_ALLOW behind the gateway: forwarded headers only from
the gateway, admin never through it, ADMIN_ALLOW on the real peer."""
import pytest

from server import proxy

GATEWAY, LAN, OUTSIDE = '10.201.4.9', '10.201.2.50', '203.0.113.7'


def seen(setting, peer, headers=None):
    """What the app sees for a request from `peer` with `headers`."""
    got = {}

    def app(environ, start_response):
        got.update(peer=environ['phonelight.peer'], via=environ['phonelight.via_proxy'],
                   ip=environ['REMOTE_ADDR'], scheme=environ['wsgi.url_scheme'])
        return []
    environ = {'REMOTE_ADDR': peer, 'wsgi.url_scheme': 'http'}
    environ.update({'HTTP_' + k.upper().replace('-', '_'): v for k, v in (headers or {}).items()})
    proxy.ProxyHeaders(app, setting)(environ, None)
    return got


def test_gateway_headers_believed_only_from_the_gateway():
    h = {'X-Forwarded-For': '198.51.100.1, 8.8.8.8', 'X-Forwarded-Proto': 'https'}
    assert seen(GATEWAY, GATEWAY, h) == dict(peer=GATEWAY, via=True, ip='8.8.8.8', scheme='https')
    assert seen(GATEWAY, LAN, h) == dict(peer=LAN, via=False, ip=LAN, scheme='http')       # forged: ignored
    assert seen('10.201.4.0/24', GATEWAY, h)['ip'] == '8.8.8.8'                            # networks work
    assert seen('', GATEWAY, h) == dict(peer=GATEWAY, via=False, ip=GATEWAY, scheme='http')  # unset: ignored
    assert seen(GATEWAY, GATEWAY, {'X-Forwarded-For': 'not-an-ip'})['ip'] == GATEWAY


def test_legacy_one_marks_any_forwarded_request():
    assert seen('1', LAN, {'X-Forwarded-For': '8.8.8.8'})['via'] is True
    assert seen('1', LAN)['via'] is False


@pytest.fixture
def client(env, monkeypatch):
    def make(trust, allow=''):
        monkeypatch.setenv('TRUST_PROXY', trust)
        monkeypatch.setenv('ADMIN_ALLOW', allow)
        from server.app import create_app
        return create_app(start=False).test_client()
    return make


def status(c, peer, path='/admin', headers=None):
    return c.get(path, headers=headers or {}, environ_base={'REMOTE_ADDR': peer}).status_code


def test_admin_never_through_the_gateway(client):
    c = client(GATEWAY)
    assert status(c, GATEWAY, headers={'X-Forwarded-For': LAN}) == 403        # even claiming a LAN address
    assert status(c, GATEWAY) == 403
    assert status(c, GATEWAY, '/admin/api/clients') == 403
    assert status(c, GATEWAY, '/') == 200                                      # guests are fine
    assert status(c, LAN) == 200                                               # building network: allowed


def test_admin_allow_checks_the_real_peer(client):
    c = client(GATEWAY, '10.201.2.0/24')
    assert status(c, LAN) == 200
    assert status(c, OUTSIDE, headers={'X-Forwarded-For': LAN}) == 403        # can't talk its way in
    assert status(c, OUTSIDE) == 403


def test_legacy_trust_proxy_one_still_protects_the_admin(client):
    c = client('1', '10.201.2.0/24')
    assert status(c, LAN) == 200
    assert status(c, LAN, headers={'X-Forwarded-For': '10.201.2.5'}) == 403
