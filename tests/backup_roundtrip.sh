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
for _ in $(seq 1 30); do curl -skf https://localhost:8443/healthz && echo && exit 0; sleep 2; done
echo "the dashboard didn't come back"; exit 1
