# glow.lighting

Version **0.8.4** · see [CHANGELOG.md](CHANGELOG.md)

> **glow.lighting** is the product name: it is what guests and the admin
> see. The code name **PhoneLight** stays for the repo, the folder
> (`~/PhoneLight`), the `phonelight` service and the code, so installs and
> update commands don't change.

Crowd lighting for festivals: the audience opens a web page on their phones,
taps once, and their whole screen becomes a light that the lighting console
drives over **sACN** (E1.31). The server splits the crowd evenly into
**fixtures** (groups), so a console can chase, split and colour sections of
the crowd like any other RGB fixtures. A fixture can also have **cells**:
each of its phones' screens is split into that many areas, each its own
RGB, like a pixel bar.

> **Trial build.** SQLite for now. If PhoneLight goes ahead it moves to
> PostgreSQL like our other apps (see [Moving to PostgreSQL](#moving-to-postgresql)).
> Plain HTTP on the internal network for now; HTTPS on a public domain later.

## Contents
- [How it works](#how-it-works)
- [Install (step by step)](#install-step-by-step)
- [Console setup](#console-setup)
- [Using the admin](#using-the-admin)
- [Blocking a device](#blocking-a-device)
- [Testing without a console](#testing-without-a-console)
- [Keeping phones in sync](#keeping-phones-in-sync)
- [Finding a delay](#finding-a-delay)
- [Performance](#performance)
- [What is logged about guests](#what-is-logged-about-guests)
- [Configuration (.env)](#configuration-env)
- [Public access: the gateway (HTTPS, glow.lighting)](#public-access-the-gateway-https-glowlighting)
- [Updating, logs, backups, uninstall](#updating-logs-backups-uninstall)
- [Development](#development)
- [Moving to PostgreSQL](#moving-to-postgresql)
- [Known limits](#known-limits)

## How it works

```
 Lighting console ──sACN unicast, UDP 5568──┐
                                            ▼
 ┌──────────────────── PhoneLight server (one process) ─────────────────────┐
 │ sacn.Receiver   one universe, HTP merge of the highest-priority sources  │
 │      │  every accepted packet, straight through (no queue)               │
 │      ▼                                                                   │
 │ engine.Engine   levels → one RGB per fixture cell (8- or 16-bit), master,│
 │      │          signal-loss hold + fade, admin overrides                 │
 │      ▼          publishes only when a colour actually changes            │
 │ hub.Hub         guests split evenly across fixtures; each changed        │
 │      │          fixture's frame (colour + show-at time) built ONCE,      │
 │      │          written to its phones; slow phones get the newest only   │
 │      ▼                                                                   │
 │ tracker         connects, durations, latency → SQLite (batched, 1/s)     │
 └───────────────────────────────┬──────────────────────────────────────────┘
                                 │ WebSocket (ws://server:5420/ws)
                ┌────────────────┼────────────────┐
             phone            phone            phone    … (full-screen colour)
```

- **Guest page** (`/`): one ~10 KB HTML file, no frameworks, no external
  files. A **glow.lighting** start screen whose wordmark and button glow
  with a halo that cycles through the colour wheel (pure CSS, one animated
  filter; for guests whose phone asks for reduced motion it still changes colour,
  at half speed, without the wordmark's pulse: nothing on it moves). Tap to start → full screen, screen kept awake, WebSocket opened.
  Each colour arrives as 7 bytes (R, G, B + when to show it; 3 more bytes
  per extra cell) and is painted at that moment (see
  [Keeping phones in sync](#keeping-phones-in-sync)). The first 8 characters
  of the phone's client id (as on the admin Clients page) are printed small in the bottom-right
  corner: white on the start screen, black once the show is running.
  If the connection drops the phone goes black after 3 s and reconnects
  (with a random delay, so a server restart is not hit by every phone at once).
- **Latest frame wins.** Nothing is ever queued for a phone. If a phone (or
  its Wi-Fi) can't keep up, it skips straight to the current colour.
- **Fixtures:** with *Fixtures = 3*, every new phone joins whichever group has
  the fewest phones, so the groups stay even. Fixture 1 starts at the *Start
  address*; the rest follow on: 3 channels each (R G B) in 8-bit mode, 6 in
  16-bit mode (R, R fine, G, G fine, B, B fine). A phone that reconnects
  goes back to its old group when that group is one of the emptiest.
  Changing the fixture count (or pressing *Rebalance groups*) deals everyone
  out again evenly.
- **Cells:** with *Cells per fixture = 6*, a fixture is 6 RGB cells (18
  channels in 8-bit, 36 in 16-bit, cell 1 first) and every phone in it
  splits its screen into 6. *Cell columns* sets the grid: 1 = six stripes
  stacked top to bottom, 2 = 2 × 3, 6 = six side by side; cell 1 is top left,
  then left to right, row by row. Every fixture has the same number of cells
  (up to 40). Changes apply live: phones redraw their grid at once.
- **sACN merge:** HTP. Among the sources heard in the last 2.5 s, only those
  at the highest priority count, merged highest-value-wins per channel.
- **Signal loss:** sACN is *lost* after 2.5 s without a packet (or at once
  when the console ends its stream). Phones then hold the last look for
  *Signal-loss hold* seconds, then fade to black over *Signal-loss fade*
  seconds. Both are on the Settings page.

## Install (step by step)

We always install in the service user's **home directory**, in a Python
**venv** inside the clone (`~/PhoneLight/venv`). Tested on Ubuntu 22.04/24.04
and Debian 12; any Linux with systemd and Python 3.9+ works.

### 1. Prepare the server

Give the server a **fixed IP** on the show network (DHCP reservation or a
static address): the console sends unicast to that address and the QR code
points at it.

```bash
sudo apt-get update
```

```bash
sudo apt-get install -y git python3 python3-venv python3-pip
```

### 2. Clone into your home directory

Log in as the user that will run the service (not root), then:

```bash
cd ~
```

```bash
git clone https://github.com/ShowSysDan/PhoneLight.git
```

```bash
cd ~/PhoneLight
```

### 3. Run the installer

```bash
sudo ./install.sh
```

It:
1. creates the venv at `~/PhoneLight/venv` and installs `requirements.txt` into it
   (nothing is installed system-wide with pip);
2. creates `~/PhoneLight/.env` from `.env.example` (mode 600; the defaults work);
3. creates the SQLite database at `~/PhoneLight/data/phonelight.db`;
4. installs and starts the systemd service `phonelight`, running as **you**
   (the user who ran `sudo`), from `~/PhoneLight/start.sh`;
5. prints the guest and admin URLs.

Run it without `sudo` (`./install.sh`) to set up only the venv and database,
no service. Then start it in the foreground with `./start.sh` (Ctrl-C to stop).

### 4. Open the firewall (if one is on)

The guests need TCP 5420; the console needs UDP 5568.

```bash
sudo ufw allow 5420/tcp
```

```bash
sudo ufw allow 5568/udp
```

### 5. Check it

```bash
systemctl status phonelight
```

```bash
curl http://localhost:5420/healthz
```

Then open `http://<server-ip>:5420/admin` in a browser.

### 6. Lock down the admin (recommended)

There is no admin sign-in yet, so anyone who can reach the server could open
`/admin`. Limit it to your control network: edit `~/PhoneLight/.env`, set
for example `ADMIN_ALLOW=192.168.10.0/24` (IPs and networks, comma-separated),
then:

```bash
sudo systemctl restart phonelight
```

### 7. Make the QR code

Point the audience QR code at `http://<server-ip>:5420/`. (Later, with a
public domain and HTTPS, it becomes `https://<domain>/`.)

### Running it by hand (no systemd)

```bash
cd ~/PhoneLight && ./install.sh && ./start.sh
```

## Console setup

1. Patch *Fixtures* × generic **RGB** fixtures (8-bit: 3 channels; 16-bit:
   6 channels) from the *Start address* in one universe. With cells, patch
   each as a multi-cell RGB fixture (pixel bar) of *Cells per fixture*
   cells: cells × 3 channels (× 6 in 16-bit), cell 1 first, no dimmer channel.
2. Set that universe to output **sACN unicast** to the PhoneLight server's IP
   (no multicast needed; PhoneLight does not join multicast groups).
3. On the admin **Settings** page set the same universe, start address,
   fixture count and 8/16-bit mode.
4. The **sACN monitor** page shows the source, its frame rate, and all 512
   levels with the fixtures' channels outlined. If the console sends to a
   different universe, that page says so ("Also arriving (ignored): universe …").

Two consoles (main + backup) work: give the backup a lower priority, or the
same priority to merge them HTP.

## Using the admin

| Page | What it shows / does |
|---|---|
| **Live** | Phones connected, average round-trip latency, output frames/s, sACN rate; a live swatch per fixture with its phone count (with cells, laid out like the phones' screens). Output buttons: **Live sACN**, **Identify** (every fixture a different colour, to check groups), **Blackout**, **Rebalance groups**. A **warning** appears when phones sit in a fixture group whose channels have stayed at 0 since sACN started: almost always a patch problem (the group isn't in the cue). **Test tools:** manual colour (all fixtures, click one fixture, or click one cell) and patterns (chase / flash / rainbow); see [Testing without a console](#testing-without-a-console). Overrides and tests are not saved: a restart goes back to Live sACN. |
| **sACN monitor** | Listening address, packet counts, every source (name, IP, priority, fps, out-of-order packets, CID), the 512 levels (fixture channels outlined R/G/B; hover a cell for fixture and colour). **Timing** (see [Finding a delay](#finding-a-delay)): how long packets waited before the app read them, how long the app took to hand the colours to the phones, and stale packets skipped. |
| **Clients** | Phones connected now (fixture, IP, device, OS, browser, connected time, last and average latency), each with **Kick** / **Block** / **Block IP**. **Blocked** list (add by client id or IP, with a note; *Unblock*). Client log: every browser seen, visits, total time, average latency, device details; click one for its sessions, or *Block* it. *Clear log* deletes the history. See [Blocking a device](#blocking-a-device). |
| **Settings** | Universe, start address, fixtures, 8/16-bit, cells per fixture and their columns, master dimmer, sync delay, signal-loss hold and fade, how long client data is kept. Applied live, no restart. |

The header always shows the sACN state, how many phones are connected and,
in amber, any active override.

**On a phone:** long-press anywhere on the colour (0.7 s) for a status
readout that disappears after 6 s: its fixture, connected or reconnecting,
round trip, sync accuracy, frames per second, current colour and the first
characters of its client id (to find it on the Clients page). Handy for
checking a phone in the crowd without the admin.

## Blocking a device

On the **Clients** page:

- **Kick** disconnects a phone. It reconnects by itself a few seconds later
  (and may land in another fixture group), so it is a reset, not a ban.
- **Block** (client id) disconnects it at once, and every time it tries
  again it is refused. The phone goes black with "This device can't join the
  show" and stops reconnecting. Blocks are kept in the database, so they
  survive a restart, until you press *Unblock*. You can also type the client
  id printed bottom right on the phone (8 characters, as the Clients page
  shows it) into the Blocked form; it is matched to the full id of a
  connected or logged phone (type more if two start the same way).
- **Block IP** refuses every phone on that address. Behind a venue Wi-Fi or
  a mobile network's NAT many guests share one address, so this can block a
  whole crowd. Use it only for a lone device on its own address.

A client id is per browser: someone who clears their browser data (or uses
private browsing) comes back with a new id and has to be blocked again. Web
pages cannot see a phone's MAC address or a hardware id (see
[What is logged about guests](#what-is-logged-about-guests)).

The block check runs once per connection, against an in-memory copy of the
list, so it costs the show nothing.

## Testing without a console

PhoneLight **never transmits sACN**, and there is deliberately no sACN
sender in the repo, so a test can never leak onto the show network. To make
colours without a console, use the **Test tools** on the admin Live page.
They are generated inside the server and go only to the phones:

- **Manual colour:** pick a colour, then *All fixtures*, or click a fixture's
  swatch to give just that fixture the colour (the others keep their look).
  With cells, clicking a cell colours just that cell (handy for checking the
  cell order); click the fixture's number bar for the whole fixture.
- **Pattern:** chase, flash or rainbow, with a colour and a rate per second.
  With cells the chase steps through every cell (fixture 1 cell 1 first) and
  the rainbow spreads over every cell.
  A flash is the easiest way to see whether phones are changing in sync.

The header shows **TEST** in amber while either is active; *Live sACN* hands
the phones back to the console.

`tools/load_test.py` simulates phones and measures them (run it from the
server's venv):

```bash
~/PhoneLight/venv/bin/python tools/load_test.py http://<server-ip>:5420 -n 500 -t 30 --drive
```

With `--drive` it changes the colour itself through the same admin test API
(no sACN) and reports the **end-to-end delay** from the change to each
simulated phone receiving it, when the phone would show it (with the Sync
delay) and the **sync spread** between phones. It puts the show back on
Live sACN when it finishes, so don't run it during a show. Without
`--drive` it just measures whatever is playing (the console or a test
pattern). Run it from a second machine to include the real network.

## Keeping phones in sync

Phones have different latencies (Wi-Fi, power saving, how busy the phone
is), so "show it when it arrives" puts them tens of milliseconds apart.
PhoneLight uses a lightweight NTP-style sync, not PTP:

1. **Each phone works out its clock offset from the server.** It sends
   its clock reading (`performance.now()`, a steady clock that does not jump
   when the phone's time is set) and the server answers with when it got
   the message and when it replied. From that one round trip the phone
   computes the offset and the round-trip time. It keeps the
   lowest-round-trip sample of its last 8 (the least delayed one is the most
   accurate), taking 6 quickly on connect, then one every 4 s, which also
   tracks clock drift.
2. **Every frame says when to show it:** "server time T", where T = the
   moment the console's change was processed + **Sync delay** (Settings,
   default 100 ms). It is the same 7 bytes (more with cells) for every phone, so the server
   still builds each frame once.
3. **Each phone paints at T on its own clock**, on its next screen refresh.
   A phone that got the frame after T (its latency is above the delay)
   shows it at once. A newer frame always replaces older ones.

So the server doesn't time each phone. It sets one deadline per frame and
each phone meets it with its own clock. The trade-off is the **Sync delay**: every change reaches the crowd that much after
the console sends it, and every phone with less latency than that changes
together. Raise it on a congested network (more phones in sync, more delay);
set it to 0 for "as fast as possible, no sync".

How close: in the load test (`tools/load_test.py --drive`), with each
simulated phone on its own random clock, every phone showed each frame
within **~2–3 ms (200 phones) / ~4 ms (1000 phones) median**, ~100 ms after the
console. Three real browser tabs flashing together matched to within 1 ms.
Real phones add their own screen timing (up to one refresh, ~16 ms at 60 Hz)
and any asymmetry in their Wi-Fi path, so expect them to be within about one
or two screen refreshes of each other.

## Finding a delay

If the crowd changes noticeably after the console, the sACN monitor's
timing cards show how much of that is the server:

- **Waiting to be read:** from the packet reaching this server's network
  card to the app reading it (kernel timestamp, Linux).
- **App processing:** from reading it to the colour being handed to every
  phone's connection.

Both measure a few milliseconds or less (0.3 ms and ~2 ms with 200 phones
in testing). The input is latest-wins like the output: everything waiting is
read in one go and only the newest packet of each source is used, so the app
can never work through a backlog. **Stale packets skipped** counts what was
skipped; it should stay at or near 0.

Then the phones add the **Sync delay** (Settings, default 100 ms) on purpose
so they change together. Set it lower (0 = no sync) to compare. The admin
test tools go through exactly the same path from the engine onwards, so if
they feel fast and the console doesn't, the difference is before the packet
reaches the server: the console's output (cue times, output rate or delay
settings), a node or converter in between, or the network path (Wi-Fi
bridges, tunnels).

## Performance

**Cells (0.7.0):** 1000 phones with 6 cells each (22-byte frames) ran the
same as 1 cell: 40 frames/s on every phone, ~105 MB, change → arrival
median 12 ms, spread median 6 ms, all within run-to-run noise of the code
without cells on the same VM (13.6 ms / 5.6 ms).

Measured on a 4-core cloud VM, server and simulated phones on the same
machine, 40 colour changes a second (the worst case), Sync delay 100 ms.
The 0.1.0 figures came from sACN input; from 0.2.0 the load test drives the
colour through the admin test API, which adds the HTTP request (a few ms):

| Phones | Frames/s per phone | Change → phone arrival (median / p99) | Phone-to-phone spread (median / p95) | Server CPU | Server memory |
|---|---|---|---|---|---|
| 200 | 40 (all that were sent) | 6–7 ms / 20–24 ms | 2–3 ms / 3–4 ms | ~7% of one core | ~55 MB |
| 1000 | 40 | 7–13 ms / 26–30 ms | 4 ms / 6–9 ms | ~25–33% of one core | ~95 MB |

- Memory stays flat over thousands of connects and disconnects (no growth per
  connection or with uptime). Every list in the server has a cap or is pruned.
- 50 phones that stopped reading were all closed by the server (ping
  timeout) while 300 others ran at full rate, unaffected.
- On a real show the limit is the **Wi-Fi**, not the server: phones on a busy
  network will see more delay, and when they can't keep up they get fewer
  frames (always the newest), not late ones. Python reading 40 fps of sACN is
  trivial; the server does almost nothing when the colours aren't changing.

## What is logged about guests

The guest page shows a short notice before the tap. What a web server can
actually learn about a phone:

| Field | Source |
|---|---|
| Client id | Random id the page keeps in localStorage + a cookie: how we know a phone came back |
| IP address | As seen by the server (behind NAT, the router's) |
| Device, OS, browser | Parsed from the User-Agent (Android hides the model on newer Chrome: shows "Android") |
| Screen, pixel ratio, language, time zone, touch points, CPU cores, memory, network type, installed-as-app | Reported by the page in its hello |
| Visits, connect / disconnect times, duration, fixture | Server |
| Latency | Round trip of a ping every 5 s: last, average, min, max per session; running average per client |

**MAC addresses are not available** to a web page or a web server on a routed
network, and modern phones randomise them per Wi-Fi network anyway. Rows not
seen for *Keep client data (days)* (default 30) are deleted hourly; *Clear
log* on the Clients page deletes them all.

## Configuration (.env)

`~/PhoneLight/.env` (from `.env.example`, documented there). Restart after
editing: `sudo systemctl restart phonelight`.

| Key | Default | |
|---|---|---|
| `APP_PORT` | 5420 | Guest + admin HTTP port |
| `ADMIN_ALLOW` | (anyone) | IPs / networks allowed to open `/admin` |
| `SACN_BIND` | 0.0.0.0 | Listen on one interface's IP only |
| `SACN_PORT` | 5568 | sACN UDP port |
| `DB_PATH` | data/phonelight.db | SQLite file (relative = inside the app directory) |
| `MAX_CONNECTIONS` | 4000 | Open sockets the server accepts |
| `TRUST_PROXY` | off | Behind the gateway: the gateway's address as this server sees it (e.g. `10.201.4.9`). Only its forwarded headers are believed, and requests through it never get the admin (see [the gateway](#public-access-the-gateway-https-glowlighting)) |
| `ACCESS_LOG` | off | 1 = log every HTTP request |

Show settings are on the admin Settings page (stored in the database).

## Public access: the gateway (HTTPS, glow.lighting)

To put glow.lighting on the internet at `https://glow.lighting`, run
`gateway/install.sh` on the gateway server (the VPS that already serves
321T's `dpc.321.theater`). It adds a glow.lighting site to the **shared Caddy**
there without touching 321T's site, keeps the admin off the internet, and
validates before reloading. Step-by-step: **[gateway/README.md](gateway/README.md)**.

For the 321T team, so that their installer shares Caddy the same way:
**[docs/321theater/SHARED_CADDY.md](docs/321theater/SHARED_CADDY.md)** (guide +
a tested patch for their `gateway/install.sh`).

On the glow server, set `TRUST_PROXY` to the gateway's address (the installer
prints it, e.g. `TRUST_PROXY=10.201.4.9`) and `ADMIN_ALLOW` to the building
network. Then guests' real IPs are logged, nobody can fake one, and the admin
can't be reached through the gateway even if the VPS were compromised.

## Updating, logs, backups, uninstall

Update (start.sh re-installs requirements when they change and applies any
new database migration on every start):

```bash
cd ~/PhoneLight && git pull && sudo systemctl restart phonelight
```

Logs:

```bash
journalctl -u phonelight -f
```

Back up the database (safe while running; needs `sudo apt-get install -y sqlite3`):

```bash
sqlite3 ~/PhoneLight/data/phonelight.db ".backup ~/phonelight-$(date +%F).db"
```

Uninstall:

```bash
sudo systemctl disable --now phonelight && sudo rm /etc/systemd/system/phonelight.service && sudo systemctl daemon-reload
```

Then delete `~/PhoneLight` if you don't want the data.

## Development

```bash
cd ~/PhoneLight && python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt
```

```bash
venv/bin/python -m server
```

(dev server on `APP_PORT`, gevent like production)

```bash
venv/bin/python -m pytest tests/
```

The tests start a real server in a subprocess, send real sACN over UDP and
connect a real WebSocket, each run on its own throw-away database.

Layout:

```
server/
  app.py         Flask app, routes, the guest and admin WebSockets, Runtime
  sacn.py        E1.31 parse, per-source state, HTP merge, UDP receiver
  engine.py      levels -> fixture colours, modes, loss hold/fade
  hub.py         guests, fixture assignment, fan-out, pings, reaper
  tracker.py     client/session log (batched writes), UA parsing, purge
  settings.py    show settings: definitions, validation, live apply
  blocks.py      blocked devices (client id / IP), checked on connect
  db.py          SQLite access     migrate.py + migrations/   schema
  templates/     guest.html (the phone page) + admin pages
  static/        admin CSS/JS, bundled fonts, keepawake.mp4
tools/           load_test.py (simulated phones; no sACN sender, on purpose)
```

## Moving to PostgreSQL

Kept easy on purpose: timestamps are ISO-8601 UTC text (→ `TIMESTAMPTZ`), ids
are app-made text (no autoincrement), upserts use `ON CONFLICT`, and no
SQLite-only SQL is used. The move is: `db.py` on psycopg2 with a pool and its
own schema (like QSYSRadio), `?` → `%s`, the migrations rewritten for PG
types, and a one-off copy of the `clients` / `sessions` / `settings` rows.
The guest path never touches the database, so nothing on it changes.

## Known limits

- **One server process.** The sACN receiver, the engine and every phone's
  socket share memory, so `start.sh` runs exactly one gunicorn worker
  (gevent). Don't raise it. One process measured fine at 1000 phones.
- **Screen sleep over HTTP.** The Wake Lock API needs HTTPS, so for now the
  page plays a tiny silent looping video to keep the screen on (the same
  trick as the NoSleep.js library; not yet tried on real phones here, so
  check on a few iPhones and Androids before a show). With HTTPS the Wake
  Lock API is used first.
- **iPhone full screen.** iOS Safari has no full-screen API for pages; the
  address bar stays unless the guest adds the page to the home screen. The
  colour still fills the page.
- **No admin sign-in** yet: use `ADMIN_ALLOW`. Through the gateway the admin
  is not reachable at all (404).
- **Caddy 2.6 reloads drop phones.** On the gateway, Debian's Caddy closes
  WebSockets on every reload (phones reconnect in seconds); Caddy 2.7+ keeps
  them (see gateway/README.md).
