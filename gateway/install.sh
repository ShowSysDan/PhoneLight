#!/usr/bin/env bash
# glow.lighting public gateway: install / update / remove the Caddy site on the
# gateway server (e.g. the VPS that already runs 321T's dpc.321.theater).
#
#   sudo bash install.sh --upstream 10.201.2.102:5420          first install
#   sudo bash install.sh                                       update (re-uses saved settings)
#   sudo bash install.sh --site glow.lighting --no-www         other options
#   sudo bash install.sh --remove                              take glow.lighting off this server
#
# Shared Caddy, by convention: the main Caddyfile imports /etc/caddy/sites.d/*.caddy
# and each app owns exactly ONE file there. This script:
#   - finds Caddy (installs Debian's caddy package if there is none),
#   - adds the import line to the main Caddyfile if it is missing (and nothing
#     else: other apps' site blocks are never touched),
#   - writes only /etc/caddy/sites.d/glow-lighting.caddy,
#   - validates the whole config BEFORE reloading; on any error it restores the
#     files it changed and leaves the running Caddy alone,
#   - reloads Caddy gracefully (other sites keep serving; no restart).
# Settings are kept in /etc/glow-gateway.env. Full guide: gateway/README.md.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SRC_DIR}/glow-lighting.caddy.template"
CONF="${GLOW_GATEWAY_CONF:-/etc/glow-gateway.env}"
SITES_DIR="${CADDY_SITES_DIR:-/etc/caddy/sites.d}"
OWN="${SITES_DIR}/glow-lighting.caddy"
BACKUP_ROOT="${GLOW_GATEWAY_BACKUPS:-/var/backups/glow-gateway}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
step()  { echo -e "\n${CYAN}==> $*${NC}"; }

usage() { sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# ── Options (saved settings first, then the command line) ────────────────────
SITE="glow.lighting"; UPSTREAM=""; WWW=1; FRESH_CADDY=0
[ -f "$CONF" ] && . "$CONF"
REMOVE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --site)     SITE="${2:?--site needs a name}"; shift 2 ;;
        --upstream) UPSTREAM="${2:?--upstream needs host:port}"; shift 2 ;;
        --no-www)   WWW=0; shift ;;
        --www)      WWW=1; shift ;;
        --remove)   REMOVE=1; shift ;;
        -h|--help)  usage 0 ;;
        *)          echo "Unknown option: $1" >&2; usage 1 ;;
    esac
done
UPSTREAM="${UPSTREAM#http://}"; UPSTREAM="${UPSTREAM%%/*}"

echo -e "\n${CYAN}glow.lighting gateway${NC} (Caddy site on this server)\n"
[ "$(id -u)" -eq 0 ] || error "Run as root: sudo bash install.sh"
[[ "$SITE" =~ ^[A-Za-z0-9.-]+$ ]] || error "--site must be a host name (got '$SITE')."
if [ "$REMOVE" -eq 0 ]; then
    [ -n "$UPSTREAM" ] || error "Where is the glow server? First run: sudo bash install.sh --upstream <ip>:5420"
    [[ "$UPSTREAM" =~ ^[A-Za-z0-9.-]+:[0-9]+$ ]] || error "--upstream must be host:port (got '$UPSTREAM')."
    [ -f "$TEMPLATE" ] || error "Template not found: $TEMPLATE (run this from PhoneLight's gateway/ folder)."
fi
command -v python3 >/dev/null || error "python3 is needed (sudo apt-get install -y python3)."

# ── Caddy: find it, or install it ────────────────────────────────────────────
step "Caddy..."
if ! command -v caddy >/dev/null; then
    [ "$REMOVE" -eq 1 ] && error "Caddy is not installed here: nothing to remove."
    command -v apt-get >/dev/null || error "Caddy is not installed and this is not a Debian/Ubuntu server: install Caddy, then re-run."
    info "Caddy not found: installing Debian's caddy package..."
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy
    FRESH_CADDY=1
fi
CADDY_VERSION="$(caddy version 2>/dev/null | awk '{print $1}' | sed 's/^v//')"
info "Caddy ${CADDY_VERSION:-?} found."

# The main config: the one the caddy service runs (Debian: /etc/caddy/Caddyfile).
MAIN="${CADDY_MAIN:-}"
if [ -z "$MAIN" ] && command -v systemctl >/dev/null; then
    MAIN="$(systemctl show -p ExecStart caddy 2>/dev/null | grep -o -- '--config [^ ;]*' | head -1 | awk '{print $2}' || true)"
fi
MAIN="${MAIN:-/etc/caddy/Caddyfile}"
case "$MAIN" in
    *.json) error "Caddy runs a JSON config ($MAIN). This script edits Caddyfiles only; add the glow site by hand (see gateway/README.md)." ;;
esac
info "Main config: $MAIN"

# ── What is already here (reported, never changed) ───────────────────────────
python3 - "$MAIN" "$SITES_DIR" "$OWN" "$SITE" <<'PY'
import glob, re, sys
main, sites_dir, own, site = sys.argv[1:]
addr = re.compile(r'^([^\s#{}()][^{]*?)\s*\{\s*$')
found = {}
for path in [main] + sorted(glob.glob(f'{sites_dir}/*.caddy')):
    if path == own:
        continue
    try:
        lines = open(path).read().splitlines()
    except FileNotFoundError:
        continue
    for line in lines:
        m = addr.match(line)
        if m and not m.group(1).startswith('import'):
            for a in re.split(r'[,\s]+', m.group(1).strip()):
                if a:
                    found.setdefault(a, path)
for a, path in found.items():
    print(f'    already served: {a}  ({path})  -> left as is')
clash = [a for a in found if a.split('://')[-1].split(':')[0] in (site, 'www.' + site)]
if clash:
    sys.exit(f'CLASH: {", ".join(clash)} is already configured outside {own}. '
             'Remove it there first, or use --site for another name.')
PY

# ── Backups (restored automatically if anything fails) ───────────────────────
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="${BACKUP_ROOT}/${STAMP}"
mkdir -p "$BACKUP"
[ -f "$MAIN" ] && cp -p "$MAIN" "$BACKUP/Caddyfile"
[ -f "$OWN" ] && cp -p "$OWN" "$BACKUP/glow-lighting.caddy"
restore() {
    warn "Restoring the files this script changed (from $BACKUP)..."
    if [ -f "$BACKUP/Caddyfile" ]; then cp -p "$BACKUP/Caddyfile" "$MAIN"; else rm -f "$MAIN"; fi
    if [ -f "$BACKUP/glow-lighting.caddy" ]; then cp -p "$BACKUP/glow-lighting.caddy" "$OWN"; else rm -f "$OWN"; fi
}

if [ "$REMOVE" -eq 1 ]; then
    step "Removing glow.lighting..."
    rm -f "$OWN"
    info "Removed $OWN (the import line stays: harmless, and other apps may use it)."
else
    # ── Main Caddyfile: make sure it imports sites.d (and change nothing else) ──
    step "Main Caddyfile..."
    mkdir -p "$SITES_DIR"
    python3 - "$MAIN" "$SITES_DIR" <<'PY'
import os, re, sys
main, sites_dir = sys.argv[1:]
line = f'import {sites_dir}/*.caddy'
header = ('# Shared Caddy: every app owns one file in ' + sites_dir + '/.\n'
          '# Keep this import line (after the global options block, if any).\n')
try:
    text = open(main).read()
except FileNotFoundError:
    text = None
if text is None:
    os.makedirs(os.path.dirname(main) or '.', exist_ok=True)
    open(main, 'w').write(header + line + '\n')
    print(f'    created {main} with the import line')
    sys.exit()
if re.search(r'^\s*import\s+' + re.escape(sites_dir) + r'/\*(\.caddy)?\s*$', text, re.M):
    print('    import line already there: nothing to change')
    sys.exit()
addr = re.compile(r'^([^\s#{}()][^{]*?)\s*\{\s*$')
sites = [m.group(1) for m in map(addr.match, text.splitlines()) if m]
if '/usr/share/caddy' in text and sites == [':80']:
    # Debian's stock welcome page: nothing to keep (it is in the backup anyway)
    open(main, 'w').write(header + line + '\n')
    print("    replaced Debian's default welcome-page Caddyfile with the import line")
    sys.exit()
lines = text.splitlines(keepends=True)
i = 0
while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith('#')):
    i += 1
if i < len(lines) and lines[i].strip() == '{':           # global options must stay first
    depth = 0
    for j in range(i, len(lines)):
        code = lines[j].split('#', 1)[0]
        depth += code.count('{') - code.count('}')
        if depth == 0:
            i = j + 1
            break
lines[i:i] = ['\n' if i else '', header, line + '\n', '\n']
open(main, 'w').write(''.join(lines))
print(f'    added "{line}" (line {i + 2}); the rest of the file is unchanged')
PY

    # ── Our site file ──────────────────────────────────────────────────────────
    step "Site file ${OWN}..."
    STREAM=""
    if python3 -c "import sys; v=[int(x) for x in sys.argv[1].split('.')[:2]]; sys.exit(0 if v>=[2,7] else 1)" "${CADDY_VERSION:-0.0}" 2>/dev/null; then
        STREAM=$' {\n\t\t\t# keep phones connected through Caddy reloads (any app\'s)\n\t\t\tstream_close_delay 10m\n\t\t}'
    else
        warn "Caddy ${CADDY_VERSION:-?} is older than 2.7: every Caddy reload (yours or another app's) drops"
        warn "the phones' connections; they reconnect within seconds. See README: newer Caddy."
    fi
    WWW_BLOCK=""
    [ "$WWW" -eq 1 ] && WWW_BLOCK=$'www.'"${SITE}"$' {\n\tredir https://'"${SITE}"$'{uri} permanent\n}'
    python3 - "$TEMPLATE" "$OWN" "$SITE" "$UPSTREAM" "$STREAM" "$WWW_BLOCK" <<'PY'
import sys
src, dst, site, upstream, stream, www = sys.argv[1:]
text = open(src).read()
for key, value in (('@@SITE@@', site), ('@@UPSTREAM@@', upstream), ('@@STREAM@@', stream), ('@@WWW@@', www)):
    text = text.replace(key, value)
open(dst, 'w').write(text)
PY
    chmod 644 "$OWN"
    info "Wrote $OWN: https://${SITE} -> ${UPSTREAM}$([ "$WWW" -eq 1 ] && echo " (www.${SITE} redirects)")"
    printf 'SITE=%s\nUPSTREAM=%s\nWWW=%s\n' "$SITE" "$UPSTREAM" "$WWW" > "$CONF"
    info "Settings saved to $CONF"
fi

# ── Validate everything, then reload (never with a broken config) ────────────
step "Checking the whole Caddy config..."
if ! OUT="$(caddy validate --config "$MAIN" --adapter caddyfile 2>&1)"; then
    echo "$OUT" | tail -5 >&2
    restore
    error "Caddy rejected the config, so nothing was reloaded and the files are back as they were."
fi
info "Valid."
if command -v systemctl >/dev/null && systemctl is-active --quiet caddy 2>/dev/null; then
    systemctl reload caddy || { restore; systemctl reload caddy || true; error "Reload failed; files restored."; }
    info "Caddy reloaded gracefully (other sites kept serving)."
elif [ "${FRESH_CADDY:-0}" -eq 1 ] && command -v systemctl >/dev/null; then
    systemctl enable --now caddy >/dev/null 2>&1 && info "Caddy started." \
        || warn "Could not start Caddy: journalctl -u caddy -n 30"
else
    # Never start a Caddy that is stopped: it may be stopped ON PURPOSE (e.g. 321T's
    # emergency off switch), and starting it would put every site back online.
    warn "Caddy is not running, so nothing was reloaded (it may be stopped on purpose: it serves"
    warn "every site on this server). When you want it on: sudo systemctl start caddy"
fi

[ "$REMOVE" -eq 1 ] && { echo; info "glow.lighting removed. Backups: $BACKUP"; exit 0; }

# ── Firewall: 80 + 443 (shared with every site on this Caddy) ────────────────
step "Firewall..."
if command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-service=http --add-service=https >/dev/null && firewall-cmd --reload >/dev/null
    info "firewalld: http + https allowed."
elif command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
    ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
    info "ufw: 80 + 443 allowed."
else
    info "No active firewalld/ufw: make sure ports 80 and 443 reach this server."
fi

# ── Checks (warnings only) ───────────────────────────────────────────────────
step "Checks..."
if command -v curl >/dev/null && curl -sf --max-time 4 "http://${UPSTREAM}/healthz" >/dev/null; then
    info "Glow server answers at http://${UPSTREAM}/healthz"
else
    warn "Can't reach http://${UPSTREAM}/healthz from here. Check the glow service, and that the tunnel /"
    warn "firewall lets this server reach ${UPSTREAM} (TCP)."
fi
DNS_IP="$(getent ahostsv4 "$SITE" 2>/dev/null | awk 'NR==1{print $1}')"
if [ -z "$DNS_IP" ]; then
    warn "DNS: ${SITE} does not resolve yet. Point an A record at this server; Caddy gets the certificate once it does."
elif ip -4 addr 2>/dev/null | grep -q "inet ${DNS_IP}/"; then
    info "DNS: ${SITE} -> ${DNS_IP} (this server)."
else
    warn "DNS: ${SITE} -> ${DNS_IP}, which is not an address on this server (fine behind NAT; otherwise fix the A record)."
fi

echo -e "\n${CYAN}Done.${NC} Backups of what changed: $BACKUP"
echo "On the glow server: set TRUST_PROXY=1 in ~/PhoneLight/.env, then: sudo systemctl restart phonelight"
echo "Guests: https://${SITE}/   Admin: only on the building network (http://${UPSTREAM}/admin)"
echo "Update later: git pull, then sudo bash install.sh (settings are remembered)."
