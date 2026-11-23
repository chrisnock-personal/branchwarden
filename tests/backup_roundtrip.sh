#!/usr/bin/env bash
# Back up, destroy and restore a running Branchwarden, then check it all came
# back: the data, a sealed secret, and the dashboard. CI runs it with docker:
#   ENGINE=docker COMPOSE="docker compose" tests/backup_roundtrip.sh
# Run from the repo root, with the stack up and the demo data loaded.
set -euo pipefail
ENGINE="${ENGINE:-podman}"; COMPOSE="${COMPOSE:-podman-compose}"
KEYGEN="${AGE_KEYGEN:-age-keygen}"      # like AGE, a container works too
export ENGINE COMPOSE
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
URL="https://hooks.slack.com/services/T0/B0/roundtrip"

$COMPOSE exec -T worker python - <<PY
import db, settings
with db.get_conn() as c:
    cid = db.one(c, "SELECT id FROM channels ORDER BY id LIMIT 1")["id"]
    settings.save(c, {settings.channel_url_key(cid): "$URL"}, "roundtrip")
PY
count_prs() {
    $COMPOSE exec -T worker python -c "
import db
with db.get_conn() as c:
    print(db.one(c, 'SELECT COUNT(*) AS n FROM pull_requests')['n'])"
}
before=$(count_prs)

$KEYGEN -o "$work/key.txt" 2>/dev/null
deploy/backup.sh -r "$($KEYGEN -y "$work/key.txt")" -o "$work/out"
file=$(ls "$work"/out/*.tar.age)
head -c 21 "$file" | grep -q "age-encryption.org/v1" || { echo "not an age file"; exit 1; }

$COMPOSE down -v
deploy/restore.sh -i "$work/key.txt" "$file"

for _ in $(seq 1 30); do count_prs >/dev/null 2>&1 && break; sleep 2; done
$COMPOSE exec -T worker python - <<PY
import db, settings
with db.get_conn() as c:
    v = settings.load(c)
    n = db.one(c, "SELECT COUNT(*) AS n FROM pull_requests")["n"]
    cid = db.one(c, "SELECT id FROM channels ORDER BY id LIMIT 1")["id"]
    assert n == $before, f"pull requests: {n}, expected $before"
    assert v.get(settings.channel_url_key(cid)) == "$URL", "the sealed webhook URL didn't decrypt: the key didn't come back"
print(f"restored: {n} pull requests, and the sealed webhook URL decrypts")
PY
if deploy/restore.sh -i "$work/key.txt" "$file" 2>/dev/null; then echo "restore over data without --force should refuse"; exit 1; fi
# Going back to an older backup over a newer database: a table the backup doesn't know, depending
# on one it does (as api_tokens does on users), mustn't stop the restore.
$COMPOSE exec -T db sh -c 'psql -q -U "${POSTGRES_USER:-branchwarden}" -d "${POSTGRES_DB:-branchwarden}" -c "CREATE TABLE from_the_future (id SERIAL PRIMARY KEY, user_id INTEGER REFERENCES users (id))"'
deploy/restore.sh -i "$work/key.txt" --force "$file"
[ "$(count_prs)" = "$before" ] || { echo "restore --force over a newer database lost data"; exit 1; }
gone=$($COMPOSE exec -T db sh -c 'psql -tA -U "${POSTGRES_USER:-branchwarden}" -d "${POSTGRES_DB:-branchwarden}" -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = '"'"'from_the_future'"'"'"')
[ "${gone// /}" = "0" ] || { echo "the newer table should be gone after restoring an older backup"; exit 1; }
echo "restored over a newer database with --force"
for _ in $(seq 1 30); do curl -skf https://localhost:8443/healthz && echo && exit 0; sleep 2; done
echo "the dashboard didn't come back"; exit 1
