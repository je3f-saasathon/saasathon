#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/contracts.sh

Exports the backend's OpenAPI schema and regenerates the frontend's
generated API types from it. Run this after changing backend endpoints.
EOF
}
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { usage; exit 0; }

if [ ! -f "$REPO_ROOT/backend/manage.py" ]; then
  log_err "backend/manage.py not found yet — cannot export OpenAPI schema"
  exit 1
fi
if [ ! -f "$REPO_ROOT/frontend/package.json" ]; then
  log_err "frontend/package.json not found yet — cannot generate API types"
  exit 1
fi

log_info "exporting OpenAPI schema"
(cd "$REPO_ROOT/backend" && uv run python manage.py export_openapi)
log_ok "backend/openapi.json written"

log_info "generating frontend API types"
(cd "$REPO_ROOT/frontend" && pnpm gen:api)
log_ok "frontend API types regenerated"
