from sacn_packets import packet

from server import sacn

CID_A, CID_B = b'A' * 16, b'B' * 16


def levels(**slots):
    data = bytearray(512)
    for k, v in slots.items():
        data[int(k[1:]) - 1] = v
    return bytes(data)


def pkt(cid=CID_A, seq=0, universe=1, data=None, priority=100, terminated=False):
    return packet(cid, 'desk', priority, seq, universe, data if data is not None else levels(c1=255), terminated)


def receiver(universe=1):
    frames = []
    r = sacn.Receiver(lambda: universe, lambda lv, live: frames.append((lv, live)))
    return r, frames


def test_parse_roundtrip():
    p = sacn.parse(pkt(seq=7, universe=12, data=levels(c1=10, c512=20), priority=150))
    assert (p.universe, p.sequence, p.priority, p.name, p.terminated) == (12, 7, 150, 'desk', False)
    assert p.data[0] == 10 and p.data[511] == 20


def test_parse_rejects_junk_preview_and_other_start_codes():
    assert sacn.parse(b'hello') is None
    good = bytearray(pkt())
    preview = bytearray(good)
    preview[112] |= 0x80
    assert sacn.parse(bytes(preview)) is None
    start_dd = bytearray(good)
    start_dd[125] = 0xDD
    assert sacn.parse(bytes(start_dd)) is None


def test_short_universe_is_padded():
    r, frames = receiver()
    r.handle(pkt(data=bytes([1, 2, 3])), '10.0.0.1', now=0)
    assert len(frames[-1][0]) == 512 and frames[-1][0][:4] == bytes([1, 2, 3, 0])


def test_htp_merge_same_priority():
    r, frames = receiver()
    r.handle(pkt(CID_A, data=levels(c1=200, c2=10)), '10.0.0.1', now=0)
    r.handle(pkt(CID_B, data=levels(c1=50, c2=90)), '10.0.0.2', now=0)
    lv, live = frames[-1]
    assert live and lv[0] == 200 and lv[1] == 90


def test_higher_priority_wins_outright():
    r, frames = receiver()
    r.handle(pkt(CID_A, data=levels(c1=255), priority=100), '10.0.0.1', now=0)
    r.handle(pkt(CID_B, data=levels(c1=5), priority=150), '10.0.0.2', now=0)
    assert frames[-1][0][0] == 5


def test_other_universe_counted_not_used():
    r, frames = receiver(universe=1)
    r.handle(pkt(universe=2), '10.0.0.9', now=0)
    assert frames == [] and r.other_universes[2]['packets'] == 1


def test_sequence_rejects_late_packets():
    r, frames = receiver()
    r.handle(pkt(seq=10, data=levels(c1=1)), 'ip', now=0)
    r.handle(pkt(seq=9, data=levels(c1=2)), 'ip', now=0)      # late: dropped
    r.handle(pkt(seq=10, data=levels(c1=3)), 'ip', now=0)     # duplicate: dropped
    r.handle(pkt(seq=11, data=levels(c1=4)), 'ip', now=0)
    assert [f[0][0] for f in frames] == [1, 4]
    r.handle(pkt(seq=0, data=levels(c1=5)), 'ip', now=0)      # wrap 11 -> 0 is a jump forward... far back (-11): dropped
    assert frames[-1][0][0] == 4
    r.handle(pkt(seq=200, data=levels(c1=6)), 'ip', now=0)    # big jump: accepted (resync)
    assert frames[-1][0][0] == 6


def test_terminated_and_timeout_drop_source():
    r, frames = receiver()
    r.handle(pkt(CID_A, seq=1), 'ip', now=0)
    r.handle(pkt(CID_A, seq=2, terminated=True), 'ip', now=0.1)
    assert frames[-1][1] is False and not r.merger.sources
    r.handle(pkt(CID_B, seq=1), 'ip', now=1)
    assert r.merger.expire(1 + sacn.SOURCE_TIMEOUT + 0.1)
    assert not r.merger.sources


def test_batch_acts_on_newest_packet_per_source_only():
    r, frames = receiver()
    batch = [(pkt(CID_A, seq=s, data=levels(c1=s)), 'ip', None) for s in range(1, 11)]
    batch.append((pkt(CID_A, seq=1, universe=9), 'ip', None))          # other universe: counted, not stale
    r.handle_batch(batch, now=0)
    assert len(frames) == 1 and frames[0][0][0] == 10                  # one update, the newest levels
    assert r.stale == 9 and r.packets == 11 and r.other_universes[9]['packets'] == 1


def test_batch_timing_from_kernel_timestamps():
    import time
    r, frames = receiver()
    r.handle_batch([(pkt(seq=1), 'ip', time.time() - 0.05)], now=100.0)
    r._roll(101.0)
    assert 45 <= r.timing['wait_avg'] <= 1000 and r.timing['proc_avg'] is not None


def test_reader_drains_a_real_socket():
    import socket
    import threading
    import time
    got = []
    done = threading.Event()

    def on_frame(levels, live):
        got.append(levels[0])
        if levels[0] == 200:
            done.set()
    r = sacn.Receiver(lambda: 1, on_frame, bind='127.0.0.1', port=0)
    sock = r._open()
    port = sock.getsockname()[1]
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for s in range(1, 201):                                             # a backlog before we read
        tx.sendto(pkt(seq=s % 256, data=levels(c1=s)), ('127.0.0.1', port))
    tx.close()
    threading.Thread(target=r._read, args=(sock,), daemon=True).start()
    assert done.wait(3)
    r.stop()
    sock.close()
    assert got[-1] == 200 and len(got) < 200 and r.stale > 0           # skipped to the newest
    r._roll(time.monotonic())
    assert r.timing['wait_max'] is not None and r.timing['wait_max'] < 3000   # Linux kernel timestamps


def test_sacn_allow_drops_other_senders():
    from server import proxy
    frames = []
    r = sacn.Receiver(lambda: 1, lambda lv, live: frames.append(lv), allow=proxy.networks('10.0.0.5, 10.1.0.0/24'))
    r.handle(pkt(cid=CID_A, data=levels(c1=200)), '192.168.1.66')
    assert r.refused == 1 and not frames and not r.merger.sources
    r.handle(pkt(cid=CID_A, seq=1, data=levels(c1=100)), '10.1.0.9')
    assert frames[-1][0] == 100 and r.refused == 1


def test_source_count_is_capped():
    r, _ = receiver()
    for i in range(sacn.MAX_SOURCES + 10):
        r.handle(pkt(cid=i.to_bytes(16, 'big')), '10.0.0.1')
    assert len(r.merger.sources) == sacn.MAX_SOURCES
