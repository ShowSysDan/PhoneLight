"""Simulate N phones against a running PhoneLight server and measure it.

    venv/bin/python tools/load_test.py http://127.0.0.1:5420 -n 200 -t 30 --drive

Each fake phone opens /ws, says hello, answers pings and counts colour
frames. Something has to change the colours while it runs: the console, a
test pattern from the admin Live page, or --drive.

--drive changes the colour itself through the admin test API (the same
"Manual colour" as the Live page; no sACN is sent), --fps times a second,
with a counter in the red channel, and measures END-TO-END delay: from the
request leaving to each phone receiving the colour. The show is put back on
Live sACN at the end. Needs the master dimmer at 100%.

Each fake phone also runs the guest page's clock sync (with its own random
clock offset) and works out WHEN it would show each frame, exactly like the
page does. With --drive the result includes the SYNC SPREAD: for each frame,
how far apart the first and last phone show it (screen refresh not
included). If phones get fewer frames than are sent, frames were dropped
(latest-wins), which is the intended behaviour under load.

Uses gevent (already installed for the server), so 1000 phones is one process.
"""
from gevent import monkey

monkey.patch_all()

import argparse  # noqa: E402
import json  # noqa: E402
import random  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402

import gevent  # noqa: E402
from simple_websocket import Client, ConnectionClosed  # noqa: E402

SENT = {}                    # counter -> monotonic send time (--drive)
SHOWN = {}                   # (counter, sent ms) -> [show times], for the sync spread


def set_mode(base, body):
    req = urllib.request.Request(base + '/admin/api/mode', json.dumps(body).encode(),
                                 {'Content-Type': 'application/json'})
    urllib.request.urlopen(req, timeout=5).read()


def driver(base, fps, until):
    start, n = time.monotonic(), 0
    try:
        while time.monotonic() < until:
            counter = n % 256
            SENT[counter] = time.monotonic()
            set_mode(base, {'mode': 'color', 'color': f'#{counter:02x}8040', 'quiet': True})
            n += 1
            gevent.sleep(max(0, start + n / fps - time.monotonic()))
    finally:
        set_mode(base, {'mode': 'sacn'})


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
    skew = random.uniform(0, 3600e3)                # this phone's clock vs ours, ms (performance.now() is never negative)
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
                    at = int.from_bytes(msg[-4:], 'big')       # after every cell's RGB
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
    ap.add_argument('--drive', action='store_true',
                    help='change the colour through the admin test API and measure end-to-end delay')
    ap.add_argument('--fps', type=float, default=40, help='colour changes per second with --drive')
    args = ap.parse_args()
    url = args.url.replace('http', 'ws', 1).rstrip('/') + '/ws'
    stats = {'frames': [], 'max_gap': [], 'delay': [], 'shown': [], 'failed': 0, 'closed': 0, 'errors': []}
    until = time.monotonic() + args.ramp + args.t
    jobs = []
    if args.drive:
        jobs.append(gevent.spawn(driver, args.url.rstrip('/'), args.fps, until))
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
        print(f'change -> phone delay: median {pct(.5):.1f} ms  p95 {pct(.95):.1f} ms  p99 {pct(.99):.1f} ms  '
              f'max {d[-1] * 1000:.1f} ms  ({len(d)} samples)')
        d = sorted(stats['shown'])
        print(f'change -> shown (with sync delay): median {pct(.5):.1f} ms  p95 {pct(.95):.1f} ms  '
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
