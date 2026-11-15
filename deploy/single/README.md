# Single-tenant server deployment

One company per install, behind an existing Caddy that owns ports 80/443.
Services: web, scheduler, db, redis, nginx (payroll-engine, face-verify and
exports are not started; see "Not running" below).

Server files that are NOT in git: `.env` (secrets) and `docker/nginx.server.conf`
(generated from `docker/nginx.conf` by `deploy.sh`).

    ssh root@<server>; cd /opt/orvahr
    deploy/single/deploy.sh          # pull, back up, rebuild, wait for health
    deploy/single/backup.sh          # one-off dump into backups/ (14 days kept)

Caddy site block (in the shared Caddyfile; validate before reload):

    orvahr.anthovai.com {
        encode zstd gzip
        reverse_proxy orvahr-nginx-1:80
    }

Restore: `gunzip -c backups/<file>.sql.gz | docker exec -i orvahr-db-1 sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'`
into an empty database; stop web and scheduler first.

Not running: face check-in (needs face-verify), Rust payroll engine (mode off),
exports service. The box has 3.8 GB RAM; watch `free -m` before adding any.
