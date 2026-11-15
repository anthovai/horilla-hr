# OrvaHR SaaS on one Mac mini — runbook

How to run OrvaHR as a multi-tenant service (one Postgres schema per customer,
`<tenant>.<base-domain>` per customer) on the Mac mini, and what to do when
something goes wrong. Read "Honest risk statement" before you sell this to
anyone.

Contents: architecture · honest risk statement · resources · first install ·
DNS and Cloudflare · creating a tenant · auto-start after reboot · keeping the
Mac awake · power loss · updates and rollback · backups · restore and drill ·
monitoring · security checklist · PDPA notes · Caddy alternative · notes for
the multi-tenant code.

---

## Architecture

```
 browser ──HTTPS──> Cloudflare edge (TLS, WAF, DDoS)  *.orvahr.example + apex
                         │  outbound-only tunnel (no open ports on the Mac)
 ┌──────────────── Colima VM on the Mac mini ───────────────────────────────┐
 │  network orvahr-saas-edge (172.31.240.0/24)                              │
 │    cloudflared ──http──> nginx :80  (Host kept, real IP from             │
 │                            │         CF-Connecting-IP, login rate limit,  │
 │                            │         /static/ from volume, headers)       │
 │  network orvahr-saas_default                                             │
 │                            └──> web (gunicorn :8000, django-tenants)     │
 │        scheduler   db (Postgres 16)   redis   payroll-engine   exports   │
 │        backup (crond 02:30 → age-encrypted sets → ~/OrvaHR-backups → S3) │
 └──────────────────────────────────────────────────────────────────────────┘
 host ports: only 127.0.0.1:8480 → nginx (uptime check / debugging)
```

Files:

| Path | What |
|---|---|
| `docker-compose.saas.yml` | overlay on `docker-compose.yml` + `docker-compose.prod.yml` |
| `deploy/saas/env.saas.example` | template for the root `.env` |
| `deploy/saas/backup.env.example` | template for `deploy/saas/backup.env` (age key, rclone, retention) |
| `deploy/saas/monitor.env.example` | template for `deploy/saas/monitor.env` (alert targets) |
| `deploy/saas/nginx/` | internal reverse proxy config |
| `deploy/saas/backup/` | backup image: `backup.sh`, `restore.sh` |
| `deploy/saas/bin/orvahr-compose` | `docker compose` with the three files — use it for everything |
| `deploy/saas/bin/start-stack.sh` | boot hook: Colima → Docker → `up -d` |
| `deploy/saas/bin/update.sh` | backup → pull → build → up → health check |
| `deploy/saas/bin/uptime-check.sh` | 5-minute watchdog with alerts |
| `deploy/saas/launchd/` | LaunchAgent templates + `install-launchd.sh` (and a LaunchDaemon variant) |
| `deploy/saas/cloudflared/config.example.yml` | locally-managed tunnel equivalent (reference) |
| `deploy/saas/caddy/kgf-caddy-site.example` | alternative: publish through the existing kgf-caddy |

Why these choices:

* **Cloudflare Tunnel, not ports 80/443.** kgf-caddy already owns 80/443 on this
  Mac. The tunnel is outbound-only: no router port forwarding, no exposed home
  IP, works behind CGNAT and a changing IP, and the free Universal SSL
  certificate covers `orvahr.example` and `*.orvahr.example` — so there is no
  wildcard-certificate plumbing on the Mac at all.
* **nginx stays in the path** (not cloudflared → gunicorn directly): it keeps
  the login rate limit per real client IP, serves `/static/`, blocks
  `/metrics/` and `/engine/`, sets security headers, marks `/media/`
  uncacheable, and is the single proxy Django trusts (`HORILLA_TRUSTED_PROXIES=nginx`).
* **No `/engine/` route.** Django calls the payroll engine internally
  (`http://payroll-engine:8090`); only the Svelte tax simulator calls `/engine/`,
  and that simulator is not a tenant feature. The engine has no authentication,
  so it is never published. The `web-app` service is behind a profile and has
  no host port (its base `127.0.0.1:8080` collides with erp-api).

---

## Honest risk statement

**One Mac mini is a single point of failure.** Everything — the app, the
database of every customer, the local backups — sits on one machine, on one
power socket, one internet line, in one room.

| Event | Effect | Mitigation here | Residual |
|---|---|---|---|
| Power cut | all tenants down | UPS, `autorestart`, auto-start | down until power returns (+ FileVault unlock, see below) |
| Internet down | all tenants down | none on this box | down until the line returns; 4G/5G failover router helps |
| macOS update / reboot | minutes down | update by hand in a window | planned downtime |
| Disk/SSD failure, theft, fire, flood | everything gone on this box | nightly encrypted offsite copy | lose up to ~24 h of data (RPO), hours–days to rebuild (RTO) |
| Colima VM disk corrupt | DB gone | backups live **outside** the VM in `~/OrvaHR-backups` + offsite | restore from last night |
| Ransomware / compromise of the Mac | data exposed | FileVault, tunnel (no inbound), age (server cannot decrypt old backups), offsite bucket with object lock | live data exposed |

What this means in practice:

* Do not promise an SLA above ~99% (≈7 h/month). Say "best effort, business
  hours support" in the contract.
* **RPO is 24 h** with nightly dumps. If a customer's payroll run is lost, it is
  re-entered by hand. Tighter RPO needs continuous WAL archiving (pgBackRest or
  WAL-G to the same bucket) — the next upgrade worth doing.
* Plan the exit: when you have more than a handful of paying tenants, move the
  same compose stack to a Thai cloud VM (or managed Postgres + a VM), keep the
  Mac as a staging box. The overlay is deliberately portable.

---

## Resources

The Colima VM (profile `default`: 4 CPU, 16 GiB, 100 GiB disk, vz) is **shared**
with kgf and invoicerr-dev. Limits in `docker-compose.saas.yml` (caps, not
reservations):

| service | CPU | RAM |
|---|---|---|
| web (3 gunicorn workers × 4 threads) | 2 | 2 GB |
| db (shared_buffers 512 MB) | 2 | 2 GB |
| scheduler | 1 | 1 GB |
| backup | 1 | 512 MB |
| redis (maxmemory 256 MB) | 0.5 | 384 MB |
| payroll-engine, exports | 0.5 each | 256 MB each |
| nginx, cloudflared | 0.5 each | 128 MB each |

Worst case ≈ 6.7 GB. Fine inside 16 GiB next to kgf. Things to do (by hand,
outside this repo):

* Stop `invoicerr-dev` and the dev `orvahr` stack on the production Mac. The
  dev stack publishes Django with `DEBUG=1` on `0.0.0.0:8000`, reachable from the
  LAN.
* If CPU is tight, the Mac has 10 cores: `colima stop && colima start --cpu 6`
  (this restarts the VM, so kgf goes down for a minute too — schedule it).
* Docker data lives on the VM disk (100 GiB). `uptime-check.sh` alerts at 85%.
  Prune old images after updates: `docker image prune -f`.

---

## First install

Use a **separate checkout outside `~/Documents`, `~/Desktop`, `~/Downloads`**.
macOS privacy protection (TCC) blocks launchd jobs from reading those folders
unless `/bin/bash` gets Full Disk Access, and Colima's file sharing can hit the
same wall after a reboot. It also keeps the dev checkout and production apart.

```bash
mkdir -p ~/srv && git clone <repo-url> ~/srv/orvahr-saas && cd ~/srv/orvahr-saas
git checkout <release-tag-or-branch>

# 1. Secrets (.env is git-ignored). Store a copy in the password manager.
cp deploy/saas/env.saas.example .env && chmod 600 .env
#    edit: ORVAHR_BASE_DOMAIN, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, every
#    change-me value (openssl rand -base64 48 | tr -d '/+=' | cut -c1-50),
#    DATABASE_URL/REDIS_URL matching the passwords, CLOUDFLARE_TUNNEL_TOKEN
#    (next section), ORVAHR_BACKUP_HOST_DIR=/Users/<you>/OrvaHR-backups

# 2. Backup encryption key -- generate on YOUR LAPTOP, not on the server:
#      brew install age && age-keygen -o orvahr-backup-key.txt
#    Put the private key file in the password manager + a printed copy in a safe.
#    Only the "Public key: age1..." line goes to the server:
cp deploy/saas/backup.env.example deploy/saas/backup.env && chmod 600 deploy/saas/backup.env
#    edit: AGE_RECIPIENTS, RCLONE_* (bucket), BACKUP_PING_URL
mkdir -p ~/OrvaHR-backups && chmod 700 ~/OrvaHR-backups

# 3. Alerts (optional but recommended)
cp deploy/saas/monitor.env.example deploy/saas/monitor.env

# 4. Validate, build, start
deploy/saas/bin/orvahr-compose config --quiet && echo config-ok
deploy/saas/bin/orvahr-compose build
deploy/saas/bin/orvahr-compose up -d
deploy/saas/bin/orvahr-compose ps          # first boot migrates: web healthy after ~5 min
deploy/saas/bin/orvahr-compose logs -f web cloudflared

# 5. Check
curl -s -H "Host: orvahr.example" http://127.0.0.1:8480/health/
curl -s -H "Host: orvahr.example" http://127.0.0.1:8480/ready/
curl -s https://orvahr.example/health/      # through Cloudflare

# 6. First backup now, then a verify drill (see "Restore and drill")
deploy/saas/bin/orvahr-compose exec backup backup.sh

# 7. Auto-start + watchdog (section below)
deploy/saas/launchd/install-launchd.sh
```

Environment values that matter for the proxy chain (already in the example):

| Variable | SaaS value | Why |
|---|---|---|
| `ALLOWED_HOSTS` | `.orvahr.example` | leading dot = apex + every subdomain. Never `*` (the prod gate rejects it). |
| `CSRF_TRUSTED_ORIGINS` | `https://orvahr.example,https://*.orvahr.example` | Django ≥4.0 supports `*.` subdomain wildcards; the apex needs its own entry. Same-origin POSTs pass anyway once `request.is_secure()` is right. |
| `SECURE_SSL_REDIRECT` | `1` | turns on HSTS too. nginx always sends `X-Forwarded-Proto: https` (it **sets** the header, never passes the client's), and `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` is always on in production (`horilla/settings/security.py`), so there is no redirect loop. |
| `SECURE_HSTS_SECONDS` | `3600` then `31536000` | Django adds `includeSubDomains; preload` — this binds every subdomain of the base domain to HTTPS. Use a domain dedicated to the product. |
| `HORILLA_TRUSTED_PROXIES` | `nginx` | `horilla/client_ip.py` reads `X-Forwarded-For` right-to-left through trusted proxies. nginx replaces XFF with the real visitor IP (realip from `CF-Connecting-IP`, trusted only from the edge subnet), so exactly one trusted hop is right. Do **not** add cloudflared. Spoofed XFF from a client is dropped (tested). |
| `ORVAHR_MULTI_TENANT` | `1` (set by the overlay) | turns on django-tenants (`horilla/settings/saas.py`). |
| `ORVAHR_BASE_DOMAIN` | `orvahr.example` | nginx `server_name`, healthcheck Host; passed to Django as `ORVAHR_SAAS_BASE_DOMAIN` (apex + `www.` = public urlconf). |
| `COMPOSE_PROJECT_NAME` | `orvahr-saas` | own containers and volumes, apart from the dev `orvahr` stack. |

Leave `SESSION_COOKIE_DOMAIN` / `CSRF_COOKIE_DOMAIN` **unset** (host-only
cookies). Setting them to `.orvahr.example` would share one login across tenants.

---

## DNS and Cloudflare

Cloudflare's UI moves around; names below are as of late 2026.

1. **Domain.** Buy a domain for the product (e.g. `orvahr.co.th` via a THNIC
   registrar, or `.com`/`.app`). Add it to Cloudflare (Free plan is enough),
   switch the registrar's nameservers to Cloudflare's.
2. **SSL/TLS.** Edge certificates: *Always Use HTTPS* on, *Minimum TLS* 1.2,
   leave edge HSTS off (Django sends it). Universal SSL covers `orvahr.example`
   and `*.orvahr.example` — one level only, which is why tenants are
   `acme.orvahr.example`, never `acme.hr.orvahr.example`.
3. **Tunnel.** Zero Trust → Networks → Tunnels → *Create a tunnel* →
   Cloudflared → name `orvahr-saas`. Copy the token (the long string after
   `--token` in the shown `docker run` command) into `.env` as
   `CLOUDFLARE_TUNNEL_TOKEN`. Ignore the install instructions — the
   `cloudflared` service runs it.
4. **Routes (public hostnames)** on that tunnel, both to service type `HTTP`,
   URL `nginx:80`:
   * subdomain empty, domain `orvahr.example`
   * subdomain `*`, domain `orvahr.example`
   Do not set *HTTP Host Header* — the original Host must reach nginx/Django.
5. **DNS records.** Check DNS → Records shows proxied (orange) CNAMEs `@` and
   `*` → `<tunnel-id>.cfargotunnel.com`. If the wildcard was not created
   automatically, add it by hand.
6. **Cache rule (important).** Caching → Cache Rules → *Bypass cache* when
   `URI Path` does not start with `/static/`. Cloudflare caches `.pdf`, `.jpg`,
   `.png` by extension by default and ignores cookies; without this rule a
   payslip PDF under `/media/` could be served from the edge to someone who
   never logged in. nginx also sends `Cache-Control: private, no-store` on
   `/media/` as a second guard.
7. **Security.** Security → WAF: one rate-limiting rule (free plan allows one)
   on `URI Path` matches `/login` with method POST, e.g. 20/min per IP, in
   addition to nginx's 5/min. Leave *Bot Fight Mode* off if the mobile app or
   API clients are used (it challenges non-browsers). Optional: Cloudflare
   Access (email one-time PIN) in front of `orvahr.example/admin/` for staff.
8. **Uploads.** Cloudflare Free/Pro reject request bodies over 100 MB; nginx is
   at 50 MB. Responses slower than ~100 s become Cloudflare 524 — long reports
   must be background jobs.
9. **Account.** 2FA on the Cloudflare account; the tunnel token is a secret
   (anyone with it can impersonate the origin) — rotate it in the dashboard if
   leaked and update `.env`.

---

## Creating a tenant

`create_tenant` (orvahr_saas, multi-tenant workstream) creates the schema, runs
the tenant migrations, adds the domain, the first company and the admin. The
admin password is read from an environment variable, never an argument:

```bash
read -rs PW   # type the first admin password
deploy/saas/bin/orvahr-compose exec -e ORVAHR_TENANT_ADMIN_PASSWORD="$PW" web \
    python manage.py create_tenant \
    --slug acme --name "Acme Co., Ltd." --domain acme.orvahr.example \
    --admin-email hr@acme.co.th --thai-setup --status active
unset PW
deploy/saas/bin/orvahr-compose exec web python manage.py create_tenant --help
```

(`--thai-demo` loads synthetic demo data: never for a real customer.) Slug
`acme-th` becomes schema `acme_th`. Nothing changes in DNS, Cloudflare or nginx
-- the wildcard already routes `acme.orvahr.example` to the stack. Check:
`curl -sI https://acme.orvahr.example/login/` -> 200.

The command already rejects reserved slugs (`public www api admin static
media`, `pg-*`); consider also reserving `app mail status help docs billing`.

Off-boarding a tenant (PDPA: delete when the contract ends): take a
per-tenant dump first (`backup.sh` writes `tenants/<schema>.dump.age` every
night) and hand it to the customer if the contract says so, then drop the
schema + its tenant/domain rows (command from the other workstream). Its data
disappears from backups after the retention window (6 months monthly tier) —
state that in the contract.

---

## Auto-start after reboot

Colima did not come back after this morning's reboot because nothing starts
it. Two layers fix that:

1. **Containers** use `restart: unless-stopped`, so they come back by
   themselves as soon as the Docker daemon in the VM starts.
2. **Colima itself** needs a launchd job. `deploy/saas/launchd/install-launchd.sh`
   installs two LaunchAgents for the current user:
   * `com.orvahr.saas.start` — at login runs `bin/start-stack.sh`: `colima
     start` if not running (retries; no flags, so the existing profile and its
     CPU/RAM are reused — kgf comes back too), waits for Docker, then
     `orvahr-compose up -d`. One-shot, logs to `~/Library/Logs/OrvaHR/start-stack.log`.
   * `com.orvahr.saas.uptime` — every 5 minutes, `bin/uptime-check.sh`.

   Installing bootstraps them immediately (the start job runs once right away;
   harmless if everything is up). Remove with `install-launchd.sh --uninstall`.
   Do not also run `brew services start colima` unless you prefer that for
   Colima — two starters are harmless (the script skips a running Colima) but
   one is easier to reason about.

**A LaunchAgent only runs after a user logs in.** Options:

| Option | How | Pros | Cons |
|---|---|---|---|
| A. LaunchAgent + **automatic login** | System Settings → Users & Groups → *Automatically log in as* | simplest; Colima runs in a normal user session (its supported mode) | **impossible with FileVault on**; anyone at the keyboard is logged in (use a locked room + screen lock right after login via a Login Item / hot corner) |
| B. LaunchAgent + **FileVault**, unlock by hand | stay as is | disk encrypted at rest (strongly preferred for HR data) | after every power cut someone must unlock the Mac (in person, or over SSH where supported — see below) before anything starts |
| C. **LaunchDaemon** (`com.orvahr.saas.start.daemon.plist.template`, runs at boot as your user via `UserName`) | install commands in the template header | no login needed when FileVault is off | Colima/vz in a non-GUI system context is not an officially supported setup — test a real reboot first; still blocked by FileVault before unlock; harder to debug |

Recommendation: **B** (FileVault on, LaunchAgent) with a UPS so power blips do
not reboot the Mac, and a documented "unlock after power cut" step for whoever
is on call. On macOS 26 (Tahoe) and later, a FileVault-locked Mac can be
unlocked over SSH when Remote Login is enabled — verify this on this machine
(`ssh` to it right after a test reboot) before counting on it. Planned reboots
can skip the unlock prompt once with `sudo fdesetup authrestart`.

---

## Keeping the Mac awake

Document only — run these yourself:

```bash
sudo pmset -a sleep 0 disksleep 0 standby 0 powernap 0 displaysleep 10
sudo pmset -a autorestart 1 womp 1 tcpkeepalive 1   # restart after power loss, wake on LAN
sudo systemsetup -setrestartfreeze on                  # reboot if the OS freezes
pmset -g                                               # verify
```

System Settings equivalents: Energy → *Prevent automatic sleeping when the
display is off* on, *Start up automatically after a power failure* on, *Wake
for network access* on. Software Update → automatic updates: keep *Security
Responses and system files* on, turn **off** *Install macOS updates* so reboots
happen in your window, not at 3 a.m. on payroll day.

UPS: a small line-interactive UPS (APC/CyberPower, USB) — macOS shows a UPS
section in Energy settings and shuts down cleanly on low battery. Size it for
≥15 min (Mac mini + router + ONT).

---

## What happens on power loss

1. Power drops. Without a UPS the Mac and VM stop instantly. Postgres is
   crash-safe (WAL + fsync on, APFS honours flushes): on next start it replays
   WAL; committed transactions survive, in-flight ones are rolled back. Redis is
   a cache — losing it is harmless.
2. Power returns → `autorestart 1` boots the Mac.
3. FileVault on → the Mac waits at the unlock screen. **Nothing runs** until
   someone unlocks it (option B above). FileVault off + auto-login → continues.
4. Login → `com.orvahr.saas.start` → Colima → Docker → containers
   (`unless-stopped`) → `up -d`. web becomes healthy in ~1 min (migrations
   already applied); cloudflared reconnects by itself.
5. A backup interrupted mid-run leaves a partial set under `.incoming/` that the
   next run discards (stale lock cleared after 12 h). Run one by hand if the
   night's backup was missed: `orvahr-compose exec backup backup.sh`.
6. Check: `orvahr-compose ps`, `curl https://orvahr.example/health/`, the
   external monitor recovers, and `/ready/` shows `"scheduler": "ok"`. If the
   payroll/attendance jobs missed their window during the outage, re-run them
   from the app.

If Postgres refuses to start after a crash (corrupt VM disk — rare):
restore from the last backup (below). Do not delete the volume until the
restore has been checked.

---

## Updates and rollback

```bash
cd ~/srv/orvahr-saas
deploy/saas/bin/update.sh            # fast-forward the tracked branch
deploy/saas/bin/update.sh v2.4.0     # or check out a tag
```

`update.sh` refuses a dirty tree, takes a backup, writes the running commit to
`deploy/saas/.previous-deploy-rev`, checks out, builds, `up -d` (web runs
migrations for every schema on start), waits up to 20 min for `web` healthy and
checks `/health/` through nginx. Announce a window (e.g. Sunday 21:00 ICT), and
avoid the last/first days of the month (payroll).

Rollback:

* **No migrations in the update** (check `git diff <prev>..HEAD --stat -- '*/migrations/*'`):
  ```bash
  git checkout --detach "$(cat deploy/saas/.previous-deploy-rev)"
  deploy/saas/bin/orvahr-compose build && deploy/saas/bin/orvahr-compose up -d
  ```
* **Migrations ran**: Django reverse migrations across N tenant schemas are not
  dependable. Restore the pre-update backup (data written since the update is
  lost — another reason for a short window and a quick check):
  ```bash
  C=deploy/saas/bin/orvahr-compose
  $C stop web scheduler
  $C run --rm -v "$HOME/restore-key.txt:/keys/identity.txt:ro" backup \
      restore.sh "/backups/daily/<pre-update-stamp>" --target-db horilla_db --force
  git checkout --detach "$(cat deploy/saas/.previous-deploy-rev)"
  $C build && $C up -d
  rm ~/restore-key.txt
  ```

Monthly: rebuild to pick up base-image security patches
(`orvahr-compose build --pull && orvahr-compose up -d`), bump the pinned
`cloudflare/cloudflared` and `nginx` tags in `docker-compose.saas.yml`, update
Colima/Homebrew in the same window.

---

## Backups

**Choice: a compose service (`backup`) with crond, not a launchd job.** It
comes back with the stack after a reboot without extra plumbing, ships the
same Postgres major as the server (`pg_dump` must not be older than the
server; Homebrew's would drift), reaches `db` and the media volume over the
Docker network with no published Postgres port, and is the same on any future
Linux host. The one thing launchd would add — running while Docker is down —
does not matter: nothing changes in the DB then. Output goes to a **host
directory** (`ORVAHR_BACKUP_HOST_DIR`, outside the Colima VM) so a lost VM disk
does not take the backups with it, and then offsite.

What runs at 02:30 Asia/Bangkok (`BACKUP_SCHEDULE`):

* `db.dump.age` — `pg_dump -Fc` of the whole database: public schema + every
  tenant schema. This is the disaster-recovery file.
* `tenants/<schema>.dump.age` — one `pg_dump -Fc -n <schema>` per tenant
  (`TENANT_SCHEMAS=all`, or a list, or empty to skip): restore or hand over one
  customer without touching the others.
* `globals.sql.age` — roles; `media.tar.gz.age` — the media volume.
* `MANIFEST`, `SHA256SUMS`.
* Encryption: [age](https://age-encryption.org) to the **public** key(s) in
  `AGE_RECIPIENTS`. The server cannot decrypt its own backups; the private key
  lives offline. The script refuses to run without a recipient.
* Rotation: 7 daily, 4 weekly (Sunday, hard-linked), 6 monthly (1st), locally
  and offsite (`KEEP_*`).
* Offsite: `rclone copy` of each new set to `RCLONE_REMOTE`, configured only
  through `RCLONE_CONFIG_OFFSITE_*` env (any S3-compatible bucket; rclone also
  speaks B2, Google Drive, Azure, SFTP…). Then remote pruning, unless
  `RCLONE_PRUNE=0`.
* Success → `.last_success` (container healthcheck: unhealthy after 26 h
  without success) and `BACKUP_PING_URL` (dead-man's switch); failure →
  `<url>/fail`.

Bucket choice (PDPA cross-border, see below): a Thai region keeps data in
Thailand — e.g. AWS `ap-southeast-7` (Bangkok), or Thai providers' S3-compatible
object storage (NIPA Cloud, True IDC, INET, Huawei Cloud Bangkok — check each
for S3 API support and price). Cloudflare R2 (cheap, no egress fees; set a
location hint / jurisdiction), Backblaze B2 or Wasabi (Singapore) are fine
technically but are a cross-border transfer. Harden the bucket: versioning or
**Object Lock** (compliance mode, ≥ 30 days), a key that can only write/list
(then `RCLONE_PRUNE=0` and a lifecycle rule deletes `daily/` after 8 days,
`weekly/` after 35, `monthly/` after 190) — a compromised Mac then cannot erase
the offsite history.

Manual commands:

```bash
C=deploy/saas/bin/orvahr-compose
$C exec backup backup.sh                       # run now
$C logs --tail 50 backup                       # last run
ls -la ~/OrvaHR-backups/daily/                 # local sets
cat ~/OrvaHR-backups/daily/<stamp>/MANIFEST
```

Media volume size grows with uploads; a daily full tarball is fine at small
scale. When it passes a few GB, switch media to `rclone sync` with versioning
on the bucket instead of a tarball per set.

---

## Restore and drill

`restore.sh` (in the backup image) never touches the live database unless you
say `--target-db horilla_db --force`. Default target: a scratch database
`horilla_db_restore`. It checks `SHA256SUMS` first.

```text
restore.sh SET [--identity F] [--verify-only] [--target-db N] [--schema S]
               [--no-db] [--media DIR] [--force]
SET = /backups/daily/<stamp> | latest | remote:<tier>/<stamp>
```

The private key comes to the server only for the restore, under `$HOME`
(Colima shares only `$HOME`), and is deleted afterwards.

**Weekly (5 minutes): verify-only** — decrypts every file and reads every
dump's table of contents, restores nothing:

```bash
C=deploy/saas/bin/orvahr-compose
$C run --rm -v "$HOME/restore-key.txt:/keys/identity.txt:ro" backup restore.sh latest --verify-only
```

**Monthly: full drill against an isolated Postgres** (never the live one),
from the **offsite** copy, so the whole chain is proven:

```bash
cd ~/srv/orvahr-saas
docker network create orvahr-drill
docker run -d --name orvahr-drill-db --network orvahr-drill \
  -e POSTGRES_USER=horilla_user -e POSTGRES_PASSWORD=drill -e POSTGRES_DB=horilla_db \
  postgres:16-alpine
sleep 5
docker run --rm --network orvahr-drill --env-file deploy/saas/backup.env \
  -e PGHOST=orvahr-drill-db -e PGUSER=horilla_user -e PGPASSWORD=drill -e PGDATABASE=horilla_db \
  -v "$HOME/restore-key.txt:/keys/identity.txt:ro" -v "$HOME/OrvaHR-drill:/backups" \
  orvahr-backup:local restore.sh "remote:daily/<stamp>" \
  --target-db horilla_db --force --media /tmp/media-check
#  -> prints schemas and table counts. Compare with live:
deploy/saas/bin/orvahr-compose exec db psql -U horilla_user -d horilla_db -c \
  "select table_schema, count(*) from information_schema.tables group by 1 order by 1"
#  spot-check one tenant, e.g. employee count:
docker exec orvahr-drill-db psql -U horilla_user -d horilla_db -c "select count(*) from acme.employee_employee"
# clean up
docker rm -f orvahr-drill-db && docker network rm orvahr-drill
rm -rf ~/OrvaHR-drill ~/restore-key.txt
```

Write the date, set, duration and result in an ops log. A backup that has
never been restored is a hope, not a backup.

**Restore one tenant** (e.g. a customer deleted data yesterday):

```bash
C=deploy/saas/bin/orvahr-compose
# safest: into the scratch DB, then copy the rows they need back
$C run --rm -v "$HOME/restore-key.txt:/keys/identity.txt:ro" backup \
  restore.sh latest --schema acme --target-db horilla_db_restore
# or replace the whole tenant schema in place (that tenant loses later changes;
# other tenants untouched). Put the tenant in maintenance first.
$C run --rm -v "$HOME/restore-key.txt:/keys/identity.txt:ro" backup \
  restore.sh latest --schema acme --target-db horilla_db --force
$C exec db dropdb -U horilla_user --if-exists horilla_db_restore   # afterwards
```

Per-tenant media is not separated in the archive (one media volume);
extract to a scratch dir with `--no-db --media /backups/media-restore` and
copy the needed files.

**Full disaster recovery (new Mac or VM):** install Docker/Colima, clone the
repo at the tag that was running, restore `.env` and `deploy/saas/backup.env`
from the password manager, then:

```bash
C=deploy/saas/bin/orvahr-compose
$C build
$C up -d db
$C run --rm -v "$HOME/restore-key.txt:/keys/identity.txt:ro" backup \
  restore.sh remote:daily/<stamp> --target-db horilla_db --force
$C run --rm -v "$HOME/restore-key.txt:/keys/identity.txt:ro" \
  -v orvahr-saas_media:/media-restore backup \
  restore.sh /backups/restore-fetch/<stamp> --no-db --media /media-restore --force
$C up -d
rm ~/restore-key.txt; rm -rf ~/OrvaHR-backups/restore-fetch
```

The same tunnel token reconnects from the new machine; DNS does not change.

Tested on 2026-11-15 against a throwaway Postgres 16 container: backup with
two fake tenant schemas + media, rotation/pruning (local and an rclone
`local` remote), verify-only, whole-DB restore to scratch and over a live DB,
single-schema restore after damage, remote fetch + media extract, wrong key
and tampered file both rejected, crond schedule firing.

---

## Monitoring

Endpoints (`horilla/urls.py`):

* `/health/` — liveness, no dependencies (used by the container healthcheck).
* `/ready/` — database + Redis, and `"scheduler": "ok" | "missing"`.
* `/metrics/` — Prometheus text, staff-only in Django **and** 404 at nginx;
  scrape from inside the Docker network if ever needed.

Under django-tenants these must answer on the apex host (public schema) —
see "Notes for the multi-tenant code".

Layers:

1. **External (essential).** Something off this Mac checks
   `https://orvahr.example/health/` every 1–5 min and pings your phone:
   UptimeRobot, Better Stack, Healthchecks.io (free tiers suffice). Also set
   `BACKUP_PING_URL` to a Healthchecks.io check with a 1-day period + grace,
   so a missed backup alerts even if the Mac is gone.
2. **Local watchdog** — `bin/uptime-check.sh` every 5 min (LaunchAgent):
   public and local `/health/`, `/ready/` (incl. scheduler), containers
   not running/unhealthy, Mac disk and Colima VM disk ≥ 85%, backup older than
   26 h. Alerts on change and every 6 h while failing, via ntfy, a
   Slack-style webhook and/or a macOS notification (`deploy/saas/monitor.env`).
   Log: `~/Library/Logs/OrvaHR/uptime.log`. (LINE Notify was shut down in 2025;
   for LINE use a Messaging API bot behind a small webhook, or ntfy.)
3. **Uptime Kuma (optional dashboard).** Better on a second device (a Pi, an old
   laptop, a ฿150/month VPS) than on the Mac it watches. If on the Mac anyway:
   ```bash
   docker run -d --name uptime-kuma --restart unless-stopped \
     -p 127.0.0.1:3001:3001 -v uptime-kuma:/app/data louislam/uptime-kuma:1
   ```
   Monitors: HTTP `https://orvahr.example/health/`, HTTP keyword `"status": "ok"`
   on `/ready/` (local, Host header), push monitor for `BACKUP_PING_URL`.

Logs: `orvahr-compose logs --since 1h web nginx` (json-file, 5 × 10 MB per
container). nginx logs include the real client IP and `cf_ray` for matching
Cloudflare events.

---

## Security checklist

Mac:

- [ ] **FileVault on** (theft of a Mac mini is the most likely breach). Recovery
      key in the password manager, not on the Mac.
- [ ] Firewall on (System Settings → Network → Firewall), *stealth mode* on.
      The tunnel needs no inbound ports; only 127.0.0.1 ports are published.
- [ ] Remote admin over SSH **keys only**: `Remote Login` on for your user only,
      `/etc/ssh/sshd_config.d/100-orvahr.conf` with
      `PasswordAuthentication no`, `KbdInteractiveAuthentication no`,
      `PermitRootLogin no`. Reach it via Tailscale (or Cloudflare Access SSH), not
      a forwarded port.
- [ ] A standard (non-admin) macOS user for day-to-day; admin only for sudo.
- [ ] Screen lock after 5 min; Mac in a locked room/cabinet; UPS.
- [ ] Security responses auto-install; macOS/Colima/Docker updates monthly in
      a window.
- [ ] Turn off unused sharing (Screen Sharing, File Sharing, AirPlay Receiver).
- [ ] Stop the dev stack and invoicerr-dev on this machine (dev Django on
      `0.0.0.0:8000` with `DEBUG=1`).

App/stack:

- [ ] `.env`, `deploy/saas/backup.env`, `monitor.env` are `chmod 600`, git-ignored,
      and copied to the password manager. Unique random values for
      `SECRET_KEY`, `DB_INIT_PASSWORD`, Postgres, Redis.
- [ ] `DEBUG=False`, `HORILLA_ENV=production` (the prod gate refuses weak secrets).
- [ ] HSTS ramp: 3600 → 31536000 once stable.
- [ ] Cloudflare: 2FA, cache-bypass rule for non-static paths, WAF rate limit on
      login, Access in front of `/admin/`.
- [ ] Payroll engine, exports, Postgres, Redis have no host ports (verify:
      `orvahr-compose ps`).
- [ ] Backups encrypted (age), private key offline, offsite bucket with Object
      Lock/versioning, monthly restore drill logged.
- [ ] Django superusers: few, named, 2FA where the app supports it.
- [ ] `HORILLA_ALLOW_PYTHON_TAX_FORMULAS` left off in SaaS (tenant admins
      running Python on a shared server is a cross-tenant risk).

---

## PDPA notes (not legal advice — have a Thai lawyer review)

* **Roles.** Each customer is the *data controller* for its employees' data;
  you are the *data processor* (PDPA s.40). Sign a Data Processing Agreement
  with each customer: purpose, security measures, sub-processors, breach
  notice, deletion/return at contract end, audit.
* **Sensitive data (s.26)** is in an HR system: health (medical certificates
  on sick leave), **biometrics** (face-detection attendance), religion, possibly
  criminal records from recruitment. Higher bar: explicit consent by the
  controller, stricter access, and a stronger case for encryption at rest
  (FileVault) and in backups (age).
* **Security measures (s.37(1), s.40(2))** — the PDPC's security
  notification expects access control, least privilege, logging/audit,
  backup and recovery, and awareness. This runbook's checklist, the restore
  drill log and the access logs are your evidence.
* **Breach.** Processor → notify the controller without delay; controller →
  PDPC within 72 h (and data subjects if high risk). Keep a written incident
  procedure and the customer contact list **off** this Mac.
* **Cross-border transfer (s.28–29).** Cloudflare terminates TLS at its edge
  (Bangkok/Singapore PoPs) and sees traffic in clear — disclose Cloudflare as
  a sub-processor. Offsite backups outside Thailand are a transfer: prefer a
  Thai region, or document the destination's protection in the DPA.
* **Single machine.** One Mac mini is an availability and durability risk
  for personal data the controller is accountable for. Tell customers where
  data is hosted, the backup RPO (24 h) and the recovery expectations; do not
  claim "bank-grade" or redundant hosting.
* **Retention.** Backups keep deleted tenants' data up to 6 months (monthly
  tier) — state this; shorten `KEEP_MONTHLY` if contracts require faster
  erasure. Logs contain IP addresses (personal data): keep them short (they
  rotate at 50 MB per container here).

---

## Alternative: publish through the existing kgf-caddy

Documented in `deploy/saas/caddy/kgf-caddy-site.example`. Summary: put
kgf-caddy on the `orvahr-saas-edge` network (a change in the kgf project),
add a site block `orvahr.example, *.orvahr.example { reverse_proxy
orvahr-saas-nginx-1:80 }`, get a wildcard certificate via DNS-01 (custom Caddy
build with a DNS module) or on-demand TLS with an `ask` endpoint, forward
80/443 on the router, and switch nginx's `real_ip_header` to
`X-Forwarded-For`. Downsides versus the tunnel: exposes the home/office IP,
needs port forwarding and a static IP or DDNS, couples OrvaHR's uptime to
another project's proxy config, and no Cloudflare WAF/DDoS unless you also
proxy through Cloudflare. Use it only if Cloudflare is not acceptable.

---

## Notes for the multi-tenant code (other workstream)

Checked against the uncommitted `orvahr_saas` / `horilla/settings/saas.py`:

1. The overlay passes `ORVAHR_MULTI_TENANT=1`, `ORVAHR_SAAS_BASE_DOMAIN`
   (= `ORVAHR_BASE_DOMAIN` from `.env`) and `ORVAHR_SAAS_PUBLIC_HOSTS` to `web`
   and `scheduler`. saas.py then appends `.<domain>` to `ALLOWED_HOSTS` and
   `https://*.<domain>` to `CSRF_TRUSTED_ORIGINS` itself; `.env` sets them too
   so the prod overlay's required variables are satisfied.
2. Probes: the middleware serves `/health/` and `/ready/` on public/unknown
   hosts, but Django's `ALLOWED_HOSTS` check (400) comes first, so the web
   healthcheck sends `Host: <base-domain>` rather than `localhost`.
3. Cache keys are tenant-prefixed (`django_tenants.cache.make_key`) -- one
   Redis for all tenants is fine.
4. Still worth doing in code: `protected_media` should send
   `Cache-Control: private, no-store` and keep media paths per tenant
   (`MEDIA_ROOT/<schema>/...`) so one tenant cannot fetch another's file by
   path. nginx adds the header as a backstop only.
5. Keep `SESSION_COOKIE_DOMAIN`/`CSRF_COOKIE_DOMAIN` unset (host-only cookies).
6. Backups treat every non-system schema except `public` as a tenant schema.
