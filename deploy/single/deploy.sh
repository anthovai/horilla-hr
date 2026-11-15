#!/usr/bin/env bash
# Update the single-tenant server deployment: pull, rebuild, wait for health.
# Run on the server in /opt/orvahr. Needs .env and docker-compose.server.yml.
set -euo pipefail
cd "$(dirname "$0")/../.."
DC="docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.server.yml"
git pull --ff-only
# Keep the proxy config in step with docker/nginx.conf: trust Caddy for the
# client IP and scheme (see docker-compose.server.yml).
sed -e "0,/http {/s//http {\n    set_real_ip_from 172.16.0.0\/12;\n    real_ip_header X-Forwarded-For;\n    real_ip_recursive on;/" \
    -e 's/\$scheme;/$http_x_forwarded_proto;/g' \
    -e 's/proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;/proxy_set_header X-Forwarded-For $remote_addr;/' \
    docker/nginx.conf > docker/nginx.server.conf
deploy/single/backup.sh || echo "WARNING: pre-deploy backup failed" >&2
$DC up -d --build web scheduler db redis
$DC up -d --force-recreate nginx
for _ in $(seq 1 120); do
  c=$(docker exec orvahr-web-1 curl -s -o /dev/null -w '%{http_code}' -H 'X-Forwarded-Proto: https' http://localhost:8000/health/ 2>/dev/null || true)
  [ "$c" = 200 ] && { echo "healthy"; exit 0; }
  sleep 10
done
echo "web did not become healthy; see: docker logs orvahr-web-1" >&2
exit 1
