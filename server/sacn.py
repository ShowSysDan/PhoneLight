"""sACN (ANSI E1.31) receiver: unicast only, one universe, HTP merge.

The console sends unicast to this server's UDP port 5568, so there is no
multicast group to join; we bind the port and read every datagram. Packets
for other universes are only counted (the sACN page lists them, which shows a
console patched to the wrong universe at a glance).

Merge, per E1.31: among the sources heard in the last SOURCE_TIMEOUT seconds,
only those at the highest priority count, and those are merged HTP (highest
value per slot wins). A source that sends Stream_Terminated leaves at once.
Preview packets and non-zero start codes are ignored.

`Receiver.on_frame(levels, live)` is called after every accepted packet (and
when the last source drops) with the merged 512 levels.
"""
import logging
import socket
import struct
import threading
import time

log = logging.getLogger('phonelight.sacn')

PORT = 5568
SOURCE_TIMEOUT = 2.5            # E1.31 network data loss
_ACN_ID = b'ASC-E1.17\x00\x00\x00'
_VECTOR_ROOT_DATA = 0x00000004
_VECTOR_FRAMING_DATA = 0x00000002
_OPT_PREVIEW = 0x80
_OPT_TERMINATED = 0x40


class Packet:
    __slots__ = ('cid', 'name', 'priority', 'sequence', 'terminated', 'universe', 'data')

    def __init__(self, cid, name, priority, sequence, terminated, universe, data):
        self.cid, self.name, self.priority = cid, name, priority
        self.sequence, self.terminated, self.universe, self.data = sequence, terminated, universe, data


def parse(buf):
    """bytes -> Packet, or None for anything that is not E1.31 DMX data we use."""
    if len(buf) < 126 or buf[4:16] != _ACN_ID:
        return None
    if struct.unpack_from('!I', buf, 18)[0] != _VECTOR_ROOT_DATA:
        return None                       # sync / discovery packets
    if struct.unpack_from('!I', buf, 40)[0] != _VECTOR_FRAMING_DATA:
        return None
    options = buf[112]
    if options & _OPT_PREVIEW:
        return None
    if buf[117] != 0x02 or buf[118] != 0xA1:
        return None
    count = struct.unpack_from('!H', buf, 123)[0]   # start code + slots
    if count < 1 or buf[125] != 0x00:               # 0xDD per-address priority etc.
        return None
    slots = min(count - 1, 512, len(buf) - 126)
    name = buf[44:108].split(b'\x00', 1)[0].decode('utf-8', 'replace')
    return Packet(cid=buf[22:38].hex(), name=name, priority=min(buf[108], 200),
                  sequence=buf[111], terminated=bool(options & _OPT_TERMINATED),
                  universe=struct.unpack_from('!H', buf, 113)[0], data=bytes(buf[126:126 + slots]))


class Source:
    def __init__(self, pkt, ip, now):
        self.cid, self.ip = pkt.cid, ip
        self.name, self.priority = pkt.name, pkt.priority
        self.sequence = pkt.sequence
        self.data = pkt.data
        self.first_seen = self.last_seen = now
        self.packets = 0
        self.out_of_order = 0
        self.fps = 0.0
        self._fps_at, self._fps_n = now, 0

    def accept(self, pkt, now):
        """False for a late / duplicate packet (E1.31 6.7.2 sequence rule)."""
        diff = (pkt.sequence - self.sequence) & 0xFF
        if diff == 0 or diff > 236:          # -20 < diff <= 0, mod 256
            self.out_of_order += 1
            return False
        self.sequence, self.name, self.priority, self.data = pkt.sequence, pkt.name, pkt.priority, pkt.data
        self.last_seen = now
        self.packets += 1
        self._fps_n += 1
        if now - self._fps_at >= 1.0:
            self.fps = self._fps_n / (now - self._fps_at)
            self._fps_at, self._fps_n = now, 0
        return True

    def info(self, now):
        return dict(cid=self.cid, ip=self.ip, name=self.name, priority=self.priority,
                    fps=round(self.fps if now - self._fps_at < 2 else 0, 1), packets=self.packets,
                    out_of_order=self.out_of_order, age=round(now - self.last_seen, 2))


class Merger:
    """Sources of one universe -> merged levels. Not thread-safe by itself;
    the Receiver calls it from its one reader thread (and holds `lock` so
    the admin feed can read a consistent snapshot)."""

    def __init__(self):
        self.sources = {}
        self.levels = bytes(512)
        self.lock = threading.Lock()

    def expire(self, now):
        gone = [cid for cid, s in self.sources.items() if now - s.last_seen > SOURCE_TIMEOUT]
        for cid in gone:
            log.info('sACN source %s (%s) timed out', self.sources[cid].name, self.sources[cid].ip)
            del self.sources[cid]
        return bool(gone)

    def merge(self):
        if not self.sources:
            return False
        top = max(s.priority for s in self.sources.values())
        winners = [s.data for s in self.sources.values() if s.priority == top]
        if len(winners) == 1:
            merged = winners[0].ljust(512, b'\x00')
        else:
            merged = bytes(max(col) for col in zip(*(w.ljust(512, b'\x00') for w in winners)))
        self.levels = merged
        return True

    def feed(self, pkt, ip, now):
        """True when the merged levels may have changed."""
        with self.lock:
            src = self.sources.get(pkt.cid)
            if pkt.terminated:
                if src:
                    log.info('sACN source %s (%s) terminated its stream', src.name, ip)
                    del self.sources[pkt.cid]
                    self.merge()
                    return True
                return False
            if src is None:
                self.sources[pkt.cid] = src = Source(pkt, ip, now)
                src.packets = 1
                log.info('sACN source %s (%s) priority %d', pkt.name, ip, pkt.priority)
            elif not src.accept(pkt, now):
                return False
            return self.merge()

    def clear(self):
        with self.lock:
            self.sources.clear()
            self.levels = bytes(512)


class Receiver:
    def __init__(self, get_universe, on_frame, bind='0.0.0.0', port=PORT):
        self.get_universe, self.on_frame = get_universe, on_frame
        self.bind, self.port = bind, port
        self.merger = Merger()
        self.error = None
        self.packets = 0
        self.ignored = 0               # not E1.31 data / preview / other start code
        self.other_universes = {}      # universe -> {ip, last_seen, packets}
        self._sock = None
        self._stop = False

    def start(self):
        threading.Thread(target=self._run, name='sacn', daemon=True).start()
        threading.Thread(target=self._watchdog, name='sacn-watchdog', daemon=True).start()

    def stop(self):
        self._stop = True
        if self._sock:
            self._sock.close()

    def _open(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        sock.bind((self.bind, self.port))
        return sock

    def _run(self):
        while not self._stop:
            try:
                self._sock = self._open()
                self.error = None
                log.info('sACN listening on %s:%d (unicast)', self.bind, self.port)
                self._read(self._sock)
            except OSError as exc:
                if self._stop:
                    return
                self.error = f'cannot listen on UDP {self.bind}:{self.port}: {exc}'
                log.error('sACN %s; retrying in 5 s', self.error)
                time.sleep(5)

    def _read(self, sock):
        while not self._stop:
            buf, addr = sock.recvfrom(1144)
            self.handle(buf, addr[0])

    def handle(self, buf, ip, now=None):
        now = time.monotonic() if now is None else now
        self.packets += 1
        pkt = parse(buf)
        if pkt is None:
            self.ignored += 1
            return
        if pkt.universe != self.get_universe():
            seen = self.other_universes.setdefault(pkt.universe, {'packets': 0})
            seen.update(ip=ip, last_seen=now)
            seen['packets'] += 1
            return
        if self.merger.feed(pkt, ip, now):
            self.on_frame(self.merger.levels, bool(self.merger.sources))

    def _watchdog(self):
        """Drop silent sources (no packet arrives to notice they are gone)."""
        while not self._stop:
            time.sleep(0.25)
            now = time.monotonic()
            with self.merger.lock:
                gone = self.merger.expire(now)
                if gone:
                    self.merger.merge()
            if gone:
                self.on_frame(self.merger.levels, bool(self.merger.sources))
            for u in [u for u, s in self.other_universes.items() if now - s['last_seen'] > 10]:
                del self.other_universes[u]

    def universe_changed(self):
        self.merger.clear()
        self.other_universes.clear()
        self.on_frame(self.merger.levels, False)

    def snapshot(self):
        now = time.monotonic()
        with self.merger.lock:
            sources = [s.info(now) for s in self.merger.sources.values()]
            levels = self.merger.levels
        return dict(sources=sources, levels=list(levels), packets=self.packets, ignored=self.ignored,
                    error=self.error, listen=f'{self.bind}:{self.port}',
                    other_universes=[dict(universe=u, ip=s['ip'], packets=s['packets'])
                                     for u, s in sorted(self.other_universes.items())])
