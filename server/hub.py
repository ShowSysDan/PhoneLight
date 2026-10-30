"""Connected guests: fixture assignment and the colour fan-out.

Frames are LATEST-WINS, never queued: memory per guest is fixed whatever the
frame rate, and a slow phone skips stale frames instead of falling behind.

Fast path (every phone keeping up): `publish()` builds each changed
fixture's WebSocket frame ONCE (`frame()`: a complete, unmasked binary
frame, 9 bytes with one cell) and writes it straight into each of that fixture's sockets
with one non-blocking send. No wake-ups, no task switches, no trip through
the WebSocket library: 1000 phones cost 1000 small syscalls per frame.

Slow path (a phone's socket buffer is full, or its sender is mid-write):
the guest is flagged `dirty` and its own sender (`serve()`, one per
WebSocket) is woken; when it next gets to run it sends whatever is CURRENT,
so every frame published while it was stuck is dropped, not queued. A
partial write leaves the rest of that one frame in `pending`, which goes
first so the stream stays valid.

Exactly one writer at a time per socket: the publisher only writes when the
guest's sender is idle (`busy` False, nothing `pending`); gevent switches
tasks only when one blocks, so those flags cannot race.

Sync (all phones change together): every colour frame carries "show at
server time T" (T = publish time + `delay_ms`, the Sync delay setting). Each
phone estimates its clock's offset from the server's with NTP-style
exchanges ('s' messages: its send time; our receive and send times) and
paints the frame at T on its own clock. A phone whose latency is above the
delay shows the frame on arrival. delay_ms 0 = no waiting, show on arrival.
Server time is `server_ms()`: monotonic milliseconds (never jumps).

Cells: a fixture can have several cells (Settings), each its own RGB; each
of its phones splits its screen into them. The frame then carries every
cell's RGB, still one frame per fixture.

Flash (the Phone flash setting): the frame carries one more byte after the
colours, 255 = camera flash on, 0 = off. The layout message says so.

Wire format (kept tiny on purpose):
  server -> guest   binary          R, G, B for each cell (cell 1 first; 1 cell = 3 bytes)
                                    + show-at server ms (uint32 BE, mod 2^32): 7 bytes with 1 cell
                    text 'p<n>'     ping; the guest echoes it back
                    text 'f<n>'     your fixture is n (kept for reconnects)
                    text 'l<cells>,<columns>'  screen layout: that many cells, that many side
                                    by side, the rest in rows below (sent before colours)
                    text 'l<cells>,<columns>,<max>'  the same, and frames carry the flash byte;
                                    max: seconds a phone keeps its flash on at most (0 = no limit)
                    text 'x'        you are blocked: the server closes, don't reconnect
                    text 'k'        you were disconnected by the admin (reconnect as usual)
                    text 's<c0>,<t1>,<t2>'  clock sync reply: the guest's send time
                                    c0, our receive time t1 and send time t2 (ms)
  guest -> server   text JSON       hello (first message): id + device info
                    text 'p<n>'     pong
                    text 's<c0>'    clock sync request (c0: its performance.now())
                    text 't1' / 't0'  its camera flash is ready / not (any more)
"""
import random
import re
import threading
import time

from . import proxy

PING_INTERVAL = 5.0
PING_TIMEOUT = 20.0          # no pong this long -> the socket is dead, close it
IDLE_WAKE = 1.0              # a sender with nothing to do still looks this often (pings)
SEND_TIMEOUT = 10.0          # one write blocked this long (phone stopped reading) -> close it
REAP_EVERY = 1.0


def server_ms():
    return time.monotonic() * 1000


def frame(rgb, show_at=0):
    """Colours (3 bytes per cell, at most 40 cells, + the flash byte) +
    show-at server ms -> a whole WebSocket frame (FIN + binary, one-byte
    length: 40 cells + flash + 4 = 125, the most it holds). A show-at in the
    past (or 0) means "now"."""
    return b'\x82' + bytes((len(rgb) + 4,)) + rgb + (int(show_at) & 0xFFFFFFFF).to_bytes(4, 'big')


BLACK = frame(b'\x00\x00\x00')
_SYNC = re.compile(r'^s\d{1,12}(\.\d{1,4})?$')


class Guest:
    __slots__ = ('sid', 'client_id', 'ip', 'info', 'fixture', 'told', 'told_layout', 'connected_at', 'raw', 'wake',
                 'closer', 'dirty', 'busy', 'busy_since', 'pending', 'ping_seq', 'ping_at', 'last_pong',
                 'lat_n', 'lat_sum', 'lat_min', 'lat_max', 'lat_last', 'alive', 'why', 'sync', 'bye', 'torch')

    def __init__(self, sid, client_id, ip, info, raw=None, closer=None):
        self.sid, self.client_id, self.ip, self.info = sid, client_id, ip, info
        self.fixture, self.told, self.told_layout = 0, None, None
        self.connected_at = time.time()
        self.raw = raw                    # non-blocking OS socket for the fast path, or None
        self.closer = closer              # fn() that closes the socket (unblocks a stuck write)
        self.wake = threading.Event()
        self.dirty, self.busy, self.busy_since, self.pending = True, False, 0.0, None
        self.ping_seq, self.ping_at = 0, None
        self.last_pong = time.monotonic()
        self.lat_n, self.lat_sum, self.lat_min, self.lat_max, self.lat_last = 0, 0.0, None, None, None
        self.alive, self.why = True, None   # why: set when the server ends it
        self.sync = None                    # (c0 text, t1) of an unanswered sync request
        self.bye = None                     # last message before the server closes ('x' / 'k'), see Hub.kick
        self.torch = False                  # its camera flash is ready ('t1')

    def pong(self, text, now=None):
        """Record a pong; returns the round trip in ms or None."""
        now = now or time.monotonic()
        try:
            seq = int(text[1:])
        except ValueError:
            return None
        self.last_pong = now
        if seq != self.ping_seq or self.ping_at is None:
            return None
        rtt = (now - self.ping_at) * 1000
        self.ping_at = None
        self.lat_n += 1
        self.lat_sum += rtt
        self.lat_last = rtt
        self.lat_min = rtt if self.lat_min is None else min(self.lat_min, rtt)
        self.lat_max = rtt if self.lat_max is None else max(self.lat_max, rtt)
        return rtt

    def latency_avg(self):
        return self.lat_sum / self.lat_n if self.lat_n else None


def _write_now(g, fr):
    """Fast path: one non-blocking send of a whole frame. False = left to the
    guest's sender (slow phone, sender busy, no raw socket, or dead)."""
    if g.busy or g.pending or g.raw is None:
        g.dirty = True
        return False
    try:
        n = g.raw.send(fr)
    except (BlockingIOError, InterruptedError):
        n = 0
    except OSError:
        g.alive = False
        return False
    if n == len(fr):
        g.dirty = False
        return True
    if n:
        g.pending = fr[n:]                    # finish this frame first, then the latest
    g.dirty = True
    return False


class Hub:
    def __init__(self, fixtures, cells=1, columns=1, flash=None):
        self.lock = threading.Lock()
        self.guests = {}                          # sid -> Guest
        self.members = [set() for _ in range(fixtures)]
        self.layout = (cells, columns, flash)     # flash: None = off, else its longest-on seconds
        self.width = 3 * cells + (flash is not None)   # bytes per fixture from the engine
        self.black = frame(bytes(self.width))
        self.frames = [self.black] * fixtures    # pre-built colour frame per fixture
        self.total_connects = 0
        self.delay_ms = 0                         # Sync delay (settings), see module doc
        self.max_per_ip = 0                       # MAX_PER_IP (.env); 0 = no limit
        self.per_ip_exempt = ()                   # networks the limit skips (a venue's shared address)
        self.per_ip = {}                          # ip -> connected guests (only while > 0)
        self.refused_full = 0                     # connects refused by MAX_PER_IP

    # ── membership ─────────────────────────────────────────────────────────
    def _limited(self, ip):
        if not self.max_per_ip or self.per_ip.get(ip, 0) < self.max_per_ip:
            return False
        return not proxy.contains(self.per_ip_exempt, ip)

    def join(self, guest, preferred=None):
        """Add to the emptiest fixture; keep `preferred` (its last one) when
        that is one of the emptiest, so a reconnect does not change colour.
        None (not added) when its address already holds MAX_PER_IP sockets."""
        with self.lock:
            if self._limited(guest.ip):
                self.refused_full += 1
                return None
            self.per_ip[guest.ip] = self.per_ip.get(guest.ip, 0) + 1
            counts = [len(m) for m in self.members]
            low = min(counts)
            emptiest = [i for i, c in enumerate(counts) if c == low]
            guest.fixture = preferred if preferred in emptiest else random.choice(emptiest)
            self.members[guest.fixture].add(guest)
            self.guests[guest.sid] = guest
            self.total_connects += 1
            guest.dirty = True
        guest.wake.set()
        return guest.fixture

    def leave(self, guest):
        with self.lock:
            guest.alive = False
            if self.guests.pop(guest.sid, None) is not None:
                n = self.per_ip.get(guest.ip, 0) - 1
                if n > 0:
                    self.per_ip[guest.ip] = n
                else:
                    self.per_ip.pop(guest.ip, None)
            for m in self.members:
                m.discard(guest)
        guest.wake.set()

    def resize(self, fixtures):
        """New fixture count: deal everyone out again, evenly."""
        with self.lock:
            self.members = [set() for _ in range(fixtures)]
            self.frames = (self.frames + [self.black] * fixtures)[:fixtures]
        self.rebalance()

    def set_layout(self, cells, columns, flash=None):
        """New cell count / columns / flash: every phone is told, then gets its
        colours again (black until the engine publishes the new size)."""
        with self.lock:
            width = 3 * cells + (flash is not None)
            if width != self.width:
                self.width = width
                self.black = frame(bytes(width))
                self.frames = [self.black] * len(self.frames)
            self.layout = (cells, columns, flash)
            guests = list(self.guests.values())
            for g in guests:
                g.dirty = True
        for g in guests:
            g.wake.set()

    def rebalance(self):
        with self.lock:
            guests = list(self.guests.values())
            random.shuffle(guests)
            n = len(self.members)
            self.members = [set() for _ in range(n)]
            for i, g in enumerate(guests):
                g.fixture = i % n
                self.members[g.fixture].add(g)
                g.dirty = True
        for g in guests:
            g.wake.set()

    # ── fan-out ────────────────────────────────────────────────────────────
    def publish(self, colors):
        """Engine output -> every guest whose fixture changed (see module doc)."""
        slow, show_at = [], None
        with self.lock:
            if len(colors) != len(self.members) or len(colors[0]) != self.width:
                return                        # mid-resize; the engine republishes after it
            layout = self.layout
            for i, rgb in enumerate(colors):
                if rgb != self.frames[i][2:-4]:
                    if show_at is None:
                        show_at = server_ms() + self.delay_ms
                    fr = self.frames[i] = frame(rgb, show_at)
                    for g in self.members[i]:
                        # a phone not yet told this layout gets it from its sender first, then the colour
                        if g.told_layout is not layout:
                            g.dirty = True
                            slow.append(g)
                        elif not _write_now(g, fr):
                            slow.append(g)
        for g in slow:
            g.wake.set()

    def serve(self, guest, send_bytes, send_text):
        """The guest's sender loop (runs in its WebSocket handler) until the
        guest leaves; returns why it ended. send_bytes writes raw bytes to
        the socket, send_text sends a WebSocket text message."""
        while guest.alive:
            if guest.pending:
                data, guest.pending = guest.pending, None
                msg = data
            else:
                msg = self.next_message(guest)
            if msg is not None:
                guest.busy, guest.busy_since = True, time.monotonic()
                try:
                    (send_bytes if isinstance(msg, bytes) else send_text)(msg)
                finally:
                    guest.busy = False
                continue
            if self.is_dead(guest):
                return 'ping timeout'
            guest.wake.wait(IDLE_WAKE)
            guest.wake.clear()
        return guest.why or 'closed by guest'

    def next_message(self, guest, now=None):
        """What the guest's sender should send now: bytes (a pre-built colour
        frame, written to the socket as-is), str (sent as a WebSocket text
        message) or None. Colour first: it is what the guest is there for."""
        now = now or time.monotonic()
        if guest.bye is not None:                 # kicked / blocked: say so, then the sender ends
            guest.alive = False
            return guest.bye
        if guest.sync is not None:                # time-critical: t2 is stamped right here
            (c0, t1), guest.sync = guest.sync, None
            return f's{c0},{t1:.1f},{server_ms():.1f}'
        if guest.told != guest.fixture:
            guest.told = guest.fixture
            return f'f{guest.fixture + 1}'
        if guest.told_layout != self.layout:
            guest.told_layout = layout = self.layout
            cells, columns, flash = layout
            return f'l{cells},{columns}' if flash is None else f'l{cells},{columns},{flash:g}'
        if guest.dirty:
            guest.dirty = False
            frames = self.frames
            return frames[guest.fixture] if guest.fixture < len(frames) else self.black
        if guest.ping_at is None and now - guest.last_pong >= PING_INTERVAL:
            guest.ping_seq = (guest.ping_seq + 1) % 1000000
            guest.ping_at = now
            return f'p{guest.ping_seq}'
        return None

    def sync_request(self, guest, text):
        """'s<c0>' from the guest's reader: stamp t1 now, the sender answers."""
        if _SYNC.match(text):
            guest.sync = (text[1:], server_ms())
            guest.wake.set()

    def torch(self, guest, text):
        """'t1' / 't0' from the guest's reader: its flash is ready or not."""
        guest.torch = text == 't1'

    def is_dead(self, guest, now=None):
        now = now or time.monotonic()
        return guest.ping_at is not None and now - guest.ping_at > PING_TIMEOUT

    def reap(self, now=None):
        """Close guests whose socket is stuck (a write blocked SEND_TIMEOUT:
        the phone stopped reading) or silent (no pong for PING_TIMEOUT). A
        stuck sender cannot notice by itself, so this runs on its own."""
        now = now or time.monotonic()
        with self.lock:
            guests = list(self.guests.values())
        for g in guests:
            stalled = g.busy and now - g.busy_since > SEND_TIMEOUT
            if stalled or self.is_dead(g, now):
                g.why = 'stalled (not reading)' if stalled else 'ping timeout'
                g.alive = False
                g.wake.set()
                if g.closer:
                    try:
                        g.closer()
                    except OSError:
                        pass

    def kick(self, match, bye='k', why='kicked by admin'):
        """Disconnect every guest for which match(guest) is true; returns how
        many. Its own sender sends `bye` and ends (one writer per socket); a
        sender stuck mid-write is closed by the reaper as usual."""
        with self.lock:
            guests = [g for g in self.guests.values() if match(g)]
        for g in guests:
            g.bye, g.why = bye, why
            g.wake.set()
        return len(guests)

    def start(self):
        threading.Thread(target=self._reaper, name='hub-reaper', daemon=True).start()

    def _reaper(self):
        while True:
            time.sleep(REAP_EVERY)
            self.reap()

    # ── admin ──────────────────────────────────────────────────────────────
    def counts(self):
        with self.lock:
            return [len(m) for m in self.members]

    def snapshot(self, now=None, limit=500):
        """For the Clients page: totals, and the `limit` most recent guests."""
        now = now or time.time()
        with self.lock:
            guests = list(self.guests.values())
        lats = [g.latency_avg() for g in guests if g.lat_n]
        return dict(
            connected=len(guests), total_connects=self.total_connects, counts=self.counts(),
            refused_full=self.refused_full, torch=sum(g.torch for g in guests),
            latency_avg=round(sum(lats) / len(lats), 1) if lats else None,
            guests=[dict(sid=g.sid, client_id=g.client_id, ip=g.ip, fixture=g.fixture + 1,
                         device=g.info.get('device'), os=g.info.get('os'), browser=g.info.get('browser'),
                         seconds=round(now - g.connected_at), torch=g.torch,
                         latency=round(g.lat_last, 1) if g.lat_last is not None else None,
                         latency_avg=round(g.latency_avg(), 1) if g.lat_n else None)
                    for g in sorted(guests, key=lambda g: g.connected_at, reverse=True)[:limit]])
