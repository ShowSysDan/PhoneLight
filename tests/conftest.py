import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A throw-away .env + database; settings cache reset."""
    from server import settings
    env_file = tmp_path / 'test.env'
    env_file.write_text(f'DB_PATH={tmp_path / "t.db"}\n')
    monkeypatch.setenv('PHONELIGHT_ENV_FILE', str(env_file))
    monkeypatch.setenv('DB_PATH', str(tmp_path / 't.db'))
    monkeypatch.setattr(settings, '_cache', None)
    monkeypatch.setattr(settings, '_listeners', [])
    from server import migrate
    migrate.migrate(out=lambda *_: None)
    return tmp_path
