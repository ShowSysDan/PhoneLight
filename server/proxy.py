"""Behind the gateway: who the guest really is, and whether a request came
through the gateway at all.

TRUST_PROXY (.env) is the gateway's address as this server sees it (e.g.
10.201.4.9, the VPS's tunnel address; comma-separated IPs / networks). Only a
request whose DIRECT peer is one of those has its X-Forwarded-For /
X-Forwarded-Proto believed; anyone else's are ignored, so nobody can claim
another address by sending the header. Requests through the gateway are
marked `phonelight.via_proxy` and the admin refuses them (app._guard_admin):
the admin is for the building network only, even if the VPS is compromised.

`phonelight.peer` always holds the real peer address; ADMIN_ALLOW is checked
against it, never against a forwarded one.

Legacy TRUST_PROXY=1 (0.1-0.5): believes the headers from anyone (logged as a
warning at start). Any request that carries X-Forwarded-For then counts as via
the gateway, so the admin still refuses it.
"""
import ipaddress
import logging

log = logging.getLogger('phonelight.proxy')


def networks(text):
    """'10.201.4.9, 10.0.0.0/8' -> [ip_network]; bad entries are logged and skipped."""
    nets = []
    for part in (text or '').split(','):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log.warning('not an IP or network: %r (ignored)', part)
    return nets


def contains(nets, ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


class ProxyHeaders:
    def __init__(self, app, setting):
        self.app = app
        setting = (setting or '').strip()
        self.anyone = setting == '1'
        self.nets = [] if self.anyone else networks(setting)
        if self.anyone:
            log.warning('TRUST_PROXY=1 believes X-Forwarded-For from anyone; set it to the gateway\'s '
                        'address instead (e.g. TRUST_PROXY=10.201.4.9)')

    def __call__(self, environ, start_response):
        peer = environ.get('REMOTE_ADDR', '')
        forwarded = environ.get('HTTP_X_FORWARDED_FOR')
        via = (forwarded is not None) if self.anyone else contains(self.nets, peer)
        environ['phonelight.peer'] = peer
        environ['phonelight.via_proxy'] = via
        if via and forwarded:
            client = forwarded.split(',')[-1].strip()     # the hop the gateway itself added
            try:
                ipaddress.ip_address(client)
                environ['REMOTE_ADDR'] = client
            except ValueError:
                pass
            proto = environ.get('HTTP_X_FORWARDED_PROTO', '').split(',')[-1].strip()
            if proto in ('http', 'https'):
                environ['wsgi.url_scheme'] = proto
        return self.app(environ, start_response)
