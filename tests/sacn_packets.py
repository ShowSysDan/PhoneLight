"""E1.31 packet builder for the TESTS only: the test server listens on
127.0.0.1 and these packets go nowhere else. PhoneLight itself never
transmits sACN, and there is deliberately no sender tool in the repo
(use the admin Live page's test tools to make colours without a console)."""
import struct


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
