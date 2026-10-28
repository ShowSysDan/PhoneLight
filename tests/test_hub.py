import socket

from server import hub as hubmod
from server.hub import BLACK, Guest, Hub, frame

RED, BLUE = b'\xff\x00\x00', b'\x00\x00\xff'


def guest(i, raw=None):
    return Guest(f's{i}', f'client{i:04d}', '10.0.0.1', {}, raw=raw)


def test_join_spreads_evenly():
    h = Hub(3)
    for i in range(10):
        h.join(guest(i))
    assert sorted(h.counts()) == [3, 3, 4]


def test_reconnect_keeps_fixture_only_when_it_is_emptiest():
    h = Hub(3)
    a, b, c = guest(1), guest(2), guest(3)
    h.join(a, preferred=0)
    h.join(b, preferred=2)
    h.join(c, preferred=0)                 # fixture 1 is fuller than 2: not kept
    assert (a.fixture, b.fixture, c.fixture) == (0, 2, 1)


def test_resize_and_rebalance():
    h = Hub(4)
    gs = [guest(i) for i in range(9)]
    for g in gs:
        h.join(g)
    h.resize(2)
    assert sorted(h.counts()) == [4, 5] and all(g.fixture < 2 for g in gs)


def test_publish_slow_path_is_latest_wins():
    """No raw socket: the guest is flagged; its sender sends only the newest frame."""
    h = Hub(1)
    g = guest(1)
    h.join(g)
    assert h.next_message(g) == 'f1' and h.next_message(g) == 'l1,1'   # fixture, then layout, then colour
    assert h.next_message(g) == BLACK            # the join flags it once
    h.publish([RED])
    h.publish([BLUE])
    assert g.dirty and h.next_message(g)[2:5] == BLUE
    assert h.next_message(g) is None


def test_publish_fast_path_writes_directly():
    a, b = socket.socketpair()
    a.setblocking(False)
    try:
        h = Hub(1)
        g = guest(1, raw=a)
        h.join(g)
        g.dirty, g.told_layout = False, h.layout
        h.publish([RED])
        assert b.recv(16) == h.frames[0] and h.frames[0][:5] == b'\x82\x07' + RED and not g.dirty
    finally:
        a.close()
        b.close()


def test_fast_path_skips_busy_sender_and_full_socket():
    a, b = socket.socketpair()
    a.setblocking(False)
    try:
        h = Hub(1)
        g = guest(1, raw=a)
        h.join(g)
        g.busy, g.told_layout = True, h.layout
        h.publish([RED])
        assert g.dirty                               # left to the sender
        b.setblocking(False)
        try:
            b.recv(16)
            raise AssertionError('publisher wrote while the sender was busy')
        except BlockingIOError:
            pass
        g.busy = False
        while True:                                  # fill the socket buffer
            try:
                a.send(b'x' * 65536)
            except BlockingIOError:
                break
        g.dirty = False
        h.publish([BLUE])
        assert g.dirty                               # full buffer: left to the sender
    finally:
        a.close()
        b.close()


def test_partial_write_finishes_frame_first():
    class Raw:
        def __init__(self):
            self.sent = []

        def send(self, data):
            self.sent.append(data[:2])
            return 2

    h = Hub(1)
    raw = Raw()
    g = guest(1, raw=raw)
    h.join(g)
    g.dirty, g.told_layout = False, h.layout
    g.told = g.fixture
    h.publish([RED])
    fr = h.frames[0]
    assert g.pending == fr[2:] and g.dirty
    out = []
    calls = iter(range(3))

    def send_bytes(data):
        out.append(data)
        if next(calls) == 1:
            g.alive = False
    h.serve(g, send_bytes, lambda t: None)
    assert out == [fr[2:], fr]


def test_ping_pong_latency():
    h = Hub(1)
    g = guest(1)
    h.join(g)
    g.told, g.told_layout, g.dirty = g.fixture, h.layout, False
    g.last_pong = 0
    msg = h.next_message(g, now=100.0)
    assert msg == 'p1' and h.next_message(g, now=100.0) is None   # one ping outstanding
    assert abs(g.pong('p1', now=100.05) - 50) < 0.01
    assert g.pong('p1', now=101) is None                           # answered already
    assert g.lat_n == 1 and abs(g.latency_avg() - 50) < 0.01


def test_reaper_closes_silent_and_stuck():
    h = Hub(1)
    closed = []
    silent, stuck, fine = guest(1), guest(2), guest(3)
    for g in (silent, stuck, fine):
        g.closer = (lambda g=g: closed.append(g.sid))
        h.join(g)
    silent.ping_at = 0.0
    stuck.busy, stuck.busy_since = True, 0.0
    h.reap(now=hubmod.PING_TIMEOUT + 1)
    assert sorted(closed) == ['s1', 's2'] and fine.alive
    assert silent.why == 'ping timeout' and stuck.why.startswith('stalled')


def test_frames_carry_show_at_publish_time_plus_delay():
    h = Hub(1)
    h.delay_ms = 100
    before = hubmod.server_ms()
    h.publish([RED])
    at = int.from_bytes(h.frames[0][5:9], 'big')
    assert len(h.frames[0]) == 9 and before + 99 <= at <= hubmod.server_ms() + 100
    assert frame(RED, 2**32 + 5)[5:9] == (5).to_bytes(4, 'big')        # wraps mod 2^32


def test_sync_request_and_reply():
    h = Hub(1)
    g = guest(1)
    h.join(g)
    h.sync_request(g, 's1234.5')
    h.sync_request(g, 'sbogus')                       # ignored, keeps the valid one
    reply = h.next_message(g)                          # sync goes before everything else
    c0, t1, t2 = reply[1:].split(',')
    assert c0 == '1234.5' and float(t1) <= float(t2) <= hubmod.server_ms() + 0.1
    assert g.sync is None


def test_cells_frame_carries_every_cell_then_show_at():
    h = Hub(1, cells=2, columns=2)
    g = guest(1)
    h.join(g)
    assert h.next_message(g) == 'f1' and h.next_message(g) == 'l2,2'
    assert h.next_message(g) == frame(bytes(6))                 # black, at the cell count
    h.publish([RED + BLUE])
    fr = h.frames[0]
    assert fr[:2] == b'\x82\x0a' and fr[2:8] == RED + BLUE and len(fr) == 12
    assert h.next_message(g) == fr
    h.publish([RED])                                            # wrong width (mid-change): dropped
    assert h.frames[0] == fr


def test_set_layout_tells_every_phone_and_resets_frames():
    h = Hub(2)
    gs = [guest(i) for i in range(2)]
    for g in gs:
        h.join(g)
        h.next_message(g), h.next_message(g), h.next_message(g)   # fixture, layout, colour
    h.publish([RED, BLUE])
    h.set_layout(3, 1)
    assert h.frames == [frame(bytes(9))] * 2
    for g in gs:
        assert h.next_message(g) == 'l3,1' and h.next_message(g) == frame(bytes(9))
    h.publish([RED * 3, BLUE * 3])
    assert h.frames[1][2:-4] == BLUE * 3
    h.set_layout(3, 3)                                          # columns only: colours kept
    assert h.frames[1][2:-4] == BLUE * 3 and h.next_message(gs[0]) == 'l3,3'


def test_fast_path_waits_until_the_phone_knows_the_layout():
    """A new cell count must reach the phone before frames of that size."""
    a, b = socket.socketpair()
    a.setblocking(False)
    b.setblocking(False)
    try:
        h = Hub(1)
        g = guest(1, raw=a)
        h.join(g)
        g.told, g.told_layout, g.dirty = g.fixture, h.layout, False
        h.set_layout(2, 1)
        h.publish([RED + BLUE])
        try:
            b.recv(16)
            raise AssertionError('frame written before the layout message')
        except BlockingIOError:
            pass
        assert g.dirty and h.next_message(g) == 'l2,1' and h.next_message(g)[2:-4] == RED + BLUE
    finally:
        a.close()
        b.close()
