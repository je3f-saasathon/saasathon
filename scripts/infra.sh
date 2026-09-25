#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/infra.sh up|down|status

Manages the SRE agent's local infra (docker-compose.infra.yml, compose
project "saasathon-infra"): Temporal and self-hosted Langfuse. It's kept
separate from the app stack so `make dev` / `make stop` don't restart it
(Langfuse takes a while to boot).

  up      start (or keep) the infra and wait until Temporal and Langfuse are healthy
  down    stop the infra containers (data volumes are kept)
  status  show infra containers
EOF
}

INFRA=(docker compose -p saasathon-infra -f "$REPO_ROOT/docker-compose.infra.yml")
LANGFUSE_URL=$(env_get "$REPO_ROOT/backend/.env" LANGFUSE_HOST "http://localhost:3100")
TEMPORAL_UI_PORT=${TEMPORAL_UI_PORT:-8243}

require_docker() {
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    log_err "docker is not available — the SRE agent's Temporal/Langfuse infra needs it"
    exit 1
  fi
}

wait_healthy() { # wait_healthy <service> <seconds>
  service="$1"; timeout="$2"; i=0
  while [ $i -lt "$timeout" ]; do
    state=$("${INFRA[@]}" ps --format '{{.Health}}' "$service" 2>/dev/null | head -n1)
    [ "$state" = "healthy" ] && return 0
    sleep 1; i=$((i+1))
  done
  return 1
}

case "${1:-}" in
  up)
    require_docker
    sandbox_image=$(env_get "$REPO_ROOT/backend/.env" SRE_SANDBOX_IMAGE "saasathon-sre-sandbox:latest")
    if ! docker image inspect "$sandbox_image" >/dev/null 2>&1; then
      log_info "building SRE sandbox image $sandbox_image (first run only)"
      if docker build -q -t "$sandbox_image" "$REPO_ROOT/backend/sre/sandbox" >/dev/null; then
        log_ok "sandbox image built"
      else
        log_warn "sandbox image build failed (retry with: make sandbox-image)"
      fi
    fi
    log_info "starting SRE infra (Temporal + Langfuse)"
    "${INFRA[@]}" up -d
    if wait_healthy temporal 90; then log_ok "temporal healthy"; else log_warn "temporal not healthy yet (docker compose -p saasathon-infra logs temporal)"; fi
    if wait_healthy langfuse-web 240; then log_ok "langfuse healthy"; else log_warn "langfuse not healthy yet (docker compose -p saasathon-infra logs langfuse-web)"; fi
    printf '  %-12s %s\n' "temporal ui" "http://localhost:$TEMPORAL_UI_PORT"
    printf '  %-12s %s\n' "langfuse" "$LANGFUSE_URL  (admin@localhost.dev / localdev-password)"
    ;;
  down)
    require_docker
    "${INFRA[@]}" down
    log_ok "SRE infra stopped (volumes kept)"
    ;;
  status)
    require_docker
    "${INFRA[@]}" ps
    ;;
  -h|--help) usage ;;
  *) usage; exit 1 ;;
esac
