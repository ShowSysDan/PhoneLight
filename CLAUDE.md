# PhoneLight (glow.lighting): notes for Claude

Product name **glow.lighting** in everything guests and admins see; code name
PhoneLight for the repo, folder, service and code (don't rename those).

Crowd lighting: phones open a page and become full-screen RGB lights driven
by sACN. Flask + gevent, ONE process, SQLite for the trial (PostgreSQL if it
goes ahead). README.md has the design, install and performance figures.

## Every change
- Every change (each commit the user gets) is its own version: bump `VERSION`
  (semver: patch = fix, minor = feature, major = breaking) and add a new top
  section `## [X.Y.Z] - YYYY-MM-DD` to CHANGELOG.md. No `[Unreleased]` section.
- Document it in the same commit: README.md wherever the change shows, plus
  `.env.example` for new keys.
- Tell the user what to redeploy: `cd ~/PhoneLight && git pull && sudo systemctl restart phonelight`.
- Installs live in the service user's home directory, in a venv inside the
  clone (`~/PhoneLight/venv`). Never pip-install system-wide.

## The hot path: sACN -> phones (keep it fast and flat)
- One process, one gunicorn gevent worker (start.sh). Never more workers:
  receiver, engine and sockets share memory. Use `threading` (gevent patches
  it) so the tests run without gevent.
- The engine runs inside the sACN reader on every accepted batch; no
  queues between receiver, engine and hub. Publish only on change.
- Input is latest-wins too: `Receiver._read` drains the socket and
  `handle_batch` uses only the newest packet per source (`stale` counts the
  rest). Keep the timing (`Receiver.timing`) cheap: accumulators, rolled
  once a second.
- LATEST WINS, never queue frames for a phone. Fast path: hub.publish
  writes each fixture's pre-built frame (`hub.frame`) with one
  non-blocking send; anything else (busy sender, full buffer, partial
  write) goes to the guest's own sender, which sends what is current.
- One writer per socket at a time (`Guest.busy` / `pending`); never write
  to a guest socket from anywhere but `hub.publish` (fast path) and `hub.serve`.
- The guest path never touches the database: tracker.py queues statements
  (bounded, `QUEUE_MAX`; overflow is dropped and counted) and one writer
  commits them once a second.
- Nothing may grow with uptime or connections: every dict/list is pruned or
  capped (sources expire, other universes pruned, guests removed on leave,
  tracker queue capped, admin guest list capped). Check memory with
  `tools/load_test.py` churn when touching this.
- After a hot-path change run `tools/load_test.py ... -n 1000 --drive` (split
  across processes) and compare with README → Performance.

## Sync
- Frames carry show-at server ms (`hub.server_ms()`, monotonic, uint32 mod
  2^32); phones convert with their NTP-style offset ('s' messages) and paint
  on requestAnimationFrame. Never stamp per guest (one frame per fixture).
  The sync reply's t2 is stamped as it is sent (`next_message`), so sync
  replies go first. `tools/load_test.py --drive` prints the sync spread.

## Never transmit sACN
- PhoneLight only RECEIVES sACN. No sACN sender anywhere in the repo (no
  tool, no test pattern over UDP): the user does not want a rogue source on
  the show network. Test colours are made inside the engine (`color` /
  `pattern` modes, admin Live page). The E1.31 builder in
  `tests/sacn_packets.py` is for the tests only, to the test server on 127.0.0.1.

## Guest page (server/templates/guest.html)
- Stays one small inline file (no frameworks, no external requests except
  /static/keepawake.mp4). Rendered once at startup. Check its size.
- Wire format is in hub.py's docstring; change both ends together.
- It is a Jinja template: never write `{#` in its CSS/JS (Jinja comment;
  the server will not boot). Put a space after the `{`.
- Start-screen glow is CSS only (one hue-rotate animation); no JS timers or
  images for decoration, and keep the reduced-motion rule.

## Gateway (gateway/: Caddy on the VPS, shared with 321T)
- Shared-Caddy convention: main Caddyfile = global options + `import
  /etc/caddy/sites.d/*.caddy`; glow writes ONLY `sites.d/glow-lighting.caddy`.
  Never rewrite the main Caddyfile wholesale, never touch another app's site,
  validate the whole config before reloading and restore on failure, never
  start a stopped Caddy. docs/321theater/ is the same contract for 321T.
- Debian's Caddy is 2.6: no `stream_close_delay` (only added for >= 2.7).
  Test changes against a real `caddy validate` (the cloud sandbox can
  `apt-get install caddy`), including next to 321T's Caddyfile.example.

## Database (SQLite now, PostgreSQL later)
- Migrations: a NEW `server/migrations/NNNN_name.sql` per change; never edit
  one that has been pushed (checksummed). Applied by start.sh and create_app.
- Keep SQL portable: ISO UTC text timestamps (`db.now()` / `db.iso()`),
  app-made text ids, `ON CONFLICT` upserts, no SQLite-only functions, date
  maths in Python.
- Show settings are defined ONLY in `server/settings.py` (label, range,
  default, help); the Settings page is generated from them.

## Admin
- One `/ws/admin` feed per page (~10 Hz); pages ask for extras with
  `{"views": [...]}` (`levels`, `guests` at 1 Hz). Escape every value
  (`PL.esc`); write the DOM only on change (`PL.setText` / `setHTML`).
- No sign-in yet; `ADMIN_ALLOW` limits /admin by IP.

## Tests
`venv/bin/python -m pytest tests/` (starts a real server in a subprocess;
own temp database). Add a test for every bug fixed.

## Git
Develop on the branch you are given; commit with clear messages; never
include model names in commits or code.
