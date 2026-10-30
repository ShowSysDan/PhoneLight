"""Levels -> each fixture's colours (one RGB per cell) -> hub.

Called straight from the sACN reader on every accepted packet (no queue, no
extra hop), so a console change reaches the WebSocket senders in the same
pass. A 40 Hz ticker runs what no packet drives: the signal-loss
hold/fade and the test patterns.

Modes (admin Live page, in memory only, back to 'sacn' on restart):
  sacn      the console (with the loss hold/fade)
  color     manual colours: one for all fixtures, or set fixture by fixture
  pattern   a test pattern made here (chase / flash / rainbow)
  identify  each fixture a different hue, to check groups
  blackout  black
The master dimmer scales every mode. The test modes are generated inside
the engine: PhoneLight never transmits sACN.

A fixture's colour is `cells` RGB triples in one bytes object (R G B R G B
...; 3 bytes with 1 cell): what its phones show, one triple per screen cell.
With the Phone flash setting on, the published bytes end in one more byte:
255 = the camera flash on, 0 = off (the channel after the colours, on at
128+; in color mode set from the Live page; the flash pattern flashes it too).
"""
import colorsys
import threading
import time

from . import settings

MODES = ('sacn', 'color', 'pattern', 'identify', 'blackout')
PATTERNS = ('chase', 'flash', 'rainbow')
TICK = 1 / 40
BLACK = b'\x00\x00\x00'


def fixture_colors(levels, values):
    """Merged 512 levels -> bytes(r, g, b * cells) per fixture (16-bit rounded to 8).
    With the flash on, each fixture's last channel is its flash (fixture_flash)."""
    start, n, cells, fl = values['start_address'] - 1, values['fixtures'], values['cells'], values.get('flash', 0)
    if values['bit_depth'] == 16:
        w = cells * 6 + fl
        return [bytes(((levels[b + 2 * k] << 8 | levels[b + 2 * k + 1]) * 255 + 32767) // 65535
                      for k in range(cells * 3)) for b in range(start, start + n * w, w)]
    w = cells * 3 + fl
    return [bytes(levels[b:b + w - fl]) for b in range(start, start + n * w, w)]


def fixture_flash(levels, values):
    """Merged 512 levels -> per fixture, is its flash channel (after its colours) at 128+?"""
    w, start = settings.channels_per_fixture(values), values['start_address'] - 1
    return [levels[b + w - 1] >= 128 for b in range(start, start + values['fixtures'] * w, w)]


def fit(colors, n, cells):
    """Exactly n fixtures of `cells` RGB each: missing fixtures / cells black, extras dropped."""
    w = cells * 3
    return [(c + bytes(w))[:w] for c in (colors + [b''] * n)[:n]]


def _hue(h):
    return bytes(round(c * 255) for c in colorsys.hsv_to_rgb(h % 1, 1, 1))


def identify_colors(n, cells=1):
    """Each fixture its own hue, on all its cells."""
    return [_hue(i * 0.618034) * cells for i in range(n)]


def pattern_colors(pattern, color, rate, n, t, cells=1):
    """Test pattern at t seconds over n fixtures × cells. rate: steps (chase,
    one cell at a time, fixture 1 cell 1 first), flashes (flash) or
    colour-wheel turns (rainbow) per second."""
    if pattern == 'flash':
        return [(color if int(t * rate * 2) % 2 == 0 else BLACK) * cells] * n
    total = n * cells
    if pattern == 'chase':
        on = int(t * rate) % total
        px = [color if i == on else BLACK for i in range(total)]
    else:
        px = [_hue(t * rate + i / total) for i in range(total)]
    return [b''.join(px[i:i + cells]) for i in range(0, total, cells)]


def scale(colors, factor):
    if factor >= 1:
        return colors
    return [bytes(round(c * factor) for c in rgb) for rgb in colors]


class Engine:
    def __init__(self, publish):
        self.publish = publish          # fn([bytes(3 * cells)] per fixture)
        self.lock = threading.Lock()
        self.levels = bytes(512)
        self.live = False
        self.lost_at = None             # monotonic time the signal was lost
        self.ever_live = False
        self.mode = 'sacn'
        self.manual = []                # 'color' mode: each fixture's colours
        self.manual_flash = []          # 'color' mode: each fixture's flash (True = on)
        self.pattern, self.pattern_color, self.pattern_rate = 'chase', b'\xff\xff\xff', 2.0
        self._pattern_t0 = time.monotonic()
        self.output = []
        self._raw = []                  # output before the master dimmer
        self.lit = []                   # per fixture: had any sACN level since the signal came (patch check)
        self.frames = 0                 # frames published (changed output)
        self._fps, self._fps_at, self._fps_n = 0.0, time.monotonic(), 0
        self._stop = False

    # ── inputs ─────────────────────────────────────────────────────────────
    def on_frame(self, levels, live):
        with self.lock:
            self.levels = levels
            if live:
                if not self.live:
                    self.lit = []               # a new signal: check the patch afresh
                self.ever_live, self.lost_at = True, None
            elif self.live:
                self.lost_at = time.monotonic()
            self.live = live
            if live or self.mode != 'sacn':
                self._compute()
            # not live: the ticker runs the hold / fade from the last look

    def set_mode(self, mode, color=None, fixture=None, pattern=None, rate=None, cell=None, flash=None):
        """color mode: `color` (one RGB) on every fixture, or only on `fixture`
        (1-based; the others keep what they show now), or only on that
        fixture's `cell` (1-based); `flash` (True / False) the same way, all
        fixtures or `fixture`. pattern mode: `pattern`, `color`, `rate`
        (each optional, the last ones are kept)."""
        if mode not in MODES:
            raise ValueError(f'unknown mode {mode!r}')
        if pattern is not None and pattern not in PATTERNS:
            raise ValueError(f'unknown pattern {pattern!r}')
        if rate is not None and not 0.05 <= rate <= 20:
            raise ValueError('rate must be 0.05–20 per second')
        v = settings.current()
        n, cells = v['fixtures'], v['cells']
        if fixture is not None and not 1 <= fixture <= n:
            raise ValueError(f'fixture must be 1–{n}')
        if cell is not None and (fixture is None or not 1 <= cell <= cells):
            raise ValueError(f'cell must be 1–{cells}, with a fixture')
        with self.lock:
            if mode == 'color':
                if self.mode != 'color':
                    self.manual = list(self._raw)       # start from what the phones show now
                    self.manual_flash = []
                self.manual = fit(self.manual, n, cells)
                self.manual_flash = (self.manual_flash + [False] * n)[:n]
                if flash is not None:
                    if fixture is None:
                        self.manual_flash = [bool(flash)] * n
                    else:
                        self.manual_flash[fixture - 1] = bool(flash)
                if color is not None:
                    if fixture is None:
                        self.manual = [color * cells] * n
                    elif cell is None:
                        self.manual[fixture - 1] = color * cells
                    else:
                        c = bytearray(self.manual[fixture - 1])
                        c[(cell - 1) * 3:cell * 3] = color
                        self.manual[fixture - 1] = bytes(c)
            elif mode == 'pattern':
                self.pattern = pattern or self.pattern
                self.pattern_color = color or self.pattern_color
                self.pattern_rate = rate or self.pattern_rate
                if self.mode != 'pattern':
                    self._pattern_t0 = time.monotonic()
            self.mode = mode
            self._compute()

    def recompute(self, *_):
        """After a settings change: publish even if the output looks the same
        (the hub may have dropped a frame that arrived mid-change)."""
        with self.lock:
            self.output = None
            self._compute()

    # ── output ─────────────────────────────────────────────────────────────
    def signal(self, now=None):
        """'live' | 'hold' | 'fade' | 'lost' | 'none' (never heard)."""
        if self.live:
            return 'live'
        if not self.ever_live:
            return 'none'
        v = settings.current()
        elapsed = (now or time.monotonic()) - (self.lost_at or 0)
        if elapsed < v['loss_hold']:
            return 'hold'
        return 'fade' if elapsed < v['loss_hold'] + v['loss_fade'] else 'lost'

    def _loss_factor(self, v, now):
        if self.live:
            return 1.0
        if not self.ever_live or self.lost_at is None:
            return 0.0
        elapsed = now - self.lost_at - v['loss_hold']
        if elapsed <= 0:
            return 1.0
        return 0.0 if elapsed >= v['loss_fade'] else 1 - elapsed / v['loss_fade']

    def _compute(self, now=None):
        now = now or time.monotonic()
        v = settings.current()
        n, cells = v['fixtures'], v['cells']
        flash = None
        if self.mode == 'blackout':
            colors, factor = [BLACK * cells] * n, 1.0
        elif self.mode == 'color':
            colors, factor = fit(self.manual, n, cells), 1.0
            flash = (self.manual_flash + [False] * n)[:n]
        elif self.mode == 'pattern':
            colors, factor = pattern_colors(self.pattern, self.pattern_color, self.pattern_rate, n,
                                            now - self._pattern_t0, cells), 1.0
            if self.pattern == 'flash':
                flash = [any(c) for c in colors]
        elif self.mode == 'identify':
            colors, factor = identify_colors(n, cells), 1.0
        else:
            colors, factor = fixture_colors(self.levels, v), self._loss_factor(v, now)
            if v['flash']:
                flash = fixture_flash(self.levels, v)
            if self.live:
                lit = (self.lit + [False] * n)[:n]
                self.lit = [was or any(rgb) for was, rgb in zip(lit, colors)]
        self._raw = colors
        factor *= v['master'] / 100
        colors = scale(colors, factor)
        if v['flash']:                  # off with the master at 0 and once a lost signal has faded out
            on = flash if flash and factor > 0 else [False] * n
            colors = [rgb + (b'\xff' if f else b'\x00') for rgb, f in zip(colors, on)]
        if colors != self.output:
            self.output = colors
            self.frames += 1
            self._fps_n += 1
            self.publish(colors)

    def fps(self):
        return round(self._fps if time.monotonic() - self._fps_at < 2 else 0, 1)

    def start(self):
        threading.Thread(target=self._tick, name='engine-tick', daemon=True).start()

    def stop(self):
        self._stop = True

    def _tick(self):
        while not self._stop:
            time.sleep(TICK)
            now = time.monotonic()
            if now - self._fps_at >= 1:
                self._fps, self._fps_at, self._fps_n = self._fps_n / (now - self._fps_at), now, 0
            if self.mode == 'pattern' or (not self.live and self.mode == 'sacn' and self.lost_at is not None):
                with self.lock:
                    if self.mode == 'pattern' or not self.live:
                        self._compute(now)

    def snapshot(self):
        out = self.output or []
        w = len(out[0]) // 3 * 3 if out else 0     # colour bytes; a byte past them is the flash
        return dict(mode=self.mode, pattern=self.pattern, pattern_color='#' + self.pattern_color.hex(),
                    pattern_rate=self.pattern_rate, signal=self.signal(),
                    fps=self.fps(), frames=self.frames,
                    # per fixture, its cells' colours
                    fixtures=[['#' + c[k:k + 3].hex() for k in range(0, w, 3)] for c in out],
                    flash=[bool(c[w]) for c in out] if out and len(out[0]) > w else None,
                    # fixtures whose channels have stayed at 0 since sACN came (1-based; only while live)
                    unlit=[i + 1 for i, was in enumerate(self.lit) if not was] if self.live else [])
