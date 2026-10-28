"""Development server: `python -m server` (gevent, like production; one process)."""
from gevent import monkey

monkey.patch_all()

from gevent.pywsgi import WSGIServer  # noqa: E402

from . import config  # noqa: E402
from .app import create_app, log  # noqa: E402

port = config.get_int('APP_PORT', 5420)
app = create_app()
log.info('PhoneLight %s (dev) on http://0.0.0.0:%d', config.version(), port)
WSGIServer(('0.0.0.0', port), app, log=None).serve_forever()
