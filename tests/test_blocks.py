import pytest

from server import blocks


@pytest.fixture
def fresh(env, monkeypatch):
    monkeypatch.setattr(blocks, '_sets', {k: set() for k in blocks.KINDS})
    monkeypatch.setattr(blocks, '_hits', {})
    blocks.load()


def test_block_by_client_and_ip_survives_reload(fresh):
    blocks.add('client', 'abcdefgh1234', 'spamming')
    blocks.add('ip', ' 10.0.0.7 ')
    assert blocks.is_blocked('abcdefgh1234', '10.9.9.9') == ('client', 'abcdefgh1234')
    assert blocks.is_blocked('someoneelse1', '10.0.0.7') == ('ip', '10.0.0.7')
    assert blocks.is_blocked('someoneelse1', '10.0.0.8') is None
    blocks._sets = {k: set() for k in blocks.KINDS}
    blocks.load()                                             # from the database, as after a restart
    assert blocks.is_blocked('abcdefgh1234', None)
    rows = {(r['kind'], r['value']): r for r in blocks.rows()}
    assert rows[('client', 'abcdefgh1234')]['note'] == 'spamming' and rows[('ip', '10.0.0.7')]['refused'] == 1
    blocks.remove('client', 'abcdefgh1234')
    assert blocks.is_blocked('abcdefgh1234', None) is None and len(blocks.rows()) == 1


def test_block_rejects_bad_values(fresh):
    for kind, value in (('client', 'short'), ('client', 'has space here'), ('ip', 'nope'), ('mac', 'x')):
        with pytest.raises(ValueError):
            blocks.add(kind, value)
