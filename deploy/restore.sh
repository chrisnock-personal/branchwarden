#!/usr/bin/env bash
# Branchwarden: restore a backup made by deploy/backup.sh.
#
# Puts back the database, the keys and certs volumes and (if missing) .env,
# then starts Branchwarden. Use it on a fresh host (copy the repo or the compose
# file there first) or over a broken install.
#
# Usage (from the directory with docker-compose.yml):
#   deploy/restore.sh -i ~/.config/age/branchwarden.key branchwarden-20261115-093000.tar.age
#   deploy/restore.sh -i key.txt --force backup.tar.age   # over a database that already has data
# Quadlets:  --db-container branchwarden-db --services "branchwarden-worker branchwarden-dashboard"
# Settings, from the environment: ENGINE, COMPOSE, AGE (as for backup.sh).
set -euo pipefail

ENGINE="${ENGINE:-podman}"
COMPOSE="${COMPOSE:-podman-compose}"
AGE="${AGE:-age}"
IDENTITY=""
FORCE=0
DB_CONTAINER=""
SERVICES=""
KEYS_VOLUME="${KEYS_VOLUME:-branchwarden_keys}"
CERTS_VOLUME="${CERTS_VOLUME:-branchwarden_certs}"
HELPER_IMAGE="${HELPER_IMAGE:-docker.io/library/postgres:16-alpine}"
FILE=""

usage() { sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
die() { echo "restore: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        -i) IDENTITY="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        --db-container) DB_CONTAINER="$2"; shift 2 ;;
        --services) SERVICES="$2"; shift 2 ;;
        -h|--help) usage ;;
        -*) die "unknown option $1 (see --help)" ;;
        *) FILE="$1"; shift ;;
    esac
done
[ -n "$FILE" ] && [ -f "$FILE" ] || die "give the backup file (see --help)"
[ -n "$IDENTITY" ] && [ -f "$IDENTITY" ] || die "give the age private key with -i (see --help)"

quadlet() { [ -n "$DB_CONTAINER" ]; }
db_exec() { if quadlet; then "$ENGINE" exec -i "$DB_CONTAINER" "$@"; else $COMPOSE exec -T db "$@"; fi; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
chmod 700 "$work"

echo "Decrypting…"
# The private key is read by age inside $AGE: if AGE runs in a container, mount it there.
$AGE -d -i "$IDENTITY" < "$FILE" | tar -C "$work" -xf -
[ -s "$work/db.dump" ] || die "this file doesn't hold a Branchwarden backup (no db.dump)"
sed 's/^/  /' "$work/manifest"

if [ ! -f .env ] && [ -f "$work/env" ]; then
    cp "$work/env" .env && chmod 600 .env && echo "Restored .env"
elif [ -f "$work/env" ] && ! cmp -s "$work/env" .env; then
    echo "Kept the .env that's here (the backup's differs). Its database password must match the restored volume's, or start fresh."
fi

# Check before changing anything: a refused restore leaves Branchwarden as it was.
echo "Starting the database…"
if quadlet; then
    systemctl --user start "${DB_CONTAINER}.service" 2>/dev/null || "$ENGINE" start "$DB_CONTAINER" >/dev/null
else
    $COMPOSE up -d db >/dev/null
fi
for _ in $(seq 1 60); do
    db_exec sh -c 'pg_isready -q -U "${POSTGRES_USER:-branchwarden}"' && break
    sleep 2
done

tables=$(db_exec sh -c 'psql -U "${POSTGRES_USER:-branchwarden}" -d "${POSTGRES_DB:-branchwarden}" -tAc \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = '"'"'public'"'"'"')
if [ "${tables// /}" != "0" ] && [ "$FORCE" != 1 ]; then
    die "the database already has tables: add --force to replace them with the backup's"
fi
# Only now that the restore is going ahead: stop the app and put the volumes back.
echo "Stopping the worker and dashboard…"
if quadlet; then
    for s in $SERVICES; do systemctl --user stop "$s" 2>/dev/null || "$ENGINE" stop "$s" >/dev/null 2>&1 || true; done
else
    $COMPOSE stop worker dashboard >/dev/null 2>&1 || true
fi

for name in keys certs; do
    [ -f "$work/$name.tar" ] || continue
    vol="$KEYS_VOLUME"; [ "$name" = certs ] && vol="$CERTS_VOLUME"
    "$ENGINE" volume inspect "$vol" >/dev/null 2>&1 || "$ENGINE" volume create "$vol" >/dev/null
    echo "Restoring the $name volume…"
    # Empty it, then unpack with the original owners (the app runs as uid 10001).
    "$ENGINE" run --rm -i -v "$vol:/v" --entrypoint sh "$HELPER_IMAGE" \
        -c 'find /v -mindepth 1 -delete && tar -C /v --numeric-owner -xpf -' < "$work/$name.tar"
done

echo "Restoring the database…"
db_exec sh -c 'pg_restore -U "${POSTGRES_USER:-branchwarden}" -d "${POSTGRES_DB:-branchwarden}" --clean --if-exists --no-owner --exit-on-error' \
    < "$work/db.dump"

echo "Starting Branchwarden…"
if quadlet; then
    for s in $SERVICES; do systemctl --user start "$s" 2>/dev/null || "$ENGINE" start "$s" >/dev/null; done
else
    $COMPOSE up -d >/dev/null
fi
echo "Restored from $FILE. Check the dashboard: stored tokens and webhook URLs should still work."
