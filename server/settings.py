"""Show settings: defined ONLY here (label, range, default, help).

Stored in the `settings` table, cached in memory (the sACN path reads them on
every packet), changed from the admin Settings page through `save()`, which
validates the whole set before writing and then tells the listeners
(engine/hub) so a change applies live without a restart.
"""
import threading

from . import db

DEFS = {
    'universe': dict(label='sACN universe', type=int, min=1, max=63999, default=1,
                     help='The universe the console sends to this server (unicast).'),
    'start_address': dict(label='Start address', type=int, min=1, max=512, default=1,
                          help='DMX address of fixture 1. Fixtures follow on in order.'),
    'fixtures': dict(label='Fixtures', type=int, min=1, max=170, default=3,
                     help='Guests are split evenly across this many fixtures (groups).'),
    'bit_depth': dict(label='Fixture mode', type=int, choices=(8, 16), default=8,
                      help='8-bit: R G B (3 channels). 16-bit: R R-fine G G-fine B B-fine (6 channels).'),
    'cells': dict(label='Cells per fixture', type=int, min=1, max=40, default=1,
                  help='Each phone\'s screen is split into this many cells, each its own RGB (a pixel bar '
                       'per phone). A fixture is then cells × 3 channels (× 6 in 16-bit). 1 = the whole '
                       'screen one colour.'),
    'cell_columns': dict(label='Cell columns', type=int, min=1, max=40, default=1,
                         help='How the cells sit on the screen: this many side by side, the rest in rows '
                              'below, cell 1 top left. 1 = stacked top to bottom; the same as the cell '
                              'count = side by side.'),
    'master': dict(label='Master dimmer %', type=int, min=0, max=100, default=100,
                   help='Scales every colour sent to phones.'),
    'sync_delay': dict(label='Sync delay (ms)', type=int, min=0, max=1000, default=100,
                       help='Every phone shows each change this long after the console sends it, so '
                            'phones with less latency than this change together. 0 = as fast as '
                            'possible, no sync.'),
    'loss_hold': dict(label='Signal-loss hold (s)', type=float, min=0, max=60, default=1,
                      help='sACN counts as lost after 2.5 s of silence (at once if the console ends '
                           'its stream). Phones then hold the last look this long...'),
    'loss_fade': dict(label='Signal-loss fade (s)', type=float, min=0, max=60, default=2,
                      help='...then fade to black over this long.'),
    'retention_days': dict(label='Keep client data (days)', type=int, min=0, max=3650, default=30,
                           help='Client and session rows older than this are deleted hourly. 0 = keep forever.'),
}

_lock = threading.Lock()
_cache = None
_listeners = []


def channels_per_fixture(values=None):
    values = values or current()
    return (6 if values['bit_depth'] == 16 else 3) * values['cells']


def _coerce(key, raw):
    d = DEFS[key]
    try:
        value = d['type'](raw)
    except (TypeError, ValueError):
        raise ValueError(f'{d["label"]}: not a number')
    if 'choices' in d and value not in d['choices']:
        raise ValueError(f'{d["label"]}: must be one of {", ".join(map(str, d["choices"]))}')
    if 'min' in d and not d['min'] <= value <= d['max']:
        raise ValueError(f'{d["label"]}: must be {d["min"]}–{d["max"]}')
    return value


def validate(values):
    """Whole-set checks: every fixture must fit in the 512 slots."""
    last = values['start_address'] - 1 + values['fixtures'] * channels_per_fixture(values)
    if last > 512:
        raise ValueError(f'{values["fixtures"]} fixtures × {channels_per_fixture(values)} channels '
                         f'from address {values["start_address"]} ends at {last}, past 512')


def load():
    global _cache
    values = {k: d['default'] for k, d in DEFS.items()}
    for row in db.query('SELECT key, value FROM settings'):
        if row['key'] in DEFS:
            try:
                values[row['key']] = _coerce(row['key'], row['value'])
            except ValueError:
                pass  # out of range after a definition change: keep the default
    with _lock:
        _cache = values
    return values


def current():
    return _cache if _cache is not None else load()


def save(changes):
    """Validate and store {key: raw}; returns the new values or raises ValueError."""
    global _cache
    values = dict(current())
    for key, raw in changes.items():
        if key in DEFS:
            values[key] = _coerce(key, raw)
    validate(values)
    stamp = db.now()
    db.run_batch([('INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) '
                   'ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at',
                   (k, str(values[k]), stamp)) for k in DEFS])
    old = current()
    with _lock:
        _cache = values
    for fn in _listeners:
        fn(old, values)
    return values


def on_change(fn):
    """fn(old, new) after every save."""
    _listeners.append(fn)
