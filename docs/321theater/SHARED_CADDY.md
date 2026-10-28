# Sharing the gateway's Caddy: a guide for the 3·2·1→Theater team

**For:** whoever looks after the 321T public gateway (VPS **cyclorama**,
`https://dpc.321.theater`).
**Ask:** apply one patch to `gateway/install.sh` (and a comment in
`gateway/Caddyfile.example`) so 321T's installer and glow.lighting's installer
can share Caddy on the VPS without either one ever breaking the other.
**Patch:** [`shared-caddy.patch`](shared-caddy.patch), made against 321Theater
`main` at `4f799c4`.

## Why

glow.lighting (code name PhoneLight, a crowd-lighting app from the same team)
needs its own public site, `https://glow.lighting`, on the same VPS. Only one
program can own ports 80 and 443, and on cyclorama that's Caddy. Caddy can
serve both sites side by side, since it chooses by host name and gets a
certificate for each. The only problem is the **installers**:

- Today, 321T's `install.sh` treats `/etc/caddy/Caddyfile` as its own file.
  Normally it leaves the file alone while the file contains
  `dpc.321.theater`, which is safe. But with `--rewrite-caddy`, or whenever
  that name is missing, it **regenerates the whole file** from
  `Caddyfile.example`, which deletes any other app's site.
- glow.lighting's installer is already careful (see "What glow.lighting's
  installer does" below), but it can't stop a later full rewrite.

## The convention (both apps)

```
/etc/caddy/Caddyfile                 global options (if any) + one line:
                                     import /etc/caddy/sites.d/*.caddy
/etc/caddy/sites.d/321theater.caddy  321T's site      (written only by 321T's install.sh)
/etc/caddy/sites.d/glow-lighting.caddy glow.lighting  (written only by glow's install.sh)
```

Rules every installer follows:

1. Write **only your own file** in `sites.d/`.
2. If the main Caddyfile lacks the `import` line, add it after any global
   options block, and change **nothing else** in the main file.
3. Run `caddy validate` on the **whole** config before reloading. If it fails,
   put your files back and don't reload, so the running sites stay as they were.
4. `systemctl reload caddy` (graceful), never a restart when a reload will do.
5. Keep backups only of what you changed.

## What the patch changes in 321T

Only the "Caddy" section of `gateway/install.sh`, plus the header comment of
`gateway/Caddyfile.example`. Packages, gateway.env, the service, firewalld and
fail2ban are untouched.

| | Before | After |
|---|---|---|
| Where 321T's site lives | the whole `/etc/caddy/Caddyfile` | `/etc/caddy/sites.d/321theater.caddy` |
| Normal re-run | leaves the Caddyfile alone if it mentions the site | leaves `321theater.caddy` alone if it exists |
| `--rewrite-caddy` | regenerates the **whole Caddyfile** | regenerates **only** `321theater.caddy` |
| Main Caddyfile | overwritten from the template | gets the `import` line if missing; otherwise untouched |
| Caddy rejects the config | warns; the broken file stays on disk | restores both files, doesn't reload, keeps copies |
| Backups | `Caddyfile.bak.<time>` on every rewrite | only kept when a run had to roll back |

### The first run after the patch (migration)

On the VPS as it is today, the first run of the patched installer:

1. finds the `dpc.321.theater { … }` block in `/etc/caddy/Caddyfile`;
2. moves it, **word for word** (so any hand edits survive), into
   `/etc/caddy/sites.d/321theater.caddy`;
3. leaves the main Caddyfile with its other content plus the `import` line;
4. validates and reloads.

Nothing about how dpc.321.theater behaves changes: same block, same
upstreams, same gate, same certificate. The block finder ignores braces
inside comments, so the commented-out multi-server example inside the
block doesn't confuse it.

## How it was tested

Against Caddy 2.6.2 (Debian 12's version), running the patched section with
`/etc/caddy` swapped for a test directory:

| Case | Result |
|---|---|
| Today's VPS: 321T's generated Caddyfile + glow.lighting installed next to it | 321T block moved byte-for-byte; Caddy then serves `dpc.321.theater`, `glow.lighting`, `www.glow.lighting` |
| Re-run | no file changes |
| `--rewrite-caddy` with two app servers | only `321theater.caddy` regenerated (with the health-checked block); main Caddyfile and glow's file untouched |
| A template Caddy rejects | both files restored exactly, no reload, copies kept |
| Brand-new VPS (Debian's stock Caddyfile) | clean layout: import line + `321theater.caddy` |

## Applying it

In a 321Theater checkout:

```bash
git checkout -b shared-caddy
```

```bash
git apply /path/to/PhoneLight/docs/321theater/shared-caddy.patch
```

```bash
git diff --stat
```

You should see `gateway/Caddyfile.example | 6` and `gateway/install.sh | 114`.

Review, merge, then on cyclorama run the usual update:

```bash
cd /opt/321gateway-src && git pull && bash gateway/install.sh
```

Then check it:

```bash
ls /etc/caddy/sites.d/
```

```bash
grep -n import /etc/caddy/Caddyfile
```

```bash
curl -sI https://dpc.321.theater/robots.txt | head -1
```

### If something looks wrong

The installer refuses to reload a config Caddy rejects and puts the files
back. To undo the move by hand, copy `321theater.caddy`'s contents back into
`/etc/caddy/Caddyfile` in place of the `import` line, then run
`systemctl reload caddy`.

## What glow.lighting's installer does on cyclorama

`PhoneLight/gateway/install.sh` (full guide: `gateway/README.md` in that repo):

- lists the sites already configured and **leaves them as they are**;
- refuses to run if `glow.lighting` is already configured anywhere else;
- adds `import /etc/caddy/sites.d/*.caddy` to the main Caddyfile if missing.
  Today that's the only change it makes to 321T's file, and 321T's normal
  `install.sh` keeps working afterwards because the file still contains
  `dpc.321.theater`;
- writes `/etc/caddy/sites.d/glow-lighting.caddy`, validates the whole config,
  restores its files and stops if Caddy rejects it, then reloads gracefully;
- **never starts a stopped Caddy.** Your emergency off switch
  (`systemctl stop caddy`) stays off until a person turns it back on.

Until this patch is in, 321T's `install.sh --rewrite-caddy` would drop the
import line, which takes glow.lighting offline (321T is unaffected). Re-running
glow's installer puts it back.

## Things to agree between the two teams

- **The emergency off switch now covers both sites.** `systemctl stop caddy`
  takes dpc.321.theater *and* glow.lighting offline. To take only one site off:
  for 321T, `systemctl stop 321gateway` (visitors get your branded
  "unavailable" page); for glow, its installer's `--remove`.
- **Caddy reloads.** Debian's Caddy 2.6 closes open WebSockets on every reload.
  321T doesn't use long-lived connections, but glow.lighting's phones do (they
  reconnect within seconds). Please avoid reloading Caddy during a glow show,
  or consider moving the VPS to Caddy's own package repository (2.8+), where
  glow's site keeps connections open through reloads. That upgrade changes
  321T's Caddy too, so it's a joint decision.
- **Ports 80/443, firewalld, fail2ban:** shared and unchanged. Both installers
  add the same `http`/`https` firewalld services, which is harmless if repeated.
  glow.lighting adds no fail2ban jail.
