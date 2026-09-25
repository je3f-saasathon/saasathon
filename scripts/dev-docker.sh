#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/dev-docker.sh [--detach] [--force]

Runs the stack via docker-compose.yml (frontend, backend, postgres,
sre-worker), after starting the SRE infra (scripts/infra.sh up).
Without --detach, follows logs in the foreground; Ctrl+C stops the stack.
With --detach, starts, waits for health, prints a summary, and returns.
Detects and stops native mode first, since both share ports.

  --detach   start in the background and return
  --force    forcibly free ports held by unrelated processes
  -h, --help
EOF
}

DETACH=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --detach) DETACH=1 ;;
    --force) FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log_err "unknown arg: $arg"; usage; exit 1 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  log_err "docker is not installed"
  if is_mac; then log_err "  fix: brew install --cask docker"; else log_err "  fix: sudo apt install docker.io docker-compose-plugin  (or your distro's equivalent)"; fi
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  log_err "docker daemon is not running"
  if is_mac; then log_err "  fix: open -a Docker"; else log_err "  fix: sudo systemctl start docker"; fi
  exit 1
fi

# stop native mode first, since ports are shared
for name in frontend backend; do
  if [ -f "$RUN_DIR/${name}.pid" ]; then
    log_warn "native mode looks like it's running — stopping it first"
    stop_tracked frontend
    stop_tracked backend
    break
  fi
done

FE_PORT=$(frontend_port)
BE_PORT=$(backend_port)
DB_PORT_VAL=$(db_port)
ensure_port_free "$FE_PORT" "$FORCE" || exit 1
ensure_port_free "$BE_PORT" "$FORCE" || exit 1
ensure_port_free "$DB_PORT_VAL" "$FORCE" || exit 1

export FRONTEND_PORT="$FE_PORT" BACKEND_PORT="$BE_PORT" DB_PORT="$DB_PORT_VAL"

COMPOSE=(docker compose -p saasathon -f "$REPO_ROOT/docker-compose.yml")

"$REPO_ROOT/scripts/infra.sh" up || log_warn "SRE infra didn't come up; the sre-worker container will retry"
mkdir -p /tmp/sre-work

if [ "$DETACH" = "1" ]; then
  log_info "starting docker stack (detached)"
  "${COMPOSE[@]}" up -d --build
  log_info "waiting for backend health..."
  wait_for_http "http://localhost:$BE_PORT/api/health" 60 && log_ok "backend healthy" || log_warn "backend health check timed out"
  wait_for_http "http://localhost:$FE_PORT" 60 && log_ok "frontend healthy" || log_warn "frontend health check timed out"
  echo
  log_ok "docker stack up:"
  printf '  %-10s %s\n' "frontend" "http://localhost:$FE_PORT"
  printf '  %-10s %s\n' "backend"  "http://localhost:$BE_PORT"
else
  cleanup() { log_info "stopping docker stack"; "${COMPOSE[@]}" down; }
  trap cleanup INT TERM
  log_info "starting docker stack (foreground)"
  "${COMPOSE[@]}" up --build
fi
