#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/logs.sh [service]

Tails logs for a service. Auto-detects whether native or docker mode is
active: native tails .run/logs/<service>.log, docker follows
`docker compose logs -f <service>`. Defaults to all services if none given.

  service   frontend | backend | db (docker only)
EOF
}
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { usage; exit 0; }

SERVICE="${1:-}"

docker_running=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  if docker compose -p saasathon -f "$REPO_ROOT/docker-compose.yml" ps -q 2>/dev/null | grep -q .; then
    docker_running=1
  fi
fi

if [ "$docker_running" = "1" ]; then
  log_info "docker mode detected"
  if [ -n "$SERVICE" ]; then
    exec docker compose -p saasathon -f "$REPO_ROOT/docker-compose.yml" logs -f "$SERVICE"
  else
    exec docker compose -p saasathon -f "$REPO_ROOT/docker-compose.yml" logs -f
  fi
else
  log_info "native mode (tailing .run/logs)"
  if [ -n "$SERVICE" ]; then
    logfile="$LOG_DIR/${SERVICE}.log"
    [ -f "$logfile" ] || { log_err "no log file at $logfile"; exit 1; }
    exec tail -n 50 -f "$logfile"
  else
    files=""
    for f in "$LOG_DIR"/*.log; do [ -f "$f" ] && files="$files $f"; done
    if [ -z "$files" ]; then log_warn "no logs found in $LOG_DIR"; exit 0; fi
    # shellcheck disable=SC2086
    exec tail -n 50 -f $files
  fi
fi
