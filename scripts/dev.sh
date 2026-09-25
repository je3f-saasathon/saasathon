#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/dev.sh [--force]

Runs the stack natively: backend (uv run python manage.py runserver) and
frontend (pnpm dev), tracked in .run/, with logs tailed in the foreground.
Ctrl+C stops both. Detects and stops docker mode first, since both share
ports.

  --force   forcibly free ports held by unrelated processes
  -h, --help
EOF
}

FORCE=0
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log_err "unknown arg: $arg"; usage; exit 1 ;;
  esac
done

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  if docker compose -p saasathon ps --status running 2>/dev/null | grep -q .; then
    log_warn "docker mode looks like it's running — stopping it first"
    docker compose -p saasathon -f "$REPO_ROOT/docker-compose.yml" down 2>/dev/null || true
  fi
fi

FE_PORT=$(frontend_port)
BE_PORT=$(backend_port)

ensure_port_free "$BE_PORT" "$FORCE" || exit 1
ensure_port_free "$FE_PORT" "$FORCE" || exit 1

if [ ! -f "$REPO_ROOT/backend/manage.py" ]; then
  log_err "backend/manage.py not found yet — backend isn't ready to run"
  exit 1
fi
if [ ! -f "$REPO_ROOT/frontend/package.json" ]; then
  log_err "frontend/package.json not found yet — frontend isn't ready to run"
  exit 1
fi

cleanup() {
  log_info "stopping dev stack"
  stop_tracked frontend
  stop_tracked backend
}
trap cleanup INT TERM EXIT

log_info "starting backend on :$BE_PORT"
( cd "$REPO_ROOT/backend" && start_bg backend "$LOG_DIR/backend.log" uv run python manage.py runserver "0.0.0.0:$BE_PORT" )

log_info "starting frontend on :$FE_PORT"
( cd "$REPO_ROOT/frontend" && start_bg frontend "$LOG_DIR/frontend.log" pnpm dev --port "$FE_PORT" --host )

log_info "waiting for backend health..."
if wait_for_http "http://localhost:$BE_PORT/api/health" 30; then
  log_ok "backend healthy"
else
  log_warn "backend did not respond to health check within 30s (check .run/logs/backend.log)"
fi

log_info "waiting for frontend..."
if wait_for_http "http://localhost:$FE_PORT" 30; then
  log_ok "frontend healthy"
else
  log_warn "frontend did not respond within 30s (check .run/logs/frontend.log)"
fi

echo
log_ok "dev stack up:"
printf '  %-10s %s\n' "frontend" "http://localhost:$FE_PORT"
printf '  %-10s %s\n' "backend"  "http://localhost:$BE_PORT"
echo
log_info "tailing logs (Ctrl+C to stop everything)"
tail -n +1 -f "$LOG_DIR/backend.log" "$LOG_DIR/frontend.log"
