#!/usr/bin/env bash
# PhoneLight server: install / update.
# Clone into your home folder, then:   sudo ./install.sh
# (Without sudo it sets up the venv and database only, no systemd service.)
# After the first install, updates are:  git pull && sudo systemctl restart phonelight
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="phonelight"
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
ENV_FILE="${APP_DIR}/.env"
VENV="${APP_DIR}/venv"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()  { echo -e "\n${CYAN}==> $*${NC}"; }

# ── Who runs the service: the user who owns the clone ────────────────────────
if [ "$(id -u)" -eq 0 ]; then
    RUN_USER="${SUDO_USER:-}"
    [ -n "$RUN_USER" ] && [ "$RUN_USER" != "root" ] \
        || error "Run as your own user via sudo (sudo ./install.sh), not as root directly."
else
    RUN_USER="$(id -un)"
fi
as_user() { if [ "$(id -u)" -eq 0 ]; then sudo -u "$RUN_USER" -H "$@"; else "$@"; fi; }
info "App directory: ${APP_DIR} (service user: ${RUN_USER})"

# ── Python ───────────────────────────────────────────────────────────────────
step "Checking Python 3..."
command -v python3 &>/dev/null || error "python3 not found. Install Python 3.9+ (and python3-venv)."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || error "Python 3.9+ is required (found $(python3 --version))."
info "Found $(python3 --version)"

step "Virtual environment..."
if [ ! -x "${VENV}/bin/python" ]; then
    as_user python3 -m venv "$VENV" || error "venv failed. On Debian/Ubuntu: sudo apt-get install -y python3-venv"
    info "Created ${VENV}"
fi
as_user "${VENV}/bin/pip" install --quiet --upgrade pip
as_user "${VENV}/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
sha256sum "${APP_DIR}/requirements.txt" | cut -d' ' -f1 | as_user tee "${VENV}/.requirements.sha256" >/dev/null
# Load what the service loads (the gevent worker + the app), so a missing package fails here, not in systemd
(cd "$APP_DIR" && as_user "${VENV}/bin/python" -c 'import gunicorn.workers.ggevent, server.app') \
    || error "The installed packages cannot run the server (see above). Report it with that error."
info "Dependencies installed."

# ── .env ─────────────────────────────────────────────────────────────────────
step "Configuration..."
if [ ! -f "$ENV_FILE" ]; then
    as_user cp "${APP_DIR}/.env.example" "$ENV_FILE"
    info "Created ${ENV_FILE} from .env.example (the defaults work; edit it to change ports or limit /admin)."
fi
chmod 600 "$ENV_FILE"
[ "$(id -u)" -eq 0 ] && chown "${RUN_USER}:" "$ENV_FILE"

# ── Database ─────────────────────────────────────────────────────────────────
step "Database (SQLite)..."
(cd "$APP_DIR" && as_user "${VENV}/bin/python" -m server.migrate) \
    || error "Migration failed (see above). Check DB_PATH in ${ENV_FILE}."
info "Database: $(cd "$APP_DIR" && "${VENV}/bin/python" -c 'from server import db; print(db.path())')"

# ── systemd (root only) ──────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    step "Done (no systemd service without sudo)."
    echo "  Run in the foreground:  ${APP_DIR}/start.sh"
    echo "  Install the service:    sudo ./install.sh"
    exit 0
fi

step "systemd service..."
chmod +x "${APP_DIR}/start.sh"
cat > "$SERVICE_FILE" <<UNIT
[Unit]
Description=PhoneLight - crowd lighting from sACN
Documentation=https://github.com/ShowSysDan/PhoneLight
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=$(id -gn "$RUN_USER")
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${APP_DIR}/start.sh
Restart=always
RestartSec=3
# every phone is an open socket: allow plenty of file descriptors
LimitNOFILE=16384
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${APP_NAME}

[Install]
WantedBy=multi-user.target
UNIT
info "Wrote ${SERVICE_FILE}"

systemctl daemon-reload
systemctl enable "$APP_NAME" --quiet
systemctl restart "$APP_NAME"
sleep 3
if systemctl is-active --quiet "$APP_NAME"; then
    info "Service is running."
else
    warn "Service may have failed to start: journalctl -u ${APP_NAME} -n 50"
fi

if command -v ufw &>/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
    PORT_FW="$(cd "$APP_DIR" && "${VENV}/bin/python" -m server.config APP_PORT 5420)"
    warn "ufw is active. Open the ports if you have not:  sudo ufw allow ${PORT_FW}/tcp && sudo ufw allow 5568/udp"
fi

PORT="$(cd "$APP_DIR" && "${VENV}/bin/python" -m server.config APP_PORT 5420)"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo ""
echo -e "  Guests:  ${CYAN}http://${LAN_IP:-localhost}:${PORT}/${NC}   (put this in the QR code)"
echo -e "  Admin:   ${CYAN}http://${LAN_IP:-localhost}:${PORT}/admin${NC}"
echo "  Console: send sACN UNICAST to ${LAN_IP:-<this server>} (UDP 5568), universe set on the admin Settings page"
echo "  Update:  cd ${APP_DIR} && git pull && sudo systemctl restart ${APP_NAME}"
echo "  Logs:    journalctl -u ${APP_NAME} -f"
