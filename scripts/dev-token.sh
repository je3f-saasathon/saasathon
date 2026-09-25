#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/dev-token.sh

Mints a bearer token for the demo user via a Django management command
and prints it to stdout, for local API testing (curl, Postman, etc).
Refuses to run unless backend DEBUG=true. Never expose this over HTTP.
EOF
}
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { usage; exit 0; }

if [ ! -f "$REPO_ROOT/backend/manage.py" ]; then
  log_err "backend/manage.py not found yet"
  exit 1
fi

debug=$(env_get "$REPO_ROOT/backend/.env" DEBUG "false")
case "$debug" in
  1|true|True|TRUE|yes) ;;
  *)
    log_err "refusing to mint a dev token: backend DEBUG is not true"
    log_err "  this command must never be usable against a production backend"
    exit 1
    ;;
esac

(cd "$REPO_ROOT/backend" && uv run python manage.py dev_token)
