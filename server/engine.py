"""Levels -> one RGB colour per fixture -> hub.

Called straight from the sACN reader on every accepted packet (no queue, no
extra hop), so a console change reaches the WebSocket senders in the same
pass. A 30 Hz ticker only runs the signal-loss hold/fade, which no packet
drives.

Modes (admin Live page, in memory only, back to 'sacn' on restart):
  sacn      the console (with the loss hold/fade)
  color     one colour on every fixture (test / override)
  identify  each fixture a different hue, to check groups
  blackout  black
The master dimmer scales every mode.
"""
import colorsys
import threading
import time

from . import settings

MODES = ('sacn', 'color', 'identify', 'blackout')
TICK = 1 / 30
BLACK = b'\x00\x00\x00'


def fixture_colors(levels, values):
    """Merged 512 levels -> [bytes(r, g, b)] per fixture (16-bit rounded to 8)."""
    out = []
    start = values['start_address'] - 1
    if values['bit_depth'] == 16:
        for i in range(values['fixtures']):
            b = start + i * 6
            out.append(bytes(((levels[b + 2 * k] << 8 | levels[b + 2 * k + 1]) * 255 + 32767) // 65535
                             for k in range(3)))
    else:
        for i in range(values['fixtures']):
            b = start + i * 3
            out.append(bytes(levels[b:b + 3]))
    return out


def identify_colors(n):
    return [bytes(round(c * 255) for c in colorsys.hsv_to_rgb((i * 0.618034) % 1, 1, 1)) for i in range(n)]


def scale(colors, factor):
    if factor >= 1:
        return colors
    return [bytes(round(c * factor) for c in rgb) for rgb in colors]


class Engine:
    def __init__(self, publish):
        self.publish = publish          # fn([bytes(3)] per fixture)
        self.lock = threading.Lock()
        self.levels = bytes(512)
        self.live = False
        self.lost_at = None             # monotonic time the signal was lost
        self.ever_live = False
        self.mode = 'sacn'
        self.override = BLACK
        self.output = []
        self.frames = 0                 # frames published (changed output)
        self._fps, self._fps_at, self._fps_n = 0.0, time.monotonic(), 0
        self._stop = False

    # ── inputs ─────────────────────────────────────────────────────────────
    def on_frame(self, levels, live):
        with self.lock:
            self.levels = levels
            if live:
                self.ever_live, self.lost_at = True, None
            elif self.live:
                self.lost_at = time.monotonic()
            self.live = live
            if live or self.mode != 'sacn':
                self._compute()
            # not live: the ticker runs the hold / fade from the last look

    def set_mode(self, mode, color=None):
        if mode not in MODES:
            raise ValueError(f'unknown mode {mode!r}')
        with self.lock:
            self.mode = mode
            if color is not None:
                self.override = bytes(color[:3])
            self._compute()

    def recompute(self, *_):
        with self.lock:
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
        n = v['fixtures']
        if self.mode == 'blackout':
            colors, factor = [BLACK] * n, 1.0
        elif self.mode == 'color':
            colors, factor = [self.override] * n, 1.0
        elif self.mode == 'identify':
            colors, factor = identify_colors(n), 1.0
        else:
            colors, factor = fixture_colors(self.levels, v), self._loss_factor(v, now)
        colors = scale(colors, factor * v['master'] / 100)
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
            if not self.live and self.mode == 'sacn' and self.lost_at is not None:
                with self.lock:
                    if not self.live:
                        self._compute(now)

    def snapshot(self):
        return dict(mode=self.mode, override='#' + self.override.hex(), signal=self.signal(),
                    fps=self.fps(), frames=self.frames,
                    fixtures=['#' + c.hex() for c in self.output])
