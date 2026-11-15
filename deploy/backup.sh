#!/usr/bin/env bash
# Branchwarden: an encrypted backup of everything needed to restore it.
#
# One file, branchwarden-YYYYMMDD-HHMMSS.tar.age, encrypted with age
# (https://age-encryption.org) to the recipients you give. Inside:
#   db.dump     pg_dump of the database (custom format, for pg_restore)
#   keys.tar    the "keys" volume: the secret key that seals stored tokens and webhook URLs
#   certs.tar   the "certs" volume: the TLS certificate and key
#   env         the .env file, if there is one (database password, settings)
#   manifest    when, which version, and from where
# Without the keys, the stored secrets in the dump can't be read, so they travel
# together; age keeps the bundle unreadable to anyone without your private key.
# The server only needs the PUBLIC recipient key: keep the private one elsewhere.
#
# Usage (from the directory with docker-compose.yml and .env):
#   deploy/backup.sh -r age1…            # or -R recipients.txt, or BACKUP_RECIPIENT=age1…
#   deploy/backup.sh -r age1… -o /backups --keep 14
# Quadlets (container names, not compose services):
#   deploy/backup.sh -r age1… --db-container branchwarden-db
# Settings, from the environment:
#   ENGINE=podman|docker   (default podman)   COMPOSE="podman-compose" (or "docker compose")
#   AGE=age                the age command, e.g. a container on a host without it
set -euo pipefail

ENGINE="${ENGINE:-podman}"
COMPOSE="${COMPOSE:-podman-compose}"
AGE="${AGE:-age}"
OUT="."
KEEP=0
DB_CONTAINER=""
KEYS_VOLUME="${KEYS_VOLUME:-branchwarden_keys}"
CERTS_VOLUME="${CERTS_VOLUME:-branchwarden_certs}"
HELPER_IMAGE="${HELPER_IMAGE:-docker.io/library/postgres:16-alpine}"   # already there: it's the database's image
RECIPIENTS=()
[ -n "${BACKUP_RECIPIENT:-}" ] && RECIPIENTS+=(-r "$BACKUP_RECIPIENT")

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
die() { echo "backup: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        -r) RECIPIENTS+=(-r "$2"); shift 2 ;;
        -R) RECIPIENTS+=(-R "$2"); shift 2 ;;
        -o) OUT="$2"; shift 2 ;;
        --keep) KEEP="$2"; shift 2 ;;
        --db-container) DB_CONTAINER="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) die "unknown option $1 (see --help)" ;;
    esac
done
[ ${#RECIPIENTS[@]} -gt 0 ] || die "give at least one age recipient: -r age1… or -R file (see --help)"
[[ "$KEEP" =~ ^[0-9]+$ ]] || die "--keep takes a number"
mkdir -p "$OUT"

db_exec() {   # run a command in the database container, with stdin
    if [ -n "$DB_CONTAINER" ]; then "$ENGINE" exec -i "$DB_CONTAINER" "$@"; else $COMPOSE exec -T db "$@"; fi
}

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
chmod 700 "$work"
stamp="$(date -u +%Y%m%d-%H%M%S)"

echo "Dumping the database…"
db_exec sh -c 'pg_dump -U "${POSTGRES_USER:-branchwarden}" -d "${POSTGRES_DB:-branchwarden}" --format=custom' > "$work/db.dump"
[ -s "$work/db.dump" ] || die "the database dump is empty"

for vol in "$KEYS_VOLUME" "$CERTS_VOLUME"; do
    name="${vol##*_}"     # keys, certs
    if "$ENGINE" volume inspect "$vol" >/dev/null 2>&1; then
        echo "Copying the $name volume…"
        "$ENGINE" run --rm -v "$vol:/v:ro" --entrypoint tar "$HELPER_IMAGE" -C /v --numeric-owner -cf - . > "$work/$name.tar"
    elif [ "$name" = keys ]; then
        echo "backup: no $vol volume: fine only if BRANCHWARDEN_SECRET_KEY is set in .env" >&2
    fi
done

[ -f .env ] && cp .env "$work/env" && chmod 600 "$work/env"
version="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' version.py 2>/dev/null || true)"
printf 'branchwarden backup\ncreated %s\nversion %s\nhost %s\n' "$stamp" "${version:-unknown}" "$(hostname 2>/dev/null || uname -n)" > "$work/manifest"

file="$OUT/branchwarden-$stamp.tar.age"
echo "Encrypting…"
tar -C "$work" -cf - . | $AGE "${RECIPIENTS[@]}" > "$file.partial"
mv "$file.partial" "$file"
chmod 600 "$file"
echo "Backup written: $file ($(du -h "$file" | cut -f1))"

if [ "$KEEP" -gt 0 ]; then
    # Keep the newest $KEEP backups in $OUT, remove older ones.
    ls -1t "$OUT"/branchwarden-*.tar.age 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
        rm -f -- "$old" && echo "Removed old backup: $old"
    done
fi
