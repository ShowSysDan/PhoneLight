# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/). Every change
ships as its own version: the top section always matches `VERSION`.

## [0.10.2] - 2026-10-30

### Changed
- **Terms & privacy page** shortened (about a quarter fewer words): same
  content (flashing-lights warning, camera use, what is logged and for how
  long, being considerate, battery and "as is"), tighter wording, lists
  folded into short paragraphs.

## [0.10.1] - 2026-10-30

### Fixed
- **Phone flash on Android.** Many Android phones have several rear cameras
  and only the main one has the torch, and Chrome often reports the torch
  only once the camera is running, so the page found none and gave up. It
  now wakes the camera (ImageCapture) before checking and, if that camera
  still has no torch, tries each other rear camera in turn (front cameras
  skipped; no extra prompt). The long-press readout says *flash: no camera
  has one* when none of them offers a torch.

## [0.10.0] - 2026-10-30

### Added
- **Phone flash.** Phones can switch their camera flash (torch) with the
  show. Settings → *Phone flash* On gives every fixture one more channel,
  after its colours: 128+ = flash on. The page opens the rear camera only
  while the show uses the flash, after the guest taps *Allow* (no frame is
  read or sent), and only where the browser can switch a torch: Android
  Chrome, iPhone patchy, HTTPS only (through the gateway). *Flash longest on
  (s)* (default 10, 0 = no limit) makes a phone switch its flash off by
  itself, for batteries and heat. Live page: *Flash on / off* test buttons,
  ⚡ on fixtures whose flash is on, phones with flash ready; the flash
  pattern flashes the torches too. Clients page: ⚡ per phone.
- **Terms & privacy page** (`/terms`): flashing-lights warning, the camera
  permission, what is logged and for how long, being considerate, "as is".
  The start screen says tapping start agrees to it, and on phones that can
  use the flash asks the guest to tap *Allow* on the camera prompt. Have it
  read by the venue's legal team before a public show.

### Changed
- Wire format: with the flash on, the layout message is
  `l<cells>,<columns>,<longest-on s>` and frames carry one more byte (255 =
  on) before the show-at time. Phones send `t1` / `t0` (flash ready / not).
- Guest page ~12 KB (was ~10 KB).

Redeploy: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`.

## [0.9.0] - 2026-10-28

Security hardening from a full audit of the app and the public gateway.

### Added
- `SACN_ALLOW` (.env): sACN accepted only from these IPs / networks (the
  console). sACN has no password, so before this anyone who could send UDP
  5568 could drive every phone. Refused packets are counted (`refused` in the
  admin feed). At most 64 sources are held at once, so spoofed CIDs can't
  grow the merger.
- `MAX_PER_IP` / `MAX_PER_IP_EXEMPT` (.env, off by default): phone sockets
  one address may hold, so one machine can't open every socket the server
  accepts and lock the audience out. Exempt a venue Wi-Fi's shared address.
- `ADMIN_HOSTS` (.env): extra names the admin answers to.

### Changed
- The admin (no sign-in yet) now also refuses what a web page in a crew
  member's browser could send it: a request whose `Origin` is another site
  (cross-site POSTs such as Clear log, and cross-site WebSocket hijacking of
  `/ws/admin`), and a `Host` that is not an IP address, `localhost` or an
  `ADMIN_HOSTS` name (DNS rebinding). Open the admin by the server's IP.
- The admin refuses any request carrying `X-Forwarded-For` / `Forwarded` /
  `X-Real-IP`, so it stays off the gateway even when `TRUST_PROXY` was never
  set (Caddy always adds the header).
- Admin pages can't be framed (`X-Frame-Options: DENY`, `frame-ancestors
  'none'`) and aren't cached; every response is `nosniff` with
  `Referrer-Policy: same-origin`.
- `/healthz` through the gateway answers only `{"ok": true}` (no version or
  audience size).
- systemd unit (install.sh): `NoNewPrivileges`, `PrivateTmp`,
  `ProtectSystem=full`, kernel/cgroup protection, `UMask=0027`. Re-run
  `sudo ./install.sh` to apply.
- Gateway site: HSTS, `nosniff`, `Referrer-Policy: no-referrer`, no `Server`
  header; stale `TRUST_PROXY=1` comment corrected. Re-run the gateway
  installer to apply.
- README: open UDP 5568 to the console only.

Redeploy: `cd ~/PhoneLight && git pull && sudo ./install.sh` (the unit
changed), or `git pull && sudo systemctl restart phonelight` for the app
alone. Gateway VPS: `cd /opt/glow-gateway-src && sudo git pull && sudo bash
gateway/install.sh`.

## [0.8.4] - 2026-10-28

### Changed
- The client id on the phone (bottom right) is now the same 8 characters
  the admin Clients page shows, instead of the full 24.
- The Blocked form accepts that short id (or any unique start of an id, 4+
  characters) and blocks the full id of the matching connected or logged
  phone. It refuses an id that matches no phone (before, it would quietly
  block an id nobody has) or more than one (type more of it).

Redeploy: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`.

## [0.8.3] - 2026-10-28

### Fixed
- Start-screen glow looking blocky (banded) on phones. The halo was a
  mostly transparent gradient (28% at its brightest) under the hue filter:
  GPUs keep such a layer premultiplied in 8 bits, leaving only a dozen or so
  levels in some channels, and the hue filter spreads those steps into every
  colour. The halo is now opaque (the same colours mixed onto the black
  page) with a smooth cosine falloff in 10 stops instead of 3 straight-line
  segments, so there is no ring where the slope changes. Same look, colours
  and size.

Redeploy: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`.

## [0.8.2] - 2026-10-28

### Changed
- The start-screen glow now also cycles colour for guests whose phone asks
  for reduced motion (iOS Reduce Motion, Windows animation effects off,
  Android "remove animations", often on in battery saver). Nothing on the
  start screen moves, so they get the colour change at half speed (12 s a
  turn) without the wordmark's brightness pulse.

Redeploy: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`.

## [0.8.1] - 2026-10-28

### Fixed
- Start-screen glow not cycling colour in Edge and iOS Safari (it did in
  Firefox). The hue animation ran from no filter to `hue-rotate(360deg)`,
  which looks the same as its start; engines that blend the colour matrix,
  or won't blend from `none`, showed no change. It now has explicit steps
  (0, 120, 240, 360 degrees). Still one CSS animation, still off for guests
  whose phone asks for reduced motion.

Redeploy: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`.

## [0.8.0] - 2026-10-28

### Added
- **Kick and block devices** on the admin Clients page. *Kick* disconnects a
  phone (it rejoins by itself). *Block* refuses a client id from now on,
  disconnecting it at once; *Block IP* refuses every phone on an address
  (with a warning: behind NAT that can be many guests). The Blocked list
  adds blocks by hand (client id or IP, with a note), shows how often each
  was refused since the restart, and unblocks. Blocks are stored in the new
  `blocks` table (migration 0002) and checked in memory on connect only.
- Wire format: text `x` (blocked: the page goes black, says "This device
  can't join the show" and stops reconnecting) and `k` (kicked).
- Admin API: `POST /admin/api/clients/kick`, `GET`/`POST /admin/api/blocks`,
  `POST /admin/api/blocks/remove`.

Redeploy: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`
(start.sh applies the new migration).

## [0.7.0] - 2026-10-28

### Added
- **Cells (sub-fixtures).** New settings *Cells per fixture* (1–40, default
  1) and *Cell columns*. Each fixture becomes cells × RGB (× 3 channels, or
  × 6 in 16-bit, cell 1 first, no dimmer) and every phone in it splits its
  screen into a grid of that many cells: *Cell columns* side by side, the
  rest in rows below, cell 1 top left. Changes apply live.
- Wire format: a colour frame carries every cell's RGB, then the show-at
  time (7 bytes with 1 cell, as before; 3 more per extra cell). New text
  message `l<cells>,<columns>` tells the phone its grid; it always reaches
  a phone before frames of the new size.
- Admin Live: fixture swatches show their cells; click a cell to colour just
  that cell (`cell` in `/admin/api/mode`). Chase steps through every cell,
  rainbow spreads over them, identify keeps one hue per fixture. sACN
  monitor labels each channel's cell; Settings shows the cell footprint.
- The guest page prints the phone's client id (as on the Clients page)
  small in the bottom-right corner: white on the start screen, black during
  the show.

### Fixed
- After a settings change the engine republishes even when its output looks
  unchanged, so a frame dropped mid-change can't leave phones black.

Redeploy: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`.

## [0.6.0] - 2026-10-28

### Changed (security)
- **`TRUST_PROXY` is now the gateway's address** (e.g. `TRUST_PROXY=10.201.4.9`,
  IPs or networks), not just `1`. X-Forwarded-For / -Proto are believed only
  from that address; anyone else's are ignored, so nobody can claim another
  IP by sending the header. Before, `TRUST_PROXY=1` believed it from anyone,
  which also let a forged header get past `ADMIN_ALLOW`.
- **The admin is never served through the gateway**, whatever headers a
  request carries: a compromised VPS can't reach it through the tunnel.
- `ADMIN_ALLOW` is always checked against the real connecting address.
- `TRUST_PROXY=1` keeps working (warning logged at start), and any request
  that carries X-Forwarded-For is then refused the admin. Replace it with
  the gateway's address.
- The gateway installer prints the exact `TRUST_PROXY` value (its own
  address on the route to the glow server) and recommends `ADMIN_ALLOW`.

Redeploy the server: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`,
after setting `TRUST_PROXY=<gateway address>` (and `ADMIN_ALLOW`) in `.env`.
Gateway (optional, for the new hint): `cd /opt/glow-gateway-src && sudo git pull`.

## [0.5.1] - 2026-10-28

### Fixed
- `gateway/install.sh` stopped silently at its DNS check when the site name
  did not resolve (`set -o pipefail` + a failing `getent`), skipping the rest
  of the summary. Now it reports the name as not resolving and carries on.

### Added
- The gateway installer checks that `www.<site>` points at the same server as
  the site (a registrar's "URL forwarding" for www stops Caddy getting www's
  certificate) and suggests `--no-www` if not. The unreachable-glow-server
  warning now says guests get the "getting ready" page (HTTP 503) until it is
  fixed.
- gateway/README.md: a **Troubleshooting** table (503, www certificate
  failures, the ZeroSSL `caddy_legacy_user_removed` noise, no certificate).

Redeploy the gateway: `cd /opt/glow-gateway-src && sudo git pull && sudo bash gateway/install.sh`.
The glow server is unchanged.

## [0.5.0] - 2026-10-28

### Added
- **Public gateway installer** (`gateway/install.sh`, guide in
  `gateway/README.md`): puts `https://glow.lighting` on the gateway VPS's
  **shared Caddy** next to 321T's `dpc.321.theater`. Finds Caddy (or installs
  Debian's), adds `import /etc/caddy/sites.d/*.caddy` to the main Caddyfile if
  missing and changes nothing else there, writes only
  `/etc/caddy/sites.d/glow-lighting.caddy`, validates the whole config before
  a graceful reload and restores its files if Caddy rejects them, never starts
  a stopped Caddy (it may be off on purpose). The site proxies the guest page
  and WebSocket, answers 404 for the admin, redirects `www.`, and shows a
  self-retrying "getting ready" page when the glow server is down. Tested
  against Caddy 2.6.2 alongside 321T's real Caddyfile.
- **Guide + tested patch for the 321T team** (`docs/321theater/`), so their
  gateway installer follows the same convention: their site moves (word for
  word) to `/etc/caddy/sites.d/321theater.caddy`, `--rewrite-caddy` only
  regenerates that file, and a rejected config is rolled back.
- **Patch warning on the Live page:** phones in a fixture group whose
  channels have stayed at 0 since sACN started ("Fixture 2 has 1 phone, but
  its channels 4–6 have stayed at 0…").
- **Phone status readout:** long-press the colour on a phone for its fixture,
  connection, round trip, sync accuracy, frames/s, colour and client id.

Redeploy the server: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`.
The gateway is new: install it on the VPS with `gateway/README.md`.

## [0.4.0] - 2026-10-28

### Added
- **sACN timing on the sACN monitor** (and the Live page's sACN card):
  how long packets waited in the server's receive buffer before the app read
  them (Linux kernel timestamps), how long the app took from reading them to
  handing the colours to the phones, and stale packets skipped. Tells you at
  a glance whether a delay is in the server or before it.

### Changed
- **sACN input is latest-wins.** The reader takes everything waiting on the
  socket at once and acts only on the newest packet of each source, so if
  the app ever falls behind it skips stale levels instead of working
  through a backlog (the receive buffer is 1 MB: a backlog there could have
  held seconds of old packets).

### Fixed
- `tools/load_test.py`: fake phones could get a negative clock (random
  offset of up to minus one hour), which the server rightly refuses to sync;
  those phones showed frames on arrival and pulled the reported "shown"
  times down. Offsets are now positive, like a real phone's clock.

Redeploy the server: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`.

## [0.3.1] - 2026-10-28

### Changed
- The product name is written all lowercase: **glow.lighting** (guest
  start screen, admin header, page titles, docs).

Redeploy the server: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`.

## [0.3.0] - 2026-10-28

### Added
- **Glow.lighting** branding: the product name on the guest page and the
  admin (header, page titles). The code name PhoneLight stays for the repo,
  folder, service and code, so install and update commands are unchanged.
- **Glowing start screen:** the Glow.lighting wordmark has a white core
  and a layered RGB halo, with a soft glow behind it and around the Tap to
  start button, all slowly cycling through the colour wheel. Pure CSS (one
  hue-rotate animation, no script, no images; guest page ~7 KB). Still for
  phones set to reduced motion. Stops once the guest taps in.

Redeploy the server: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`.

## [0.2.1] - 2026-10-28

### Fixed
- **Service failed to start on a fresh install** (`class uri 'gevent'
  invalid or not found ... No module named 'packaging'`): gunicorn 26's
  gevent worker imports `packaging` without declaring it. Added to
  requirements.txt. Missed in testing because the dev venv had it through
  pytest.
- `install.sh` now loads the gevent worker and the app right after
  installing packages, so a missing package stops the install with the
  error instead of leaving a crashing service.

Redeploy the server: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`
(start.sh installs the new requirement on that restart).

## [0.2.0] - 2026-10-28

### Removed
- **`tools/sacn_sender.py`**, and the load test's `--sacn` option: no
  sACN transmitter anywhere in PhoneLight, so a test can never become a
  rogue source on the show network. The tests' packet builder now lives in
  `tests/sacn_packets.py` and only targets the test server on 127.0.0.1.

### Added
- **Test tools** on the admin Live page, made inside the engine and sent
  only to the phones: **manual colour** (all fixtures, or click a fixture to
  set just that one; starts from the current look) and **patterns** (chase,
  flash, rainbow; colour and rate). The header shows TEST while active.
  API: `POST /admin/api/mode` takes `fixture`, `pattern`, `rate`.
- `tools/load_test.py --drive`: changes the colour through the admin test
  API and measures end-to-end delay and sync spread (puts the show back on
  Live sACN when done).

### Changed
- "Solid colour" is now the Test tools' manual colour.

Redeploy the server: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`.

## [0.1.0] - 2026-10-28

### Added
- First trial build: a Flask + gevent server (one process, SQLite) that turns
  phones into full-screen RGB lights driven by sACN.
  - **sACN in:** unicast only (UDP 5568, no multicast join), one universe,
    E1.31 sequence checking, preview / non-zero start codes ignored, HTP
    merge of the highest-priority sources, 2.5 s source timeout,
    Stream_Terminated honoured; other universes arriving are listed.
  - **Fixtures:** N RGB fixtures from a start address, 8-bit (3 ch) or 16-bit
    (6 ch); guests split evenly, reconnects keep their group when it is one
    of the emptiest; rebalance on fixture-count change or on demand.
  - **Fan-out:** latest frame wins, never queued. Each changed fixture's
    WebSocket frame is built once and written to its phones' sockets with
    one non-blocking send; slow phones are skipped to the newest colour by
    their own sender. Stuck (not reading) and silent (no pong) sockets are
    closed by a reaper.
  - **Sync:** every frame carries "show at server time" (now + Sync delay,
    default 100 ms); each phone estimates its clock offset NTP-style
    (lowest-round-trip of its last 8 samples, re-synced every 4 s) and paints
    each frame at that moment on its next screen refresh. Phones slower than
    the delay show on arrival. Load test: ~2–4 ms median phone-to-phone spread.
  - **Signal loss:** hold, then fade to black (both settable).
  - **Guest page:** ~6 KB, inline, tap to start, full screen, screen kept
    awake (Wake Lock, or a silent looping video over HTTP), reconnect with
    jittered back-off, black after 3 s disconnected, data-use notice.
  - **Admin:** Live (fixture preview + phone counts, overrides: solid
    colour / identify / blackout, rebalance), sACN monitor (sources + 512
    levels), Clients (live list + log with sessions), Settings (applied
    live). Optional `ADMIN_ALLOW` IP limit; no sign-in yet.
  - **Client log:** id, IP, User-Agent (device / OS / browser), screen,
    language, time zone and more; visits, durations, latency (ping every
    5 s). Writes batched once a second off the guest path; retention purge.
  - `install.sh` (venv in the clone, systemd service as the installing
    user), `start.sh` (deps + migrations on every start), tools:
    `sacn_sender.py` test patterns, `load_test.py` (simulated phones,
    end-to-end delay), pytest suite.

Deploy: first install, see README → Install.
