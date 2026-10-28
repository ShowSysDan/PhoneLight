# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/). Every change
ships as its own version: the top section always matches `VERSION`.

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
