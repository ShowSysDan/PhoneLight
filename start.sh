#!/usr/bin/env bash
# PhoneLight service launcher (systemd ExecStart).
# Every start: sync Python deps if requirements.txt changed, apply pending DB
# migrations, then exec gunicorn. So an update is just:
#   git pull && sudo systemctl restart phonelight
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${APP_DIR}/venv"
cd "${APP_DIR}"

if [ ! -x "${VENV}/bin/python" ]; then
    echo "[phonelight] No virtualenv at ${VENV}. Run: sudo ./install.sh" >&2
    exit 1
fi

# ── Python dependencies (only when requirements.txt changed) ────────────────
REQ_STAMP="${VENV}/.requirements.sha256"
REQ_HASH="$(sha256sum requirements.txt | cut -d' ' -f1)"
if [ "$(cat "${REQ_STAMP}" 2>/dev/null || true)" != "${REQ_HASH}" ]; then
    echo "[phonelight] requirements.txt changed, installing dependencies..."
    "${VENV}/bin/pip" install --quiet -r requirements.txt
    echo "${REQ_HASH}" > "${REQ_STAMP}"
fi

# ── Database migrations (fail loud: no start on a half-migrated database) ───
"${VENV}/bin/python" -m server.migrate

# ── Web server ───────────────────────────────────────────────────────────────
# ONE worker, always: the sACN receiver, the colour engine and every phone's
# socket share memory. gevent serves thousands of sockets in that one process.
PORT="$("${VENV}/bin/python" -m server.config APP_PORT 5420)"
CONNS="$("${VENV}/bin/python" -m server.config MAX_CONNECTIONS 4000)"
ACCESS_LOG=()
[ "$("${VENV}/bin/python" -m server.config ACCESS_LOG 0)" = "1" ] && ACCESS_LOG=(--access-logfile -)
echo "[phonelight] $(cat VERSION) starting on port ${PORT} (1 gevent worker, ${CONNS} connections)"

exec "${VENV}/bin/gunicorn" \
    --workers 1 \
    --worker-class gevent \
    --worker-connections "${CONNS}" \
    --bind "0.0.0.0:${PORT}" \
    --timeout 60 \
    --graceful-timeout 5 \
    ${ACCESS_LOG[@]+"${ACCESS_LOG[@]}"} \
    --chdir "${APP_DIR}" \
    'server.app:create_app()'
