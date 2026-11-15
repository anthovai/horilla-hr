# Backup runner for the OrvaHR SaaS stack. Same Postgres major as the `db`
# service (pg_dump must not be older than the server), plus age for
# encryption and rclone for the offsite copy. Runs busybox crond.
FROM postgres:16-alpine

RUN apk add --no-cache age rclone tzdata curl coreutils \
    && touch /etc/rclone.conf

# rclone is configured only through RCLONE_CONFIG_<REMOTE>_* env vars; an
# empty config file keeps it from warning about a missing one on every run.
ENV RCLONE_CONFIG=/etc/rclone.conf

COPY backup.sh restore.sh backup-healthcheck.sh entrypoint.sh /usr/local/bin/
RUN chmod 755 /usr/local/bin/backup.sh /usr/local/bin/restore.sh \
        /usr/local/bin/backup-healthcheck.sh /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["crond"]
