"""Server configuration: ONE file, the app's .env.

Every per-machine setting (APP_PORT, DB_PATH, SACN_*, ...) lives in `.env`
in the repo directory (override the path with PHONELIGHT_ENV_FILE). A key is
read from the process environment first (systemd `EnvironmentFile=`), then
from the file, so the service and CLI tools agree. Show settings (universe,
fixtures, ...) are NOT here: they live in the database (server/settings.py)
and are changed from the admin Settings page. Keep this module
dependency-free: start.sh imports it.
"""
import os
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = Path(os.environ.get('PHONELIGHT_ENV_FILE') or REPO_DIR / '.env')


def _parse_env_file(path):
    """KEY=value lines; '#' comments; optional matching quotes. No $VAR
    expansion and no inline comments (same as systemd EnvironmentFile)."""
    values = {}
    try:
        text = Path(path).read_text(encoding='utf-8')
    except FileNotFoundError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        values[key] = value
    return values


def get(key, default=None):
    """Process env first, then .env; empty values count as unset."""
    value = os.environ.get(key)
    if value:
        return value
    value = _parse_env_file(ENV_PATH).get(key)
    return value if value else default


def get_int(key, default):
    try:
        return int(get(key, default))
    except (TypeError, ValueError):
        return default


def version():
    try:
        return (REPO_DIR / 'VERSION').read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        return 'unknown'


if __name__ == '__main__':
    # `python -m server.config KEY [default]` prints one value (used by start.sh).
    import sys
    if len(sys.argv) < 2:
        sys.exit('usage: python -m server.config KEY [default]')
    print(get(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ''))
