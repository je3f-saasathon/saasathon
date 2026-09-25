#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/stop.sh [--all]

Stops anything this repo started, in either native or docker mode.
Succeeds quietly if nothing is running. Leaves the SRE infra (Temporal +
Langfuse) running; stop that with `make infra-down`.

  --all   also sweep the known ports for matching processes and project
          docker containers even if .run/ was deleted
  -h, --help
EOF
}

ALL=0
for arg in "$@"; do
  case "$arg" in
    --all) ALL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log_err "unknown arg: $arg"; usage; exit 1 ;;
  esac
done

did_something=0

for name in sre-worker frontend backend tunnel; do
  if [ -f "$RUN_DIR/${name}.pid" ]; then
    stop_tracked "$name"
    did_something=1
  fi
done

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  if docker compose -p saasathon -f "$REPO_ROOT/docker-compose.yml" ps -q 2>/dev/null | grep -q .; then
    log_info "stopping docker compose stack"
    docker compose -p saasathon -f "$REPO_ROOT/docker-compose.yml" down
    did_something=1
  fi
fi

if [ "$ALL" = "1" ]; then
  for pair in "frontend:$(frontend_port)" "backend:$(backend_port)" "db:$(db_port)"; do
    port=${pair##*:}
    info=$(port_listener "$port")
    [ -z "$info" ] && continue
    pid=${info%%|*}; cmd=${info##*|}
    if looks_like_ours "$pid"; then
      log_warn "sweeping stray process on port $port (pid $pid, $cmd)"
      kill -TERM "$pid" 2>/dev/null || true
      sleep 0.3
      kill -KILL "$pid" 2>/dev/null || true
      did_something=1
    else
      log_warn "port $port held by pid $pid ($cmd) — doesn't look like ours, leaving it (use kill $pid manually if needed)"
    fi
  done
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    ids=$(docker ps -aq --filter "label=com.docker.compose.project=saasathon" 2>/dev/null)
    if [ -n "$ids" ]; then
      log_info "removing lingering saasathon containers"
      docker rm -f $ids >/dev/null 2>&1 || true
      did_something=1
    fi
  fi
fi

if [ "$did_something" = "1" ]; then
  log_ok "stopped"
else
  log_ok "nothing was running"
fi
