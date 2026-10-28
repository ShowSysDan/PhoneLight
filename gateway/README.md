# glow.lighting public gateway: install how-to

This puts **https://glow.lighting** on the internet through a gateway server
running **Caddy**, typically the same VPS that already serves 321T's
`https://dpc.321.theater`. Guests reach the glow server inside the building
through it; the admin pages stay on the building network only.

```
 phone ──https──► gateway VPS: Caddy (ports 80 + 443, shared by every app)
                     ├─ dpc.321.theater ─► 321T gateway ─► 321T app   (321T's own site file)
                     └─ glow.lighting    ─────────────────► glow server :5420 (this site file)
                                          (WireGuard tunnel / private network)
```

## How sharing one Caddy works

Only one program can listen on ports 80 and 443, so Caddy does it for every
app and picks the site by the name the browser asked for. Each app gets its
own certificate automatically.

So that no app's installer can break another app's site, the apps follow one
convention:

- The main **`/etc/caddy/Caddyfile`** holds only global options and this line:
  `import /etc/caddy/sites.d/*.caddy`
- **Each app owns exactly one file** in `/etc/caddy/sites.d/`:
  glow.lighting's is `glow-lighting.caddy`, 321T's is `321theater.caddy`.
- An installer only writes its own file, adds the import line if it's
  missing, **validates the whole config before reloading**, and puts its files
  back if Caddy rejects them. It never rewrites the main Caddyfile wholesale and
  never starts a Caddy that is stopped (it may be stopped on purpose).

glow.lighting's installer follows this today. For 321T's installer to follow
it too, see [docs/321theater/SHARED_CADDY.md](../docs/321theater/SHARED_CADDY.md)
(a guide and a ready patch for the 321T team).

## Before you start

| What | Where |
|---|---|
| A DNS **A record** for `glow.lighting` (and `www.glow.lighting` if you want it) pointing at the gateway VPS's public IP | domain registrar / DNS |
| The gateway can reach the glow server's port (`10.201.2.102:5420`) over the tunnel: add a rule like 321T's (VPS → `10.201.2.102`, TCP 5420) | tunnel / building firewall |
| Ports **80 and 443** open on the VPS (already true if 321T's site works) | VPS firewall |
| `sudo` on the VPS; `git` and `python3` installed | VPS |

## Install (on the gateway VPS)

1. Get the PhoneLight code onto the VPS. Only the `gateway/` folder is needed:

   ```bash
   sudo git clone --depth 1 --filter=blob:none --sparse https://github.com/ShowSysDan/PhoneLight.git /opt/glow-gateway-src
   ```

   ```bash
   cd /opt/glow-gateway-src && sudo git sparse-checkout set gateway
   ```

2. Run the installer, giving it the glow server's address on the tunnel:

   ```bash
   sudo bash /opt/glow-gateway-src/gateway/install.sh --upstream 10.201.2.102:5420
   ```

   It reports every step:
   - **Caddy:** finds it (installs Debian's `caddy` package if there is none)
     and lists the sites already configured, which it leaves as they are.
   - **Main Caddyfile:** adds the `import` line if it's missing. Nothing else in
     the file changes. Debian's stock welcome-page Caddyfile is replaced,
     since there's nothing in it to keep.
   - **Site file:** writes `/etc/caddy/sites.d/glow-lighting.caddy`.
   - **Validation:** checks the whole config. If Caddy rejects it, both files
     are restored and nothing is reloaded.
   - **Reload:** reloads Caddy gracefully, so other sites keep serving.
   - **Firewall:** opens 80 and 443 (firewalld or ufw) if needed.
   - **Checks:** whether the glow server answers through the tunnel, and
     whether DNS points here.

   Settings are saved in `/etc/glow-gateway.env`. Backups of anything it
   changed go to `/var/backups/glow-gateway/<date-time>/`.

   Options: `--site <name>` (default `glow.lighting`), `--no-www` (no
   `www.` redirect).

3. On the **glow server** (not the VPS), tell it the gateway's address, so it
   believes the guests' real IPs only from the gateway and never serves the
   admin through it. Limit the admin to the building network too. The
   installer prints the exact `TRUST_PROXY` value (cyclorama's tunnel address).
   Add to `~/PhoneLight/.env`:

   ```
   TRUST_PROXY=10.201.4.9
   ADMIN_ALLOW=10.201.2.0/24
   ```

   (Use your building network for `ADMIN_ALLOW`; include every network the
   crew opens the admin from.)

   Then restart it:

   ```bash
   sudo systemctl restart phonelight
   ```

4. Check it:

   ```bash
   curl -sI https://glow.lighting/ | head -1
   ```

   That should print `HTTP/2 200` once DNS has propagated and Caddy has its
   certificate (usually within a minute of DNS being live). Then open
   https://glow.lighting on a phone.

## What the glow.lighting site does

- `https://glow.lighting/`: the guest page and its WebSocket, proxied to the
  glow server. Over HTTPS, phones use the proper keep-screen-awake API.
- `/admin`: **404 from the internet.** Use the admin on the building network:
  `http://10.201.2.102:5420/admin`.
- `www.glow.lighting` redirects to `glow.lighting`.
- If the glow server or the tunnel is down, guests see a small
  "glow.lighting, getting ready" page that retries by itself, not a blank error.

## Updating and removing

Update (re-uses the saved settings):

```bash
cd /opt/glow-gateway-src && sudo git pull && sudo bash gateway/install.sh
```

Change the glow server's address:

```bash
sudo bash /opt/glow-gateway-src/gateway/install.sh --upstream 10.201.2.103:5420
```

Take glow.lighting off the gateway. 321T and anything else stay up:

```bash
sudo bash /opt/glow-gateway-src/gateway/install.sh --remove
```

## Troubleshooting

| What you see | Meaning | Fix |
|---|---|---|
| `curl -sI https://glow.lighting/` gives **`HTTP/2 503`** | HTTPS works; Caddy can't reach the glow server, so it shows the "getting ready" page | From the VPS: `curl -s --max-time 4 http://10.201.2.102:5420/healthz`. A timeout or refusal means the tunnel / building firewall rule for TCP 5420 is missing, or the `phonelight` service isn't running |
| Caddy log: **`challenge failed` for `www.…`** (`Cannot negotiate ALPN`, or `Fetching https://glow.lighting/`) | `www` doesn't point at the VPS. Usually the registrar's "URL forwarding" for www | Give `www` an A record to the VPS (and turn the forwarding off), or re-run the installer with `--no-www` |
| Caddy log: `caddy_legacy_user_removed` (ZeroSSL) | Caddy 2.6 trying its backup certificate provider after another failure | Noise: fix the failure before it (usually the www one above) |
| No certificate at all, `glow.lighting` itself failing | DNS for `glow.lighting` isn't pointing at the VPS yet, or 80/443 are blocked | `dig +short glow.lighting` should show the VPS's IP; Caddy retries by itself |

Caddy's log: `sudo journalctl -u caddy -n 50 --no-pager`.

## Good to know

- **Stopping Caddy stops every site on it.** That includes 321T's documented
  emergency switch, `systemctl stop caddy`. To take only glow.lighting
  offline, use `--remove` (or stop the `phonelight` service on the glow server;
  guests then get the "getting ready" page).
- **Caddy reloads and phones.** With Caddy 2.7 or newer, the site keeps phones
  connected for up to 10 minutes through any reload (`stream_close_delay`).
  Debian 12's Caddy is 2.6, which drops open WebSockets on every reload, from
  any app's installer. Phones reconnect within a few seconds, so for a
  show it's best not to reload Caddy mid-performance. Moving to Caddy's own
  package repository (2.8+) fixes this for everyone on the VPS, but changes
  321T's Caddy too, so agree it with the 321T team first.
- **Until 321T applies the shared-Caddy patch:** their
  `install.sh --rewrite-caddy` rebuilds the main Caddyfile from its template,
  which drops the `import` line and takes glow.lighting offline. Re-run this
  installer afterwards to put it back. Their normal `install.sh` (without
  `--rewrite-caddy`) is safe: it leaves the main Caddyfile alone while it
  still contains `dpc.321.theater`.
- **Latency:** phones' traffic now goes out through the internet and the tunnel,
  which adds delay compared with the venue network. Raise the Sync delay
  (admin Settings) if phones drift apart, and compare with
  `tools/load_test.py` run from outside the building.
