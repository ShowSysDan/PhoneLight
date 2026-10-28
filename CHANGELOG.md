# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/). Every change
ships as its own version: the top section always matches `VERSION`.

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
