import pytest

from server import engine, settings


def lv(*values, at=1):
    data = bytearray(512)
    data[at - 1:at - 1 + len(values)] = bytes(values)
    return bytes(data)


def test_8bit_mapping():
    v = dict(start_address=10, fixtures=2, bit_depth=8, cells=1)
    assert engine.fixture_colors(lv(1, 2, 3, 4, 5, 6, at=10), v) == [b'\x01\x02\x03', b'\x04\x05\x06']


def test_16bit_mapping_rounds_to_8bit():
    v = dict(start_address=1, fixtures=1, bit_depth=16, cells=1)
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


def test_manual_colour_per_fixture_starts_from_current_look(eng):
    e, out = eng
    e.on_frame(lv(10, 20, 30, 40, 50, 60), True)
    e.set_mode('color', b'\xff\x00\x00', fixture=2)
    assert out[-1] == [bytes((10, 20, 30)), b'\xff\x00\x00']
    e.set_mode('color', b'\x00\x00\xff')                  # no fixture: all of them
    assert out[-1] == [b'\x00\x00\xff'] * 2
    with pytest.raises(ValueError):
        e.set_mode('color', b'\x00\x00\x00', fixture=3)
    e.on_frame(lv(1, 1, 1), True)                            # the console does not override a test
    assert out[-1] == [b'\x00\x00\xff'] * 2


def test_patterns():
    w, b = b'\xff\xff\xff', engine.BLACK
    assert engine.pattern_colors('chase', w, 2, 3, 0.0) == [w, b, b]
    assert engine.pattern_colors('chase', w, 2, 3, 0.6) == [b, w, b]
    assert engine.pattern_colors('flash', w, 1, 2, 0.1) == [w, w]
    assert engine.pattern_colors('flash', w, 1, 2, 0.6) == [b, b]
    assert len(set(engine.pattern_colors('rainbow', w, 1, 3, 0.2))) == 3


def test_pattern_mode_runs_on_the_ticker(eng):
    e, out = eng
    e.set_mode('pattern', b'\xff\xff\xff', pattern='flash', rate=1)
    first = out[-1]
    e._compute(e._pattern_t0 + 0.6)
    assert first == [b'\xff\xff\xff'] * 2 and out[-1] == [engine.BLACK] * 2
    with pytest.raises(ValueError):
        e.set_mode('pattern', pattern='strobe')
    with pytest.raises(ValueError):
        e.set_mode('pattern', rate=100)


def test_settings_reject_overflow(env):
    with pytest.raises(ValueError, match='past 512'):
        settings.save({'fixtures': 100, 'bit_depth': 16, 'start_address': 1})
    with pytest.raises(ValueError):
        settings.save({'bit_depth': 12})
    assert settings.save({'fixtures': 85, 'bit_depth': 16, 'start_address': 1})['fixtures'] == 85


def test_unlit_fixtures_flag_a_patch_problem(eng):
    e, out = eng                                             # 2 fixtures, 8-bit from address 1
    e.on_frame(lv(255, 0, 0), True)                          # only fixture 1 gets levels
    assert e.snapshot()['unlit'] == [2]
    e.on_frame(lv(255, 0, 0, 0, 0, 9), True)                 # fixture 2 lit once: no longer flagged
    e.on_frame(lv(255, 0, 0), True)
    assert e.snapshot()['unlit'] == []
    e.on_frame(lv(255, 0, 0), False)                         # signal lost: nothing to flag
    assert e.snapshot()['unlit'] == []
    e.on_frame(lv(255, 0, 0), True)                          # a new signal: checked afresh
    assert e.snapshot()['unlit'] == [2]


def test_cells_mapping():
    v = dict(start_address=2, fixtures=2, bit_depth=8, cells=2)
    assert engine.fixture_colors(lv(*range(1, 13), at=2), v) == [bytes(range(1, 7)), bytes(range(7, 13))]
    v = dict(start_address=1, fixtures=1, bit_depth=16, cells=2)
    assert engine.fixture_colors(lv(255, 255, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255), v) == [bytes((255, 0, 0, 0, 0, 255))]


@pytest.fixture
def cells_eng(env):
    settings.save({'fixtures': 2, 'cells': 3, 'cell_columns': 3, 'master': 100})
    out = []
    return engine.Engine(out.append), out


def test_cells_live_and_unlit(cells_eng):
    e, out = cells_eng
    e.on_frame(lv(0, 0, 0, 0, 0, 0, 9, 9, 9), True)          # only fixture 1 cell 3
    assert out[-1] == [bytes(6) + b'\x09' * 3, bytes(9)]
    assert e.snapshot()['fixtures'][0] == ['#000000', '#000000', '#090909']
    assert e.snapshot()['unlit'] == [2]


def test_cells_test_modes(cells_eng):
    e, out = cells_eng
    red = b'\xff\x00\x00'
    e.set_mode('color', red)
    assert out[-1] == [red * 3] * 2
    e.set_mode('color', b'\x00\x00\xff', fixture=2, cell=2)
    assert out[-1] == [red * 3, red + b'\x00\x00\xff' + red]
    with pytest.raises(ValueError):
        e.set_mode('color', red, fixture=1, cell=4)
    with pytest.raises(ValueError):
        e.set_mode('color', red, cell=1)                      # a cell needs its fixture
    e.set_mode('blackout')
    assert out[-1] == [bytes(9)] * 2
    e.set_mode('identify')
    assert all(len(c) == 9 and c[:3] == c[3:6] == c[6:] for c in out[-1])


def test_cells_patterns():
    w, b = b'\xff\xff\xff', engine.BLACK
    assert engine.pattern_colors('chase', w, 1, 2, 0.0, cells=2) == [w + b, b + b]
    assert engine.pattern_colors('chase', w, 1, 2, 2.5, cells=2) == [b + b, w + b]   # 3rd cell: fixture 2 cell 1
    assert engine.pattern_colors('flash', w, 1, 2, 0.1, cells=2) == [w * 2] * 2
    rb = engine.pattern_colors('rainbow', w, 1, 2, 0.0, cells=2)
    assert len(rb) == 2 and len(set(rb[0][k:k + 3] for k in (0, 3))) == 2


def test_cell_count_change_republishes(cells_eng):
    e, out = cells_eng
    e.set_mode('color', b'\x01\x02\x03')
    settings.save({'cells': 1})
    e.recompute()
    assert out[-1] == [b'\x01\x02\x03'] * 2                  # manual colours fitted to the new size
    n = len(out)
    e.recompute()                                             # republished even when unchanged
    assert len(out) == n + 1


def test_settings_cells_footprint(env):
    with pytest.raises(ValueError, match='past 512'):
        settings.save({'fixtures': 30, 'cells': 6, 'bit_depth': 8, 'start_address': 1})   # 540 channels
    assert settings.save({'fixtures': 28, 'cells': 6, 'start_address': 1})['cells'] == 6
    with pytest.raises(ValueError):
        settings.save({'cells': 41})


@pytest.fixture
def flash_eng(env):
    settings.save({'fixtures': 2, 'cells': 1, 'cell_columns': 1, 'master': 100, 'flash': 1})
    out = []
    return engine.Engine(out.append), out


def test_flash_footprint(env):
    settings.save({'fixtures': 2, 'cells': 2, 'bit_depth': 16, 'flash': 1})
    assert settings.channels_per_fixture() == 13
    with pytest.raises(ValueError):                           # 40 × 13 = 520, past 512
        settings.save({'fixtures': 40})


def test_flash_from_sacn(flash_eng):
    e, out = flash_eng
    e.on_frame(lv(1, 2, 3, 128, 4, 5, 6, 127), True)          # R G B flash, per fixture
    assert out[-1] == [b'\x01\x02\x03\xff', b'\x04\x05\x06\x00']
    snap = e.snapshot()
    assert snap['fixtures'] == [['#010203'], ['#040506']] and snap['flash'] == [True, False]
    settings.save({'master': 0})
    e.recompute()
    assert out[-1] == [bytes(4)] * 2                          # master 0: the flash goes off too
    settings.save({'master': 100, 'flash': 0})
    e.recompute()
    assert out[-1] == [b'\x01\x02\x03', b'\x80\x04\x05'] and e.snapshot()['flash'] is None


def test_flash_test_modes(flash_eng):
    e, out = flash_eng
    red = b'\xff\x00\x00'
    e.set_mode('color', red, flash=True)
    assert out[-1] == [red + b'\xff'] * 2
    e.set_mode('color', flash=False, fixture=2)
    assert out[-1] == [red + b'\xff', red + b'\x00']
    e.set_mode('color', b'\x00\x00\xff', fixture=1)          # a colour change keeps the flash
    assert out[-1] == [b'\x00\x00\xff\xff', red + b'\x00']
    e.set_mode('identify')
    assert all(c[3] == 0 for c in out[-1])
    e.set_mode('pattern', red, pattern='flash', rate=1)
    assert out[-1] == [red + b'\xff'] * 2                     # the flash pattern flashes it too
    e.set_mode('blackout')
    assert out[-1] == [bytes(4)] * 2
