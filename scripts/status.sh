#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/status.sh

Shows what's running (native pids or docker containers), which ports are
bound, and the result of a health check against each service.
EOF
}
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { usage; exit 0; }

FE_PORT=$(frontend_port)
BE_PORT=$(backend_port)

log_info "native processes:"
found_native=0
for name in frontend backend sre-worker; do
  if [ -f "$RUN_DIR/${name}.pid" ]; then
    pid=$(cat "$RUN_DIR/${name}.pid" 2>/dev/null)
    if [ -n "$pid" ] && pid_alive "$pid"; then
      printf '  %-10s running (pid %s)\n' "$name" "$pid"
      found_native=1
    else
      printf '  %-10s stale pid file\n' "$name"
    fi
  fi
done
[ "$found_native" = "0" ] && echo "  (none)"

echo
log_info "docker containers:"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  out=$(docker compose -p saasathon -f "$REPO_ROOT/docker-compose.yml" ps 2>/dev/null || true)
  if [ -n "$out" ] && [ "$(echo "$out" | wc -l)" -gt 1 ]; then
    echo "$out" | sed 's/^/  /'
  else
    echo "  (none)"
  fi
else
  echo "  (docker not available)"
fi

echo
log_info "SRE infra (Temporal + Langfuse):"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  out=$(docker compose -p saasathon-infra -f "$REPO_ROOT/docker-compose.infra.yml" ps 2>/dev/null || true)
  if [ -n "$out" ] && [ "$(echo "$out" | wc -l)" -gt 1 ]; then
    echo "$out" | sed 's/^/  /'
  else
    echo "  (none — make infra-up)"
  fi
fi

echo
log_info "ports:"
for pair in "frontend:$FE_PORT" "backend:$BE_PORT" "db:$(db_port)"; do
  name=${pair%%:*}; port=${pair##*:}
  info=$(port_listener "$port")
  if [ -z "$info" ]; then
    printf '  %-10s :%-6s free\n' "$name" "$port"
  else
    pid=${info%%|*}; cmd=${info##*|}
    printf '  %-10s :%-6s in use (pid %s, %s)\n' "$name" "$port" "$pid" "$cmd"
  fi
done

echo
log_info "health checks:"
if command -v curl >/dev/null 2>&1; then
  if curl -fsS -o /dev/null -m 3 "http://localhost:$BE_PORT/api/health" 2>/dev/null; then
    log_ok "backend http://localhost:$BE_PORT/api/health"
  else
    log_warn "backend http://localhost:$BE_PORT/api/health not responding"
  fi
  if curl -fsS -o /dev/null -m 3 "http://localhost:$FE_PORT" 2>/dev/null; then
    log_ok "frontend http://localhost:$FE_PORT"
  else
    log_warn "frontend http://localhost:$FE_PORT not responding"
  fi
fi
