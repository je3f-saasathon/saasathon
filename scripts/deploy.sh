#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: scripts/deploy.sh [--dry-run]

LOCAL REHEARSAL ONLY. Real deploys are CI-driven (see
.github/workflows/deploy-backend.yml and deploy-frontend.yml), triggered
automatically after CI goes green on main. This script builds the prod
images locally and runs the same health checks against
docker-compose.prod.yml so you can catch build/runtime issues before
pushing.

  --dry-run   print the commands that would run, without executing them
  -h, --help
EOF
}

DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log_err "unknown arg: $arg"; usage; exit 1 ;;
  esac
done

run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "+ $*"
  else
    "$@"
  fi
}

if ! command -v docker >/dev/null 2>&1; then
  log_err "docker is required for a local deploy rehearsal"
  exit 1
fi

COMPOSE=(docker compose -p saasathon-prod-local -f "$REPO_ROOT/docker-compose.prod.yml")

log_info "building prod images"
run "${COMPOSE[@]}" build

log_info "starting prod stack locally"
run "${COMPOSE[@]}" up -d

BE_PORT=$(backend_port)
FE_PORT=$(frontend_port)

if [ "$DRY_RUN" = "0" ]; then
  log_info "waiting for backend health"
  if wait_for_http "http://localhost:$BE_PORT/api/health" 60; then
    log_ok "backend healthy"
  else
    log_err "backend failed local prod health check"
    "${COMPOSE[@]}" logs --tail 100
    "${COMPOSE[@]}" down
    exit 1
  fi
  log_info "waiting for frontend"
  if wait_for_http "http://localhost:$FE_PORT" 60; then
    log_ok "frontend healthy"
  else
    log_err "frontend failed local prod health check"
    "${COMPOSE[@]}" logs --tail 100
    "${COMPOSE[@]}" down
    exit 1
  fi
  log_ok "local prod rehearsal passed"
  log_info "tear down with: docker compose -p saasathon-prod-local -f docker-compose.prod.yml down"
else
  log_info "(dry run — nothing was started, no health checks were run)"
fi
