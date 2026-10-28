from sacn_sender import packet

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
