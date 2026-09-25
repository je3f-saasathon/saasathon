#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/reset-db.sh

Drops and recreates the dev database, then seeds the demo user.
Native mode: deletes the SQLite file and re-runs migrations.
Docker mode: drops the postgres volume, brings the stack back up, and
migrates. Auto-detects which mode applies from backend/.env DATABASE_URL.
EOF
}
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { usage; exit 0; }

if [ ! -f "$REPO_ROOT/backend/manage.py" ]; then
  log_err "backend/manage.py not found yet"
  exit 1
fi

db_url=$(env_get "$REPO_ROOT/backend/.env" DATABASE_URL "")

case "$db_url" in
  postgres*)
    log_info "docker/postgres mode detected"
    "$(dirname "$0")/stop.sh"
    log_info "removing postgres volume"
    docker compose -p saasathon -f "$REPO_ROOT/docker-compose.yml" down -v
    log_info "starting stack"
    "$(dirname "$0")/dev-docker.sh" --detach
    log_info "running migrations"
    docker compose -p saasathon -f "$REPO_ROOT/docker-compose.yml" exec -T backend uv run python manage.py migrate
    log_info "seeding demo user"
    docker compose -p saasathon -f "$REPO_ROOT/docker-compose.yml" exec -T backend uv run python manage.py seed_demo_user
    ;;
  *)
    log_info "native/sqlite mode detected"
    db_file="$REPO_ROOT/backend/db.sqlite3"
    if [ -f "$db_file" ]; then
      rm -f "$db_file"
      log_ok "removed $db_file"
    fi
    (cd "$REPO_ROOT/backend" && uv run python manage.py migrate)
    log_ok "migrated"
    (cd "$REPO_ROOT/backend" && uv run python manage.py seed_demo_user)
    log_ok "seeded demo user"
    ;;
esac

log_ok "database reset complete"
