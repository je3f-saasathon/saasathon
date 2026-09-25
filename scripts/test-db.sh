#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF2'
Usage: scripts/test-db.sh [--clear]

Fills the dev database with fake data for trying the UI without GitHub or
Uptrace: migrates, seeds the demo user, then adds two "[demo]" projects with
ten incidents (every status, playbooks, PR links, token usage). Every user
is added to the demo projects. Re-running replaces the demo data.

  --clear   remove the demo projects and incidents instead

Refuses to run unless backend DEBUG=true. Uses the docker backend container
when backend/.env DATABASE_URL points at postgres, otherwise runs natively.
EOF2
}
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { usage; exit 0; }

clear=""
[ "${1:-}" = "--clear" ] && clear="--clear"

if [ ! -f "$REPO_ROOT/backend/manage.py" ]; then
  log_err "backend/manage.py not found yet"
  exit 1
fi

debug=$(env_get "$REPO_ROOT/backend/.env" DEBUG "false")
case "$debug" in
  1|true|True|TRUE|yes) ;;
  *)
    log_err "refusing to seed test data: backend DEBUG is not true"
    exit 1
    ;;
esac

db_url=$(env_get "$REPO_ROOT/backend/.env" DATABASE_URL "")
manage() {
  case "$db_url" in
    postgres*) docker compose -p saasathon -f "$REPO_ROOT/docker-compose.yml" exec -T backend uv run python manage.py "$@" ;;
    *) (cd "$REPO_ROOT/backend" && uv run python manage.py "$@") ;;
  esac
}

if [ -n "$clear" ]; then
  manage seed_demo_incidents --clear
  log_ok "demo data removed"
  exit 0
fi

log_info "running migrations"
manage migrate
log_info "seeding demo user"
manage seed_demo_user
log_info "seeding demo projects and incidents"
manage seed_demo_incidents
log_ok "test data ready — log in as demo@example.com / demo-password-123, or as your own account"
