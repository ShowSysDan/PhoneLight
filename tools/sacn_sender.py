"""Send a test pattern to PhoneLight over unicast sACN (no console needed).

    python tools/sacn_sender.py 192.168.1.50                  # rainbow chase, universe 1, 40 fps
    python tools/sacn_sender.py 127.0.0.1 -u 2 --fps 30 --fixtures 6 --bits 16
    python tools/sacn_sender.py 127.0.0.1 --pattern flash      # all fixtures white/black at 2 Hz
    python tools/sacn_sender.py 127.0.0.1 --pattern static --rgb ff0080
    python tools/sacn_sender.py 127.0.0.1 --priority 150 --name 'Backup desk'

Ctrl-C sends Stream_Terminated (the server fades out at once, after its hold).
Standard library only, so it runs on any laptop with Python 3.
"""
import argparse
import colorsys
import math
import socket
import struct
import time
import uuid


def packet(cid, name, priority, sequence, universe, data, terminated=False):
    slots = len(data)
    pdu_dmp = 10 + 1 + slots
    pdu_frame = 77 + pdu_dmp
    pdu_root = 22 + pdu_frame
    return b''.join([
        struct.pack('!HH12s', 0x0010, 0, b'ASC-E1.17\x00\x00\x00'),
        struct.pack('!HI16s', 0x7000 | pdu_root, 0x00000004, cid),
        struct.pack('!HI64sBHBBH', 0x7000 | pdu_frame, 0x00000002, name.encode()[:63],
                    priority, 0, sequence, 0x40 if terminated else 0, universe),
        struct.pack('!HBBHHHB', 0x7000 | pdu_dmp, 0x02, 0xA1, 0, 1, slots + 1, 0),
        bytes(data),
    ])


def frame(args, t):
    per = 6 if args.bits == 16 else 3
    data = bytearray(512)
    for i in range(args.fixtures):
        if args.pattern == 'rainbow':
            rgb = colorsys.hsv_to_rgb((t * args.speed + i / args.fixtures) % 1, 1, 1)
        elif args.pattern == 'flash':
            on = int(t * 4) % 2
            rgb = (on, on, on)
        elif args.pattern == 'chase':
            rgb = (1, 1, 1) if int(t * 4) % args.fixtures == i else (0, 0, 0)
        else:
            rgb = tuple(b / 255 for b in bytes.fromhex(args.rgb))
        base = args.start - 1 + i * per
        for k, v in enumerate(rgb):
            if per == 6:
                v16 = round(v * 65535)
                data[base + 2 * k], data[base + 2 * k + 1] = v16 >> 8, v16 & 0xFF
            else:
                data[base + k] = round(v * 255)
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('host', help='PhoneLight server IP (unicast)')
    ap.add_argument('-u', '--universe', type=int, default=1)
    ap.add_argument('--port', type=int, default=5568)
    ap.add_argument('--fps', type=float, default=40)
    ap.add_argument('--fixtures', type=int, default=3)
    ap.add_argument('--start', type=int, default=1)
    ap.add_argument('--bits', type=int, choices=(8, 16), default=8)
    ap.add_argument('--pattern', choices=('rainbow', 'flash', 'chase', 'static'), default='rainbow')
    ap.add_argument('--speed', type=float, default=0.2, help='rainbow cycles per second')
    ap.add_argument('--rgb', default='ffffff', help='static colour, hex')
    ap.add_argument('--priority', type=int, default=100)
    ap.add_argument('--name', default='PhoneLight test sender')
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cid = uuid.uuid4().bytes
    seq, start, n = 0, time.monotonic(), 0
    print(f'sending {args.pattern} to {args.host}:{args.port} universe {args.universe} at {args.fps} fps (Ctrl-C to stop)')
    try:
        while True:
            t = time.monotonic() - start
            sock.sendto(packet(cid, args.name, args.priority, seq, args.universe, frame(args, t)), (args.host, args.port))
            seq = (seq + 1) & 0xFF
            n += 1
            time.sleep(max(0, start + n / args.fps - time.monotonic()))
    except KeyboardInterrupt:
        for _ in range(3):
            sock.sendto(packet(cid, args.name, args.priority, seq, args.universe, bytes(512), True), (args.host, args.port))
            seq = (seq + 1) & 0xFF
        print('\nstream terminated')


if __name__ == '__main__':
    main()
