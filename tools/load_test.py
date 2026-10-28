"""Simulate N phones against a running PhoneLight server and measure it.

    venv/bin/python tools/load_test.py http://127.0.0.1:5420 -n 200 -t 30

Each fake phone opens /ws, says hello, answers pings and counts colour
frames. Run tools/sacn_sender.py at the same time so there is something to
receive, or pass --sacn to have this script send the sACN itself and measure
END-TO-END delay: from the UDP packet leaving to each phone receiving the
colour (the packet's counter rides in the red channel of every fixture). At
the end it prints frames per second per phone and that delay; if phones get
fewer frames than are sent, frames were dropped (latest-wins), which is the
intended behaviour under load.

Each fake phone also runs the guest page's clock sync (with its own random
clock offset) and works out WHEN it would show each frame, exactly like the
page does. With --sacn the result includes the SYNC SPREAD: for each frame,
how far apart the first and last phone show it (screen refresh not included).


Uses gevent (already installed for the server), so 1000 phones is one process.
"""
from gevent import monkey

monkey.patch_all()

import argparse  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402

import socket  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

import gevent  # noqa: E402
from simple_websocket import Client, ConnectionClosed  # noqa: E402


sys.path.insert(0, str(Path(__file__).resolve().parent))
from sacn_sender import packet  # noqa: E402

SENT = {}                    # counter -> monotonic send time (--sacn)
SHOWN = {}                   # (counter, sent ms) -> [show times], for the sync spread


def sacn_source(host, universe, fps, fixtures, until):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cid, seq, start, n = uuid.uuid4().bytes, 0, time.monotonic(), 0
    while time.monotonic() < until:
        counter = n % 256
        data = bytearray(512)
        for f in range(fixtures):
            data[f * 3:f * 3 + 3] = bytes((counter, 128, 64))
        SENT[counter] = time.monotonic()
        sock.sendto(packet(cid, 'load test', 100, seq, universe, data), (host, 5568))
        seq, n = (seq + 1) & 0xFF, n + 1
        gevent.sleep(max(0, start + n / fps - time.monotonic()))


def phone(url, i, until, stats):
    try:
        ws = Client.connect(url, headers={'User-Agent': f'PhoneLight load test {i}'})
    except Exception as exc:
        stats['failed'] += 1
        stats['errors'].append(str(exc))
        return
    frames = 0
    t0 = time.monotonic()
    last_frame = None
    gaps = []
    skew = random.uniform(-3600e3, 3600e3)          # this phone's clock vs ours, ms
    clock = lambda: time.monotonic() * 1000 + skew  # noqa: E731  (its performance.now())
    samples, off, nsync, next_sync = [], None, 0, 0.0
    try:
        ws.send(json.dumps({'id': f'loadtest{i:06d}', 'sw': 390, 'sh': 844, 'dpr': 3, 'lang': 'en', 'tz': 'UTC'}))
        while time.monotonic() < until:
            if time.monotonic() >= next_sync:
                ws.send(f's{clock():.1f}')
                nsync += 1
                next_sync = time.monotonic() + (0.15 if nsync < 6 else 4)
            msg = ws.receive(timeout=0.1)
            if msg is None:
                continue
            if isinstance(msg, (bytes, bytearray)):
                frames += 1
                now = time.monotonic()
                show = now
                if off is not None and len(msg) >= 7:     # the guest page's rule
                    at = int.from_bytes(msg[3:7], 'big')
                    wait = (at - (round(clock() + off) & 0xFFFFFFFF) + 2**31) % 2**32 - 2**31
                    if 0 < wait <= 2000:
                        show = now + wait / 1000
                sent = SENT.get(msg[0])
                if sent is not None and msg[1:3] == b'\x80\x40' and 0 <= now - sent < 3:
                    stats['delay'].append(now - sent)
                    stats['shown'].append(show - sent)
                    if off is not None:
                        SHOWN.setdefault((msg[0], round(sent * 1000)), []).append(show)
                if last_frame is not None:
                    gaps.append(now - last_frame)
                last_frame = now
            elif msg[:1] == 'p':
                ws.send(msg)
            elif msg[:1] == 's':
                c0, t1, t2 = (float(x) for x in msg[1:].split(','))
                c3 = clock()
                samples = (samples + [((c3 - c0) - (t2 - t1), ((t1 - c0) + (t2 - c3)) / 2)])[-8:]
                off = min(samples)[1]
    except ConnectionClosed:
        stats['closed'] += 1
    finally:
        ws.close()
    stats['frames'].append(frames / max(time.monotonic() - t0, 1e-3))
    if gaps:
        stats['max_gap'].append(max(gaps))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('url', help='http://server:port')
    ap.add_argument('-n', type=int, default=100, help='phones')
    ap.add_argument('-t', type=float, default=20, help='seconds')
    ap.add_argument('--ramp', type=float, default=2, help='seconds to connect them all over')
    ap.add_argument('--sacn', action='store_true', help='send the sACN from here and measure end-to-end delay')
    ap.add_argument('--fps', type=float, default=40, help='sACN rate with --sacn')
    ap.add_argument('--universe', type=int, default=1)
    ap.add_argument('--fixtures', type=int, default=3, help='match the server setting (8-bit) with --sacn')
    args = ap.parse_args()
    url = args.url.replace('http', 'ws', 1).rstrip('/') + '/ws'
    stats = {'frames': [], 'max_gap': [], 'delay': [], 'shown': [], 'failed': 0, 'closed': 0, 'errors': []}
    until = time.monotonic() + args.ramp + args.t
    jobs = []
    if args.sacn:
        host = args.url.split('//', 1)[1].split(':')[0].split('/')[0]
        jobs.append(gevent.spawn(sacn_source, socket.gethostbyname(host), args.universe, args.fps,
                                 args.fixtures, until))
    for i in range(args.n):
        jobs.append(gevent.spawn(phone, url, i, until, stats))
        gevent.sleep(args.ramp / args.n)
    gevent.joinall(jobs)
    f = stats['frames']
    print(f'phones: {args.n}  connected: {len(f)}  failed: {stats["failed"]}  closed early: {stats["closed"]}')
    if f:
        rate = f
        print(f'frames/s per phone: min {min(rate):.1f}  median {statistics.median(rate):.1f}  max {max(rate):.1f}')
    if stats['max_gap']:
        print(f'longest gap between frames: median {statistics.median(stats["max_gap"]) * 1000:.0f} ms  '
              f'worst {max(stats["max_gap"]) * 1000:.0f} ms')
    d = sorted(stats['delay'])
    if d:
        pct = lambda p: d[min(len(d) - 1, int(len(d) * p))] * 1000  # noqa: E731
        print(f'sACN -> phone delay: median {pct(.5):.1f} ms  p95 {pct(.95):.1f} ms  p99 {pct(.99):.1f} ms  '
              f'max {d[-1] * 1000:.1f} ms  ({len(d)} samples)')
        d = sorted(stats['shown'])
        print(f'sACN -> shown (with sync delay): median {pct(.5):.1f} ms  p95 {pct(.95):.1f} ms  '
              f'p99 {pct(.99):.1f} ms')
    spreads = sorted(max(v) - min(v) for v in SHOWN.values() if len(v) >= max(2, len(stats['frames']) // 2))
    if spreads:
        sp = lambda p: spreads[min(len(spreads) - 1, int(len(spreads) * p))] * 1000  # noqa: E731
        print(f'sync spread (first to last phone showing a frame): median {sp(.5):.1f} ms  '
              f'p95 {sp(.95):.1f} ms  max {spreads[-1] * 1000:.1f} ms  ({len(spreads)} frames)')
    for e in sorted(set(stats['errors']))[:5]:
        print('  error:', e)


if __name__ == '__main__':
    main()
