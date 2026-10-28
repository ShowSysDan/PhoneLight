"""sACN (ANSI E1.31) receiver: unicast only, one universe, HTP merge.

The console sends unicast to this server's UDP port 5568, so there is no
multicast group to join; we bind the port and read every datagram. Packets
for other universes are only counted (the sACN page lists them, which shows a
console patched to the wrong universe at a glance).

Merge, per E1.31: among the sources heard in the last SOURCE_TIMEOUT seconds,
only those at the highest priority count, and those are merged HTP (highest
value per slot wins). A source that sends Stream_Terminated leaves at once.
Preview packets and non-zero start codes are ignored.

sACN has no authentication: anyone who can send UDP to this port can drive
the phones. SACN_ALLOW (.env, IPs / networks) limits it to the console(s);
packets from anyone else are dropped before parsing and counted (`refused`).
At most MAX_SOURCES sources are held at once, so spoofed CIDs can't grow it.

Input is LATEST-WINS too: the reader takes everything waiting in the
socket at once and acts only on the newest packet of each source, so if the
app ever falls behind it skips stale levels instead of working through a
backlog (counted as `stale`). Timing for the sACN monitor: how long packets
waited in the kernel before we read them (SO_TIMESTAMPNS, Linux) and how
long the app took from reading them to handing the colours to the phones.

`Receiver.on_frame(levels, live)` is called after every batch that changed
the merged 512 levels (and when the last source drops).
"""
import ipaddress
import logging
import select
import socket
import struct
import sys
import threading
import time

log = logging.getLogger('phonelight.sacn')

PORT = 5568
SOURCE_TIMEOUT = 2.5            # E1.31 network data loss
DRAIN_MAX = 256                 # packets read per batch before yielding to the rest of the app
MAX_SOURCES = 64                # sources held at once; more (spoofed CIDs, a flood) are refused
# Kernel receive timestamps: Linux SO_TIMESTAMPNS (Python's socket module has no name for it;
# 35 on every common Linux architecture). Elsewhere: no wait timing, everything else the same.
_TS = getattr(socket, 'SO_TIMESTAMPNS', 35 if sys.platform.startswith('linux') else None)
_ACN_ID = b'ASC-E1.17\x00\x00\x00'
_VECTOR_ROOT_DATA = 0x00000004
_VECTOR_FRAMING_DATA = 0x00000002
_OPT_PREVIEW = 0x80
_OPT_TERMINATED = 0x40


def _in_networks(nets, ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


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
                if len(self.sources) >= MAX_SOURCES:
                    return False
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
    def __init__(self, get_universe, on_frame, bind='0.0.0.0', port=PORT, allow=None):
        self.get_universe, self.on_frame = get_universe, on_frame
        self.bind, self.port = bind, port
        self.allow = allow             # SACN_ALLOW networks, or None = every sender
        self._allowed = {}             # ip -> bool (cache, cleared when it grows)
        self.merger = Merger()
        self.error = None
        self.packets = 0
        self.ignored = 0               # not E1.31 data / preview / other start code
        self.refused = 0               # from an address not in SACN_ALLOW
        self.other_universes = {}      # universe -> {ip, last_seen, packets}
        self.stale = 0                 # older packets skipped because a newer one was waiting
        self.timing = dict(wait_avg=None, wait_max=None, proc_avg=None, proc_max=None, batch_max=0)
        self._acc = [0, 0.0, 0.0, 0, 0.0, 0.0, 0]  # waits n/sum/max, procs n/sum/max, batch max
        self._acc_at = time.monotonic()
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
        if _TS is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, _TS, 1)   # kernel receive time on every packet
            except OSError:
                pass
        sock.bind((self.bind, self.port))
        sock.setblocking(False)
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
        anc = socket.CMSG_SPACE(16) if _TS is not None else 0
        while not self._stop:
            batch = []
            while len(batch) < DRAIN_MAX:
                try:
                    buf, ancdata, _, addr = sock.recvmsg(1144, anc)
                except (BlockingIOError, InterruptedError):
                    break
                kernel_ts = None
                for level, kind, data in ancdata:
                    if kind == _TS and len(data) >= 8:          # struct timespec: 2 x long (8 or 4 bytes)
                        sec, nsec = struct.unpack_from('qq' if len(data) >= 16 else 'ii', data)
                        kernel_ts = sec + nsec / 1e9
                batch.append((buf, addr[0], kernel_ts))
            if batch:
                self.handle_batch(batch)
                if len(batch) == DRAIN_MAX:
                    time.sleep(0)              # a flood: let the phones' senders run between batches
            else:
                select.select([sock], [], [])  # wait for the next packet (cooperative under gevent)

    def handle(self, buf, ip, now=None):
        """One packet (tests, and any caller without a batch)."""
        self.handle_batch([(buf, ip, None)], now)

    def handle_batch(self, batch, now=None):
        """[(datagram, ip, kernel receive time or None)] read in one go: act
        on the newest packet of each source in our universe only."""
        t0 = time.perf_counter()
        wall = time.time()
        now = time.monotonic() if now is None else now
        universe = self.get_universe()
        newest = {}
        acc = self._acc
        for buf, ip, kernel_ts in batch:
            self.packets += 1
            if kernel_ts is not None:
                wait = max(0.0, wall - kernel_ts)
                acc[0] += 1
                acc[1] += wait
                acc[2] = max(acc[2], wait)
            if self.allow is not None and not self._sender_allowed(ip):
                self.refused += 1
                continue
            pkt = parse(buf)
            if pkt is None:
                self.ignored += 1
                continue
            if pkt.universe != universe:
                seen = self.other_universes.setdefault(pkt.universe, {'packets': 0})
                seen.update(ip=ip, last_seen=now)
                seen['packets'] += 1
                continue
            if pkt.cid in newest:
                self.stale += 1
            newest[pkt.cid] = (pkt, ip)
        changed = False
        for pkt, ip in newest.values():
            changed |= self.merger.feed(pkt, ip, now)
        if changed:
            self.on_frame(self.merger.levels, bool(self.merger.sources))
        if newest:
            proc = time.perf_counter() - t0
            acc[3] += 1
            acc[4] += proc
            acc[5] = max(acc[5], proc)
        acc[6] = max(acc[6], len(batch))
        if now - self._acc_at >= 1.0:
            self._roll(now)

    def _sender_allowed(self, ip):
        ok = self._allowed.get(ip)
        if ok is None:
            if len(self._allowed) > 1024:
                self._allowed.clear()
            ok = self._allowed[ip] = _in_networks(self.allow, ip)
        return ok

    def _roll(self, now):
        a = self._acc
        ms = lambda v: round(v * 1000, 2)  # noqa: E731
        self.timing = dict(wait_avg=ms(a[1] / a[0]) if a[0] else None, wait_max=ms(a[2]) if a[0] else None,
                           proc_avg=ms(a[4] / a[3]) if a[3] else None, proc_max=ms(a[5]) if a[3] else None,
                           batch_max=a[6])
        self._acc = [0, 0.0, 0.0, 0, 0.0, 0.0, 0]
        self._acc_at = now

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
        if now - self._acc_at > 2:                  # no packets lately: don't show old timing
            self._roll(now)
        return dict(sources=sources, levels=list(levels), packets=self.packets, ignored=self.ignored,
                    refused=self.refused,
                    stale=self.stale, timing=self.timing,
                    error=self.error, listen=f'{self.bind}:{self.port}',
                    other_universes=[dict(universe=u, ip=s['ip'], packets=s['packets'])
                                     for u, s in sorted(self.other_universes.items())])
