import pytest

from server import engine, settings


def lv(*values, at=1):
    data = bytearray(512)
    data[at - 1:at - 1 + len(values)] = bytes(values)
    return bytes(data)


def test_8bit_mapping():
    v = dict(start_address=10, fixtures=2, bit_depth=8)
    assert engine.fixture_colors(lv(1, 2, 3, 4, 5, 6, at=10), v) == [b'\x01\x02\x03', b'\x04\x05\x06']


def test_16bit_mapping_rounds_to_8bit():
    v = dict(start_address=1, fixtures=1, bit_depth=16)
    assert engine.fixture_colors(lv(255, 255, 128, 0, 0, 0), v) == [bytes((255, 128, 0))]
    assert engine.fixture_colors(lv(0, 128, 0, 0, 0, 0), v) == [bytes((0, 0, 0))]   # 0.498 -> 0
    assert engine.fixture_colors(lv(0, 129, 0, 0, 0, 0), v) == [bytes((1, 0, 0))]   # 0.502 -> 1


@pytest.fixture
def eng(env):
    settings.save({'fixtures': 2, 'loss_hold': 1, 'loss_fade': 2, 'master': 100})
    out = []
    return engine.Engine(out.append), out


def test_live_publishes_only_on_change(eng):
    e, out = eng
    e.on_frame(lv(10, 20, 30), True)
    e.on_frame(lv(10, 20, 30), True)
    assert out == [[b'\x0a\x14\x1e', b'\x00\x00\x00']]


def test_master_scales(eng):
    e, out = eng
    settings.save({'master': 50})
    e.on_frame(lv(200, 100, 0), True)
    assert out[-1][0] == bytes((100, 50, 0))


def test_loss_holds_then_fades(eng):
    e, out = eng
    e.on_frame(lv(200, 200, 200), True)
    e.on_frame(lv(200, 200, 200), False)
    t0 = e.lost_at
    e._compute(t0 + 0.5)                      # holding
    assert out[-1][0] == bytes((200, 200, 200)) and e.signal(t0 + 0.5) == 'hold'
    e._compute(t0 + 2)                        # half way through the fade
    assert out[-1][0] == bytes((100, 100, 100)) and e.signal(t0 + 2) == 'fade'
    e._compute(t0 + 3.5)
    assert out[-1][0] == b'\x00\x00\x00' and e.signal(t0 + 3.5) == 'lost'


def test_override_modes(eng):
    e, out = eng
    e.set_mode('color', b'\x01\x02\x03')
    assert out[-1] == [b'\x01\x02\x03'] * 2
    e.set_mode('blackout')
    assert out[-1] == [b'\x00\x00\x00'] * 2
    e.set_mode('identify')
    assert len(set(out[-1])) == 2
    with pytest.raises(ValueError):
        e.set_mode('disco')


def test_settings_reject_overflow(env):
    with pytest.raises(ValueError, match='past 512'):
        settings.save({'fixtures': 100, 'bit_depth': 16, 'start_address': 1})
    with pytest.raises(ValueError):
        settings.save({'bit_depth': 12})
    assert settings.save({'fixtures': 85, 'bit_depth': 16, 'start_address': 1})['fixtures'] == 85
